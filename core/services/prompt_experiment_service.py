from __future__ import annotations

from django.utils import timezone

from core.models import JobPosting
from core.services.file_run_logger import append_and_print, append_exception
from core.services.email_ai_settings_service import default_anthropic_email_model
from core.services.openai_cold_email_service import generate_cold_email_with_anthropic_custom_prompt
from core.services.test_email_delivery_service import _build_company_context, _build_value_prop, _resume_text_for_prompt
from core.utils import safe_str


def generate_email_for_job_with_custom_prompt(*, job_id: int, prompt_text: str, run_log_path: str) -> dict:
    prompt_text = safe_str(prompt_text).strip()
    if not prompt_text:
        raise RuntimeError("Prompt text is empty.")

    job = JobPosting.objects.get(id=job_id)

    append_and_print(run_log_path, f"START job_id={job_id} at={timezone.now().isoformat()}")
    append_and_print(run_log_path, f"JOB company={safe_str(getattr(job, 'company', ''))} title={safe_str(getattr(job, 'title', ''))}")

    append_and_print(run_log_path, "CUSTOM_PROMPT_TEXT_BEGIN")
    for line in prompt_text.splitlines():
        append_and_print(run_log_path, line)
    append_and_print(run_log_path, "CUSTOM_PROMPT_TEXT_END")

    company_context = _build_company_context(job)
    value_proposition = _build_value_prop(job)
    resume_text = _resume_text_for_prompt()

    try:
        result = generate_cold_email_with_anthropic_custom_prompt(
            prompt_text=prompt_text,
            model=default_anthropic_email_model(),
            sender_name="Gayathri Emuru",
            sender_role="Data Scientist",
            sender_signature="",
            company_name=safe_str(getattr(job, "company", "")),
            recipient_name="Hiring Team",
            recipient_role="Recruiter",
            job_title=safe_str(getattr(job, "title", "")),
            job_description=safe_str(getattr(job, "description", "")),
            company_context=company_context,
            value_proposition=value_proposition,
            resume_text=resume_text,
        )
    except Exception as exc:
        append_exception(run_log_path, "ANTHROPIC_CUSTOM_PROMPT_FAIL", exc)
        raise

    subject = safe_str(result.get("subject")).strip()
    body = safe_str(result.get("email")).strip()
    prompt_version = safe_str(result.get("prompt_version")).strip()

    append_and_print(run_log_path, f"ANTHROPIC_CUSTOM_PROMPT_OK subject={subject[:200]!r}")

    return {
        "ok": True,
        "job_id": job_id,
        "company": safe_str(getattr(job, "company", "")),
        "title": safe_str(getattr(job, "title", "")),
        "prompt_version": prompt_version,
        "subject": subject,
        "body": body,
        "run_log_path": run_log_path,
    }
