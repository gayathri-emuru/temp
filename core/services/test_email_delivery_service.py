from __future__ import annotations

import os
import re
import time
from typing import Optional

from django.conf import settings
from django.utils import timezone

from core.models import GeneratedEmail, JobPosting, SendRun, SenderAccount, SentEmailLog, TestEmailAccount
from core.services.email_sending_control_service import (
    get_email_sending_state,
    get_resume_attachment_state,
    is_email_sending_enabled,
)
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.openai_cold_email_service import generate_cold_email_with_gpt, get_cold_email_prompt_text
from core.services.email_composition_service import build_full_email_body
from core.services.mail_delivery_service import send_via_sender_account as send_via_smtp
from core.services.smtp_send_service import build_mime_message
from core.services.normalization_service import normalize_email_address
from core.utils import safe_str


DEFAULT_DELAY_SECONDS = 3
DEFAULT_SENDER_ROLE = "Data Scientist"

DEFAULT_SIGNATURE = (
    "Best regards,\n"
    "Gayathri Emuru\n"
    "Portfolio: https://gayathri-emuru.github.io/\n"
    "LinkedIn: https://www.linkedin.com/in/gayathri-emuru-ud/"
)


def _build_company_context(job: JobPosting) -> str:
    parts = []
    if safe_str(job.title):
        parts.append(f"Role title: {safe_str(job.title)}.")
    if safe_str(job.location):
        parts.append(f"Location: {safe_str(job.location)}.")
    if safe_str(job.company):
        parts.append(f"Company: {safe_str(job.company)}.")
    if safe_str(job.salary):
        parts.append(f"Salary info: {safe_str(job.salary)}.")
    if safe_str(job.description):
        trimmed = " ".join(safe_str(job.description).split())
        parts.append(f"Job description summary source: {trimmed[:1200]}")
    return " ".join(parts).strip()


def _build_value_prop(job: JobPosting) -> str:
    title = safe_str(job.title).lower()
    description = safe_str(job.description).lower()

    if any(x in title or x in description for x in ["machine learning", "ml", "nlp", "ai", "llm", "data scientist"]):
        return (
            "My background lines up well with production ML delivery, NLP/search systems, "
            "AWS deployment, and high-scale API-backed data products."
        )
    if any(x in title or x in description for x in ["data engineer", "etl", "pipeline", "spark"]):
        return (
            "My background lines up well with scalable ETL, Python/SQL data engineering, "
            "cloud workflows, and production analytics systems."
        )
    if any(x in title or x in description for x in ["software", "developer", "backend", "api", "django"]):
        return (
            "My background lines up well with Python backend development, REST APIs, "
            "data-intensive systems, and production deployment."
        )

    return (
        "My background combines production ML systems, data engineering, cloud deployment, "
        "and high-scale API-driven product work."
    )


def _resume_text_for_prompt() -> str:
    """
    Resume text extraction for GPT prompt context.
    Hard rule: fail fast if the resume PDF cannot be read/extracted.
    """
    path = _default_resume_path()
    if not path:
        raise RuntimeError("DEFAULT_RESUME_PATH is not set. Refusing to generate email content without a resume path.")
    if not os.path.exists(path):
        raise RuntimeError(f"Resume file not found: {path}")

    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except ModuleNotFoundError as e:
            if getattr(e, "name", None) == "pypdf":
                raise RuntimeError(
                    "Missing dependency 'pypdf' required for resume PDF text extraction. "
                    "Install it with: python -m pip install pypdf"
                ) from e
            raise

        reader = PdfReader(path)
        chunks = []
        for page in reader.pages[:3]:
            text = page.extract_text() or ""
            if text:
                chunks.append(text)
        combined = "\n".join(chunks).strip()
        combined = re.sub(r"\s+", " ", combined)
        if not combined:
            raise RuntimeError("Resume PDF text extraction returned empty text.")
        return combined[:3500]
    except Exception:
        raise


def _sending_disabled_error_message() -> str:
    state = get_email_sending_state()
    return (
        "Sending is disabled. Nothing was sent. "
        f"(EMAIL_SENDING_ENABLED={'1' if state['env_enabled'] else '0'}, "
        f"EMAIL_SENDING_PAUSED={'1' if state['paused'] else '0'})"
    )


def _stop_run_due_to_disabled_sending(*, send_run: SendRun, run_log_path: str, reason: str) -> None:
    send_run.status = SendRun.Status.STOPPED
    send_run.stopped_manually = True
    send_run.finished_at = timezone.now()
    send_run.notes = safe_str(reason)[:4000]
    send_run.save(update_fields=["status", "stopped_manually", "finished_at", "notes"])
    append_and_print(run_log_path, f"STOPPED reason={reason}")


def _default_resume_path() -> str:
    return safe_str(getattr(settings, "DEFAULT_RESUME_PATH", "")).strip()


def _resume_attachments() -> list[str]:
    if not get_resume_attachment_state()["enabled"]:
        return []
    path = _default_resume_path()
    if not path:
        raise RuntimeError("DEFAULT_RESUME_PATH is not set. Refusing to send without an explicit resume path.")
    if not os.path.exists(path):
        raise RuntimeError(f"Resume file not found: {path}")
    return [path]


def parse_email_list(raw_text: str) -> list[str]:
    """
    Parses a dashboard-provided email list (comma/space/newline separated) into normalized emails.
    Returns a de-duped list preserving input order.
    """
    raw_text = safe_str(raw_text)
    if not raw_text:
        return []

    parts = re.split(r"[\s,;]+", raw_text.strip())
    emails: list[str] = []
    for p in parts:
        e = normalize_email_address(p)
        if e:
            emails.append(e)

    seen = set()
    out: list[str] = []
    for e in emails:
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


def _get_generated_email(job: JobPosting) -> Optional[GeneratedEmail]:
    try:
        g = job.generated_email
    except GeneratedEmail.DoesNotExist:
        return None
    if not safe_str(g.subject).strip() or not safe_str(g.body).strip():
        return None
    return g


def run_test_email_delivery_for_job(
    *,
    job_id: int,
    delay_seconds: Optional[int] = None,
    sender_emails: Optional[list[str]] = None,
    test_recipient_emails: Optional[list[str]] = None,
    use_openai_email: bool = True,
    regenerate_if_generated_email_exists: bool = False,
    prefix_subject_with_test_tag: bool = True,
) -> dict:
    """
    Delivery test: send emails ONLY from SenderAccount -> ONLY to TestEmailAccount.

    Defaults (backward compatible): all active senders -> all active test recipients.

    - Uses the job's GeneratedEmail subject/body (if present), otherwise a basic fallback.
    - Attaches resume (DEFAULT_RESUME_PATH) if SEND_ATTACH_RESUME is enabled.
    """
    if not is_email_sending_enabled():
        raise RuntimeError(_sending_disabled_error_message())

    if delay_seconds is None:
        delay_seconds = int(os.getenv("DELIVERY_TEST_DELAY_SECONDS", str(DEFAULT_DELAY_SECONDS)) or DEFAULT_DELAY_SECONDS)
    delay_seconds = max(0, int(delay_seconds))

    run_log_path = create_run_log_path("test_email_delivery", f"job_{job_id}")
    append_and_print(run_log_path, f"START job_id={job_id} delay_seconds={delay_seconds}")

    job = JobPosting.objects.select_related("company_ref").prefetch_related("generated_email").get(id=job_id)
    generated = _get_generated_email(job)

    sender_emails = [normalize_email_address(e) for e in (sender_emails or []) if normalize_email_address(e)]
    test_recipient_emails = [normalize_email_address(e) for e in (test_recipient_emails or []) if normalize_email_address(e)]

    created_test_recipients = []
    reactivated_test_recipients = []
    for email in test_recipient_emails:
        existing_test = TestEmailAccount.objects.filter(email__iexact=email).first()
        if existing_test:
            if not existing_test.is_active:
                existing_test.is_active = True
                existing_test.save(update_fields=["is_active"])
                reactivated_test_recipients.append(email)
            continue
        TestEmailAccount.objects.create(
            email=email,
            is_active=True,
            notes="Auto-created from Operations Dashboard delivery test recipient list.",
        )
        created_test_recipients.append(email)

    sender_qs = (
        SenderAccount.objects
        .filter(is_active=True, is_paused=False)
        .order_by("round_robin_order", "email", "id")
    )
    if sender_emails:
        sender_qs = sender_qs.filter(email__in=sender_emails)
    senders = list(sender_qs)

    tests_qs = (
        TestEmailAccount.objects
        .filter(is_active=True)
        .order_by("rotation_order", "email", "id")
    )
    if test_recipient_emails:
        tests_qs = tests_qs.filter(email__in=test_recipient_emails)
    tests = list(tests_qs)

    if not senders:
        raise RuntimeError("No active sender accounts found.")
    if not tests:
        raise RuntimeError("No active test email accounts found.")

    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.TEST,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=delay_seconds,
        notes=f"Delivery test for job_id={job_id}",
    )

    totals = {
        "send_run_id": send_run.id,
        "job_id": job_id,
        "senders": len(senders),
        "test_recipients": len(tests),
        "sender_emails": [safe_str(s.email).strip().lower() for s in senders[:200]],
        "recipient_emails": [safe_str(t.email).strip().lower() for t in tests[:500]],
        "created_test_recipients": created_test_recipients[:500],
        "reactivated_test_recipients": reactivated_test_recipients[:500],
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "run_log_path": run_log_path,
    }

    subject_base = safe_str(getattr(generated, "subject", "")).strip() if generated else ""
    body_base = safe_str(getattr(generated, "body", "")).strip() if generated else ""

    # Optional: use OpenAI to generate a job-tailored outreach email (once per run).
    # If disabled, we fall back to the stored GeneratedEmail (if present) or a basic template.
    if use_openai_email and (regenerate_if_generated_email_exists or not (subject_base and body_base)):
        try:
            try:
                prompt = get_cold_email_prompt_text()
                append_and_print(run_log_path, f"OPENAI_PROMPT_VERSION {safe_str(prompt.get('prompt_version'))}")
                append_and_print(run_log_path, "OPENAI_PROMPT_TEXT_BEGIN")
                for line in safe_str(prompt.get("prompt_text")).splitlines():
                    append_and_print(run_log_path, line)
                append_and_print(run_log_path, "OPENAI_PROMPT_TEXT_END")
            except Exception as exc:
                append_exception(run_log_path, "OPENAI_PROMPT_LOG_FAIL", exc)

            sender_name_for_prompt = safe_str(senders[0].display_name).strip() or "Gayathri Emuru"
            gpt = generate_cold_email_with_gpt(
                sender_name=sender_name_for_prompt,
                sender_role=DEFAULT_SENDER_ROLE,
                sender_signature=DEFAULT_SIGNATURE,
                company_name=safe_str(getattr(job, "company", "")),
                recipient_name="Hiring Team",
                recipient_role="Recruiter",
                job_title=safe_str(getattr(job, "title", "")),
                job_description=safe_str(getattr(job, "description", "")),
                company_context=_build_company_context(job),
                value_proposition=_build_value_prop(job),
                resume_text=_resume_text_for_prompt(),
            )
            subject_base = safe_str(gpt.get("subject")).strip() or subject_base
            body_base = safe_str(gpt.get("email")).strip() or body_base
            append_and_print(run_log_path, f"OPENAI_EMAIL_OK subject={subject_base[:120]!r}")
        except Exception as exc:
            append_exception(run_log_path, "OPENAI_EMAIL_FAIL", exc)
            raise

    if not subject_base:
        subject_base = f"Application - {safe_str(getattr(job, 'title', '')).strip() or f'Job {job_id}'}"
    if not body_base and False:
        job_url = safe_str(getattr(job, "linkedin_url", "")).strip()
        body_base = (
            "I’m reaching out to express my interest in this role.\n\n"
            f"For your reference, here is the job posting: {job_url or '[missing url]'}\n\n"
            "Thank you for your time,\n"
            "Gayathri Emuru"
        )

    if not body_base:
        job_url = safe_str(getattr(job, "linkedin_url", "")).strip()
        company_name = safe_str(getattr(job, "company", "")).strip()
        title_name = safe_str(getattr(job, "title", "")).strip()
        body_base = (
            f"I'm reaching out to express my interest in the {title_name or 'role'}"
            + (f" at {company_name}" if company_name else "")
            + ". My resume is attached."
        )

    subject = subject_base.strip()
    if prefix_subject_with_test_tag:
        subject = f"[DELIVERY TEST] {subject}".strip()
    attachments = _resume_attachments()

    job_linkedin_url = safe_str(getattr(job, "normalized_linkedin_url", "")).strip() or safe_str(getattr(job, "linkedin_url", "")).strip()
    final_body = build_full_email_body(
        recipient_name="Hiring Team",
        base_body=body_base,
        job_linkedin_url=job_linkedin_url,
        manual_job_reference_id=safe_str(getattr(job, "manual_job_reference_id", "")).strip(),
    )

    stopped = False
    for sender in senders:
        from_name = safe_str(sender.display_name).strip() or "Gayathri Emuru"

        for rec in tests:
            if not is_email_sending_enabled():
                _stop_run_due_to_disabled_sending(
                    send_run=send_run,
                    run_log_path=run_log_path,
                    reason=_sending_disabled_error_message(),
                )
                stopped = True
                break

            to_email = safe_str(rec.email).strip().lower()
            totals["emails_attempted"] += 1

            log_row = SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                job_recruiter_target=None,
                sender_account=sender,
                to_email=to_email,
                subject_snapshot=subject[:500],
                body_snapshot=final_body,
                attachment_path=";".join(attachments)[:1000],
                send_type=SentEmailLog.SendType.TEST,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.PENDING,
            )

            append_and_print(run_log_path, f"SEND_START sender={sender.email} to={to_email} log_id={log_row.id}")

            try:
                msg = build_mime_message(
                    from_name=from_name,
                    from_email=sender.email,
                    to_email=to_email,
                    subject=subject,
                    body_text=final_body,
                    attachment_paths=attachments,
                )
                send_via_smtp(sender=sender, message=msg)

                log_row.status = SentEmailLog.SendStatus.SENT
                log_row.sent_at = timezone.now()
                log_row.error_message = ""
                log_row.save(update_fields=["status", "sent_at", "error_message"])

                totals["emails_sent"] += 1
                append_and_print(run_log_path, f"SEND_OK sender={sender.email} to={to_email} log_id={log_row.id}")
            except Exception as exc:
                totals["emails_failed"] += 1
                log_row.status = SentEmailLog.SendStatus.FAILED
                log_row.error_message = safe_str(str(exc))[:4000]
                log_row.save(update_fields=["status", "error_message"])
                append_exception(run_log_path, f"SEND_FAIL sender={sender.email} to={to_email} log_id={log_row.id}", exc)

            if delay_seconds:
                time.sleep(delay_seconds)

        if stopped:
            break

    if send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = f"Delivery test complete. sent={totals['emails_sent']} failed={totals['emails_failed']}"
        send_run.save(update_fields=["status", "finished_at", "notes"])

    append_and_print(run_log_path, f"END totals={totals}")
    return {"totals": totals, "send_run_id": send_run.id, "run_log_path": run_log_path}
