from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Callable, Optional

from django.db.models import Count, Min
from django.db import transaction
from django.utils import timezone

from core.models import SenderAccount, SenderDailyUsage, SentEmailLog
from core.utils import safe_str


SMTP_SERIOUS_LIMIT_PATTERNS = (
    "daily user sending limit exceeded",
    "daily sending limit exceeded",
    "user-rate limit exceeded",
    "user rate limit exceeded",
    "rate limit exceeded",
    "too many messages",
    "too many emails",
    "quota exceeded",
    "try again later",
    "temporarily rate limited",
    "4.7.0",
    "421",
    "450 4.",
    "550 5.4.5",
)


def is_smtp_daily_limit_error(error_message: str) -> bool:
    text = safe_str(error_message).strip().lower()
    return bool(text and any(pattern in text for pattern in SMTP_SERIOUS_LIMIT_PATTERNS))


class SenderUnavailableError(RuntimeError):
    def __init__(self, message: str, *, next_available_at=None):
        super().__init__(message)
        self.next_available_at = next_available_at


class SenderWaitInterrupted(RuntimeError):
    pass


def _rolling_quota_hours() -> int:
    try:
        return max(1, int(os.getenv("SENDER_QUOTA_ROLLING_HOURS", "24") or "24"))
    except Exception:
        return 24


def sender_min_gap_seconds() -> int:
    try:
        return max(0, int(os.getenv("SENDER_MIN_GAP_SECONDS", "0") or "0"))
    except Exception:
        return 0


def only_sender_email() -> str:
    return safe_str(os.getenv("SENDER_ONLY_EMAIL") or os.getenv("STREAMLIT_ONLY_SENDER_EMAIL")).strip().lower()


def sender_availability_summary(*, now=None) -> dict:
    now = now or timezone.now()
    min_gap = sender_min_gap_seconds()
    quota_window = timedelta(hours=_rolling_quota_hours())
    next_available_at = None
    available_count = 0
    blocked_by_gap = 0
    blocked_by_quota = 0

    only_email = only_sender_email()
    base_qs = SenderAccount.objects.filter(is_active=True)
    if only_email:
        base_qs = base_qs.filter(email__iexact=only_email)

    active_total = base_qs.count()
    paused_count = base_qs.filter(is_paused=True).count()
    senders = base_qs.filter(is_paused=False).order_by("last_used_at", "round_robin_order", "id")

    for sender in senders:
        limit = int(sender.daily_limit or 0) or 50
        rolling_count, oldest_sent_at = _rolling_sender_usage(sender, now=now)
        if rolling_count >= limit:
            blocked_by_quota += 1
            if oldest_sent_at:
                sender_next = oldest_sent_at + quota_window
                next_available_at = min(next_available_at, sender_next) if next_available_at else sender_next
            continue

        if min_gap and sender.last_used_at and now < sender.last_used_at + timedelta(seconds=min_gap):
            blocked_by_gap += 1
            sender_next = sender.last_used_at + timedelta(seconds=min_gap)
            next_available_at = min(next_available_at, sender_next) if next_available_at else sender_next
            continue

        available_count += 1

    seconds_until_next = None
    if next_available_at:
        seconds_until_next = max(0, int((next_available_at - now).total_seconds()))

    return {
        "active_total": active_total,
        "active_unpaused": active_total - paused_count,
        "paused_count": paused_count,
        "available_count": available_count,
        "blocked_by_gap": blocked_by_gap,
        "blocked_by_quota": blocked_by_quota,
        "min_gap_seconds": min_gap,
        "quota_window_hours": _rolling_quota_hours(),
        "next_available_at": next_available_at,
        "seconds_until_next": seconds_until_next,
    }


def _cooldown_hours_for_error(error_message: str) -> int:
    text = safe_str(error_message).lower()
    if "daily" in text or "quota" in text or "550 5.4.5" in text:
        return int(os.getenv("SENDER_DAILY_LIMIT_COOLDOWN_HOURS", "24") or "24")
    if "4.7.0" in text or "421" in text or "rate" in text or "too many" in text:
        return int(os.getenv("SENDER_THROTTLE_COOLDOWN_HOURS", "6") or "6")
    return int(os.getenv("SENDER_ERROR_COOLDOWN_HOURS", "12") or "12")


@transaction.atomic
def _clear_expired_sender_cooldowns(now=None) -> int:
    now = now or timezone.now()
    return SenderAccount.objects.filter(is_paused=True, paused_until__isnull=False, paused_until__lte=now).update(
        is_paused=False,
        paused_until=None,
        pause_reason="",
        updated_at=now,
    )


def _rolling_sender_usage(sender: SenderAccount, *, now=None) -> tuple[int, Optional[object]]:
    now = now or timezone.now()
    cutoff = now - timedelta(hours=_rolling_quota_hours())
    rows = (
        SentEmailLog.objects.filter(
            sender_account=sender,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at__gte=cutoff,
        )
        .aggregate(count=Count("id"), oldest=Min("sent_at"))
    )
    return int(rows.get("count") or 0), rows.get("oldest")


@transaction.atomic
def _pick_next_sender_now() -> SenderAccount:
    """
    Picks an active sender account that still has remaining rolling 24h quota.
    Updates SenderAccount.last_used_at optimistically to spread sends in near-real-time.
    """
    now = timezone.now()
    _clear_expired_sender_cooldowns(now=now)
    min_gap = sender_min_gap_seconds()
    quota_window = timedelta(hours=_rolling_quota_hours())
    next_available_at = None

    qs = (
        SenderAccount.objects
        .select_for_update()
        .filter(is_active=True, is_paused=False)
        .order_by("last_used_at", "round_robin_order", "id")
    )
    only_email = only_sender_email()
    if only_email:
        qs = qs.filter(email__iexact=only_email)

    for sender in qs:
        limit = int(sender.daily_limit or 0) or 50
        rolling_count, oldest_sent_at = _rolling_sender_usage(sender, now=now)
        if rolling_count >= limit:
            if oldest_sent_at:
                sender_next = oldest_sent_at + quota_window
                next_available_at = min(next_available_at, sender_next) if next_available_at else sender_next
            continue

        if min_gap and sender.last_used_at and now < sender.last_used_at + timedelta(seconds=min_gap):
            sender_next = sender.last_used_at + timedelta(seconds=min_gap)
            next_available_at = min(next_available_at, sender_next) if next_available_at else sender_next
            continue

        sender.last_used_at = now
        sender.save(update_fields=["last_used_at", "updated_at"])
        return sender

    only_sender_note = f" matching {only_email}" if only_email else ""
    raise SenderUnavailableError(f"No available sender account{only_sender_note} with remaining rolling quota or sender gap.", next_available_at=next_available_at)


def pick_next_sender_for_today(
    *,
    wait_if_needed: bool = True,
    on_wait: Optional[Callable[[object, int], None]] = None,
    should_continue_waiting: Optional[Callable[[], bool]] = None,
) -> SenderAccount:
    max_wait = int(os.getenv("SENDER_MAX_WAIT_SECONDS", "7200") or "7200")
    while True:
        try:
            return _pick_next_sender_now()
        except SenderUnavailableError as exc:
            if not wait_if_needed or not exc.next_available_at:
                raise
            wait_seconds = max(1, int((exc.next_available_at - timezone.now()).total_seconds()) + 1)
            if wait_seconds > max_wait:
                raise
            if on_wait:
                on_wait(exc.next_available_at, wait_seconds)
            for _ in range(wait_seconds):
                if should_continue_waiting and not should_continue_waiting():
                    raise SenderWaitInterrupted("Sender wait interrupted.")
                time.sleep(1)


@transaction.atomic
def increment_sender_usage(sender: SenderAccount, count: int = 1) -> SenderDailyUsage:
    today = timezone.localdate()
    usage, _ = SenderDailyUsage.objects.get_or_create(
        sender_account=sender,
        usage_date=today,
        defaults={"sent_count": 0},
    )
    usage.sent_count = int(usage.sent_count or 0) + int(count or 0)
    usage.save(update_fields=["sent_count"])

    sender.sent_today_count = int(sender.sent_today_count or 0) + int(count or 0)
    sender.save(update_fields=["sent_today_count", "updated_at"])

    return usage


@transaction.atomic
def pause_sender_for_daily_limit(sender: SenderAccount, error_message: str = "") -> bool:
    if not sender or not getattr(sender, "id", None):
        return False
    sender = SenderAccount.objects.select_for_update().get(id=sender.id)
    now = timezone.now()
    cooldown_hours = max(1, _cooldown_hours_for_error(error_message))
    paused_until = now + timedelta(hours=cooldown_hours)
    sender.is_paused = True
    sender.paused_until = paused_until
    note = safe_str(sender.notes).strip()
    reason = safe_str(error_message).strip()[:1000] or "Daily user sending limit exceeded"
    sender.pause_reason = reason
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] Auto-paused until {paused_until.strftime('%Y-%m-%d %H:%M:%S')}: {reason}"
    sender.notes = f"{note}\n{line}".strip() if note else line
    sender.save(update_fields=["is_paused", "paused_until", "pause_reason", "notes", "updated_at"])
    return True
