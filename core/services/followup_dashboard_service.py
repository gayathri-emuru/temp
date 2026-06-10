from __future__ import annotations

import hashlib
import os
import re
from collections import OrderedDict, defaultdict
from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone

from core.models import (
    CompanyRecruiter,
    InboxScanEvent,
    SendRun,
    SentEmailLog,
    SuppressedEmail,
)
from core.services.company_send_block_service import company_is_send_blocked
from core.services.email_composition_service import build_full_email_body
from core.services.email_sending_control_service import is_email_sending_enabled
from core.services.email_suppression_service import is_suppressed_email, suppress_if_hard_bounce
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.sender_account_service import (
    increment_sender_usage,
    is_smtp_daily_limit_error,
    pause_sender_for_daily_limit,
)
from core.services.send_run_service import (
    _pick_sender_for_send,
    _render_followup_body,
    _send_switch_allows_send,
    _sending_disabled_error_message,
    _sleep_between_sends,
    _stop_run_due_to_disabled_sending,
)
from core.services.send_timing_service import configured_send_delay_seconds
from core.services.mail_delivery_service import send_via_sender_account as send_via_smtp
from core.services.smtp_send_service import build_mime_message
from core.utils import safe_str


DEFAULT_MAX_COMPANY_ROWS = 250


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none" and "@" in value)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _local_time(value) -> str:
    if not value:
        return ""
    try:
        return timezone.localtime(value).strftime("%b %#d, %Y %#I:%M %p")
    except Exception:
        return safe_str(value)


def _days_since(value) -> int:
    if not value:
        return 0
    try:
        return max(0, int((timezone.now() - value).total_seconds() // 86400))
    except Exception:
        return 0


def _company_group_key(company_id, company_name: str) -> str:
    if company_id:
        return f"company_{int(company_id)}"
    digest = hashlib.md5(safe_str(company_name).strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"name_{digest}"


def _display_name_from_email(email: str) -> str:
    local = safe_str(email).split("@", 1)[0]
    local = re.sub(r"[._+-]+", " ", local).strip()
    return " ".join(piece.capitalize() for piece in local.split() if piece) or "there"


def _followup_settings() -> dict:
    return {
        "min_days_since_initial": max(0, _safe_int(os.getenv("FOLLOWUP_MIN_DAYS_SINCE_LAST", "3"), 3)),
        "min_gap_days": max(0, _safe_int(os.getenv("FOLLOWUP_MIN_GAP_DAYS", "7"), 7)),
        "max_followups_per_person": max(1, _safe_int(os.getenv("FOLLOWUP_MAX_PER_PERSON", "1"), 1)),
        "max_total_per_run": max(1, _safe_int(os.getenv("FOLLOWUP_MAX_TOTAL_PER_RUN", "50"), 50)),
    }


def followup_subject() -> str:
    return os.getenv("FOLLOWUP_EMAIL_SUBJECT", "Following up on the role").strip() or "Following up on the role"


def _event_email_sets() -> tuple[set[str], set[str], set[str]]:
    replied = set()
    bounced = set()
    blocked = set()
    events = (
        InboxScanEvent.objects.filter(
            classification__in=[
                InboxScanEvent.Classification.REPLY,
                InboxScanEvent.Classification.BOUNCE,
                InboxScanEvent.Classification.BLOCKED,
            ]
        )
        .select_related("matched_sent_log")
        .only("classification", "detected_email", "matched_sent_log__to_email")
    )
    for event in events:
        emails = {
            safe_str(event.detected_email).strip().lower(),
            safe_str(getattr(event.matched_sent_log, "to_email", "")).strip().lower(),
        }
        emails = {email for email in emails if _has_real_email(email)}
        if event.classification == InboxScanEvent.Classification.REPLY:
            replied.update(emails)
        elif event.classification == InboxScanEvent.Classification.BOUNCE:
            bounced.update(emails)
        elif event.classification == InboxScanEvent.Classification.BLOCKED:
            blocked.update(emails)
    return replied, bounced, blocked


def _followup_stats_by_email() -> dict[str, dict]:
    rows = (
        SentEmailLog.objects.filter(
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.FOLLOW_UP,
        )
        .values("to_email")
        .annotate(count=Count("id"), last_sent_at=Max("sent_at"))
    )
    stats = {}
    for row in rows:
        email = safe_str(row.get("to_email")).strip().lower()
        if _has_real_email(email):
            stats[email] = {
                "count": int(row.get("count") or 0),
                "last_sent_at": row.get("last_sent_at"),
            }
    return stats


def _pending_followup_emails() -> set[str]:
    return {
        safe_str(email).strip().lower()
        for email in SentEmailLog.objects.filter(
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.PENDING,
            message_type=SentEmailLog.MessageType.FOLLOW_UP,
        ).values_list("to_email", flat=True)
        if _has_real_email(email)
    }


def _initial_logs():
    return (
        SentEmailLog.objects.filter(
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
            sent_at__isnull=False,
        )
        .select_related(
            "job_posting",
            "job_posting__company_ref",
            "job_recruiter_target",
            "job_recruiter_target__company_recruiter",
        )
        .order_by("sent_at", "id")
    )


def _candidate_priority(candidate: dict) -> tuple:
    return (
        -1 if candidate.get("is_exact_or_manual") else 0,
        -1 if candidate.get("is_data_lead") else 0,
        -int(candidate.get("days_since_initial") or 0),
        safe_str(candidate.get("recipient_name")).lower(),
    )


def _is_data_lead_title(title: str) -> bool:
    text = safe_str(title).strip().lower()
    return any(word in text for word in ("data", "analytics", "machine learning", "ml", "ai", "business intelligence"))


def _candidate_from_log(log: SentEmailLog, *, followup_stats: dict, settings: dict) -> tuple[dict | None, str]:
    email = safe_str(log.to_email).strip().lower()
    if not _has_real_email(email):
        return None, "missing_email"

    job = log.job_posting
    if not job:
        return None, "missing_job"

    recruiter = None
    if log.job_recruiter_target_id and log.job_recruiter_target:
        recruiter = log.job_recruiter_target.company_recruiter
    if not recruiter and job.company_ref_id:
        recruiter = (
            CompanyRecruiter.objects.filter(company_id=job.company_ref_id, email__iexact=email, is_active=True)
            .order_by("id")
            .first()
        )

    if company_is_send_blocked(job):
        return None, "company_blocked"

    stat = followup_stats.get(email) or {"count": 0, "last_sent_at": None}
    followup_count = int(stat.get("count") or 0)
    if followup_count >= int(settings["max_followups_per_person"]):
        return None, "max_followups_sent"

    next_due_at = log.sent_at + timedelta(days=int(settings["min_days_since_initial"]))
    last_followup_at = stat.get("last_sent_at")
    if last_followup_at:
        next_due_at = last_followup_at + timedelta(days=int(settings["min_gap_days"]))
    if timezone.now() < next_due_at:
        return None, "too_soon"

    company = job.company_ref if job.company_ref_id else None
    company_name = safe_str(getattr(company, "normalized_name", "")).strip() or safe_str(job.company).strip()
    recipient_name = (
        safe_str(getattr(log.job_recruiter_target, "recipient_name_snapshot", "")).strip()
        or safe_str(getattr(recruiter, "person_name", "")).strip()
        or _display_name_from_email(email)
    )
    title = safe_str(getattr(recruiter, "apollo_title", "")).strip()
    is_exact_or_manual = bool(getattr(recruiter, "manually_targeted", False)) or (
        safe_str(getattr(job, "recruiter_name", "")).strip().lower() == recipient_name.strip().lower()
    )
    source = safe_str(getattr(recruiter, "source", "")).strip() or "sent log"
    if is_exact_or_manual:
        source = "job poster / targeted"
    elif title:
        source = f"{source} | {title}"

    return (
        {
            "initial_log_id": int(log.id),
            "company_key": _company_group_key(getattr(company, "id", None), company_name),
            "company_id": getattr(company, "id", None),
            "company_name": company_name,
            "job_id": int(job.id),
            "job_title": safe_str(job.title).strip(),
            "job_url": safe_str(job.normalized_linkedin_url).strip() or safe_str(job.linkedin_url).strip(),
            "recipient_name": recipient_name,
            "email": email,
            "title": title,
            "source": source,
            "sent_at": log.sent_at,
            "sent_at_display": _local_time(log.sent_at),
            "days_since_initial": _days_since(log.sent_at),
            "followup_count": followup_count,
            "last_followup_at_display": _local_time(last_followup_at),
            "is_exact_or_manual": is_exact_or_manual,
            "is_data_lead": _is_data_lead_title(title),
        },
        "ok",
    )


def build_followup_dashboard_context(*, max_rows: int = DEFAULT_MAX_COMPANY_ROWS) -> dict:
    settings = _followup_settings()
    replied_emails, bounced_emails, blocked_warning_emails = _event_email_sets()
    suppressed_emails = {
        safe_str(email).strip().lower()
        for email in SuppressedEmail.objects.filter(is_active=True).values_list("email", flat=True)
        if _has_real_email(email)
    }
    pending_followups = _pending_followup_emails()
    followup_stats = _followup_stats_by_email()
    skip_reasons: defaultdict[str, int] = defaultdict(int)
    grouped: OrderedDict[str, dict] = OrderedDict()
    total_initials = 0

    for log in _initial_logs():
        total_initials += 1
        email = safe_str(log.to_email).strip().lower()
        if email in replied_emails:
            skip_reasons["replied"] += 1
            continue
        if email in bounced_emails or email in suppressed_emails:
            skip_reasons["bounced_or_suppressed"] += 1
            continue
        if email in blocked_warning_emails:
            skip_reasons["blocked_warning"] += 1
            continue
        if email in pending_followups:
            skip_reasons["followup_pending"] += 1
            continue
        candidate, reason = _candidate_from_log(log, followup_stats=followup_stats, settings=settings)
        if not candidate:
            skip_reasons[reason] += 1
            continue
        row = grouped.setdefault(
            candidate["company_key"],
            {
                "key": candidate["company_key"],
                "company_id": candidate["company_id"],
                "company_name": candidate["company_name"],
                "candidates": [],
                "due_count": 0,
                "oldest_sent_at": candidate["sent_at"],
            },
        )
        row["candidates"].append(candidate)
        row["due_count"] += 1
        if candidate["sent_at"] < row["oldest_sent_at"]:
            row["oldest_sent_at"] = candidate["sent_at"]

    rows = list(grouped.values())
    for row in rows:
        row["candidates"].sort(key=_candidate_priority)
        row["oldest_sent_at_display"] = _local_time(row["oldest_sent_at"])
        row["sample_people"] = ", ".join(candidate["recipient_name"] for candidate in row["candidates"][:3])
    rows.sort(key=lambda row: (-int(row["due_count"]), row["oldest_sent_at"], safe_str(row["company_name"]).lower()))
    rows = rows[: max(1, int(max_rows or DEFAULT_MAX_COMPANY_ROWS))]

    first_candidate = rows[0]["candidates"][0] if rows and rows[0].get("candidates") else {
        "recipient_name": "Brett",
        "job_title": "Spark Driver Experience",
        "company_name": "the company",
        "job_url": "",
    }
    preview_body = build_full_email_body(
        recipient_name=safe_str(first_candidate.get("recipient_name")).strip() or "Brett",
        base_body=_render_followup_body(
            recipient_name=safe_str(first_candidate.get("recipient_name")).strip() or "Brett",
            sender_name="Gayathri Emuru",
            job_title=safe_str(first_candidate.get("job_title")).strip(),
            company_name=safe_str(first_candidate.get("company_name")).strip(),
        ),
        job_linkedin_url=safe_str(first_candidate.get("job_url")).strip(),
        include_resume_attachment_sentence=False,
    )

    due_people = sum(int(row["due_count"]) for row in rows)
    return {
        "settings": settings,
        "subject": followup_subject(),
        "preview_body": preview_body,
        "preview_is_real_candidate": bool(rows),
        "rows": rows,
        "totals": {
            "initials_seen": total_initials,
            "companies_due": len(rows),
            "people_due": due_people,
            "replied": int(skip_reasons.get("replied", 0)),
            "bounced_or_suppressed": int(skip_reasons.get("bounced_or_suppressed", 0)),
            "too_soon": int(skip_reasons.get("too_soon", 0)),
            "max_followups_sent": int(skip_reasons.get("max_followups_sent", 0)),
            "followup_pending": int(skip_reasons.get("followup_pending", 0)),
            "skipped": dict(skip_reasons),
        },
    }


def _selected_candidates_from_post(post_data) -> list[dict]:
    context = build_followup_dashboard_context()
    max_total = max(1, _safe_int(post_data.get("max_total_followups"), context["settings"]["max_total_per_run"]))
    selected = []
    selected_emails = set()
    for row in context["rows"]:
        count = max(0, _safe_int(post_data.get(f"followup_count__{row['key']}"), 0))
        if count <= 0:
            continue
        for candidate in row["candidates"][:count]:
            email = safe_str(candidate.get("email")).strip().lower()
            if not _has_real_email(email) or email in selected_emails:
                continue
            selected_emails.add(email)
            selected.append(candidate)
    return _round_robin_by_company(selected)[:max_total]


def _round_robin_by_company(candidates: list[dict]) -> list[dict]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for candidate in candidates:
        grouped.setdefault(safe_str(candidate.get("company_key")), []).append(candidate)
    ordered = []
    index = 0
    while True:
        added = False
        for company_candidates in grouped.values():
            if index < len(company_candidates):
                ordered.append(company_candidates[index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def run_company_followups_from_dashboard(*, post_data, delay_seconds: int | None = None) -> dict:
    if not is_email_sending_enabled():
        raise RuntimeError(_sending_disabled_error_message())

    if delay_seconds is None:
        delay_seconds = configured_send_delay_seconds()
    delay_seconds = max(0, int(delay_seconds or 0))

    run_log_path = create_run_log_path("send_company_followups", "dashboard")
    selected = _selected_candidates_from_post(post_data)
    append_and_print(run_log_path, f"START selected={len(selected)} delay_seconds={delay_seconds}")

    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.REAL,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=delay_seconds,
        notes=f"Follow-up dashboard company-level send run_log_path={run_log_path}",
    )

    totals = {
        "send_run_id": send_run.id,
        "selected": len(selected),
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_skipped": 0,
        "emails_skipped_suppressed": 0,
        "emails_failed": 0,
        "run_log_path": run_log_path,
    }
    if not selected:
        send_run.status = SendRun.Status.STOPPED
        send_run.finished_at = timezone.now()
        send_run.notes = "No company follow-up counts selected."
        send_run.save(update_fields=["status", "finished_at", "notes"])
        append_and_print(run_log_path, f"END totals={totals}")
        return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}

    subject = followup_subject()
    sender_name = os.getenv("SENDER_DISPLAY_NAME", "Gayathri Emuru").strip() or "Gayathri Emuru"
    stopped = False
    attempted_emails = set()

    for candidate in selected:
        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(
                send_run=send_run,
                run_log_path=run_log_path,
                reason=_sending_disabled_error_message(),
            )
            stopped = True
            break

        email = safe_str(candidate.get("email")).strip().lower()
        if email in attempted_emails:
            totals["emails_skipped"] += 1
            append_and_print(run_log_path, f"SKIP to={email} reason=duplicate_selected_email")
            continue
        attempted_emails.add(email)
        initial_log = (
            SentEmailLog.objects.select_related("job_posting", "job_recruiter_target")
            .filter(id=candidate.get("initial_log_id"))
            .first()
        )
        if not initial_log or not _has_real_email(email):
            totals["emails_skipped"] += 1
            append_and_print(run_log_path, f"SKIP initial_log_id={candidate.get('initial_log_id')} reason=missing_initial_log_or_email")
            continue
        if is_suppressed_email(email):
            totals["emails_skipped_suppressed"] += 1
            append_and_print(run_log_path, f"SKIP initial_log_id={initial_log.id} to={email} reason=suppressed")
            continue

        sender = _pick_sender_for_send(send_run=send_run, run_log_path=run_log_path)
        if not sender:
            stopped = True
            break
        if not _send_switch_allows_send(send_run=send_run, run_log_path=run_log_path):
            stopped = True
            break

        body = _render_followup_body(
            recipient_name=safe_str(candidate.get("recipient_name")).strip() or "there",
            sender_name=sender_name,
            job_title=safe_str(candidate.get("job_title")).strip(),
            company_name=safe_str(candidate.get("company_name")).strip(),
        )
        final_body = build_full_email_body(
            recipient_name=safe_str(candidate.get("recipient_name")).strip(),
            base_body=body,
            job_linkedin_url=safe_str(candidate.get("job_url")).strip(),
            include_resume_attachment_sentence=False,
        )

        log_row = SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=initial_log.job_posting,
            job_recruiter_target=initial_log.job_recruiter_target,
            sender_account=sender,
            to_email=email,
            subject_snapshot=subject[:500],
            body_snapshot=final_body,
            attachment_path="",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.FOLLOW_UP,
            status=SentEmailLog.SendStatus.PENDING,
        )
        totals["emails_attempted"] += 1
        append_and_print(
            run_log_path,
            f"SEND_START initial_log_id={initial_log.id} followup_log_id={log_row.id} company={candidate.get('company_name')} to={email} sender={sender.email}",
        )

        try:
            msg = build_mime_message(
                from_name=safe_str(sender.display_name) or sender_name,
                from_email=sender.email,
                to_email=email,
                subject=subject,
                body_text=final_body,
                attachment_paths=[],
            )
            send_via_smtp(sender=sender, message=msg)
            log_row.status = SentEmailLog.SendStatus.SENT
            log_row.sent_at = timezone.now()
            log_row.error_message = ""
            log_row.save(update_fields=["status", "sent_at", "error_message"])
            increment_sender_usage(sender, 1)
            totals["emails_sent"] += 1
            append_and_print(run_log_path, f"SEND_OK followup_log_id={log_row.id} to={email}")
        except Exception as exc:
            totals["emails_failed"] += 1
            error_text = safe_str(exc)
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = error_text[:4000]
            log_row.save(update_fields=["status", "error_message"])
            suppress_if_hard_bounce(email=email, error_message=error_text)
            if is_smtp_daily_limit_error(error_text):
                pause_sender_for_daily_limit(sender, error_text)
            append_exception(run_log_path, f"SEND_FAIL followup_log_id={log_row.id} to={email}", exc)

        if delay_seconds and not _sleep_between_sends(
            delay_seconds,
            send_run=send_run,
            run_log_path=run_log_path,
            last_recipient={
                "email": email,
                "name": safe_str(candidate.get("recipient_name")).strip(),
                "company": safe_str(candidate.get("company_name")).strip(),
                "job_title": safe_str(candidate.get("job_title")).strip(),
                "sender": safe_str(sender.email).strip(),
            },
            last_sender=sender,
        ):
            stopped = True
            break

    if not stopped and send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = f"Done. sent={totals['emails_sent']} failed={totals['emails_failed']}"
        send_run.save(update_fields=["status", "finished_at", "notes"])

    append_and_print(run_log_path, f"END totals={totals}")
    return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}
