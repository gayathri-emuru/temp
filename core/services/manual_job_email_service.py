from __future__ import annotations

import time
import uuid
import re
import hashlib
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.utils import timezone

from core.models import (
    ApprovalRecord,
    Company,
    CompanyRecruiter,
    DailyBatch,
    GeneratedEmail,
    JobPosting,
    JobRecruiterTarget,
    SendRun,
    SentEmailLog,
)
from core.services.email_composition_service import build_full_email_body
from core.services.email_sending_control_service import get_email_sending_state, is_email_sending_enabled
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.cold_email_generation_service import RESUME_TEXT
from core.services.email_ai_settings_service import get_email_ai_generation_settings
from core.services.email_generation_throttle_service import (
    email_generation_batch_delay_seconds,
    email_generation_rate_limit_backoff_seconds,
    is_email_generation_rate_limit_error,
    sleep_between_email_generation_requests,
    sleep_for_email_generation_rate_limit,
)
from core.services.manual_bulk_email_service import _has_prior_real_initial_log
from core.services.openai_cold_email_service import (
    build_compact_cold_email_subject,
    generate_cold_email_with_provider_custom_prompt,
)
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
    normalize_email_address,
    normalize_location,
    normalize_person_name,
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


TOKEN_PREFIX = "manual-job-email"
MANUAL_REVIEW_PROMPT_FILENAME = "manual_job_review_prompt.txt"

TITLE_SKIP_LINES = {
    "-",
    "back to search results",
    "back to jobs",
    "careers",
    "careers at apple",
    "company logo",
    "description",
    "job description",
    "jd banner",
    "life at apple",
    "profile",
    "search",
    "sign in",
    "skip to content",
    "skip to main content",
    "work at apple",
}
ROLE_TITLE_KEYWORDS = (
    "analyst",
    "analytics",
    "artificial intelligence",
    "associate",
    "backend",
    "business development",
    "business intelligence",
    "data",
    "developer",
    "engineer",
    "machine learning",
    "ml",
    "research scientist",
    "scientist",
    "software",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manual_review_prompt_path() -> Path:
    return _repo_root() / "prompts" / MANUAL_REVIEW_PROMPT_FILENAME


def _load_manual_review_prompt_text() -> str:
    path = _manual_review_prompt_path()
    if path.exists() and path.is_file():
        prompt_text = safe_str(path.read_text(encoding="utf-8")).strip()
        if prompt_text:
            return prompt_text
    raise RuntimeError(
        "Manual job review prompt is missing. "
        f"Expected a non-empty file at {path}."
    )


def _clean_email(value: str) -> tuple[str, str]:
    email = normalize_email_address(safe_str(value).strip().strip("<>()[]{}\"'"))
    if not email:
        return "", "missing_email"
    try:
        validate_email(email)
    except ValidationError:
        return "", email
    return email, ""


def parse_manual_job_rows(*, names: list[str], emails: list[str], job_texts: list[str]) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()
    count = max(len(names), len(emails), len(job_texts))

    for index in range(count):
        name = safe_str(names[index] if index < len(names) else "").strip()
        raw_email = safe_str(emails[index] if index < len(emails) else "").strip()
        job_text = safe_str(job_texts[index] if index < len(job_texts) else "").strip()

        if not name and not raw_email and not job_text:
            continue

        email, bad_email = _clean_email(raw_email)
        if not name or not email or not job_text:
            invalid.append(
                {
                    "row_number": index + 1,
                    "name": name,
                    "email": raw_email,
                    "reason": "missing_name_email_or_job_text" if not bad_email else f"invalid_email: {bad_email}",
                }
            )
            continue

        if email in seen:
            invalid.append({"row_number": index + 1, "name": name, "email": email, "reason": "duplicate_in_input"})
            continue

        seen.add(email)
        rows.append({"row_number": index + 1, "name": name, "email": email, "job_text": job_text})

    return rows, invalid


def _clean_possible_title_line(line: str) -> str:
    line = " ".join(safe_str(line).split()).strip(" -|\t")
    if not line:
        return ""

    line = re.sub(r"^(?:job\s+title|title|role)\s*[:\-]\s*", "", line, flags=re.I).strip()
    line = re.split(r"\s+Location\b", line, maxsplit=1, flags=re.I)[0].strip(" -,.")
    line = re.split(r"\s+This job\b", line, maxsplit=1, flags=re.I)[0].strip(" -,.")
    lowered = line.lower().strip()
    if lowered in TITLE_SKIP_LINES:
        return ""
    if lowered.startswith(("http://", "https://", "www.")):
        return ""
    return line[:180]


def _looks_like_role_title(line: str) -> bool:
    lowered = safe_str(line).lower()
    if not lowered or lowered in TITLE_SKIP_LINES:
        return False
    return any(keyword in lowered for keyword in ROLE_TITLE_KEYWORDS)


def _title_from_job_text(job_text: str) -> str:
    fallback = ""
    for line in safe_str(job_text).splitlines():
        line = _clean_possible_title_line(line)
        if not line:
            continue
        if _looks_like_role_title(line):
            return line[:180]
        if not fallback:
            fallback = line[:180]
    return fallback or "Pasted job or recruiter post"


def _manual_job_title_is_obvious_page_chrome(title: str) -> bool:
    title = _clean_possible_title_line(title)
    if not title:
        return True
    return title.lower() in TITLE_SKIP_LINES


def _best_manual_job_title(job: JobPosting) -> str:
    from_description = _title_from_job_text(safe_str(getattr(job, "description", "")))
    current = safe_str(getattr(job, "title", "")).strip()
    if from_description and from_description != "Pasted job or recruiter post":
        if _manual_job_title_is_obvious_page_chrome(current) or _looks_like_role_title(from_description):
            return from_description
    return current or from_description


def _repair_manual_job_title_if_needed(job: JobPosting) -> str:
    current = safe_str(getattr(job, "title", "")).strip()
    best = _best_manual_job_title(job)
    if not best or best == current or not _manual_job_title_is_obvious_page_chrome(current):
        return best or current

    job.title = best[:500]
    job.normalized_title = normalize_title(best)
    job.canonical_title = canonical_title(best)
    job.sort_title = build_sort_title(best)
    job.dedupe_key = build_dedupe_key(job.normalized_company, best, job.location)
    job.save(update_fields=["title", "normalized_title", "canonical_title", "sort_title", "dedupe_key", "updated_at"])
    return best


def _manual_job_subject(job: JobPosting, generated_subject: str = "") -> str:
    job_title = _best_manual_job_title(job)
    subject = build_compact_cold_email_subject(
        company_name="",
        job_title=job_title,
        fallback_subject=generated_subject,
    )
    return subject or safe_str(generated_subject).strip()


def _repair_manual_job_subject_if_needed(job: JobPosting, generated: GeneratedEmail | None) -> str:
    if not generated:
        return ""

    current = safe_str(generated.subject).strip()
    if generated.edited_manually and current:
        return current

    _repair_manual_job_title_if_needed(job)
    repaired = _manual_job_subject(job, current)
    if repaired and repaired != current:
        generated.subject = repaired[:500]
        generated.save(update_fields=["subject", "updated_at"])
    return repaired or current


def _manual_review_company_name(job: JobPosting) -> str:
    company_name = safe_str(getattr(job, "company", "")).strip()
    if not company_name:
        return ""

    if getattr(job, "is_manual_email_job", False) and company_name.lower().startswith("manual job email "):
        return ""

    return company_name


def _manual_review_company_context(job: JobPosting) -> str:
    parts = []
    title = safe_str(getattr(job, "title", "")).strip()
    location = safe_str(getattr(job, "location", "")).strip()
    company_name = _manual_review_company_name(job)
    salary = safe_str(getattr(job, "salary", "")).strip()
    description = safe_str(getattr(job, "description", "")).strip()

    if title:
        parts.append(f"Role title: {title}.")
    if location:
        parts.append(f"Location: {location}.")
    if company_name:
        parts.append(f"Company: {company_name}.")
    if salary:
        parts.append(f"Salary info: {salary}.")
    if description:
        trimmed = " ".join(description.split())
        parts.append(f"Job description summary source: {trimmed[:1200]}")

    return " ".join(parts).strip()


def _manual_review_value_prop(job: JobPosting) -> str:
    title = safe_str(getattr(job, "title", "")).lower()
    description = safe_str(getattr(job, "description", "")).lower()

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


def _manual_job_description_field(job: JobPosting, field_label: str) -> str:
    label = safe_str(field_label).strip()
    if not label:
        return ""
    pattern = rf"(?im)^\s*{re.escape(label)}\s*:\s*(\S[^\r\n]*)\s*$"
    match = re.search(pattern, safe_str(getattr(job, "description", "")))
    return safe_str(match.group(1)).strip() if match else ""


def _manual_job_description_block(job: JobPosting, block_label: str) -> str:
    label = safe_str(block_label).strip()
    if not label:
        return ""
    text = safe_str(getattr(job, "description", ""))
    pattern = rf"(?ims)^\s*{re.escape(label)}\s*:\s*\n?(.*?)(?=^\s*[A-Z][A-Za-z /-]{{1,60}}\s*:\s*$|^\s*[A-Z][A-Za-z /-]{{1,60}}\s*:\s*\S|\Z)"
    match = re.search(pattern, text)
    return safe_str(match.group(1)).strip() if match else ""


def _clean_manual_review_body_text(body: str, job: JobPosting) -> str:
    body = safe_str(body)
    if not body:
        return ""

    placeholder = safe_str(getattr(job, "company", "")).strip()
    replacements = [
        r"\s+at\s+Manual Job Email\s+[^\.\n!?]+",
        r"\s+for\s+Manual Job Email\s+[^\.\n!?]+",
        r"\s+with\s+Manual Job Email\s+[^\.\n!?]+",
    ]
    if placeholder:
        escaped = re.escape(placeholder)
        replacements.extend(
            [
                rf"\s+at\s+{escaped}",
                rf"\s+for\s+{escaped}",
                rf"\s+with\s+{escaped}",
                rf"\b{escaped}\b",
            ]
        )

    for pattern in replacements:
        body = re.sub(pattern, "", body, flags=re.I)

    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(r"\s+\.", ".", body)
    body = re.sub(r"\s+,", ",", body)
    body = re.sub(r"\.\s+\.", ".", body)
    return body.strip()


def _get_best_recipient(job: JobPosting):
    target = (
        job.targets
        .filter(
            recipient_email_snapshot__isnull=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .order_by("selection_order", "id")
        .first()
    )

    if target and _target_allows_real_send(target):
        return {
            "recipient_name": safe_str(target.recipient_name_snapshot) or "there",
            "recipient_role": "Recruiter",
            "recipient_email": safe_str(target.recipient_email_snapshot).lower(),
        }

    return None


def _target_allows_real_send(target: JobRecruiterTarget) -> bool:
    recruiter = getattr(target, "company_recruiter", None)
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    apollo_id = safe_str(getattr(recruiter, "apollo_person_id", "")).strip()
    if source != "apollo" and not apollo_id:
        return True
    return safe_str(getattr(recruiter, "email_status", "")).strip().lower() == "verified"


def run_cold_email_generation_for_job(job: JobPosting, run_log_path: str = ""):
    if not run_log_path:
        run_log_path = create_run_log_path("manual_job_email_job", f"{job.company}_{job.id}")

    stats = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "generated": 0,
        "skipped_no_recipient": 0,
        "skipped_no_description": 0,
        "error": "",
        "provider": "",
        "model": "",
        "run_log_path": run_log_path,
    }

    append_and_print(
        run_log_path,
        f"START job_id={job.id} company={job.company} title={job.title}",
    )

    recipient = _get_best_recipient(job)
    if not recipient:
        stats["skipped_no_recipient"] = 1
        append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_recipient_email")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    if not safe_str(job.description).strip():
        stats["skipped_no_description"] = 1
        append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_job_description")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    prompt_text = _load_manual_review_prompt_text()
    company_name = _manual_review_company_name(job)
    company_context = _manual_review_company_context(job)
    value_prop = _manual_review_value_prop(job)
    ai_settings = get_email_ai_generation_settings()
    stats["provider"] = ai_settings["provider"]
    stats["model"] = ai_settings["model"]

    append_and_print(
        run_log_path,
        f"PROMPT_INPUT job_id={job.id} recipient_name={recipient['recipient_name']} "
        f"recipient_role={recipient['recipient_role']} recipient_email={recipient['recipient_email']}",
    )
    append_and_print(
        run_log_path,
        f"PROMPT_CONTEXT job_id={job.id} company_context={company_context[:1500]}",
    )
    append_and_print(
        run_log_path,
        f"PROMPT_VALUE_PROP job_id={job.id} value_prop={value_prop}",
    )
    append_and_print(run_log_path, f"PROMPT_VERSION manual_review:{hashlib.sha256(prompt_text.encode('utf-8')).hexdigest()[:10]}")
    append_and_print(
        run_log_path,
        f"EMAIL_AI_SELECTION job_id={job.id} provider={ai_settings['provider']} model={ai_settings['model']}",
    )
    append_and_print(run_log_path, "MANUAL_REVIEW_PROMPT_TEXT_BEGIN")
    for line in prompt_text.splitlines():
        append_and_print(run_log_path, line)
    append_and_print(run_log_path, "MANUAL_REVIEW_PROMPT_TEXT_END")

    try:
        result = generate_cold_email_with_provider_custom_prompt(
            provider=ai_settings["provider"],
            prompt_text=prompt_text,
            model=ai_settings["model"],
            sender_name="Gayathri Emuru",
            sender_role="Data Scientist",
            sender_signature="",
            company_name=company_name,
            recipient_name=recipient["recipient_name"],
            recipient_role=recipient["recipient_role"],
            job_title=safe_str(job.title),
            job_description=safe_str(job.description),
            company_context=company_context,
            value_proposition=value_prop,
            resume_text=RESUME_TEXT,
        )
    except Exception as exc:
        stats["error"] = str(exc)
        append_exception(run_log_path, f"AI_COLD_EMAIL_ERROR job_id={job.id}", exc)
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    subject = _manual_job_subject(job, safe_str(result.get("subject")).strip())
    email_body = _clean_manual_review_body_text(result.get("email", ""), job)
    prompt_version = safe_str(result.get("prompt_version")) or "manual_review_prompt_unknown"

    append_and_print(
        run_log_path,
        f"AI_RESULT job_id={job.id} provider={stats['provider']} model={stats['model']} subject={subject}",
    )
    append_and_print(
        run_log_path,
        f"EMAIL_BODY job_id={job.id} body={email_body}",
    )

    generated, _ = GeneratedEmail.objects.update_or_create(
        job_posting=job,
        defaults={
            "subject": subject,
            "body": email_body,
            "generation_status": GeneratedEmail.GenerationStatus.GENERATED,
            "prompt_version": prompt_version,
            "edited_manually": False,
        },
    )

    stats["generated"] = 1
    append_and_print(run_log_path, f"SAVED job_id={job.id} generated_email_id={generated.id}")
    append_and_print(run_log_path, f"END stats={stats}")
    return stats


def _create_manual_job(*, batch: DailyBatch, token: str, row: dict, position: int) -> JobPosting:
    company_name = f"Manual Job Email {token} #{position}"
    normalized_company = normalize_company_name(company_name)
    company, _ = Company.objects.get_or_create(
        normalized_name=normalized_company,
        defaults={
            "raw_name_latest": company_name,
            "daily_send_limit": 1,
            "rolling_7d_send_limit": 1,
        },
    )

    title = _title_from_job_text(row["job_text"])
    location = "United States"
    job = JobPosting.objects.create(
        daily_batch=batch,
        is_manual_import=True,
        is_manual_email_job=True,
        company_ref=company,
        external_job_id=f"{TOKEN_PREFIX}-{token}-{position}",
        linkedin_url=f"https://manual.local/job-email/{token}/{position}/",
        apply_url="",
        normalized_linkedin_url=f"https://manual.local/job-email/{token}/{position}/",
        normalized_apply_url="",
        title=title,
        company=company.raw_name_latest,
        location=location,
        salary="",
        description=row["job_text"],
        description_fingerprint=build_description_fingerprint(row["job_text"]),
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
        status=JobPosting.Status.EMAIL_GENERATED,
    )

    recruiter = CompanyRecruiter.objects.create(
        company=company,
        person_name=row["name"],
        normalized_person_name=normalize_person_name(row["name"]),
        source=CompanyRecruiter.Source.LEGACY,
        email_status="manual_verified",
        title_match=True,
        location_match=True,
        email=row["email"],
        is_active=True,
    )
    JobRecruiterTarget.objects.create(
        job_posting=job,
        company_recruiter=recruiter,
        recipient_email_snapshot=row["email"],
        recipient_name_snapshot=row["name"],
        selection_order=1,
        is_selected_for_job=True,
        is_verified_for_job=True,
    )
    ApprovalRecord.objects.update_or_create(job_posting=job, defaults={"is_approved": True, "approved_at": timezone.now()})
    return job


def create_manual_job_email_batch(
    *,
    names: list[str],
    emails: list[str],
    job_texts: list[str],
    generate_immediately: bool = True,
) -> dict:
    token = uuid.uuid4().hex[:12]
    run_log_path = create_run_log_path("manual_job_email_generate", token)
    rows, invalid = parse_manual_job_rows(names=names, emails=emails, job_texts=job_texts)
    batch, _ = DailyBatch.objects.get_or_create(
        batch_date=timezone.localdate(),
        defaults={
            "lookback_hours": 24,
            "max_jobs_requested": 0,
            "apify_run_status": DailyBatch.RunStatus.SUCCESS,
            "notes": "Manual job-tailored email batches",
        },
    )

    totals = {
        "input_rows": len(rows) + len(invalid),
        "valid_unique_rows": len(rows),
        "invalid_rows": len(invalid),
        "skipped_already_sent_or_pending": 0,
        "created_jobs": 0,
        "generated": 0,
        "generation_errors": 0,
        "generate_immediately": bool(generate_immediately),
    }
    result_rows: list[dict] = []
    ai_settings = get_email_ai_generation_settings()
    totals["provider"] = ai_settings["provider"]
    totals["model"] = ai_settings["model"]
    append_and_print(
        run_log_path,
        f"MANUAL_JOB_EMAIL_GENERATE_START token={token} valid={len(rows)} invalid={len(invalid)} "
        f"generate_immediately={1 if generate_immediately else 0}",
    )
    append_and_print(
        run_log_path,
        "BATCH_THROTTLE "
        f"provider={ai_settings['provider']} "
        f"per_request_delay={email_generation_batch_delay_seconds(ai_settings['provider'])}s "
        f"rate_limit_backoff={email_generation_rate_limit_backoff_seconds(ai_settings['provider'])}s",
    )

    for index, row in enumerate(rows):
        position = index + 1
        email = row["email"]
        job = _create_manual_job(batch=batch, token=token, row=row, position=position)
        totals["created_jobs"] += 1
        append_and_print(run_log_path, f"JOB_CREATED row={row['row_number']} job_id={job.id} to={email}")

        if not generate_immediately:
            result_rows.append({**row, "status": "draft_not_generated", "detail": "open review page and generate drafts", "job_id": job.id})
            continue

        stats = run_cold_email_generation_for_job(
            job=job,
            run_log_path=create_run_log_path("manual_job_email_job", f"{token}_{job.id}"),
        )
        if stats.get("generated"):
            totals["generated"] += 1
            status = "generated"
            detail = ""
        else:
            totals["generation_errors"] += 1
            status = "generation_failed"
            detail = safe_str(stats.get("error") or "generation did not produce an email")[:1000]
        result_rows.append({**row, "status": status, "detail": detail, "job_id": job.id})
        provider = safe_str(stats.get("provider")) or ai_settings["provider"]
        is_last = index >= len(rows) - 1
        if stats.get("error") and is_email_generation_rate_limit_error(stats.get("error")) and not is_last:
            sleep_for_email_generation_rate_limit(provider, run_log_path=run_log_path, log_func=append_and_print)
        else:
            sleep_between_email_generation_requests(
                provider,
                is_last=is_last,
                run_log_path=run_log_path,
                log_func=append_and_print,
            )

    append_and_print(run_log_path, f"MANUAL_JOB_EMAIL_GENERATE_DONE token={token} totals={totals}")
    return {
        "ok": True,
        "token": token,
        "batch_date": batch.batch_date.isoformat(),
        "review_url": f"/manual-bulk-email/job-review/{token}/",
        "send_control_url": f"/send-control/?batch_date={batch.batch_date.isoformat()}",
        "read_only_url": f"/review-readonly/?batch_date={batch.batch_date.isoformat()}",
        "totals": totals,
        "rows": result_rows,
        "invalid_rows": invalid,
        "run_log_path": run_log_path,
    }


def _jobs_for_token(token: str):
    return (
        JobPosting.objects.filter(external_job_id__startswith=f"{TOKEN_PREFIX}-{token}-")
        .select_related("company_ref", "generated_email")
        .prefetch_related("targets", "targets__company_recruiter")
        .order_by("id")
    )


def _job_has_generated_draft(job: JobPosting) -> bool:
    try:
        generated = job.generated_email
    except GeneratedEmail.DoesNotExist:
        return False
    return bool(safe_str(generated.subject).strip() and safe_str(generated.body).strip())


def run_manual_job_email_generation_for_token(*, token: str, skip_existing: bool = True) -> dict:
    token = safe_str(token).strip()
    if not token:
        raise RuntimeError("Manual job email review token is required.")

    run_log_path = create_run_log_path("manual_job_email_bulk_generate", token)
    jobs = list(_jobs_for_token(token))
    if not jobs:
        raise RuntimeError(f"No manual job email jobs found for token={token}.")

    ai_settings = get_email_ai_generation_settings()
    totals = {
        "jobs_found": len(jobs),
        "jobs_seen": 0,
        "generated": 0,
        "generation_errors": 0,
        "skipped_existing": 0,
        "skip_existing": bool(skip_existing),
        "provider": ai_settings["provider"],
        "model": ai_settings["model"],
        "master_log_path": run_log_path,
    }
    rows: list[dict] = []
    jobs_to_generate: list[JobPosting] = []

    append_and_print(
        run_log_path,
        f"MANUAL_JOB_EMAIL_BULK_GENERATE_START token={token} jobs_found={len(jobs)} skip_existing={skip_existing}",
    )
    append_and_print(
        run_log_path,
        "BATCH_THROTTLE "
        f"provider={ai_settings['provider']} "
        f"per_request_delay={email_generation_batch_delay_seconds(ai_settings['provider'])}s "
        f"rate_limit_backoff={email_generation_rate_limit_backoff_seconds(ai_settings['provider'])}s",
    )

    for job in jobs:
        if skip_existing and _job_has_generated_draft(job):
            totals["skipped_existing"] += 1
            rows.append({"job_id": job.id, "status": "skipped_existing", "detail": "generated draft already exists"})
            append_and_print(run_log_path, f"SKIP_EXISTING job_id={job.id}")
            continue
        jobs_to_generate.append(job)

    for index, job in enumerate(jobs_to_generate):
        job_log_path = create_run_log_path("manual_job_email_job", f"{token}_{job.id}")
        totals["jobs_seen"] += 1
        append_and_print(run_log_path, f"JOB_START job_id={job.id} title={job.title} job_log={job_log_path}")
        stats = run_cold_email_generation_for_job(job=job, run_log_path=job_log_path)

        if stats.get("generated"):
            totals["generated"] += 1
            rows.append({"job_id": job.id, "status": "generated", "detail": "", "stats": stats})
            append_and_print(run_log_path, f"JOB_DONE job_id={job.id} generated=1 job_log={job_log_path}")
        else:
            totals["generation_errors"] += 1
            detail = safe_str(stats.get("error") or "generation did not produce an email")[:1000]
            rows.append({"job_id": job.id, "status": "generation_failed", "detail": detail, "stats": stats})
            append_and_print(run_log_path, f"JOB_ERROR job_id={job.id} detail={detail} job_log={job_log_path}")

        provider = safe_str(stats.get("provider")) or ai_settings["provider"]
        is_last = index >= len(jobs_to_generate) - 1
        if stats.get("error") and is_email_generation_rate_limit_error(stats.get("error")) and not is_last:
            sleep_for_email_generation_rate_limit(provider, run_log_path=run_log_path, log_func=append_and_print)
            continue
        sleep_between_email_generation_requests(
            provider,
            is_last=is_last,
            run_log_path=run_log_path,
            log_func=append_and_print,
        )

    append_and_print(run_log_path, f"MANUAL_JOB_EMAIL_BULK_GENERATE_DONE token={token} totals={totals}")
    return {"ok": True, "token": token, "totals": totals, "rows": rows, "run_log_path": run_log_path}


def build_manual_job_email_review_context(*, token: str) -> dict:
    jobs = list(_jobs_for_token(token))
    rows = []
    for job in jobs:
        target = job.targets.filter(is_selected_for_job=True).select_related("company_recruiter").order_by("id").first()
        if target and not _target_allows_real_send(target):
            target = None
        try:
            generated = job.generated_email
        except GeneratedEmail.DoesNotExist:
            generated = None
        subject = _repair_manual_job_subject_if_needed(job, generated)
        email = safe_str(getattr(target, "recipient_email_snapshot", "")).strip().lower()
        name = safe_str(getattr(target, "recipient_name_snapshot", "")).strip() or "there"
        final_body = ""
        if generated:
            final_body = build_full_email_body(
                recipient_name=name,
                base_body=_clean_manual_review_body_text(safe_str(generated.body), job),
                job_linkedin_url=safe_str(job.normalized_linkedin_url) or safe_str(job.linkedin_url),
                include_job_reference=False,
            )
        already_sent_or_pending = _has_prior_real_initial_log(email, include_pending=True)
        source_linkedin_post_url = _manual_job_description_field(job, "LinkedIn post URL")
        source_job_url = _manual_job_description_field(job, "Job URL")
        poster_linkedin_url = _manual_job_description_field(job, "Poster LinkedIn")
        source_post_text = _manual_job_description_block(job, "LinkedIn post/context")
        manual_notes = _manual_job_description_block(job, "Manual notes")
        if not source_post_text:
            source_post_text = safe_str(getattr(job, "description", "")).strip()
        rows.append(
            {
                "job": job,
                "target": target,
                "generated": generated,
                "name": name,
                "email": email,
                "already_real_sent_or_pending": already_sent_or_pending,
                "subject": subject,
                "final_body": final_body,
                "source_linkedin_post_url": source_linkedin_post_url,
                "source_job_url": source_job_url,
                "poster_linkedin_url": poster_linkedin_url,
                "source_post_text": source_post_text,
                "manual_notes": manual_notes,
                "ready": bool(generated and subject and safe_str(generated.body).strip() and target and not already_sent_or_pending),
            }
        )
    return {"token": token, "rows": rows, "totals": {"jobs": len(rows), "ready": sum(1 for row in rows if row["ready"])}}


def update_manual_job_email_recipient(*, token: str, job_id: int, name: str, email: str) -> dict:
    token = safe_str(token).strip()
    name = safe_str(name).strip()
    raw_email = safe_str(email).strip().lower()
    if not token:
        raise RuntimeError("Manual review token is required.")
    if not name:
        raise RuntimeError("Recipient name is required.")
    email, bad_email = _clean_email(raw_email)
    if not email:
        raise RuntimeError(f"Recipient email is invalid: {bad_email or raw_email}")

    job = _jobs_for_token(token).filter(id=int(job_id)).first()
    if not job:
        raise RuntimeError(f"Job {job_id} was not found for this review token.")
    target = job.targets.filter(is_selected_for_job=True).select_related("company_recruiter").order_by("id").first()
    if not target:
        raise RuntimeError(f"Job {job_id} does not have a selected target.")

    recruiter = target.company_recruiter
    recruiter.person_name = name[:255]
    recruiter.email = email
    recruiter.email_status = "manual_verified"
    recruiter.is_active = True
    recruiter.save(update_fields=["person_name", "email", "email_status", "is_active", "updated_at"])

    target.recipient_name_snapshot = name[:255]
    target.recipient_email_snapshot = email
    target.save(update_fields=["recipient_name_snapshot", "recipient_email_snapshot", "updated_at"])

    return {
        "ok": True,
        "token": token,
        "job_id": job.id,
        "name": name,
        "email": email,
        "already_real_sent_or_pending": _has_prior_real_initial_log(email, include_pending=True),
    }


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


def send_manual_job_email_batch(*, token: str, job_ids: list[int], delay_seconds: int = 15) -> dict:
    selected_ids = {int(x) for x in job_ids if str(x).strip().isdigit()}
    run_log_path = create_run_log_path("manual_job_email_send", token)
    jobs = [job for job in _jobs_for_token(token) if job.id in selected_ids]
    if not jobs:
        raise RuntimeError("No selected manual generated emails to send.")
    if not is_email_sending_enabled():
        raise RuntimeError("Email sending is disabled or paused. Nothing was sent.")

    attachments = _resume_attachments()
    send_run = SendRun.objects.create(
        run_type=SendRun.RunType.REAL,
        status=SendRun.Status.RUNNING,
        started_at=timezone.now(),
        delay_seconds=int(delay_seconds or 0),
        notes=f"Manual job-tailored email send token={token}",
    )
    totals = {
        "selected_jobs": len(jobs),
        "emails_attempted": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "skipped_already_sent_or_pending": 0,
        "skipped_suppressed": 0,
        "skipped_unverified_apollo": 0,
        "sender_auto_paused": 0,
        "stopped": 0,
        "delay_seconds": int(delay_seconds or 0),
    }
    rows: list[dict] = []
    seen_this_run: set[str] = set()
    stopped = False

    for job_index, job in enumerate(jobs):
        target = job.targets.filter(is_selected_for_job=True).select_related("company_recruiter").order_by("id").first()
        if not target:
            rows.append({"job_id": job.id, "status": "skipped", "detail": "missing_selected_target"})
            continue
        if not _target_allows_real_send(target):
            totals["skipped_unverified_apollo"] += 1
            rows.append({"job_id": job.id, "status": "skipped", "detail": "apollo_email_not_verified"})
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} reason=apollo_email_not_verified")
            continue
        try:
            generated = job.generated_email
        except GeneratedEmail.DoesNotExist:
            rows.append({"job_id": job.id, "status": "skipped", "detail": "missing_generated_email"})
            continue

        email = safe_str(target.recipient_email_snapshot).strip().lower()
        name = safe_str(target.recipient_name_snapshot).strip() or "there"
        if email in seen_this_run:
            totals["skipped_already_sent_or_pending"] += 1
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "skipped", "detail": "already_sent_or_pending_real_initial"})
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={email} reason=duplicate_in_manual_send")
            continue
        seen_this_run.add(email)

        if _has_prior_real_initial_log(email, include_pending=True):
            totals["skipped_already_sent_or_pending"] += 1
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "skipped", "detail": "already_sent_or_pending_real_initial"})
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={email} reason=already_sent_or_pending_real_initial")
            continue

        if is_suppressed_email(email):
            totals["skipped_suppressed"] += 1
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "skipped", "detail": "suppressed"})
            append_and_print(run_log_path, f"SKIP_EMAIL job_id={job.id} to={email} reason=suppressed")
            continue

        if not is_email_sending_enabled():
            totals["stopped"] = 1
            stopped = True
            _stop_run_due_to_disabled_sending(send_run=send_run, run_log_path=run_log_path)
            break

        sender = pick_next_sender_for_today()
        from_name = safe_str(sender.display_name) or "Gayathri Emuru"
        subject = (
            safe_str(generated.subject).strip()
            if generated.edited_manually
            else _manual_job_subject(job, safe_str(generated.subject).strip())
        )
        if not generated.edited_manually and subject and subject != safe_str(generated.subject).strip():
            generated.subject = subject[:500]
            generated.save(update_fields=["subject", "updated_at"])
        final_body = build_full_email_body(
            recipient_name=name,
            base_body=safe_str(generated.body),
            job_linkedin_url=safe_str(job.normalized_linkedin_url) or safe_str(job.linkedin_url),
            include_job_reference=False,
        )

        try:
            log_row = SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                job_recruiter_target=target,
                sender_account=sender,
                to_email=email,
                subject_snapshot=subject[:500],
                body_snapshot=final_body,
                attachment_path=";".join(attachments)[:1000],
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.PENDING,
                bypass_global_dedupe=True,
            )
        except IntegrityError as exc:
            totals["skipped_already_sent_or_pending"] += 1
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "skipped", "detail": f"duplicate_guard: {exc}"[:1000]})
            append_and_print(run_log_path, f"SEND_SKIP_DUP_BEFORE_SMTP job_id={job.id} to={email} err={exc}")
            continue

        totals["emails_attempted"] += 1
        append_and_print(run_log_path, f"SEND_START job_id={job.id} to={email} sender={sender.email} log_id={log_row.id}")
        try:
            msg = build_mime_message(
                from_name=from_name,
                from_email=sender.email,
                to_email=email,
                subject=subject,
                body_text=final_body,
                attachment_paths=attachments,
            )
            send_via_smtp(sender=sender, message=msg, enforce_recipient_verification=True)
            log_row.status = SentEmailLog.SendStatus.SENT
            log_row.sent_at = timezone.now()
            log_row.error_message = ""
            log_row.save(update_fields=["status", "sent_at", "error_message"])
            increment_sender_usage(sender, 1)
            JobRecruiterTarget.objects.filter(id=target.id).update(is_sent_real=True)
            CompanyRecruiter.objects.filter(id=target.company_recruiter_id).update(
                email_sent=True,
                email_sent_date=timezone.localdate(),
            )
            JobPosting.objects.filter(id=job.id).update(status=JobPosting.Status.REAL_SENT)
            totals["emails_sent"] += 1
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "sent", "sender": sender.email, "log_id": log_row.id})
            append_and_print(run_log_path, f"SEND_OK job_id={job.id} to={email} log_id={log_row.id}")
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
            rows.append({"job_id": job.id, "email": email, "name": name, "status": "failed", "sender": sender.email, "detail": error_text[:1000]})
            append_exception(run_log_path, f"SEND_FAIL job_id={job.id} to={email} log_id={log_row.id}", exc)

        is_last_selected_job = job_index >= len(jobs) - 1
        if delay_seconds and not is_last_selected_job and not _sleep_between_sends(delay_seconds, send_run=send_run, run_log_path=run_log_path):
            totals["stopped"] = 1
            stopped = True
            break

    if not stopped and send_run.status != SendRun.Status.STOPPED:
        send_run.status = SendRun.Status.SUCCESS if totals["emails_failed"] == 0 else SendRun.Status.FAILED
        send_run.finished_at = timezone.now()
        send_run.notes = (
            f"Manual job-tailored email send done. token={token} "
            f"sent={totals['emails_sent']} failed={totals['emails_failed']} skipped={totals['skipped_already_sent_or_pending']}"
        )[:4000]
        send_run.save(update_fields=["status", "finished_at", "notes"])

    return {"ok": True, "token": token, "send_run_id": send_run.id, "totals": totals, "rows": rows, "run_log_path": run_log_path}
