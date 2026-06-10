from __future__ import annotations

import imaplib
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from django.utils import timezone

from core.models import InboxScanEvent, SenderAccount, SentEmailLog
from core.services.email_suppression_service import suppress_if_hard_bounce
from core.services.live_company_reply_service import record_reply_stop_for_event
from core.services.smtp_send_service import imap_host_for_email
from core.utils import safe_str


DEFAULT_INBOX_MONITOR_MAX_MESSAGES = 300

BLOCK_WARNING_PHRASES = [
    "message blocked",
    "blocked your message",
    "message was blocked",
    "has been blocked",
    "blocked for policy reasons",
    "blocked due to",
    "blocked by",
    "rate limit",
    "daily user sending limit",
    "temporarily rejected",
    "suspicious activity",
    "account disabled",
    "account has been disabled",
    "policy violation",
    "spam policy",
    "550 5.7",
    "421 4.7",
]

BOUNCE_PHRASES = [
    "address not found",
    "no such user",
    "no such recipient",
    "does not exist",
    "couldn't be found",
    "unable to receive mail",
    "recipient address rejected",
    "mailbox unavailable",
    "mailbox not found",
    "invalid recipient",
    "invalid address",
    "550 5.1.1",
    "550-5.1.1",
    "5.1.1",
]


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return safe_str(value)


def _message_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = safe_str(part.get_content_type()).lower()
            disposition = safe_str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disposition:
                continue
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        except Exception:
            pass
    return safe_str("\n\n".join(p.strip() for p in parts if p and p.strip())).strip()


def _imap_host_for_email(email: str) -> str:
    return imap_host_for_email(email)


def _message_status(*, from_header: str, subject: str, body: str) -> tuple[str, str]:
    text = f"{from_header}\n{subject}\n{body}".lower()
    if any(phrase in text for phrase in BLOCK_WARNING_PHRASES):
        return "blocked", "Block / account warning"
    if any(phrase in text for phrase in BOUNCE_PHRASES):
        return "bounce", "Hard bounce"
    system_senders = ("mailer-daemon", "postmaster", "googlemail.com", "google.com")
    if any(sender in from_header.lower() for sender in system_senders):
        return "notice", "System notice"
    return "reply", "Human / recruiter message"


def _extract_email(text: str) -> str:
    matches = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", safe_str(text), flags=re.I)
    for match in matches:
        email = match.strip().lower()
        if email and "mailer-daemon@" not in email and "postmaster@" not in email:
            return email
    return ""


def _format_message_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed and parsed.tzinfo is None:
            parsed = timezone.make_aware(parsed, timezone=timezone.utc)
        return _format_local_time(timezone.localtime(parsed))
    except Exception:
        return safe_str(value).strip()


def _parse_message_date(value: str):
    try:
        parsed = parsedate_to_datetime(value)
        if parsed and parsed.tzinfo is None:
            parsed = timezone.make_aware(parsed, timezone=timezone.utc)
        return parsed
    except Exception:
        return None


def _format_local_time(value=None) -> str:
    value = value or timezone.localtime()
    return value.strftime("%b %#d, %Y %#I:%M:%S %p")


def _scan_account(sender: SenderAccount, *, max_messages: int, timeout_seconds: int) -> dict:
    started = timezone.now()
    host = _imap_host_for_email(sender.email)
    old_timeout = socket.getdefaulttimeout()
    imap = None
    try:
        socket.setdefaulttimeout(max(3, int(timeout_seconds)))
        imap = imaplib.IMAP4_SSL(host)
        imap.login(sender.email, sender.app_password)
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, "ALL")
        ids = data[0].split() if status == "OK" and data else []
        latest_ids = ids[-max(1, int(max_messages)):]
        messages = []
        counts = {"reply": 0, "bounce": 0, "blocked": 0, "notice": 0}
        latest_key = ""
        for msg_id in reversed(latest_ids):
            status, fetched = imap.fetch(msg_id, "(RFC822 FLAGS)")
            if status != "OK" or not fetched:
                continue
            raw = None
            flags_text = ""
            for part in fetched:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw = part[1]
                    flags_text += safe_str(part[0])
            if not raw:
                continue
            msg = message_from_bytes(raw)
            from_header = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            body = _message_text(msg)
            status_key, status_label = _message_status(from_header=from_header, subject=subject, body=body)
            counts[status_key] = counts.get(status_key, 0) + 1
            message_id = safe_str(msg.get("Message-ID", "")).strip()
            message_key = message_id or f"{sender.email}:{msg_id.decode(errors='ignore')}:{safe_str(msg.get('Date', ''))}:{subject[:80]}"
            latest_key = latest_key or message_key
            detected_email = _extract_email(body) if status_key == "bounce" else _extract_email(from_header)
            parsed_date = _parse_message_date(msg.get("Date", ""))
            messages.append(
                {
                    "key": message_key,
                    "account": sender.email,
                    "from": from_header,
                    "subject": subject or "(no subject)",
                    "date_raw": safe_str(msg.get("Date", "")),
                    "date": _format_message_date(msg.get("Date", "")),
                    "date_sort": parsed_date.isoformat() if parsed_date else "",
                    "date_ts": parsed_date.timestamp() if parsed_date else 0,
                    "status": status_key,
                    "status_label": status_label,
                    "detected_email": detected_email,
                    "unread": "\\Seen" not in flags_text,
                    "snippet": body[:650],
                }
            )
        return {
            "account": sender.email,
            "ok": True,
            "host": host,
            "checked_at": _format_local_time(),
            "duration_ms": int((timezone.now() - started).total_seconds() * 1000),
            "message_count": len(messages),
            "counts": counts,
            "latest_key": latest_key,
            "messages": messages,
            "error": "",
        }
    except Exception as exc:
        return {
            "account": sender.email,
            "ok": False,
            "host": host,
            "checked_at": _format_local_time(),
            "duration_ms": int((timezone.now() - started).total_seconds() * 1000),
            "message_count": 0,
            "counts": {"reply": 0, "bounce": 0, "blocked": 0, "notice": 0},
            "latest_key": "",
            "messages": [],
            "error": safe_str(exc)[:500],
        }
    finally:
        socket.setdefaulttimeout(old_timeout)
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def _monitorable_sender_accounts():
    return (
        SenderAccount.objects.exclude(app_password="")
        .exclude(app_password__isnull=True)
        .order_by("email")
    )


def build_inbox_monitor_context() -> dict:
    monitorable_accounts = _monitorable_sender_accounts().count()
    return {
        "active_account_count": monitorable_accounts,
        "monitorable_account_count": monitorable_accounts,
        "default_poll_seconds": 60,
        "default_max_messages": DEFAULT_INBOX_MONITOR_MAX_MESSAGES,
    }


def _sort_messages_newest_first(messages: list[dict]) -> list[dict]:
    severity_order = {"blocked": 0, "reply": 1, "bounce": 2, "notice": 3}

    return sorted(
        messages,
        key=lambda item: (
            -(float(item.get("date_ts") or 0)),
            severity_order.get(item.get("status"), 9),
            safe_str(item.get("account")).lower(),
            safe_str(item.get("subject")).lower(),
        ),
    )


def scan_inbox_monitor(*, max_messages: int = DEFAULT_INBOX_MONITOR_MAX_MESSAGES) -> dict:
    requested_max_messages = max(1, int(max_messages))
    accounts = list(_monitorable_sender_accounts())
    started = timezone.now()
    rows = []
    max_workers = min(32, max(1, len(accounts)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _scan_account,
                account,
                max_messages=requested_max_messages,
                timeout_seconds=8,
            )
            for account in accounts
        ]
        for future in as_completed(futures):
            rows.append(future.result())

    rows.sort(key=lambda row: row["account"].lower())
    all_messages = []
    totals = {"accounts": len(rows), "ok": 0, "unavailable": 0, "reply": 0, "bounce": 0, "blocked": 0, "notice": 0}
    for row in rows:
        if row["ok"]:
            totals["ok"] += 1
        else:
            totals["unavailable"] += 1
        for key in ("reply", "bounce", "blocked", "notice"):
            totals[key] += int(row["counts"].get(key, 0) or 0)
        all_messages.extend(row["messages"])

    all_messages = _sort_messages_newest_first(all_messages)
    available_message_count = len(all_messages)
    all_messages = all_messages[:requested_max_messages]
    latest_key = safe_str(all_messages[0].get("key")) if all_messages else ""
    return {
        "checked_at": _format_local_time(),
        "duration_ms": int((timezone.now() - started).total_seconds() * 1000),
        "totals": totals,
        "accounts": rows,
        "messages": all_messages,
        "requested_max_messages": requested_max_messages,
        "available_message_count": available_message_count,
        "returned_message_count": len(all_messages),
        "scan_scope_label": "mailbox history",
        "latest_key": latest_key,
    }


def _match_sent_log(*, detected_email: str, sender_email: str):
    qs = SentEmailLog.objects.filter(send_type=SentEmailLog.SendType.REAL, status=SentEmailLog.SendStatus.SENT)
    if detected_email:
        match = qs.filter(to_email__iexact=detected_email).order_by("-sent_at", "-id").first()
        if match:
            return match
    if sender_email:
        return qs.filter(to_email__iexact=sender_email).order_by("-sent_at", "-id").first()
    return None


def scan_and_store_inbox_events(*, max_messages: int = DEFAULT_INBOX_MONITOR_MAX_MESSAGES) -> dict:
    result = scan_inbox_monitor(max_messages=max_messages)
    created = 0
    updated = 0
    suppressed = 0
    matched = 0
    reply_stops = 0
    accounts = {account.email.lower(): account for account in _monitorable_sender_accounts()}

    for message in result.get("messages", []):
        account = accounts.get(safe_str(message.get("account")).lower())
        if not account:
            continue
        detected_email = safe_str(message.get("detected_email")).strip().lower()
        sent_log = _match_sent_log(
            detected_email=detected_email,
            sender_email=detected_email if message.get("status") == "reply" else "",
        )
        if sent_log:
            matched += 1
        if message.get("status") == "bounce" and detected_email:
            if suppress_if_hard_bounce(email=detected_email, error_message=message.get("snippet", "")):
                suppressed += 1

        event, was_created = InboxScanEvent.objects.update_or_create(
            sender_account=account,
            message_key=safe_str(message.get("key"))[:500],
            defaults={
                "from_header": safe_str(message.get("from"))[:1000],
                "subject": safe_str(message.get("subject"))[:1000],
                "message_date": _parse_message_date(message.get("date_raw", "")),
                "classification": safe_str(message.get("status")) or InboxScanEvent.Classification.NOTICE,
                "detected_email": detected_email[:320],
                "matched_sent_log": sent_log,
                "snippet": safe_str(message.get("snippet"))[:4000],
                "raw_detail": safe_str(message)[:4000],
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1
        if message.get("status") == "reply" and sent_log and record_reply_stop_for_event(event):
            reply_stops += 1

    result["stored"] = {
        "created": created,
        "updated": updated,
        "suppressed": suppressed,
        "matched": matched,
        "reply_stops": reply_stops,
    }
    return result
