from __future__ import annotations

import re
import ast
import json
import time

from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Company, DailyBatch, JobPosting, SendRun, SentEmailLog
from core.services.email_sending_control_service import get_email_sending_state, is_email_sending_enabled
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.normalization_service import (
    build_description_fingerprint,
    build_dedupe_key,
    build_sort_company,
    build_sort_location,
    build_sort_title,
    canonical_company_name,
    canonical_location,
    canonical_title,
    normalize_company_name,
    normalize_location,
    normalize_title,
)
from core.services.sender_account_service import (
    increment_sender_usage,
    is_smtp_daily_limit_error,
    pause_sender_for_daily_limit,
    pick_next_sender_for_today,
)
from core.services.email_suppression_service import is_suppressed_email, suppress_if_hard_bounce
from core.services.send_run_service import _resume_attachments
from core.services.send_timing_service import randomized_send_delay_seconds
from core.services.mail_delivery_service import send_via_sender_account as send_via_smtp
from core.services.smtp_send_service import build_mime_message
from core.utils import safe_str


def parse_manual_bulk_emails(raw_text: str) -> tuple[list[str], list[str]]:
    raw_text = safe_str(raw_text)
    candidates = re.split(r"[\s,;]+", raw_text)
    emails: list[str] = []
    invalid: list[str] = []
    seen = set()
    for item in candidates:
        email = safe_str(item).strip().strip("<>()[]{}\"'").lower()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            invalid.append(email)
            continue
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails, invalid


def _normalize_email(value: str) -> tuple[str, str]:
    email = safe_str(value).strip().strip("<>()[]{}\"'").lower()
    if not email:
        return "", ""
    try:
        validate_email(email)
    except ValidationError:
        return "", email
    return email, ""


def parse_manual_named_recipients(raw_text: str) -> tuple[list[dict], list[str]]:
    """
    Parses a JSON/Python dict in either direction:
      {"Jane Doe": "jane@example.com"}
      {"jane@example.com": "Jane Doe"}
    """
    raw_text = safe_str(raw_text).strip()
    if not raw_text:
        return [], []

    try:
        payload = json.loads(raw_text)
    except Exception:
        try:
            payload = ast.literal_eval(raw_text)
        except Exception:
            return [], ["Could not parse known-names dict."]

    if not isinstance(payload, dict):
        return [], ["Known-names input must be a dict."]

    recipients = []
    invalid = []
    seen = set()
    for key, value in payload.items():
        key_text = safe_str(key).strip()
        value_text = safe_str(value).strip()

        email, bad_email = _normalize_email(value_text)
        name = key_text
        if not email:
            email, bad_email = _normalize_email(key_text)
            name = value_text

        if not email:
            invalid.append(bad_email or f"{key_text}: {value_text}")
            continue

        if email in seen:
            continue
        seen.add(email)
        recipients.append({"email": email, "name": safe_str(name).strip() or "there"})

    return recipients, invalid


def _merge_manual_recipients(*, named_recipients: list[dict], unnamed_emails: list[str]) -> list[dict]:
    recipients = []
    seen = set()
    for row in named_recipients:
        email = safe_str(row.get("email")).strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        recipients.append({"email": email, "name": safe_str(row.get("name")).strip() or "there"})
    for email in unnamed_emails:
        email = safe_str(email).strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        recipients.append({"email": email, "name": "there"})
    return recipients


def _normalize_prepared_recipients(prepared_recipients: list[dict] | None) -> tuple[list[dict], list[str]]:
    recipients = []
    invalid = []
    seen = set()
    for row in prepared_recipients or []:
        email, bad_email = _normalize_email(safe_str(row.get("email")))
        if not email:
            invalid.append(bad_email or safe_str(row))
            continue
        if email in seen:
            continue
        seen.add(email)
        company = safe_str(row.get("company")).strip()
        recipients.append(
            {
                "email": email,
                "name": safe_str(row.get("name")).strip() or company or "there",
                "company": company,
                "website": safe_str(row.get("website")).strip(),
            }
        )
    return recipients, invalid


def _render_manual_text(template: str, *, name: str, company: str = "", website: str = "", email: str = "") -> str:
    name = safe_str(name).strip() or "there"
    company = safe_str(company).strip() or name
    website = safe_str(website).strip()
    email = safe_str(email).strip()
    rendered = safe_str(template)
    for key, value in {
        "name": name,
        "company": company,
        "website": website,
        "email": email,
    }.items():
        rendered = rendered.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return rendered


def _has_prior_real_initial_log(to_email: str, *, include_pending: bool = True) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    statuses = [SentEmailLog.SendStatus.SENT]
    if include_pending:
        statuses.append(SentEmailLog.SendStatus.PENDING)
    return SentEmailLog.objects.filter(
        to_email=to_email,
        send_type=SentEmailLog.SendType.REAL,
        status__in=statuses,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


def _stop_run_due_to_disabled_sending(*, send_run: SendRun, run_log_path: str) -> None:
    state = get_email_sending_state()
    reason = (
        "Sending stopped because email sending is disabled or paused. "
        f"EMAIL_SENDING_ENABLED={'1' if state['env_enabled'] else '0'} "
        f"EMAIL_SENDING_PAUSED={'1' if state['paused'] else '0'}"
    )
    send_run.status = SendRun.Status.STOPPED
    send_run.stopped_manually = True
    send_run.finished_at = timezone.now()
    send_run.notes = reason[:4000]
    send_run.save(update_fields=["status", "stopped_manually", "finished_at", "notes"])
    append_and_print(run_log_path, f"STOPPED reason={reason}")


def _sleep_between_sends(delay_seconds: int, *, send_run: SendRun, run_log_path: str) -> bool:
    sleep_seconds = randomized_send_delay_seconds(delay_seconds)
    for _ in range(max(0, int(sleep_seconds or 0))):
        time.sleep(1)
        if not is_email_sending_enabled():
            _stop_run_due_to_disabled_sending(send_run=send_run, run_log_path=run_log_path)
            return False
    return True


def _manual_bulk_job(*, subject: str, body: str, send_run: SendRun) -> JobPosting:
    batch, _ = DailyBatch.objects.get_or_create(
        batch_date=timezone.localdate(),
        defaults={
            "lookback_hours": 24,
            "max_jobs_requested": 0,
            "apify_run_status": DailyBatch.RunStatus.SUCCESS,
            "notes": "Manual bulk email send",
        },
    )
    company, _ = Company.objects.get_or_create(
        normalized_name="manual_bulk_email",
        defaults={"raw_name_latest": "Manual Bulk Email"},
    )
    title = safe_str(subject).strip()[:500] or "Manual Bulk Email"
    description = safe_str(body).strip()
    location = "United States"
    now_key = timezone.now().strftime("%Y%m%d%H%M%S")
    normalized_company = normalize_company_name(company.raw_name_latest)
    return JobPosting.objects.create(
        daily_batch=batch,
        is_manual_import=True,
        is_manual_email_job=True,
        company_ref=company,
        external_job_id=f"manual-bulk-{send_run.id}-{now_key}",
        linkedin_url=f"https://manual.local/bulk-email/{send_run.id}/",
        apply_url="",
        normalized_linkedin_url=f"https://manual.local/bulk-email/{send_run.id}/",
        normalized_apply_url="",
        title=title,
        company=company.raw_name_latest,
        location=location,
        salary="",
        description=description,
        description_fingerprint=build_description_fingerprint(description),
        normalized_company=normalized_company,
        normalized_title=normalize_title(title),
        normalized_location=normalize_location(location),
        canonical_company=canonical_company_name(normalized_company),
        canonical_title=canonical_title(title),
        canonical_location=canonical_location(location),
        dedupe_key=build_dedupe_key(normalized_company, title, location),
        sort_company=build_sort_company(company.raw_name_latest),
        sort_title=build_sort_title(title),
        sort_location=build_sort_location(location),
        status=JobPosting.Status.IMPORTED,
    )


def send_manual_bulk_email(
    *,
    raw_named_recipients: str = "",
    raw_recipient_emails: str = "",
    prepared_recipients: list[dict] | None = None,
    subject: str,
    body: str,
    delay_seconds: int = 15,
) -> dict:
    subject = safe_str(subject).strip()
    body = safe_str(body).strip()
    if prepared_recipients is not None:
        named_recipients = []
        unnamed_emails = []
        recipients, invalid_prepared_recipients = _normalize_prepared_recipients(prepared_recipients)
        invalid_named_recipients = invalid_prepared_recipients
        invalid_emails = []
    else:
        named_recipients, invalid_named_recipients = parse_manual_named_recipients(raw_named_recipients)
        unnamed_emails, invalid_emails = parse_manual_bulk_emails(raw_recipient_emails)
        recipients = _merge_manual_recipients(named_recipients=named_recipients, unnamed_emails=unnamed_emails)
    run_log_path = create_run_log_path("manual_bulk_email", "all")

    totals = {
        "input_valid_emails": len(recipients),
        "named_recipients": len(named_recipients),
        "unnamed_recipients": len([r for r in recipients if r.get("name") == "there"]),
        "invalid_emails": len(invalid_emails) + len(invalid_named_recipients),
        "skipped_already_sent_or_pending": 0,
        "skipped_suppressed": 0,
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "sender_auto_paused": 0,
        "stopped": 0,
        "delay_seconds": int(delay_seconds or 0),
        "run_log_path": run_log_path,
    }
    rows: list[dict] = []

    append_and_print(
        run_log_path,
        f"MANUAL_BULK_START valid={len(recipients)} invalid={totals['invalid_emails']} delay_seconds={int(delay_seconds or 0)}",
    )

    if not subject:
        raise RuntimeError("Subject is required.")
    if not body:
        raise RuntimeError("Email body is required.")
    if not recipients:
        raise RuntimeError("No valid recipient emails were provided.")

    attachments = _resume_attachments()
    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.REAL,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=int(delay_seconds or 0),
        notes="Manual bulk email send running",
    )
    job = _manual_bulk_job(subject=subject, body=body, send_run=send_run)

    stopped = False
    for recipient in recipients:
        email = safe_str(recipient.get("email")).strip().lower()
        recipient_name = safe_str(recipient.get("name")).strip() or "there"
        recipient_company = safe_str(recipient.get("company")).strip()
        recipient_website = safe_str(recipient.get("website")).strip()
        if not is_email_sending_enabled():
            totals["stopped"] = 1
            stopped = True
            _stop_run_due_to_disabled_sending(send_run=send_run, run_log_path=run_log_path)
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "stopped", "detail": "email_sending_disabled_or_paused"})
            break

        if _has_prior_real_initial_log(email, include_pending=True):
            totals["skipped_already_sent_or_pending"] += 1
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "skipped", "detail": "already_sent_or_pending_real_initial"})
            append_and_print(run_log_path, f"SKIP_EMAIL to={email} reason=already_sent_or_pending")
            continue

        if is_suppressed_email(email):
            totals["skipped_suppressed"] += 1
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "skipped", "detail": "suppressed"})
            append_and_print(run_log_path, f"SKIP_EMAIL to={email} reason=suppressed")
            continue

        try:
            sender = pick_next_sender_for_today()
        except Exception as exc:
            totals["emails_failed"] += 1
            totals["stopped"] = 1
            stopped = True
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "stopped", "detail": f"no_sender: {exc}"})
            append_exception(run_log_path, f"NO_SENDER to={email}", exc)
            send_run.status = SendRun.Status.FAILED
            send_run.finished_at = timezone.now()
            send_run.notes = f"Manual bulk email stopped: no sender available. sent={totals['emails_sent']} failed={totals['emails_failed']}"
            send_run.save(update_fields=["status", "finished_at", "notes"])
            break

        rendered_subject = _render_manual_text(
            subject,
            name=recipient_name,
            company=recipient_company,
            website=recipient_website,
            email=email,
        )
        rendered_body = _render_manual_text(
            body,
            name=recipient_name,
            company=recipient_company,
            website=recipient_website,
            email=email,
        )
        from_name = safe_str(sender.display_name) or "Gayathri Emuru"
        log_row = SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job,
            job_recruiter_target=None,
            sender_account=sender,
            to_email=email,
            subject_snapshot=rendered_subject[:500],
            body_snapshot=rendered_body,
            attachment_path=";".join(attachments)[:1000],
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.PENDING,
        )
        totals["emails_attempted"] += 1
        append_and_print(run_log_path, f"SEND_START to={email} sender={sender.email} log_id={log_row.id}")

        try:
            msg = build_mime_message(
                    from_name=from_name,
                    from_email=sender.email,
                    to_email=email,
                    subject=rendered_subject,
                    body_text=rendered_body,
                    attachment_paths=attachments,
                )
            send_via_smtp(sender=sender, message=msg, enforce_recipient_verification=True)
            log_row.status = SentEmailLog.SendStatus.SENT
            log_row.sent_at = timezone.now()
            log_row.error_message = ""
            log_row.save(update_fields=["status", "sent_at", "error_message"])
            increment_sender_usage(sender, 1)
            totals["emails_sent"] += 1
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "sent", "sender": sender.email, "log_id": log_row.id})
            append_and_print(run_log_path, f"SEND_OK to={email} sender={sender.email} log_id={log_row.id}")
        except IntegrityError as exc:
            totals["skipped_already_sent_or_pending"] += 1
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = f"IntegrityError (likely already-sent): {exc}"[:4000]
            log_row.save(update_fields=["status", "error_message"])
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "skipped", "sender": sender.email, "detail": "already_sent_integrity"})
            append_and_print(run_log_path, f"SEND_SKIP_DUP to={email} sender={sender.email} err={exc}")
        except Exception as exc:
            totals["emails_failed"] += 1
            error_text = str(exc)
            log_row.status = SentEmailLog.SendStatus.FAILED
            log_row.error_message = error_text[:4000]
            log_row.save(update_fields=["status", "error_message"])
            suppress_if_hard_bounce(email=email, error_message=error_text)
            paused = False
            if is_smtp_daily_limit_error(error_text):
                paused = pause_sender_for_daily_limit(sender, error_text)
                totals["sender_auto_paused"] += 1 if paused else 0
                append_and_print(run_log_path, f"SENDER_AUTO_PAUSE sender={sender.email} reason=daily_limit paused={paused}")
            rows.append({"email": email, "name": recipient_name, "company": recipient_company, "website": recipient_website, "status": "failed", "sender": sender.email, "detail": error_text[:1000], "sender_paused": paused})
            append_exception(run_log_path, f"SEND_FAIL to={email} sender={sender.email} log_id={log_row.id}", exc)

        if delay_seconds and not _sleep_between_sends(delay_seconds, send_run=send_run, run_log_path=run_log_path):
            totals["stopped"] = 1
            stopped = True
            break

    if not stopped and send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = f"Manual bulk email done. sent={totals['emails_sent']} failed={totals['emails_failed']} skipped={totals['skipped_already_sent_or_pending']}"
        send_run.save(update_fields=["status", "finished_at", "notes"])

    append_and_print(run_log_path, f"MANUAL_BULK_DONE totals={totals}")
    return {
        "ok": True,
        "send_run_id": send_run.id,
        "job_id": job.id,
        "totals": totals,
        "invalid_emails": invalid_emails + invalid_named_recipients,
        "rows": rows,
        "run_log_path": run_log_path,
    }
