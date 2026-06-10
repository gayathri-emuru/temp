from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Count, Max, Min, Prefetch, Q, Sum
from django.utils import timezone

from core.models import (
    ApprovalRecord,
    Company,
    DailyBatch,
    GeneratedEmail,
    JobPosting,
    JobRecruiterTarget,
    SendRun,
    SenderAccount,
    SenderDailyUsage,
    SentEmailLog,
)
from core.services.email_composition_service import build_full_email_body
from core.services.email_sending_control_service import (
    get_email_sending_state,
    get_resume_attachment_state,
    set_email_sending_paused,
)
from core.services.app_settings_service import get_max_people_per_company
from core.services.company_send_block_service import company_is_send_blocked
from core.services.live_company_reply_service import company_has_reply_stop_today
from core.services.email_suppression_service import is_blocked_or_suppressed_email
from core.services.sender_account_service import sender_availability_summary
from core.services.send_run_service import get_send_run_progress, run_send_initial_for_batch
from core.services.send_timing_service import (
    configured_send_delay_range_seconds,
    configured_send_delay_seconds,
    set_send_delay_range_seconds,
)
from core.utils import safe_str


_SEND_THREAD_LOCK = threading.Lock()
_SEND_THREADS: dict[str, threading.Thread] = {}


def sender_daily_limit_summary() -> dict:
    totals = SenderAccount.objects.aggregate(
        account_count=Count("id"),
        min_limit=Min("daily_limit"),
        max_limit=Max("daily_limit"),
    )
    account_count = int(totals.get("account_count") or 0)
    min_limit = int(totals.get("min_limit") or 0)
    max_limit = int(totals.get("max_limit") or 0)
    return {
        "account_count": account_count,
        "min_limit": min_limit,
        "max_limit": max_limit,
        "is_uniform": account_count > 0 and min_limit == max_limit,
        "display_limit": min_limit if account_count > 0 and min_limit == max_limit else "",
    }


def set_all_sender_daily_limits(limit: int) -> dict:
    limit = max(1, min(500, int(limit or 0)))
    updated = SenderAccount.objects.update(daily_limit=limit)
    return {
        "limit": limit,
        "updated": updated,
        "summary": sender_daily_limit_summary(),
    }


def latest_populated_batch() -> DailyBatch | None:
    return (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0)
        .order_by("-batch_date", "-id")
        .first()
    )


def populated_batch_for_date(batch_date_str: str = "") -> DailyBatch | None:
    text = safe_str(batch_date_str).strip()
    if not text:
        return latest_populated_batch()
    try:
        selected = datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return latest_populated_batch()
    batch = (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0, batch_date=selected)
        .order_by("-id")
        .first()
    )
    return batch or latest_populated_batch()


def available_batch_rows() -> list[dict]:
    batches = (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0)
        .order_by("-batch_date", "-id")
    )
    return [{"batch": b, "job_count": int(getattr(b, "job_count", 0) or 0)} for b in batches]


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _already_real_sent(to_email: str) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    return SentEmailLog.objects.filter(
        to_email=to_email,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


def _has_pending_real_send(to_email: str) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    return SentEmailLog.objects.filter(
        to_email=to_email,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.PENDING,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


def _job_is_approved(job: JobPosting) -> bool:
    approval = getattr(job, "prefetched_approval", None)
    if approval is not None:
        return bool(approval and approval.is_approved)
    try:
        return bool(job.approval_record and job.approval_record.is_approved)
    except ApprovalRecord.DoesNotExist:
        return False


def _get_generated_email(job: JobPosting) -> GeneratedEmail | None:
    generated = getattr(job, "prefetched_generated_email", None)
    if generated is None:
        try:
            generated = job.generated_email
        except GeneratedEmail.DoesNotExist:
            return None
    if not safe_str(generated.subject).strip() or not safe_str(generated.body).strip():
        return None
    return generated


def _target_rows_for_job(job: JobPosting) -> list[JobRecruiterTarget]:
    cap = get_max_people_per_company()
    prefetched = getattr(job, "prefetched_send_targets", None)
    if prefetched is not None:
        return [target for target in list(prefetched) if _target_allows_real_send(target)][:cap]
    targets = list(
        job.targets.filter(
            is_selected_for_job=True,
            recipient_email_snapshot__isnull=False,
            company_recruiter__email_sent=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter")
        .order_by("selection_order", "id")
    )
    return [target for target in targets if _target_allows_real_send(target)][:cap]


def _target_allows_real_send(target: JobRecruiterTarget) -> bool:
    recruiter = getattr(target, "company_recruiter", None)
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    apollo_id = safe_str(getattr(recruiter, "apollo_person_id", "")).strip()
    if source != "apollo" and not apollo_id:
        return True
    return safe_str(getattr(recruiter, "email_status", "")).strip().lower() == "verified"


def _company_key(job: JobPosting) -> str:
    if job.company_ref_id and job.company_ref:
        return safe_str(job.company_ref.normalized_name).strip() or safe_str(job.company).strip()
    return safe_str(job.company).strip() or "unknown company"


def _company_plan_key(job: JobPosting) -> str:
    return _company_key(job).strip().lower()


def _company_real_initial_sent_count(job: JobPosting) -> int:
    from core.services.app_settings_service import get_company_cooldown_days
    cooldown_days = get_company_cooldown_days()
    if cooldown_days <= 0:
        return 0
    base = SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
        sent_at__gte=timezone.now() - timedelta(days=cooldown_days),
    )
    if job.company_ref_id and job.company_ref:
        normalized = safe_str(job.company_ref.normalized_name).strip()
        if normalized:
            return base.filter(job_posting__company_ref__normalized_name=normalized).count()
    company_name = safe_str(job.company).strip()
    if company_name:
        return base.filter(job_posting__company__iexact=company_name).count()
    return 0


def build_send_plan_for_batch(batch: DailyBatch) -> dict:
    target_prefetch = Prefetch(
        "targets",
        queryset=(
            JobRecruiterTarget.objects.filter(
                is_selected_for_job=True,
                recipient_email_snapshot__isnull=False,
                company_recruiter__email_sent=False,
            )
            .exclude(recipient_email_snapshot__in=["", "none"])
            .select_related("company_recruiter")
            .order_by("selection_order", "id")
        ),
        to_attr="prefetched_send_targets",
    )

    jobs = list(
        JobPosting.objects.filter(daily_batch=batch, is_manual_email_job=False)
        .select_related("company_ref", "approval_record", "generated_email")
        .prefetch_related(target_prefetch)
        .order_by("company_ref__normalized_name", "id")
    )

    totals = {
        "jobs_seen": len(jobs),
        "approved_jobs": 0,
        "ready_jobs": 0,
        "companies": 0,
        "recipients": 0,
        "skipped_not_approved": 0,
        "skipped_no_generated_email": 0,
        "skipped_no_recipients": 0,
        "skipped_already_sent": 0,
        "skipped_pending_send": 0,
        "skipped_suppressed": 0,
        "skipped_company_blocked": 0,
        "skipped_company_reply_stop": 0,
        "skipped_company_cap": 0,
    }
    job_rows = []
    company_map: OrderedDict[str, dict] = OrderedDict()
    for job in jobs:
        approved = _job_is_approved(job)
        if not approved:
            totals["skipped_not_approved"] += 1
            continue
        totals["approved_jobs"] += 1

        if company_is_send_blocked(job):
            totals["skipped_company_blocked"] += 1
            continue
        if company_has_reply_stop_today(job):
            totals["skipped_company_reply_stop"] += 1
            continue

        generated = _get_generated_email(job)
        if not generated:
            totals["skipped_no_generated_email"] += 1
            continue

        recipients = []
        already_sent_count = 0
        pending_count = 0
        for target in _target_rows_for_job(job):
            email = safe_str(target.recipient_email_snapshot).strip().lower()
            if not _has_real_email(email):
                continue
            if _already_real_sent(email):
                already_sent_count += 1
                continue
            if _has_pending_real_send(email):
                pending_count += 1
                continue
            if is_blocked_or_suppressed_email(email):
                totals["skipped_suppressed"] += 1
                continue
            recruiter = target.company_recruiter
            recipients.append(
                {
                    "target": target,
                    "target_id": target.id,
                    "name": safe_str(target.recipient_name_snapshot).strip() or "there",
                    "email": email,
                    "selection_order": target.selection_order,
                    "apollo_title": safe_str(getattr(recruiter, "apollo_title", "")).strip(),
                    "apollo_linkedin_url": safe_str(getattr(recruiter, "apollo_linkedin_url", "")).strip(),
                }
            )

        totals["skipped_already_sent"] += already_sent_count
        totals["skipped_pending_send"] += pending_count

        if not recipients:
            totals["skipped_no_recipients"] += 1
            continue

        company = _company_key(job)
        full_email = build_full_email_body(
            recipient_name=recipients[0]["name"],
            base_body=safe_str(generated.body),
            job_linkedin_url=safe_str(job.normalized_linkedin_url) or safe_str(job.linkedin_url),
            manual_job_reference_id=safe_str(getattr(job, "manual_job_reference_id", "")).strip(),
        )
        job_row = {
            "job": job,
            "company": company,
            "subject": safe_str(generated.subject).strip(),
            "full_email": full_email,
            "recipients": recipients,
        }
        job_rows.append(job_row)
        totals["ready_jobs"] += 1
        totals["recipients"] += len(recipients)

        if company not in company_map:
            company_map[company] = {
                "company": company,
                "company_ref_id": job.company_ref_id,
                "jobs": [],
                "recipient_emails": [],
                "preview_subject": job_row["subject"],
                "preview_body": job_row["full_email"],
            }
        company_map[company]["jobs"].append(job_row)
        company_map[company]["recipient_emails"].extend([r["email"] for r in recipients])

    totals["companies"] = len(company_map)
    return {"totals": totals, "jobs": job_rows, "companies": list(company_map.values())}


def _blocked_company_rows_for_batch(batch: DailyBatch) -> list[dict]:
    companies = (
        Company.objects.filter(is_blocked=True, jobs__daily_batch=batch, jobs__is_manual_email_job=False)
        .distinct()
        .order_by("normalized_name", "id")
    )
    rows = []
    for company in companies:
        jobs = list(
            JobPosting.objects.filter(
                company_ref=company,
                daily_batch=batch,
                is_manual_email_job=False,
            )
            .order_by("id")
        )
        sent_count = SentEmailLog.objects.filter(
            job_posting__company_ref=company,
            job_posting__daily_batch=batch,
            job_posting__is_manual_email_job=False,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
        ).count()
        rows.append(
            {
                "company": company,
                "company_id": company.id,
                "name": safe_str(company.normalized_name).strip() or safe_str(company.raw_name_latest).strip(),
                "domain": safe_str(company.active_domain).strip(),
                "job_count": len(jobs),
                "sent_count": sent_count,
                "jobs": [{"id": job.id, "title": safe_str(job.title).strip()} for job in jobs],
                "notes": safe_str(company.notes).strip(),
            }
        )
    return rows


def _active_run_for_batch(batch: DailyBatch) -> SendRun | None:
    needle = f"batch_date={batch.batch_date.isoformat()}"
    return (
        SendRun.objects.filter(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.RUNNING,
            notes__icontains=needle,
        )
        .order_by("-id")
        .first()
    )


def _last_run_for_batch(batch: DailyBatch) -> SendRun | None:
    needle = f"batch_date={batch.batch_date.isoformat()}"
    return (
        SendRun.objects.filter(run_type=SendRun.RunType.REAL, notes__icontains=needle)
        .order_by("-id")
        .first()
    )


def _next_planned_recipient(plan: dict) -> dict:
    items = []
    for job_row in plan.get("jobs") or []:
        for rec in job_row.get("recipients") or []:
            items.append(
                {
                    "company_key": safe_str(job_row.get("company", "")).strip().lower(),
                    "company": safe_str(job_row.get("company", "")).strip(),
                    "email": safe_str(rec.get("email", "")).strip().lower(),
                    "name": safe_str(rec.get("name", "")).strip(),
                    "job_title": safe_str(getattr(job_row.get("job"), "title", "")).strip(),
                    "selection_order": rec.get("selection_order"),
                }
            )
    if not items:
        return {}
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for item in items:
        grouped.setdefault(item["company_key"] or item["company"] or item["email"], []).append(item)
    for company_items in grouped.values():
        if company_items:
            return company_items[0]
    return {}


def _format_status_time(value) -> str:
    if not value:
        return ""
    try:
        return timezone.localtime(value).strftime("%b %#d, %Y %#I:%M:%S %p")
    except Exception:
        return ""


def _run_log_path_from_notes(notes: str) -> str:
    match = re.search(r"run_log_path=([^|]+)", safe_str(notes))
    return safe_str(match.group(1)).strip() if match else ""


def _read_run_log_tail(path_text: str, *, line_count: int = 24) -> list[str]:
    path_text = safe_str(path_text).strip()
    if not path_text:
        return []
    try:
        path = Path(path_text)
        run_log_dir = Path(settings.BASE_DIR) / "media" / "run_logs"
        if not path.exists() or run_log_dir.resolve() not in path.resolve().parents:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-line_count:]
    except Exception:
        return []


def _send_run_status_summary(run: SendRun | None, *, plan: dict | None = None) -> dict:
    sending_state = get_email_sending_state()
    sender_availability = sender_availability_summary()
    if not run:
        return {
            "has_run": False,
            "run": None,
            "sent_count": 0,
            "failed_count": 0,
            "pending_count": 0,
            "attempted_count": 0,
            "last_log": None,
            "last_email": "",
            "last_sender": "",
            "last_status": "",
            "last_error": "",
            "stop_reason": "",
            "stopped_before_attempt": False,
            "is_safety_stop": False,
            "progress": {},
            "next_recipient": {},
            "time_until_next_seconds": None,
            "last_activity_at": None,
            "last_activity_display": "",
            "seconds_since_last_activity": None,
            "minutes_since_last_activity": None,
            "is_paused": bool(sending_state.get("paused")),
            "is_waiting_for_sender": False,
            "is_stalled": False,
            "sender_availability": sender_availability,
            "time_until_next_sender_seconds": sender_availability.get("seconds_until_next"),
            "next_sender_available_display": _format_status_time(sender_availability.get("next_available_at")),
            "run_log_path": "",
            "run_log_tail": [],
        }

    logs = SentEmailLog.objects.filter(send_run=run).select_related(
        "job_posting",
        "job_posting__company_ref",
        "job_recruiter_target",
        "sender_account",
    )
    counts = dict(logs.values_list("status").annotate(count=Count("id")))
    last_log = logs.order_by("-sent_at", "-id").first()
    stop_reason = safe_str(run.notes).strip()
    last_error = safe_str(getattr(last_log, "error_message", "")).strip() if last_log else ""
    target = getattr(last_log, "job_recruiter_target", None) if last_log else None
    progress = get_send_run_progress(run.id)
    next_sender_available_at = sender_availability.get("next_available_at")
    time_until_next_sender_seconds = sender_availability.get("seconds_until_next")
    is_waiting_for_sender = bool(
        run.status == SendRun.Status.RUNNING
        and sending_state.get("effective_enabled")
        and int(sender_availability.get("available_count") or 0) <= 0
        and next_sender_available_at
    )
    if not progress and run.status == SendRun.Status.RUNNING and last_log:
        if is_waiting_for_sender:
            progress = {
                "phase": "waiting_for_sender",
                "phase_label": "Waiting for sender availability",
                "timer_is_estimated": False,
                "last_recipient": {
                    "email": safe_str(getattr(last_log, "to_email", "")).strip().lower(),
                    "name": safe_str(getattr(target, "recipient_name_snapshot", "")).strip() if target else "",
                    "company": _company_key(last_log.job_posting) if last_log and last_log.job_posting_id else "",
                    "job_title": safe_str(getattr(getattr(last_log, "job_posting", None), "title", "")).strip(),
                    "sender": safe_str(getattr(getattr(last_log, "sender_account", None), "email", "")).strip(),
                },
                "next_send_after": next_sender_available_at,
                "next_send_after_display": _format_status_time(next_sender_available_at),
            }
        else:
            progress = {
                "phase": "waiting",
                "phase_label": "Worker active; waiting or checking next email",
                "timer_is_estimated": True,
                "last_recipient": {
                    "email": safe_str(getattr(last_log, "to_email", "")).strip().lower(),
                    "name": safe_str(getattr(target, "recipient_name_snapshot", "")).strip() if target else "",
                    "company": _company_key(last_log.job_posting) if last_log and last_log.job_posting_id else "",
                    "job_title": safe_str(getattr(getattr(last_log, "job_posting", None), "title", "")).strip(),
                    "sender": safe_str(getattr(getattr(last_log, "sender_account", None), "email", "")).strip(),
                },
                "next_send_after": None,
                "next_send_after_display": "",
            }
    next_recipient = _next_planned_recipient(plan or {})
    next_send_after = progress.get("next_send_after")
    time_until_next_seconds = None
    if next_send_after:
        try:
            time_until_next_seconds = max(0, int((next_send_after - timezone.now()).total_seconds()))
        except Exception:
            time_until_next_seconds = None
    last_activity_at = None
    if last_log:
        last_activity_at = getattr(last_log, "sent_at", None) or getattr(run, "started_at", None)
    else:
        last_activity_at = getattr(run, "started_at", None)
    seconds_since_last_activity = None
    minutes_since_last_activity = None
    if last_activity_at:
        seconds_since_last_activity = max(0, int((timezone.now() - last_activity_at).total_seconds()))
        minutes_since_last_activity = int(seconds_since_last_activity // 60)
    stale_threshold = max(
        600,
        int(configured_send_delay_seconds()) * 4,
        int(sender_availability.get("min_gap_seconds") or 0) + 60,
    )
    is_stalled = bool(
        run.status == SendRun.Status.RUNNING
        and sending_state.get("effective_enabled")
        and not is_waiting_for_sender
        and seconds_since_last_activity is not None
        and seconds_since_last_activity > stale_threshold
    )
    run_log_path = _run_log_path_from_notes(run.notes)
    return {
        "has_run": True,
        "run": run,
        "sent_count": int(counts.get(SentEmailLog.SendStatus.SENT, 0) or 0),
        "failed_count": int(counts.get(SentEmailLog.SendStatus.FAILED, 0) or 0),
        "pending_count": int(counts.get(SentEmailLog.SendStatus.PENDING, 0) or 0),
        "attempted_count": int(sum(counts.values()) if counts else 0),
        "last_log": last_log,
        "last_email": safe_str(getattr(last_log, "to_email", "")).strip().lower() if last_log else "",
        "last_sender": safe_str(getattr(getattr(last_log, "sender_account", None), "email", "")).strip()
        if last_log
        else "",
        "last_status": safe_str(getattr(last_log, "status", "")).strip() if last_log else "",
        "last_error": last_error,
        "last_company": _company_key(last_log.job_posting) if last_log and last_log.job_posting_id else "",
        "last_job_title": safe_str(getattr(getattr(last_log, "job_posting", None), "title", "")).strip(),
        "last_recipient_name": safe_str(getattr(target, "recipient_name_snapshot", "")).strip() if target else "",
        "stop_reason": stop_reason,
        "stopped_before_attempt": not last_log and run.status == SendRun.Status.STOPPED,
        "is_safety_stop": False,
        "progress": progress,
        "next_recipient": next_recipient,
        "time_until_next_seconds": time_until_next_seconds,
        "last_activity_at": last_activity_at,
        "last_activity_display": _format_status_time(last_activity_at),
        "seconds_since_last_activity": seconds_since_last_activity,
        "minutes_since_last_activity": minutes_since_last_activity,
        "is_paused": bool(sending_state.get("paused")),
        "is_waiting_for_sender": is_waiting_for_sender,
        "is_stalled": is_stalled,
        "sender_availability": sender_availability,
        "time_until_next_sender_seconds": time_until_next_sender_seconds,
        "next_sender_available_display": _format_status_time(next_sender_available_at),
        "run_log_path": run_log_path,
        "run_log_tail": _read_run_log_tail(run_log_path),
    }


def _sent_log_rows(batch: DailyBatch, limit: int = 250) -> list[dict]:
    logs = (
        SentEmailLog.objects.filter(
            job_posting__daily_batch=batch,
            job_posting__is_manual_email_job=False,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
        )
        .select_related("job_posting", "job_posting__company_ref", "job_recruiter_target", "sender_account")
        .order_by("-sent_at", "-id")[:limit]
    )
    rows = []
    for log in logs:
        target = log.job_recruiter_target
        rows.append(
            {
                "log": log,
                "sent_at": log.sent_at,
                "company": _company_key(log.job_posting),
                "job_id": log.job_posting_id,
                "job_title": safe_str(log.job_posting.title),
                "name": safe_str(getattr(target, "recipient_name_snapshot", "")).strip() or "",
                "email": safe_str(log.to_email).strip().lower(),
                "sender": safe_str(getattr(log.sender_account, "email", "")).strip(),
            }
        )
    return rows


def _sent_log_count(batch: DailyBatch) -> int:
    return SentEmailLog.objects.filter(
        job_posting__daily_batch=batch,
        job_posting__is_manual_email_job=False,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).count()


def _failed_log_rows_today(limit: int = 100) -> list[dict]:
    today = timezone.localdate()
    logs = (
        SentEmailLog.objects.filter(
            send_run__started_at__date=today,
            job_posting__is_manual_email_job=False,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.FAILED,
            message_type=SentEmailLog.MessageType.INITIAL,
        )
        .select_related("job_posting", "job_posting__company_ref", "job_recruiter_target", "sender_account")
        .order_by("-id")[:limit]
    )
    rows = []
    for log in logs:
        target = log.job_recruiter_target
        rows.append(
            {
                "log": log,
                "company": _company_key(log.job_posting),
                "job_id": log.job_posting_id,
                "job_title": safe_str(log.job_posting.title),
                "name": safe_str(getattr(target, "recipient_name_snapshot", "")).strip() or "",
                "email": safe_str(log.to_email).strip().lower(),
                "sender": safe_str(getattr(log.sender_account, "email", "")).strip(),
                "error": safe_str(log.error_message).strip(),
            }
        )
    return rows


def _today_counts() -> dict:
    today = timezone.localdate()
    rolling_cutoff = timezone.now() - timedelta(hours=24)
    real_sent_today = SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        sent_at__date=today,
    ).count()
    real_sent_rolling_24h = SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        sent_at__gte=rolling_cutoff,
    ).count()
    usage_rows = list(
        SenderDailyUsage.objects.filter(usage_date=today)
        .select_related("sender_account")
        .order_by("sender_account__email")
    )
    failed_logs = list(
        SentEmailLog.objects.filter(
            send_run__started_at__date=today,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.FAILED,
            message_type=SentEmailLog.MessageType.INITIAL,
        )
        .select_related("sender_account")
        .order_by("-id")
    )
    failed_by_sender = defaultdict(int)
    last_error_by_sender = {}
    for log in failed_logs:
        sender_id = log.sender_account_id or 0
        failed_by_sender[sender_id] += 1
        last_error_by_sender.setdefault(sender_id, safe_str(log.error_message).strip())

    sender_map = OrderedDict()
    for row in usage_rows:
        sender_map[row.sender_account_id] = {
            "sender_account": row.sender_account,
            "email": safe_str(row.sender_account.email).strip(),
            "sent_count": int(row.sent_count or 0),
            "failed_count": int(failed_by_sender.get(row.sender_account_id, 0)),
            "last_error": last_error_by_sender.get(row.sender_account_id, ""),
        }
    for log in failed_logs:
        sender_id = log.sender_account_id or 0
        if sender_id in sender_map:
            continue
        sender_map[sender_id] = {
            "sender_account": log.sender_account,
            "email": safe_str(getattr(log.sender_account, "email", "")).strip() or "-",
            "sent_count": 0,
            "failed_count": int(failed_by_sender.get(sender_id, 0)),
            "last_error": last_error_by_sender.get(sender_id, ""),
        }

    sender_rows = sorted(sender_map.values(), key=lambda row: row["email"].lower())
    domain_map = defaultdict(lambda: {"domain": "", "active_senders": 0, "sent_rolling_24h": 0})
    for sender in SenderAccount.objects.all().order_by("email"):
        email = safe_str(sender.email).strip().lower()
        domain = email.split("@", 1)[1] if "@" in email else ""
        if not domain:
            continue
        domain_map[domain]["domain"] = domain
        if sender.is_active and not sender.is_paused:
            domain_map[domain]["active_senders"] += 1
    for row in (
        SentEmailLog.objects.filter(
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at__gte=rolling_cutoff,
            sender_account__isnull=False,
        )
        .select_related("sender_account")
        .values_list("sender_account__email", "id")
    ):
        email = safe_str(row[0]).strip().lower()
        domain = email.split("@", 1)[1] if "@" in email else ""
        if not domain:
            continue
        domain_map[domain]["domain"] = domain
        domain_map[domain]["sent_rolling_24h"] += 1
    domain_rows = sorted(domain_map.values(), key=lambda row: row["domain"])
    return {
        "date": today,
        "real_sent_today": real_sent_today,
        "real_sent_rolling_24h": real_sent_rolling_24h,
        "real_failed_today": len(failed_logs),
        "sender_usage_total": sum(int(row["sent_count"] or 0) for row in sender_rows),
        "sender_failed_total": sum(int(row["failed_count"] or 0) for row in sender_rows),
        "sender_rows": sender_rows,
        "domain_rows": domain_rows,
    }


def build_send_control_context(batch_date: str = "") -> dict:
    send_delay_seconds = configured_send_delay_seconds()
    send_delay_min_seconds, send_delay_max_seconds = configured_send_delay_range_seconds(send_delay_seconds)
    requested_batch_date = safe_str(batch_date).strip()
    batch = populated_batch_for_date(batch_date)
    context = {
        "batch": batch,
        "selected_batch_date": batch.batch_date.isoformat() if batch else requested_batch_date,
        "available_batches": available_batch_rows(),
        "email_sending_state": get_email_sending_state(),
        "resume_attachment_state": get_resume_attachment_state(),
        "send_delay_seconds": send_delay_seconds,
        "send_delay_min_seconds": send_delay_min_seconds,
        "send_delay_max_seconds": send_delay_max_seconds,
        "send_delay_is_randomized": send_delay_max_seconds > send_delay_min_seconds,
        "sender_limit_summary": sender_daily_limit_summary(),
        "estimated_min_send_minutes": 0,
        "estimated_max_send_minutes": 0,
        "today": _today_counts(),
        "plan": {"totals": {}, "jobs": [], "companies": []},
        "blocked_company_rows": [],
        "active_run": None,
        "last_run": None,
        "send_status": _send_run_status_summary(None, plan={}),
        "sent_logs": [],
        "sent_log_count": 0,
        "failed_logs_today": [],
    }
    if not batch:
        return context

    context["plan"] = build_send_plan_for_batch(batch)
    context["blocked_company_rows"] = _blocked_company_rows_for_batch(batch)
    recipient_count = int(context["plan"]["totals"].get("recipients") or 0)
    wait_count = max(0, recipient_count - 1)
    context["estimated_min_send_minutes"] = int((wait_count * send_delay_min_seconds + 59) // 60)
    context["estimated_max_send_minutes"] = int((wait_count * send_delay_max_seconds + 59) // 60)
    context["plan"]["max_people_per_company"] = get_max_people_per_company()
    context["active_run"] = _active_run_for_batch(batch)
    context["last_run"] = _last_run_for_batch(batch)
    context["send_status"] = _send_run_status_summary(
        context["active_run"] or context["last_run"],
        plan=context["plan"],
    )
    context["sent_logs"] = _sent_log_rows(batch)
    context["sent_log_count"] = _sent_log_count(batch)
    context["failed_logs_today"] = _failed_log_rows_today()
    return context


def _run_send_worker(batch_date: str) -> None:
    close_old_connections()
    try:
        run_send_initial_for_batch(
            batch_date_str=batch_date,
            send_type="real",
            allow_recipient_discovery=False,
            skip_pending_recipients=True,
            source_label="Send control send-only",
        )
    finally:
        close_old_connections()
        with _SEND_THREAD_LOCK:
            _SEND_THREADS.pop(batch_date, None)


def _start_send_worker_process(batch_date: str) -> int:
    command = [
        sys.executable,
        "manage.py",
        "send_control_worker",
        "--batch-date",
        batch_date,
    ]
    kwargs = {
        "cwd": str(getattr(settings, "BASE_DIR", ".")),
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"[SEND_WORKER_START] batch_date={batch_date} command={' '.join(command)}", flush=True)
    process = subprocess.Popen(command, **kwargs)
    print(f"[SEND_WORKER_STARTED] batch_date={batch_date} pid={int(process.pid or 0)}", flush=True)
    return int(process.pid or 0)


def start_batch_send(batch_date_str: str = "", *, delay_min_seconds: int | None = None, delay_max_seconds: int | None = None) -> tuple[bool, str]:
    batch = populated_batch_for_date(batch_date_str)
    if not batch:
        return False, "No populated batch found."

    batch_date = batch.batch_date.isoformat()
    if _active_run_for_batch(batch):
        return False, f"A send run is already active for {batch_date}."

    plan = build_send_plan_for_batch(batch)
    if int(plan["totals"].get("recipients") or 0) <= 0:
        return False, f"No ready recipients to send for {batch_date}. Review approvals and selected recipients first."

    state = get_email_sending_state()
    if not state["env_enabled"]:
        return False, "EMAIL_SENDING_ENABLED is off. Turn on the master safety gate before sending."

    if delay_min_seconds is not None or delay_max_seconds is not None:
        current_min, current_max = configured_send_delay_range_seconds(configured_send_delay_seconds())
        min_seconds = current_min if delay_min_seconds is None else delay_min_seconds
        max_seconds = current_max if delay_max_seconds is None else delay_max_seconds
        min_seconds, max_seconds = set_send_delay_range_seconds(
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            persist_to_dotenv=True,
        )
    else:
        min_seconds, max_seconds = configured_send_delay_range_seconds(configured_send_delay_seconds())

    set_email_sending_paused(paused=False, persist_to_dotenv=True)

    pid = _start_send_worker_process(batch_date)
    return True, f"Started sending for batch {batch_date}. Delay randomized between {min_seconds}-{max_seconds} seconds. Worker process pid={pid}."


def start_latest_batch_send() -> tuple[bool, str]:
    return start_batch_send("")


def stop_sending() -> str:
    set_email_sending_paused(paused=True, persist_to_dotenv=True)
    return "Stop requested. The current email, if already in SMTP, may finish; the sender will stop before the next email."


def clear_stuck_runs(batch_date_str: str = "") -> int:
    """Mark all RUNNING SendRuns as STOPPED and unpause sending. Returns count cleared."""
    qs = SendRun.objects.filter(run_type=SendRun.RunType.REAL, status=SendRun.Status.RUNNING)
    if batch_date_str:
        qs = qs.filter(notes__icontains=f"batch_date={batch_date_str}")
    cleared = 0
    for run in qs:
        run.status = SendRun.Status.STOPPED
        run.finished_at = timezone.now()
        run.notes = (safe_str(run.notes) + " [manually cleared via dashboard]")[:4000]
        run.save(update_fields=["status", "finished_at", "notes"])
        cleared += 1
    set_email_sending_paused(paused=False, persist_to_dotenv=True)
    return cleared
