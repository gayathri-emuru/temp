from __future__ import annotations

import os
import time
from collections import OrderedDict, defaultdict
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    CompanyRecruiter,
    ApprovalRecord,
    GeneratedEmail,
    JobPosting,
    JobRecruiterTarget,
    SendRun,
    SenderAccount,
    SentEmailLog,
    TestEmailAccount,
)
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.apollo_recruiter_fetch_service import (
    job_has_apify_person_lead,
    upsert_apify_person_recruiter_from_apollo,
    upsert_company_recruiters_from_apollo,
)
from core.services.job_target_sync_service import sync_job_targets_for_job
from core.services.sender_account_service import (
    SenderUnavailableError,
    SenderWaitInterrupted,
    increment_sender_usage,
    is_smtp_daily_limit_error,
    pause_sender_for_daily_limit,
    pick_next_sender_for_today,
)
from core.services.email_suppression_service import (
    is_blocked_or_suppressed_email,
    is_suppressed_email,
    suppress_if_hard_bounce,
)
from core.services.email_sending_control_service import (
    get_email_sending_state,
    get_resume_attachment_state,
    is_email_sending_enabled,
)
from core.services.company_send_block_service import company_is_send_blocked
from core.services.live_company_reply_service import company_has_reply_stop_today
from core.services.mail_delivery_service import send_via_sender_account as send_via_smtp
from core.services.smtp_send_service import build_mime_message
from core.services.email_verification_service import EmailVerificationBlockedError, enforce_email_verification
from core.services.email_composition_service import build_full_email_body
from core.services.send_timing_service import configured_send_delay_seconds, randomized_send_delay_seconds
from core.utils import safe_str


DEFAULT_DELAY_SECONDS = 300
_SEND_RUN_PROGRESS: dict[int, dict] = {}


def _format_progress_time(value):
    if not value:
        return ""
    try:
        return timezone.localtime(value).strftime("%b %#d, %Y %#I:%M:%S %p")
    except Exception:
        return ""


def _set_send_run_progress(send_run: SendRun, **updates) -> None:
    if not send_run or not send_run.id:
        return
    current = dict(_SEND_RUN_PROGRESS.get(send_run.id) or {})
    current.update(updates)
    current["updated_at"] = timezone.now()
    current["updated_at_display"] = _format_progress_time(current["updated_at"])
    _SEND_RUN_PROGRESS[send_run.id] = current


def get_send_run_progress(send_run_id: Optional[int]) -> dict:
    if not send_run_id:
        return {}
    return dict(_SEND_RUN_PROGRESS.get(int(send_run_id)) or {})


def _recipient_progress_payload(*, job: JobPosting, rec: dict, sender: SenderAccount | None = None) -> dict:
    return {
        "email": safe_str(rec.get("email", "")).strip().lower(),
        "name": safe_str(rec.get("name", "")).strip(),
        "company": safe_str(job.company).strip(),
        "job_title": safe_str(job.title).strip(),
        "sender": safe_str(getattr(sender, "email", "")).strip() if sender else "",
    }


def _sending_disabled_error_message() -> str:
    state = get_email_sending_state()
    return (
        "Sending is disabled. No further emails will be sent. "
        f"(EMAIL_SENDING_ENABLED={'1' if state['env_enabled'] else '0'}, "
        f"EMAIL_SENDING_PAUSED={'1' if state['paused'] else '0'})"
    )


def _stop_run_due_to_disabled_sending(*, send_run: SendRun, run_log_path: str, reason: str) -> None:
    existing_notes = safe_str(send_run.notes).strip()
    send_run.status = SendRun.Status.STOPPED
    send_run.stopped_manually = True
    send_run.finished_at = timezone.now()
    send_run.notes = (f"{existing_notes} | {reason}" if existing_notes else safe_str(reason))[:4000]
    send_run.save(update_fields=["status", "stopped_manually", "finished_at", "notes"])
    _set_send_run_progress(
        send_run,
        phase="stopped",
        phase_label="Stopped",
        stop_reason=safe_str(reason),
        finished_at=send_run.finished_at,
        finished_at_display=_format_progress_time(send_run.finished_at),
    )
    append_and_print(run_log_path, f"STOPPED reason={reason}")


def _stop_run_due_to_sender_unavailable(
    *,
    send_run: SendRun,
    run_log_path: str,
    error: SenderUnavailableError,
) -> None:
    next_available_at = getattr(error, "next_available_at", None)
    next_display = _format_progress_time(next_available_at) if next_available_at else ""
    reason = safe_str(error).strip() or "No sender account is currently available."
    if next_display:
        reason = f"{reason} Next available sender: {next_display}."
    existing_notes = safe_str(send_run.notes).strip()
    send_run.status = SendRun.Status.STOPPED
    send_run.stopped_manually = False
    send_run.finished_at = timezone.now()
    send_run.notes = (f"{existing_notes} | {reason}" if existing_notes else reason)[:4000]
    send_run.save(update_fields=["status", "stopped_manually", "finished_at", "notes"])
    _set_send_run_progress(
        send_run,
        phase="stopped",
        phase_label="Stopped: sender unavailable",
        stop_reason=reason,
        finished_at=send_run.finished_at,
        finished_at_display=_format_progress_time(send_run.finished_at),
    )
    append_and_print(run_log_path, f"STOPPED reason=sender_unavailable detail={reason}")


def _send_run_was_stopped_externally(send_run: SendRun) -> bool:
    if not send_run or not send_run.id:
        return False
    return SendRun.objects.filter(id=send_run.id, status=SendRun.Status.STOPPED).exists()


def _note_run_stopped_externally(*, send_run: SendRun, run_log_path: str) -> None:
    try:
        send_run.refresh_from_db(fields=["status", "finished_at", "notes"])
    except Exception:
        pass
    _set_send_run_progress(
        send_run,
        phase="stopped",
        phase_label="Stopped from dashboard",
        stop_reason="Dashboard marked this run stopped.",
        finished_at=getattr(send_run, "finished_at", None),
        finished_at_display=_format_progress_time(getattr(send_run, "finished_at", None)),
    )
    append_and_print(run_log_path, "STOPPED reason=dashboard_stop")


def _pick_sender_for_send(*, send_run: SendRun, run_log_path: str) -> Optional[SenderAccount]:
    def _on_sender_wait(next_available_at, wait_seconds: int) -> None:
        next_display = _format_progress_time(next_available_at)
        _set_send_run_progress(
            send_run,
            phase="waiting_for_sender",
            phase_label="Waiting for sender availability",
            next_send_after=next_available_at,
            next_send_after_display=next_display,
            wait_seconds=max(0, int(wait_seconds or 0)),
        )
        append_and_print(
            run_log_path,
            f"WAIT_SENDER seconds={max(0, int(wait_seconds or 0))} next_available_at={next_display or next_available_at}",
        )

    def _should_continue_waiting() -> bool:
        if _send_run_was_stopped_externally(send_run):
            _note_run_stopped_externally(send_run=send_run, run_log_path=run_log_path)
            return False
        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(
                send_run=send_run,
                run_log_path=run_log_path,
                reason=_sending_disabled_error_message(),
            )
            return False
        return True

    try:
        return pick_next_sender_for_today(
            on_wait=_on_sender_wait,
            should_continue_waiting=_should_continue_waiting,
        )
    except SenderWaitInterrupted:
        return None
    except SenderUnavailableError as exc:
        _stop_run_due_to_sender_unavailable(send_run=send_run, run_log_path=run_log_path, error=exc)
        return None


def _resume_attachments() -> list[str]:
    if not get_resume_attachment_state()["enabled"]:
        return []

    path = safe_str(getattr(settings, "DEFAULT_RESUME_PATH", "")).strip()
    if not path:
        raise RuntimeError("DEFAULT_RESUME_PATH is not set. Refusing to send without an explicit resume path.")
    if not os.path.exists(path):
        raise RuntimeError(f"Resume file not found: {path}")
    return [path]


def _get_test_to_email() -> str:
    env_value = os.getenv("TEST_TO_EMAIL", "").strip().lower()
    if env_value:
        return env_value

    obj = TestEmailAccount.objects.filter(is_active=True).order_by("rotation_order", "id").first()
    if obj:
        return obj.email.strip().lower()

    raise RuntimeError("No test recipient configured. Set TEST_TO_EMAIL or add an active TestEmailAccount.")


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _job_is_approved(job: JobPosting) -> bool:
    try:
        return bool(job.approval_record and job.approval_record.is_approved)
    except ApprovalRecord.DoesNotExist:
        return False


def _get_generated_email(job: JobPosting) -> Optional[GeneratedEmail]:
    try:
        g = job.generated_email
    except GeneratedEmail.DoesNotExist:
        return None
    if not safe_str(g.subject).strip() or not safe_str(g.body).strip():
        return None
    return g


def _already_real_sent(to_email: str) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    return SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
        to_email=to_email,
    ).exists()


def _has_prior_real_initial_log(to_email: str, *, include_pending: bool = False) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    statuses = [SentEmailLog.SendStatus.SENT]
    if include_pending:
        statuses.append(SentEmailLog.SendStatus.PENDING)
    return SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status__in=statuses,
        message_type=SentEmailLog.MessageType.INITIAL,
        to_email=to_email,
    ).exists()


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


def _company_round_robin_jobs(jobs) -> list[JobPosting]:
    grouped: OrderedDict[str, list[JobPosting]] = OrderedDict()
    for job in jobs:
        if job.company_ref_id and job.company_ref:
            key = safe_str(job.company_ref.normalized_name).strip().lower()
        else:
            key = safe_str(job.company).strip().lower()
        grouped.setdefault(key or f"job:{job.id}", []).append(job)

    ordered = []
    index = 0
    while True:
        added = False
        for company_jobs in grouped.values():
            if index < len(company_jobs):
                ordered.append(company_jobs[index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def _company_send_order_key(job: JobPosting) -> str:
    if job.company_ref_id and job.company_ref:
        return safe_str(job.company_ref.normalized_name).strip().lower()
    return safe_str(job.company).strip().lower() or f"job:{job.id}"


def _round_robin_send_items(items: list[dict]) -> list[dict]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for item in items:
        grouped.setdefault(safe_str(item.get("company_key")).strip() or f"job:{item['job'].id}", []).append(item)

    ordered = []
    index = 0
    while True:
        added = False
        for company_items in grouped.values():
            if index < len(company_items):
                ordered.append(company_items[index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def _eligible_targets_for_initial_send(job: JobPosting) -> list[JobRecruiterTarget]:
    targets = list(
        job.targets
        .filter(
            is_selected_for_job=True,
            recipient_email_snapshot__isnull=False,
            company_recruiter__email_sent=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter")
        .order_by("selection_order", "id")
    )
    return [target for target in targets if _target_allows_real_send(target)]


def _target_allows_real_send(target: JobRecruiterTarget) -> bool:
    recruiter = getattr(target, "company_recruiter", None)
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    apollo_id = safe_str(getattr(recruiter, "apollo_person_id", "")).strip()
    if source != "apollo" and not apollo_id:
        return True
    return safe_str(getattr(recruiter, "email_status", "")).strip().lower() == "verified"


def _sleep_between_sends(
    delay_seconds: int,
    *,
    send_run: SendRun,
    run_log_path: str,
    last_recipient: dict | None = None,
    last_sender: SenderAccount | None = None,
) -> bool:
    sleep_seconds = randomized_send_delay_seconds(delay_seconds)
    next_send_after = timezone.now() + timedelta(seconds=max(0, int(sleep_seconds)))
    _set_send_run_progress(
        send_run,
        phase="waiting",
        phase_label="Waiting before next email",
        last_recipient=last_recipient or {},
        next_send_after=next_send_after,
        next_send_after_display=_format_progress_time(next_send_after),
        wait_seconds=max(0, int(sleep_seconds)),
    )
    for _ in range(max(0, int(sleep_seconds))):
        time.sleep(1)
        if _send_run_was_stopped_externally(send_run):
            _note_run_stopped_externally(send_run=send_run, run_log_path=run_log_path)
            return False
        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(
                send_run=send_run,
                run_log_path=run_log_path,
                reason=_sending_disabled_error_message(),
            )
            return False
    _set_send_run_progress(send_run, phase="ready", phase_label="Preparing next email")
    return True


def _send_switch_allows_send(*, send_run: SendRun, run_log_path: str) -> bool:
    if _send_run_was_stopped_externally(send_run):
        _note_run_stopped_externally(send_run=send_run, run_log_path=run_log_path)
        return False
    return is_email_sending_enabled()


def _company_stopped_by_live_reply(*, job: JobPosting, run_log_path: str, refresh: bool = True) -> bool:
    return company_has_reply_stop_today(job)


def _build_followup_template_default() -> str:
    return (
        "Just following up on my note about {role_reference}. "
        "I'm still very interested in the team's work and believe I could contribute strongly in a data/ML role. "
        "Would you be open to a quick coffee chat this week?"
    )


def _followup_role_reference(*, job_title: str = "", company_name: str = "") -> str:
    title = safe_str(job_title).strip()
    company = safe_str(company_name).strip()
    if title and company:
        return f"the {title} role at {company}"
    if title:
        return f"the {title} role"
    if company:
        return f"the role at {company}"
    return "the role"


def _render_followup_body(
    *,
    recipient_name: str,
    sender_name: str,
    job_title: str = "",
    company_name: str = "",
) -> str:
    template = os.getenv("FOLLOWUP_EMAIL_BODY", "").strip()
    if not template:
        template = _build_followup_template_default()
    return template.format(
        recipient_name=recipient_name or "there",
        sender_name=sender_name or "Gayathri",
        job_title=safe_str(job_title).strip() or "the role",
        company_name=safe_str(company_name).strip() or "the team",
        role_reference=_followup_role_reference(job_title=job_title, company_name=company_name),
    )


def _followup_allowed(to_email: str) -> tuple[bool, str, Optional[timezone.datetime]]:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False, "empty_email", None

    last_initial = (
        SentEmailLog.objects
        .filter(
            to_email=to_email,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
        )
        .order_by("-sent_at", "-id")
        .first()
    )

    if not last_initial or not last_initial.sent_at:
        return False, "no_prior_real_send", None

    min_days = int(os.getenv("FOLLOWUP_MIN_DAYS_SINCE_LAST", "3") or "3")
    if timezone.now() < last_initial.sent_at + timedelta(days=min_days):
        return False, "too_soon", last_initial.sent_at

    min_gap_days = int(os.getenv("FOLLOWUP_MIN_GAP_DAYS", "7") or "7")
    last_followup = (
        SentEmailLog.objects
        .filter(
            to_email=to_email,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.FOLLOW_UP,
        )
        .order_by("-sent_at", "-id")
        .first()
    )
    if last_followup and last_followup.sent_at and timezone.now() < last_followup.sent_at + timedelta(days=min_gap_days):
        return False, "followup_too_recent", last_followup.sent_at

    return True, "ok", last_initial.sent_at


def run_send_initial_for_batch(
    *,
    batch_date_str: str,
    send_type: str,
    delay_seconds: Optional[int] = None,
    allow_recipient_discovery: bool = True,
    skip_pending_recipients: bool = False,
    source_label: str = "Review dashboard",
) -> dict:
    send_type = safe_str(send_type).strip().lower()
    if send_type not in {"test", "real"}:
        raise ValueError("send_type must be 'test' or 'real'")

    if not is_email_sending_enabled():
        raise RuntimeError(_sending_disabled_error_message())

    if delay_seconds is None:
        delay_seconds = configured_send_delay_seconds()
    delay_seconds = max(0, int(delay_seconds))

    run_log_path = create_run_log_path("send_initial", f"{batch_date_str}_{send_type}")
    append_and_print(run_log_path, f"START batch_date={batch_date_str} send_type={send_type} delay_seconds={delay_seconds}")
    if not allow_recipient_discovery:
        append_and_print(run_log_path, "SEND_ONLY_MODE discovery=disabled apollo_topup=disabled legacy_refresh=disabled")

    jobs = list(
        JobPosting.objects
        .filter(daily_batch__batch_date=batch_date_str, is_manual_email_job=False)
        .select_related("company_ref")
        .prefetch_related("targets", "approval_record", "generated_email")
        .order_by("company_ref__normalized_name", "id")
    )
    if send_type == "real":
        jobs = _company_round_robin_jobs(jobs)

    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.TEST if send_type == "test" else SendRun.RunType.REAL,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=delay_seconds,
        notes=f"{source_label} send_initial batch_date={batch_date_str} run_log_path={run_log_path}",
    )
    _set_send_run_progress(
        send_run,
        phase="planning",
        phase_label="Building send plan",
        batch_date=batch_date_str,
        source_label=source_label,
    )

    totals = {
        "send_run_id": send_run.id,
        "jobs_seen": 0,
        "jobs_skipped_not_approved": 0,
        "jobs_skipped_no_content": 0,
        "jobs_skipped_no_recipients": 0,
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_skipped_already_sent": 0,
        "emails_skipped_pending": 0,
        "emails_skipped_company_cap": 0,
        "emails_skipped_company_blocked": 0,
        "emails_skipped_company_reply_stop": 0,
        "emails_skipped_suppressed": 0,
        "emails_failed": 0,
        "run_log_path": run_log_path,
    }

    test_to = _get_test_to_email() if send_type == "test" else ""

    stopped = False
    deferred_send_items = []
    deferred_success_by_job_id: defaultdict[int, int] = defaultdict(int)

    def _verify_recipient_before_send(*, job: JobPosting, to_email: str) -> bool:
        if send_type != "real":
            return True
        try:
            enforce_email_verification(to_email)
            return True
        except EmailVerificationBlockedError as exc:
            totals["emails_skipped_suppressed"] += 1
            append_and_print(
                run_log_path,
                f"SKIP_EMAIL job_id={job.id} to={to_email} reason=email_verifier_blocked detail={safe_str(exc)[:300]}",
            )
            return False

    def _send_one_recipient(job: JobPosting, generated: GeneratedEmail, rec: dict) -> tuple[int, bool]:
        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(
                send_run=send_run,
                run_log_path=run_log_path,
                reason=_sending_disabled_error_message(),
            )
            return 0, True

        to_email = rec["email"]
        target_id = rec.get("target_id")

        if send_type == "real" and company_is_send_blocked(job):
            totals["emails_skipped_company_blocked"] += 1
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=company_blocked")
            return 0, False

        if send_type == "real" and _company_stopped_by_live_reply(job=job, run_log_path=run_log_path):
            totals["emails_skipped_company_reply_stop"] += 1
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=manual_company_stop_today")
            return 0, False

        if send_type == "real" and _already_real_sent(to_email):
            totals["emails_skipped_already_sent"] += 1
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=already_sent")
            return 0, False

        if send_type == "real" and skip_pending_recipients and _has_prior_real_initial_log(to_email, include_pending=True):
            totals["emails_skipped_pending"] += 1
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=pending_or_sent")
            return 0, False

        if send_type == "real" and is_blocked_or_suppressed_email(to_email):
            totals["emails_skipped_suppressed"] += 1
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=suppressed_or_verifier_blocked")
            return 0, False

        if not _verify_recipient_before_send(job=job, to_email=to_email):
            return 0, False

        sender = _pick_sender_for_send(send_run=send_run, run_log_path=run_log_path)
        if not sender:
            return 0, True
        if not _send_switch_allows_send(send_run=send_run, run_log_path=run_log_path):
            return 0, True
        current_recipient = _recipient_progress_payload(job=job, rec=rec, sender=sender)
        _set_send_run_progress(
            send_run,
            phase="sending",
            phase_label="Sending now",
            current_recipient=current_recipient,
            last_recipient=current_recipient,
        )
        from_name = safe_str(sender.display_name) or "Gayathri Emuru"
        subject = safe_str(generated.subject).strip()
        body = safe_str(generated.body).strip()

        if send_type == "test":
            subject = f"[TEST] {subject}"
            intended = rec.get("intended") or []
            body = f"INTENDED_TO: {', '.join(intended) if intended else '[NONE]'}\n\n" + body

        attachments = _resume_attachments() if send_type == "real" else _resume_attachments()
        final_body = build_full_email_body(
            recipient_name=safe_str(rec.get("name") or "").strip(),
            base_body=body,
            job_linkedin_url=safe_str(getattr(job, "normalized_linkedin_url", "")).strip()
            or safe_str(getattr(job, "linkedin_url", "")).strip(),
            manual_job_reference_id=safe_str(getattr(job, "manual_job_reference_id", "")).strip(),
        )

        log_row = SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job,
            job_recruiter_target_id=target_id,
            sender_account=sender,
            to_email=to_email,
            subject_snapshot=subject[:500],
            body_snapshot=final_body,
            attachment_path=";".join(attachments)[:1000],
            send_type=SentEmailLog.SendType.TEST if send_type == "test" else SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.PENDING,
        )
        totals["emails_attempted"] += 1
        append_and_print(run_log_path, f"SEND_START job_id={job.id} to={to_email} sender={sender.email} log_id={log_row.id}")

        sent_success = 0
        try:
            msg = build_mime_message(
                from_name=from_name,
                from_email=sender.email,
                to_email=to_email,
                subject=subject,
                body_text=final_body,
                attachment_paths=attachments,
            )
            send_via_smtp(sender=sender, message=msg, enforce_recipient_verification=send_type == "real")
            log_row.status = SentEmailLog.SendStatus.SENT
            log_row.sent_at = timezone.now()
            log_row.error_message = ""
            log_row.save(update_fields=["status", "sent_at", "error_message"])
            increment_sender_usage(sender, 1)

            if send_type == "real" and target_id:
                JobRecruiterTarget.objects.filter(id=target_id).update(is_sent_real=True)
                CompanyRecruiter.objects.filter(job_targets__id=target_id).update(
                    email_sent=True,
                    email_sent_date=timezone.localdate(),
                )

            totals["emails_sent"] += 1
            sent_success = 1
            append_and_print(run_log_path, f"SEND_OK job_id={job.id} to={to_email} log_id={log_row.id}")
        except IntegrityError as exc:
            totals["emails_skipped_already_sent"] += 1
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = f"IntegrityError (likely already-sent): {exc}"
            log_row.save(update_fields=["status", "error_message"])
            append_and_print(run_log_path, f"SEND_SKIP_DUP job_id={job.id} to={to_email} err={exc}")
        except Exception as exc:
            totals["emails_failed"] += 1
            error_text = str(exc)
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = error_text[:4000]
            log_row.save(update_fields=["status", "error_message"])
            if send_type == "real":
                suppress_if_hard_bounce(email=to_email, error_message=error_text)
            if is_smtp_daily_limit_error(error_text):
                paused = pause_sender_for_daily_limit(sender, error_text)
                append_and_print(run_log_path, f"SENDER_AUTO_PAUSE sender={sender.email} reason=daily_limit paused={paused}")
            append_exception(run_log_path, f"SEND_FAIL job_id={job.id} to={to_email} log_id={log_row.id}", exc)

        if delay_seconds and not _sleep_between_sends(
            delay_seconds,
            send_run=send_run,
            run_log_path=run_log_path,
            last_recipient=current_recipient,
            last_sender=sender,
        ):
            return sent_success, True
        return sent_success, False
    # Track companies already auto-fetched this send run — prevents multiple Apollo
    # calls for the same company when it has several jobs with no recipients.
    auto_fetched_company_ids: set[int] = set()
    for job in jobs:
        totals["jobs_seen"] += 1

        if not _job_is_approved(job):
            totals["jobs_skipped_not_approved"] += 1
            continue

        if send_type == "real" and company_is_send_blocked(job):
            totals["emails_skipped_company_blocked"] += len(_eligible_targets_for_initial_send(job))
            append_and_print(run_log_path, f"SKIP job_id={job.id} reason=company_blocked")
            continue

        if send_type == "real" and company_has_reply_stop_today(job):
            totals["emails_skipped_company_reply_stop"] += len(_eligible_targets_for_initial_send(job))
            append_and_print(run_log_path, f"SKIP job_id={job.id} reason=manual_company_stop_today")
            continue

        generated = _get_generated_email(job)
        if not generated:
            totals["jobs_skipped_no_content"] += 1
            append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_generated_email")
            continue

        recipients = []

        for t in _eligible_targets_for_initial_send(job):
            recipients.append({
                "name": safe_str(t.recipient_name_snapshot).strip() or "there",
                "email": safe_str(t.recipient_email_snapshot).strip().lower(),
                "target_id": t.id,
            })

        if send_type == "real":
            filtered = []
            for r in recipients:
                email = r["email"]
                if _already_real_sent(email):
                    totals["emails_skipped_already_sent"] += 1
                    continue
                if skip_pending_recipients and _has_prior_real_initial_log(email, include_pending=True):
                    totals["emails_skipped_pending"] += 1
                    continue
                filtered.append(r)
            recipients = filtered

        if allow_recipient_discovery and not recipients and send_type == "real" and job.company_ref_id and job.company_ref:
            if not is_email_sending_enabled():
                _stop_run_due_to_disabled_sending(
                    send_run=send_run,
                    run_log_path=run_log_path,
                    reason=_sending_disabled_error_message(),
                )
                stopped = True
                break
            auto_fetch = os.getenv("AUTO_APOLLO_FETCH_ON_SEND", "1").strip().lower() in {"1", "true", "yes", "on"}
            has_key = bool(os.getenv("APOLLO_API_KEY", "").strip())
            if auto_fetch and has_key and job.company_ref.id in auto_fetched_company_ids and not job_has_apify_person_lead(job):
                append_and_print(
                    run_log_path,
                    f"AUTO_APOLLO_SKIP job_id={job.id} company={job.company_ref.normalized_name} reason=already_attempted_this_run",
                )
            elif auto_fetch and has_key:
                append_and_print(run_log_path, f"AUTO_APOLLO_START job_id={job.id} company={job.company_ref.normalized_name}")
                try:
                    from core.services.app_settings_service import get_max_people_per_company
                    _auto_fetch_cap = get_max_people_per_company()
                    exact_stats = {}
                    if job_has_apify_person_lead(job):
                        append_and_print(run_log_path, f"AUTO_APOLLO_EXACT_PERSON_START job_id={job.id}")
                        exact_stats = upsert_apify_person_recruiter_from_apollo(
                            job=job,
                            run_log_path=create_run_log_path("apollo_autofetch_exact_person", f"job_{job.id}"),
                        )
                        append_and_print(
                            run_log_path,
                            (
                                f"AUTO_APOLLO_EXACT_PERSON_DONE job_id={job.id} "
                                f"emails={int(exact_stats.get('emails_found') or 0)} "
                                f"status={safe_str(exact_stats.get('status')) or '[NONE]'}"
                            ),
                        )

                    if not int(exact_stats.get("emails_found") or 0):
                        auto_fetched_company_ids.add(job.company_ref.id)
                        upsert_company_recruiters_from_apollo(
                            company=job.company_ref,
                            max_people=_auto_fetch_cap,
                            run_log_path=create_run_log_path("apollo_autofetch_on_send", job.company_ref.normalized_name),
                        )
                        sync_job_targets_for_job(
                            job=job,
                            max_targets=_auto_fetch_cap,
                            auto_select=True,
                            allow_fallback_contacts=True,
                        )
                    # Refresh targets for recipient build.
                    job.refresh_from_db()
                except Exception as exc:
                    append_exception(run_log_path, f"AUTO_APOLLO_FAIL job_id={job.id}", exc)

                # Rebuild recipients after auto-fetch.
                recipients = []
                for t in _eligible_targets_for_initial_send(job):
                    recipients.append({
                        "name": safe_str(t.recipient_name_snapshot).strip() or "there",
                        "email": safe_str(t.recipient_email_snapshot).strip().lower(),
                        "target_id": t.id,
                    })
                filtered = []
                for r in recipients:
                    email = r["email"]
                    if _already_real_sent(email):
                        totals["emails_skipped_already_sent"] += 1
                        continue
                    if skip_pending_recipients and _has_prior_real_initial_log(email, include_pending=True):
                        totals["emails_skipped_pending"] += 1
                        continue
                    filtered.append(r)
                recipients = filtered

        if not recipients:
            totals["jobs_skipped_no_recipients"] += 1
            append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_selected_recipients")
            continue

        if send_type == "test":
            # For test sends, send one email per approved job to the test inbox.
            recipients = [{
                "name": "Test",
                "email": test_to,
                "target_id": None,
                "intended": [r["email"] for r in recipients],
            }]

        if send_type == "real" and not allow_recipient_discovery:
            for rec in recipients:
                deferred_send_items.append(
                    {
                        "company_key": _company_send_order_key(job),
                        "job": job,
                        "generated": generated,
                        "rec": rec,
                    }
                )
            continue

        job_sent_success = 0

        for rec in recipients:
            if not is_email_sending_enabled():
                _stop_run_due_to_disabled_sending(
                    send_run=send_run,
                    run_log_path=run_log_path,
                    reason=_sending_disabled_error_message(),
                )
                stopped = True
                break

            to_email = rec["email"]
            target_id = rec.get("target_id")

            if send_type == "real" and company_is_send_blocked(job):
                totals["emails_skipped_company_blocked"] += 1
                append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=company_blocked")
                continue

            if send_type == "real" and _company_stopped_by_live_reply(job=job, run_log_path=run_log_path):
                totals["emails_skipped_company_reply_stop"] += 1
                append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=manual_company_stop_today")
                continue

            if send_type == "real" and _already_real_sent(to_email):
                totals["emails_skipped_already_sent"] += 1
                append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=already_sent")
                continue

            if send_type == "real" and is_blocked_or_suppressed_email(to_email):
                totals["emails_skipped_suppressed"] += 1
                append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={to_email} reason=suppressed_or_verifier_blocked")
                continue

            if not _verify_recipient_before_send(job=job, to_email=to_email):
                continue

            sender = _pick_sender_for_send(send_run=send_run, run_log_path=run_log_path)
            if not sender:
                stopped = True
                break
            if not _send_switch_allows_send(send_run=send_run, run_log_path=run_log_path):
                stopped = True
                break
            current_recipient = _recipient_progress_payload(job=job, rec=rec, sender=sender)
            _set_send_run_progress(
                send_run,
                phase="sending",
                phase_label="Sending now",
                current_recipient=current_recipient,
                last_recipient=current_recipient,
            )
            from_name = safe_str(sender.display_name) or "Gayathri Emuru"

            subject = safe_str(generated.subject).strip()
            body = safe_str(generated.body).strip()

            if send_type == "test":
                subject = f"[TEST] {subject}"
                intended = rec.get("intended") or []
                body = f"INTENDED_TO: {', '.join(intended) if intended else '[NONE]'}\n\n" + body

            attachments = _resume_attachments() if send_type == "real" else _resume_attachments()
            final_body = build_full_email_body(
                recipient_name=safe_str(rec.get("name") or "").strip(),
                base_body=body,
                job_linkedin_url=safe_str(getattr(job, "normalized_linkedin_url", "")).strip()
                or safe_str(getattr(job, "linkedin_url", "")).strip(),
                manual_job_reference_id=safe_str(getattr(job, "manual_job_reference_id", "")).strip(),
            )

            log_row = SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                job_recruiter_target_id=target_id,
                sender_account=sender,
                to_email=to_email,
                subject_snapshot=subject[:500],
                body_snapshot=final_body,
                attachment_path=";".join(attachments)[:1000],
                send_type=SentEmailLog.SendType.TEST if send_type == "test" else SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.PENDING,
            )

            totals["emails_attempted"] += 1
            append_and_print(
                run_log_path,
                f"SEND_START job_id={job.id} to={to_email} sender={sender.email} log_id={log_row.id}",
            )

            try:
                msg = build_mime_message(
                    from_name=from_name,
                    from_email=sender.email,
                    to_email=to_email,
                    subject=subject,
                    body_text=final_body,
                    attachment_paths=attachments,
                )
                send_via_smtp(sender=sender, message=msg, enforce_recipient_verification=send_type == "real")

                log_row.status = SentEmailLog.SendStatus.SENT
                log_row.sent_at = timezone.now()
                log_row.error_message = ""
                log_row.save(update_fields=["status", "sent_at", "error_message"])

                increment_sender_usage(sender, 1)

                if send_type == "real" and target_id:
                    JobRecruiterTarget.objects.filter(id=target_id).update(is_sent_real=True)
                    CompanyRecruiter.objects.filter(job_targets__id=target_id).update(
                        email_sent=True,
                        email_sent_date=timezone.localdate(),
                    )

                totals["emails_sent"] += 1
                job_sent_success += 1
                append_and_print(run_log_path, f"SEND_OK job_id={job.id} to={to_email} log_id={log_row.id}")

            except IntegrityError as exc:
                # In case another row already marked SENT for this to_email (uniq constraint).
                totals["emails_skipped_already_sent"] += 1
                log_row.status = SentEmailLog.SendStatus.FAILED
                log_row.error_message = f"IntegrityError (likely already-sent): {exc}"
                log_row.save(update_fields=["status", "error_message"])
                append_and_print(run_log_path, f"SEND_SKIP_DUP job_id={job.id} to={to_email} err={exc}")

            except Exception as exc:
                totals["emails_failed"] += 1
                error_text = str(exc)
                log_row.status = SentEmailLog.SendStatus.FAILED
                log_row.error_message = error_text[:4000]
                log_row.save(update_fields=["status", "error_message"])
                if send_type == "real":
                    suppress_if_hard_bounce(email=to_email, error_message=error_text)
                if is_smtp_daily_limit_error(error_text):
                    paused = pause_sender_for_daily_limit(sender, error_text)
                    append_and_print(
                        run_log_path,
                        f"SENDER_AUTO_PAUSE sender={sender.email} reason=daily_limit paused={paused}",
                    )
                append_exception(run_log_path, f"SEND_FAIL job_id={job.id} to={to_email} log_id={log_row.id}", exc)

            if delay_seconds and not _sleep_between_sends(
                delay_seconds,
                send_run=send_run,
                run_log_path=run_log_path,
                last_recipient=current_recipient,
                last_sender=sender,
            ):
                stopped = True
                break

        if stopped:
            break

        if send_type == "test":
            JobPosting.objects.filter(id=job.id).update(status=JobPosting.Status.TEST_SENT)
        else:
            if job_sent_success > 0:
                JobPosting.objects.filter(id=job.id).update(status=JobPosting.Status.REAL_SENT)

    if not stopped and deferred_send_items:
        ordered_items = _round_robin_send_items(deferred_send_items)
        append_and_print(
            run_log_path,
            f"SEND_ORDER mode=company_recipient_round_robin tasks={len(ordered_items)} companies={len(set(i['company_key'] for i in ordered_items))}",
        )
        for item in ordered_items:
            sent_success, stopped = _send_one_recipient(
                job=item["job"],
                generated=item["generated"],
                rec=item["rec"],
            )
            if sent_success:
                deferred_success_by_job_id[item["job"].id] += sent_success
            if stopped:
                break

        for job_id, sent_count in deferred_success_by_job_id.items():
            if sent_count > 0:
                JobPosting.objects.filter(id=job_id).update(status=JobPosting.Status.REAL_SENT)

    if send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = f"{source_label} done. sent={totals['emails_sent']} failed={totals['emails_failed']}"
        send_run.save(update_fields=["status", "finished_at", "notes"])
        _set_send_run_progress(
            send_run,
            phase="done",
            phase_label="Finished",
            finished_at=send_run.finished_at,
            finished_at_display=_format_progress_time(send_run.finished_at),
        )

    append_and_print(run_log_path, f"END totals={totals}")
    return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}


def run_send_followups_for_batch(
    *,
    batch_date_str: str,
    post_data,
    delay_seconds: Optional[int] = None,
) -> dict:
    if not is_email_sending_enabled():
        raise RuntimeError(_sending_disabled_error_message())

    if delay_seconds is None:
        delay_seconds = configured_send_delay_seconds()
    delay_seconds = max(0, int(delay_seconds))

    run_log_path = create_run_log_path("send_followups", batch_date_str)
    append_and_print(run_log_path, f"START batch_date={batch_date_str} delay_seconds={delay_seconds}")

    # Collect selected follow-up recipients (company-level) from POST keys.
    selected_recruiter_ids = set()
    prefix = "company_followup__"
    for key, value in post_data.items():
        if safe_str(value) != "1":
            continue
        if not safe_str(key).startswith(prefix):
            continue
        try:
            # key: company_followup__{company_id}__{recruiter_id}
            parts = safe_str(key).split("__")
            recruiter_id = int(parts[-1])
            selected_recruiter_ids.add(recruiter_id)
        except Exception:
            continue

    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.REAL,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=delay_seconds,
        notes=f"Review dashboard followups batch_date={batch_date_str}",
    )

    totals = {
        "send_run_id": send_run.id,
        "selected_recruiters": len(selected_recruiter_ids),
        "recruiters_seen": 0,
        "recruiters_skipped": 0,
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_skipped_suppressed": 0,
        "emails_failed": 0,
        "run_log_path": run_log_path,
    }

    if not selected_recruiter_ids:
        send_run.status = SendRun.Status.STOPPED
        send_run.finished_at = timezone.now()
        send_run.notes = "No follow-up targets selected."
        send_run.save(update_fields=["status", "finished_at", "notes"])
        append_and_print(run_log_path, f"END totals={totals}")
        return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}

    recruiters = (
        CompanyRecruiter.objects
        .filter(id__in=list(selected_recruiter_ids), is_active=True)
        .select_related("company")
        .order_by("company__normalized_name", "normalized_person_name", "id")
    )

    sender_name = os.getenv("SENDER_DISPLAY_NAME", "Gayathri Emuru").strip() or "Gayathri Emuru"
    subject = os.getenv("FOLLOWUP_EMAIL_SUBJECT", "Following up").strip() or "Following up"

    stopped = False
    for recruiter in recruiters:
        totals["recruiters_seen"] += 1

        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(
                send_run=send_run,
                run_log_path=run_log_path,
                reason=_sending_disabled_error_message(),
            )
            stopped = True
            break

        to_email = safe_str(recruiter.email).strip().lower()
        if not _has_real_email(to_email):
            totals["recruiters_skipped"] += 1
            continue

        if is_suppressed_email(to_email):
            totals["emails_skipped_suppressed"] += 1
            totals["recruiters_skipped"] += 1
            append_and_print(run_log_path, f"SKIP recruiter_id={recruiter.id} to={to_email} reason=suppressed")
            continue

        allowed, reason, last_sent_at = _followup_allowed(to_email)
        if not allowed:
            totals["recruiters_skipped"] += 1
            append_and_print(run_log_path, f"SKIP recruiter_id={recruiter.id} to={to_email} reason={reason} last={last_sent_at}")
            continue

        job_for_log = (
            JobPosting.objects
            .filter(daily_batch__batch_date=batch_date_str, company_ref_id=recruiter.company_id, is_manual_email_job=False)
            .order_by("-id")
            .first()
        )
        if not job_for_log:
            totals["recruiters_skipped"] += 1
            append_and_print(run_log_path, f"SKIP recruiter_id={recruiter.id} to={to_email} reason=no_job_in_batch")
            continue

        sender = _pick_sender_for_send(send_run=send_run, run_log_path=run_log_path)
        if not sender:
            stopped = True
            break
        if not _send_switch_allows_send(send_run=send_run, run_log_path=run_log_path):
            stopped = True
            break
        current_recipient = {
            "email": to_email,
            "name": safe_str(recruiter.person_name).strip(),
            "company": safe_str(recruiter.company.normalized_name if recruiter.company_id else "").strip(),
            "job_title": "Follow-up",
            "sender": safe_str(sender.email).strip(),
        }
        _set_send_run_progress(
            send_run,
            phase="sending",
            phase_label="Sending follow-up now",
            current_recipient=current_recipient,
            last_recipient=current_recipient,
        )
        from_name = safe_str(sender.display_name) or sender_name

        body = _render_followup_body(
            recipient_name=safe_str(recruiter.person_name).strip() or "there",
            sender_name=sender_name,
            job_title=safe_str(getattr(job_for_log, "title", "")).strip(),
            company_name=safe_str(getattr(job_for_log, "company", "")).strip(),
        )
        final_body = build_full_email_body(
            recipient_name=safe_str(recruiter.person_name).strip(),
            base_body=body,
            job_linkedin_url=safe_str(getattr(job_for_log, "normalized_linkedin_url", "")).strip()
            or safe_str(getattr(job_for_log, "linkedin_url", "")).strip(),
            manual_job_reference_id=safe_str(getattr(job_for_log, "manual_job_reference_id", "")).strip(),
            include_resume_attachment_sentence=False,
        )

        log_row = SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job_for_log,
            job_recruiter_target=None,
            sender_account=sender,
            to_email=to_email,
            subject_snapshot=subject[:500],
            body_snapshot=final_body,
            attachment_path="",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.FOLLOW_UP,
            status=SentEmailLog.SendStatus.PENDING,
        )

        totals["emails_attempted"] += 1
        append_and_print(run_log_path, f"SEND_START recruiter_id={recruiter.id} to={to_email} sender={sender.email} log_id={log_row.id}")

        try:
            msg = build_mime_message(
                from_name=from_name,
                from_email=sender.email,
                to_email=to_email,
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
            append_and_print(run_log_path, f"SEND_OK recruiter_id={recruiter.id} to={to_email} log_id={log_row.id}")

        except Exception as exc:
            totals["emails_failed"] += 1
            error_text = str(exc)
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = error_text[:4000]
            log_row.save(update_fields=["status", "error_message"])
            suppress_if_hard_bounce(email=to_email, error_message=error_text)
            if is_smtp_daily_limit_error(error_text):
                paused = pause_sender_for_daily_limit(sender, error_text)
                append_and_print(
                    run_log_path,
                    f"SENDER_AUTO_PAUSE sender={sender.email} reason=daily_limit paused={paused}",
                )
            append_exception(run_log_path, f"SEND_FAIL recruiter_id={recruiter.id} to={to_email} log_id={log_row.id}", exc)

        if delay_seconds and not _sleep_between_sends(
            delay_seconds,
            send_run=send_run,
            run_log_path=run_log_path,
            last_recipient=current_recipient,
            last_sender=sender,
        ):
            stopped = True
            break

    if not stopped and send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = f"Done. sent={totals['emails_sent']} failed={totals['emails_failed']}"
        send_run.save(update_fields=["status", "finished_at", "notes"])
        _set_send_run_progress(
            send_run,
            phase="done",
            phase_label="Finished",
            finished_at=send_run.finished_at,
            finished_at_display=_format_progress_time(send_run.finished_at),
        )

    append_and_print(run_log_path, f"END totals={totals}")
    return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}
