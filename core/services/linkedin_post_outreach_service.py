from __future__ import annotations

import re
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from core.models import GeneratedEmail, JobPosting, SentEmailLog
from core.services.apollo_recruiter_fetch_service import (
    _email_has_prior_real_initial_send,
    _extract_match_email,
    _person_location_string,
    _split_name,
    match_person_email_from_apollo,
    match_person_email_from_apollo_linkedin_url,
)
from core.services.linkedin_post_ai_extraction_service import (
    extract_email_from_text,
    extract_linkedin_post_details_with_openai,
    openai_linkedin_post_extraction_configured,
)
from core.services.linkedin_post_preview_service import preview_linkedin_post
from core.services.manual_job_email_service import TOKEN_PREFIX, create_manual_job_email_batch
from core.utils import safe_str


_URL_RE = re.compile(r"(https?://[^\s<>\"']+|www\.linkedin\.com/[^\s<>\"']+|linkedin\.com/[^\s<>\"']+)", flags=re.I)
_REVIEW_FIELD_LABELS = {
    "poster_name": "poster name",
    "email": "email",
    "company": "company",
    "role": "role",
    "post_context": "post/context",
    "valid_email": "valid email",
}
_TOKEN_RE = re.compile(rf"^{re.escape(TOKEN_PREFIX)}-([0-9a-f]+)-\d+$", flags=re.I)


def split_linkedin_post_urls(raw_text: str) -> list[str]:
    raw_text = safe_str(raw_text)
    if not raw_text:
        return []

    urls = []
    for item in _URL_RE.findall(raw_text):
        cleaned = safe_str(item).strip().rstrip("),.;\"'<>")
        if cleaned:
            urls.append(cleaned)

    if urls:
        return urls

    return [safe_str(line).strip().rstrip("),.;\"'<>") for line in raw_text.splitlines() if safe_str(line).strip()]


def _infer_company_from_post_text(result: dict) -> str:
    for key in ("job_company", "company_name", "shared_company_name"):
        value = safe_str(result.get(key)).strip()
        if value:
            return value

    for key in ("profile_company",):
        value = safe_str(result.get(key)).strip()
        if value:
            return value

    text = safe_str(result.get("post_text")).strip() or safe_str(result.get("shared_post_text")).strip()
    first_line = next((safe_str(line).strip() for line in text.splitlines() if safe_str(line).strip()), "")
    if not first_line:
        return ""

    for sep in ("|", " - ", " – ", " — "):
        if sep in first_line:
            candidate = safe_str(first_line.split(sep, 1)[0]).strip(" :-")
            if 2 <= len(candidate) <= 80 and not candidate.lower().startswith(("hiring", "we are", "we're")):
                return candidate

    match = re.search(r"\b(?:at|with)\s+([A-Z][A-Za-z0-9&.,' -]{2,70})", first_line)
    if match:
        return safe_str(match.group(1)).strip(" .,-")
    return ""


def _infer_role_from_post_text(result: dict) -> str:
    for key in ("job_title",):
        value = safe_str(result.get(key)).strip()
        if value:
            return value

    text = safe_str(result.get("post_text")).strip() or safe_str(result.get("shared_post_text")).strip()
    for line in text.splitlines():
        cleaned = safe_str(line).strip(" -*•\t")
        if not cleaned:
            continue
        match = re.search(r"(?i)\b(?:position|role|job title)\s*[:\-]\s*(.+)$", cleaned)
        if match:
            return safe_str(match.group(1)).strip()[:180]
        if any(token in cleaned.lower() for token in ("data scientist", "data analyst", "ai engineer", "machine learning", "ml engineer")):
            return cleaned[:180]
    return "LinkedIn hiring post"


def _build_manual_job_text(result: dict, *, company: str, role: str) -> str:
    parts = []
    if role:
        parts.append(f"Role: {role}")
    if company:
        parts.append(f"Company: {company}")
    poster_name = safe_str(result.get("poster_name")).strip()
    if poster_name:
        parts.append(f"Poster: {poster_name}")
    poster_url = safe_str(result.get("poster_linkedin_url")).strip()
    if poster_url:
        parts.append(f"Poster LinkedIn: {poster_url}")
    profile_headline = safe_str(result.get("profile_headline")).strip()
    if profile_headline:
        parts.append(f"Poster profile headline: {profile_headline}")
    post_url = safe_str(result.get("canonical_url")).strip() or safe_str(result.get("source_url")).strip()
    if post_url:
        parts.append(f"LinkedIn post URL: {post_url}")
    location = safe_str(result.get("job_location")).strip()
    if location:
        parts.append(f"Location: {location}")
    job_url = safe_str(result.get("job_url")).strip()
    if job_url:
        parts.append(f"Job URL: {job_url}")

    text_blocks = []
    for label, key in (("Post text", "post_text"), ("Shared post text", "shared_post_text"), ("Raw page text", "raw_text_preview")):
        value = safe_str(result.get(key)).strip()
        if value and value not in text_blocks:
            text_blocks.append(f"{label}:\n{value}")

    return "\n".join(parts + [""] + text_blocks).strip()


def _truthy(value) -> bool:
    return safe_str(value).strip().lower() in {"1", "true", "on", "yes", "y"}


def _review_context_present(row: dict) -> bool:
    return bool(
        safe_str(row.get("post_text")).strip()
        or safe_str(row.get("job_text")).strip()
        or safe_str(row.get("manual_notes")).strip()
        or safe_str(row.get("role")).strip()
        or safe_str(row.get("company")).strip()
        or safe_str(row.get("job_url")).strip()
    )


def _email_format_is_valid(email: str) -> bool:
    email = safe_str(email).strip()
    if not email:
        return False
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def _review_missing_field_keys(row: dict) -> list[str]:
    missing = []
    if not safe_str(row.get("poster_name")).strip():
        missing.append("poster_name")
    email = safe_str(row.get("email")).strip()
    if not email:
        missing.append("email")
    elif not _email_format_is_valid(email):
        missing.append("valid_email")
    if not safe_str(row.get("company")).strip():
        missing.append("company")
    if not safe_str(row.get("role")).strip():
        missing.append("role")
    if not _review_context_present(row):
        missing.append("post_context")
    return missing


def _review_blocking_field_keys(row: dict) -> list[str]:
    missing = []
    if not safe_str(row.get("poster_name")).strip():
        missing.append("poster_name")
    email = safe_str(row.get("email")).strip()
    if not email:
        missing.append("email")
    elif not _email_format_is_valid(email):
        missing.append("valid_email")
    if not _review_context_present(row):
        missing.append("post_context")
    return missing


def prepare_linkedin_post_rows_for_review(rows: list[dict]) -> list[dict]:
    prepared = []
    for index, row in enumerate(rows):
        out = dict(row)
        out.setdefault("row_number", index + 1)
        out.setdefault("review_index", index)
        out["review_index"] = index
        out["poster_name"] = safe_str(out.get("poster_name")).strip()
        out["email"] = safe_str(out.get("email")).strip().lower()
        out["company"] = safe_str(out.get("company")).strip()
        out["role"] = safe_str(out.get("role")).strip()
        out["location"] = safe_str(out.get("location")).strip()
        out["url"] = safe_str(out.get("url")).strip()
        out["canonical_url"] = safe_str(out.get("canonical_url")).strip()
        out["poster_linkedin_url"] = safe_str(out.get("poster_linkedin_url")).strip()
        out["company_linkedin_url"] = safe_str(out.get("company_linkedin_url")).strip()
        out["job_url"] = safe_str(out.get("job_url")).strip()
        out["profile_headline"] = safe_str(out.get("profile_headline")).strip()
        out["post_text"] = safe_str(out.get("post_text")).strip()
        out["manual_notes"] = safe_str(out.get("manual_notes")).strip()
        out["job_text"] = safe_str(out.get("job_text")).strip()
        missing_keys = _review_missing_field_keys(out)
        blocking_keys = _review_blocking_field_keys(out)
        out["missing_field_keys"] = missing_keys
        out["missing_fields"] = [_REVIEW_FIELD_LABELS[key] for key in missing_keys]
        out["blocking_missing_fields"] = [_REVIEW_FIELD_LABELS[key] for key in blocking_keys]
        out["ready_for_review"] = not blocking_keys
        out["needs_manual_input"] = bool(missing_keys)
        out["include_by_default"] = bool(out["ready_for_review"] and out["email"])
        if "include" in out:
            out["include_by_default"] = bool(out.get("include"))
        prepared.append(out)
    return prepared


def summarize_linkedin_post_rows(rows: list[dict]) -> dict:
    return {
        "input_urls": len(rows),
        "unique_urls": len({safe_str(row.get("url") or row.get("canonical_url")).strip() for row in rows if safe_str(row.get("url") or row.get("canonical_url")).strip()}),
        "extracted_posts": sum(1 for row in rows if safe_str(row.get("status")).strip() != "extract_error"),
        "extract_errors": sum(1 for row in rows if safe_str(row.get("status")).strip() == "extract_error"),
        "emails_found": sum(1 for row in rows if safe_str(row.get("email")).strip()),
        "apollo_credits": sum(int(row.get("apollo_credits") or 0) for row in rows),
        "ready_for_review": sum(1 for row in rows if row.get("ready_for_review")),
        "needs_manual_input": sum(1 for row in rows if row.get("needs_manual_input")),
    }


def build_linkedin_post_review_history(*, selected_date: str = "", limit: int = 40) -> dict:
    selected_date_obj = _parse_history_date(selected_date)
    jobs = list(
        JobPosting.objects.filter(
            is_manual_email_job=True,
            external_job_id__startswith=f"{TOKEN_PREFIX}-",
            description__icontains="LinkedIn post URL:",
        )
        .select_related("daily_batch")
        .order_by("-created_at", "-id")
    )

    grouped: dict[str, dict] = {}
    available_dates = set()
    for job in jobs:
        token = _token_from_external_job_id(job.external_job_id)
        if not token:
            continue
        created_date = timezone.localtime(job.created_at).date()
        available_dates.add(created_date)
        if selected_date_obj and created_date != selected_date_obj:
            continue
        row = grouped.setdefault(
            token,
            {
                "token": token,
                "created_at": job.created_at,
                "created_date": created_date,
                "batch_date": job.daily_batch.batch_date if job.daily_batch_id else None,
                "jobs": [],
                "job_ids": [],
                "titles": [],
                "companies": [],
            },
        )
        if job.created_at and job.created_at < row["created_at"]:
            row["created_at"] = job.created_at
        row["jobs"].append(job)
        row["job_ids"].append(job.id)
        if safe_str(job.title).strip():
            row["titles"].append(safe_str(job.title).strip())
        company = _manual_linkedin_company_from_description(job.description)
        if company:
            row["companies"].append(company)

    history_rows = []
    for row in grouped.values():
        job_ids = row["job_ids"]
        generated_count = GeneratedEmail.objects.filter(
            job_posting_id__in=job_ids,
            subject__gt="",
            body__gt="",
        ).count()
        sent_count = SentEmailLog.objects.filter(
            job_posting_id__in=job_ids,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
        ).count()
        row["job_count"] = len(job_ids)
        row["generated_count"] = generated_count
        row["sent_count"] = sent_count
        row["pending_count"] = max(0, len(job_ids) - sent_count)
        row["review_url"] = f"/manual-bulk-email/job-review/{row['token']}/"
        row["send_control_url"] = f"/send-control/?batch_date={row['batch_date'].isoformat()}" if row["batch_date"] else "/send-control/"
        row["read_only_url"] = f"/review-readonly/?batch_date={row['batch_date'].isoformat()}" if row["batch_date"] else "/review-readonly/"
        row["display_title"] = _compact_unique(row["titles"], fallback="LinkedIn post outreach")
        row["display_company"] = _compact_unique(row["companies"], fallback="")
        history_rows.append(row)

    history_rows.sort(key=lambda item: (item["created_at"], item["token"]), reverse=True)
    available_dates = sorted(available_dates, reverse=True)
    return {
        "selected_date": selected_date_obj.isoformat() if selected_date_obj else "",
        "available_dates": [{"date": value, "iso": value.isoformat()} for value in available_dates],
        "rows": history_rows[: max(1, int(limit or 40))],
        "total_rows": len(history_rows),
    }


def _parse_history_date(value: str):
    value = safe_str(value).strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _token_from_external_job_id(value: str) -> str:
    match = _TOKEN_RE.match(safe_str(value).strip())
    return match.group(1) if match else ""


def _manual_linkedin_company_from_description(description: str) -> str:
    match = re.search(r"(?im)^Company:\s*(.+?)\s*$", safe_str(description))
    return safe_str(match.group(1)).strip() if match else ""


def _compact_unique(values: list[str], *, fallback: str) -> str:
    seen = []
    for value in values:
        value = safe_str(value).strip()
        if value and value.lower() not in {item.lower() for item in seen}:
            seen.append(value)
        if len(seen) >= 2:
            break
    if not seen:
        return fallback
    return " / ".join(seen)


def build_manual_job_text_from_linkedin_review_row(row: dict) -> str:
    company = safe_str(row.get("company")).strip()
    role = safe_str(row.get("role")).strip() or "LinkedIn hiring post"
    name = safe_str(row.get("poster_name")).strip()
    location = safe_str(row.get("location")).strip()
    post_url = safe_str(row.get("canonical_url")).strip() or safe_str(row.get("url")).strip()
    poster_url = safe_str(row.get("poster_linkedin_url")).strip()
    company_url = safe_str(row.get("company_linkedin_url")).strip()
    job_url = safe_str(row.get("job_url")).strip()
    profile_headline = safe_str(row.get("profile_headline")).strip()
    post_text = safe_str(row.get("post_text")).strip()
    manual_notes = safe_str(row.get("manual_notes")).strip()

    parts = [f"Role: {role}"]
    if company:
        parts.append(f"Company: {company}")
    if location:
        parts.append(f"Location: {location}")
    if name:
        parts.append(f"Poster: {name}")
    if profile_headline:
        parts.append(f"Poster profile headline: {profile_headline}")
    if poster_url:
        parts.append(f"Poster LinkedIn: {poster_url}")
    if company_url:
        parts.append(f"Company LinkedIn: {company_url}")
    if post_url:
        parts.append(f"LinkedIn post URL: {post_url}")
    if job_url:
        parts.append(f"Job URL: {job_url}")

    blocks = []
    if manual_notes:
        blocks.append(f"Manual notes:\n{manual_notes}")
    if post_text:
        blocks.append(f"LinkedIn post/context:\n{post_text}")
    if not blocks and safe_str(row.get("job_text")).strip():
        blocks.append(f"Extracted context:\n{safe_str(row.get('job_text')).strip()}")

    return "\n".join(parts + [""] + blocks).strip()


def parse_linkedin_post_review_rows_from_post(post_data) -> list[dict]:
    try:
        row_count = int(post_data.get("row_count") or 0)
    except (TypeError, ValueError):
        row_count = 0

    rows = []
    for index in range(max(0, row_count)):
        row = {
            "row_number": index + 1,
            "review_index": index,
            "include": _truthy(post_data.get(f"include_{index}")),
            "status": safe_str(post_data.get(f"status_{index}")).strip() or "edited",
            "url": safe_str(post_data.get(f"url_{index}")).strip(),
            "canonical_url": safe_str(post_data.get(f"canonical_url_{index}")).strip(),
            "poster_name": safe_str(post_data.get(f"poster_name_{index}")).strip(),
            "poster_linkedin_url": safe_str(post_data.get(f"poster_linkedin_url_{index}")).strip(),
            "company": safe_str(post_data.get(f"company_{index}")).strip(),
            "company_linkedin_url": safe_str(post_data.get(f"company_linkedin_url_{index}")).strip(),
            "role": safe_str(post_data.get(f"role_{index}")).strip(),
            "location": safe_str(post_data.get(f"location_{index}")).strip(),
            "job_url": safe_str(post_data.get(f"job_url_{index}")).strip(),
            "email": safe_str(post_data.get(f"email_{index}")).strip().lower(),
            "email_status": safe_str(post_data.get(f"email_status_{index}")).strip(),
            "apollo_status": safe_str(post_data.get(f"apollo_status_{index}")).strip(),
            "apollo_reason": safe_str(post_data.get(f"apollo_reason_{index}")).strip(),
            "apollo_lookup_type": safe_str(post_data.get(f"apollo_lookup_type_{index}")).strip(),
            "apollo_credits": int(post_data.get(f"apollo_credits_{index}") or 0),
            "ai_status": safe_str(post_data.get(f"ai_status_{index}")).strip(),
            "ai_reason": safe_str(post_data.get(f"ai_reason_{index}")).strip(),
            "ai_model": safe_str(post_data.get(f"ai_model_{index}")).strip(),
            "ai_confidence": safe_str(post_data.get(f"ai_confidence_{index}")).strip(),
            "already_contacted": _truthy(post_data.get(f"already_contacted_{index}")),
            "profile_headline": safe_str(post_data.get(f"profile_headline_{index}")).strip(),
            "post_text": safe_str(post_data.get(f"post_text_{index}")).strip(),
            "manual_notes": safe_str(post_data.get(f"manual_notes_{index}")).strip(),
        }
        row["job_text"] = build_manual_job_text_from_linkedin_review_row(row)
        rows.append(row)
    return prepare_linkedin_post_rows_for_review(rows)


def create_linkedin_post_review_batch_from_rows(rows: list[dict]) -> dict:
    prepared_rows = prepare_linkedin_post_rows_for_review(rows)
    selected_rows = []
    skipped_rows = []
    for row in prepared_rows:
        if "include" in row and not row.get("include"):
            continue
        job_text = build_manual_job_text_from_linkedin_review_row(row)
        row = {**row, "job_text": job_text}
        blocking = _review_blocking_field_keys(row)
        email = safe_str(row.get("email")).strip().lower()
        already_contacted = bool(row.get("already_contacted")) or _email_has_prior_real_initial_send(email)
        if blocking:
            skipped_rows.append(
                {
                    "row_number": row.get("row_number"),
                    "name": safe_str(row.get("poster_name")).strip(),
                    "email": email,
                    "reason": "missing_" + "_".join(blocking),
                }
            )
            continue
        if already_contacted:
            skipped_rows.append(
                {
                    "row_number": row.get("row_number"),
                    "name": safe_str(row.get("poster_name")).strip(),
                    "email": email,
                    "reason": "already_sent_or_pending_real_initial",
                }
            )
            continue
        selected_rows.append(
            {
                "name": safe_str(row.get("poster_name")).strip(),
                "email": email,
                "job_text": job_text,
            }
        )

    if not selected_rows:
        return {
            "ok": False,
            "error": "No selected rows have poster name, email, and post context.",
            "totals": {
                "valid_unique_rows": 0,
                "invalid_rows": len(skipped_rows),
                "generated": 0,
            },
            "rows": [],
            "invalid_rows": skipped_rows,
        }

    batch_result = create_manual_job_email_batch(
        names=[row["name"] for row in selected_rows],
        emails=[row["email"] for row in selected_rows],
        job_texts=[row["job_text"] for row in selected_rows],
        generate_immediately=False,
    )
    batch_result["skipped_incomplete_rows"] = skipped_rows
    return batch_result


def _apollo_lookup_result_from_payload(payload: dict, *, lookup_type: str) -> dict:
    credits = int((payload or {}).get("credits_consumed") or 0)
    email, email_status = _extract_match_email(payload if isinstance(payload, dict) else {})
    person = payload.get("person") if isinstance(payload, dict) and isinstance(payload.get("person"), dict) else {}
    return {
        "status": "found" if email else "not_found",
        "reason": "" if email else "apollo_returned_no_work_email",
        "lookup_type": lookup_type,
        "credits": credits,
        "email": email,
        "email_status": email_status,
        "title": safe_str(person.get("title")).strip() or safe_str(person.get("headline")).strip(),
        "location": _person_location_string(person),
        "linkedin_url": safe_str(person.get("linkedin_url")).strip(),
        "raw": payload if isinstance(payload, dict) else {},
    }


def _lookup_poster_email(*, poster_name: str, company_name: str, poster_linkedin_url: str = "") -> dict:
    poster_linkedin_url = safe_str(poster_linkedin_url).strip()
    if poster_linkedin_url:
        payload = match_person_email_from_apollo_linkedin_url(linkedin_url=poster_linkedin_url)
        return _apollo_lookup_result_from_payload(payload if isinstance(payload, dict) else {}, lookup_type="linkedin_url")

    first_name, last_name = _split_name(poster_name)
    if not first_name or not last_name:
        return {"status": "skipped", "reason": "poster_name_not_matchable", "credits": 0, "email": ""}
    if not company_name:
        return {"status": "skipped", "reason": "missing_company_for_apollo_match", "credits": 0, "email": ""}

    payload = match_person_email_from_apollo(
        first_name=first_name,
        last_name=last_name,
        organization_name=company_name,
    )
    return _apollo_lookup_result_from_payload(payload if isinstance(payload, dict) else {}, lookup_type="name_company")


def _apply_ai_extraction(row: dict, result: dict, *, ai_extract_details: bool) -> dict:
    post_blob = "\n".join(
        [
            safe_str(result.get("post_text")),
            safe_str(result.get("shared_post_text")),
            safe_str(result.get("raw_text_preview")),
        ]
    )
    deterministic_email = extract_email_from_text(post_blob)
    if deterministic_email and not row.get("email"):
        row["email"] = deterministic_email
        row["email_status"] = "found_in_post"
        row["apollo_status"] = "not_needed"
        row["apollo_reason"] = "email_found_in_linkedin_post_text"

    if not ai_extract_details:
        row["ai_status"] = "not_requested"
        row["ai_reason"] = ""
        return row

    if not openai_linkedin_post_extraction_configured():
        row["ai_status"] = "skipped"
        row["ai_reason"] = "OPENAI_API_KEY missing"
        return row

    try:
        extracted = extract_linkedin_post_details_with_openai(source=result)
    except Exception as exc:
        row["ai_status"] = "error"
        row["ai_reason"] = str(exc)[:1000]
        return row

    row["ai_status"] = "ok"
    row["ai_reason"] = safe_str(extracted.get("notes")).strip()
    row["ai_model"] = safe_str(extracted.get("model")).strip()
    row["ai_confidence"] = safe_str(extracted.get("confidence")).strip()

    if not row.get("poster_name") and extracted.get("poster_name"):
        row["poster_name"] = safe_str(extracted.get("poster_name")).strip()
    if extracted.get("company_name"):
        row["company"] = safe_str(extracted.get("company_name")).strip()
    if extracted.get("role_title"):
        row["role"] = safe_str(extracted.get("role_title")).strip()
    if extracted.get("location"):
        row["location"] = safe_str(extracted.get("location")).strip()
    if extracted.get("job_url"):
        row["job_url"] = safe_str(extracted.get("job_url")).strip()
    if not row.get("email") and extracted.get("poster_email"):
        row["email"] = safe_str(extracted.get("poster_email")).strip().lower()
        row["email_status"] = "found_in_post_by_chatgpt"
        row["apollo_status"] = "not_needed"
        row["apollo_reason"] = "email_found_in_linkedin_post_text_by_chatgpt"
    return row


def run_linkedin_post_outreach(
    *,
    raw_urls_text: str,
    find_emails: bool = True,
    create_review_batch: bool = True,
    ai_extract_details: bool = True,
) -> dict:
    raw_urls = split_linkedin_post_urls(raw_urls_text)
    seen = set()
    rows = []
    invalid_rows = []
    totals = {
        "input_urls": len(raw_urls),
        "unique_urls": 0,
        "extracted_posts": 0,
        "extract_errors": 0,
        "apollo_attempts": 0,
        "apollo_credits": 0,
        "chatgpt_attempts": 0,
        "chatgpt_extracted": 0,
        "chatgpt_errors": 0,
        "emails_found": 0,
        "already_contacted": 0,
        "review_rows": 0,
    }

    for index, url in enumerate(raw_urls, start=1):
        if "linkedin.com/" not in safe_str(url).lower():
            invalid_rows.append({"row_number": index, "url": url, "reason": "not_linkedin_url"})
            continue
        if url in seen:
            invalid_rows.append({"row_number": index, "url": url, "reason": "duplicate_in_input"})
            continue
        seen.add(url)

        row = {"row_number": index, "url": url}
        try:
            result = preview_linkedin_post(url)
        except Exception as exc:
            totals["extract_errors"] += 1
            row.update({"status": "extract_error", "reason": str(exc)[:1000]})
            rows.append(row)
            continue

        totals["extracted_posts"] += 1
        poster_name = safe_str(result.get("poster_name")).strip()
        company_name = _infer_company_from_post_text(result)
        role = _infer_role_from_post_text(result)
        job_text = _build_manual_job_text(result, company=company_name, role=role)
        post_context = (
            safe_str(result.get("post_text")).strip()
            or safe_str(result.get("shared_post_text")).strip()
            or safe_str(result.get("raw_text_preview")).strip()
        )
        row.update(
            {
                "status": "extracted",
                "poster_name": poster_name,
                "poster_linkedin_url": safe_str(result.get("poster_linkedin_url")).strip(),
                "company": company_name,
                "company_linkedin_url": safe_str(result.get("company_linkedin_url")).strip(),
                "role": role,
                "location": safe_str(result.get("job_location")).strip(),
                "post_text": post_context,
                "canonical_url": safe_str(result.get("canonical_url")).strip(),
                "job_url": safe_str(result.get("job_url")).strip(),
                "profile_headline": safe_str(result.get("profile_headline")).strip(),
                "job_text": job_text,
                "email": "",
                "email_status": "",
                "apollo_status": "not_requested",
                "apollo_reason": "",
                "apollo_lookup_type": "",
                "apollo_credits": 0,
                "ai_status": "not_requested",
                "ai_reason": "",
                "ai_model": "",
                "ai_confidence": "",
            }
        )

        if ai_extract_details:
            totals["chatgpt_attempts"] += 1
        row = _apply_ai_extraction(row, result, ai_extract_details=ai_extract_details)
        if row.get("ai_status") == "ok":
            totals["chatgpt_extracted"] += 1
        elif row.get("ai_status") == "error":
            totals["chatgpt_errors"] += 1

        row["job_text"] = build_manual_job_text_from_linkedin_review_row(row)

        if find_emails and not row["email"]:
            totals["apollo_attempts"] += 1
            try:
                lookup = _lookup_poster_email(
                    poster_name=safe_str(row.get("poster_name")).strip(),
                    company_name=safe_str(row.get("company")).strip(),
                    poster_linkedin_url=safe_str(row.get("poster_linkedin_url")).strip(),
                )
            except Exception as exc:
                lookup = {"status": "error", "reason": str(exc)[:1000], "credits": 0, "email": ""}
            totals["apollo_credits"] += int(lookup.get("credits") or 0)
            row.update(
                {
                    "email": safe_str(lookup.get("email")).strip().lower(),
                    "email_status": safe_str(lookup.get("email_status")).strip(),
                    "apollo_status": safe_str(lookup.get("status")).strip(),
                    "apollo_reason": safe_str(lookup.get("reason")).strip(),
                    "apollo_lookup_type": safe_str(lookup.get("lookup_type")).strip(),
                    "apollo_credits": int(lookup.get("credits") or 0),
                    "apollo_title": safe_str(lookup.get("title")).strip(),
                    "apollo_location": safe_str(lookup.get("location")).strip(),
                }
            )
            if not row.get("location") and row.get("apollo_location"):
                row["location"] = row["apollo_location"]
            row["job_text"] = build_manual_job_text_from_linkedin_review_row(row)

        if row["email"]:
            totals["emails_found"] += 1
            already_contacted = _email_has_prior_real_initial_send(row["email"])
            row["already_contacted"] = already_contacted
            if already_contacted:
                totals["already_contacted"] += 1
        rows.append(row)

    totals["unique_urls"] = len(seen)
    rows = prepare_linkedin_post_rows_for_review(rows)
    row_summary = summarize_linkedin_post_rows(rows)
    totals["ready_for_review"] = row_summary["ready_for_review"]
    totals["needs_manual_input"] = row_summary["needs_manual_input"]
    result = {
        "ok": True,
        "totals": totals,
        "rows": rows,
        "invalid_rows": invalid_rows,
        "review_batch": None,
    }

    if create_review_batch and rows:
        auto_rows = [{**row, "include": bool(row.get("ready_for_review"))} for row in rows]
        batch_result = create_linkedin_post_review_batch_from_rows(auto_rows)
        result["review_batch"] = batch_result
        if batch_result.get("ok") is not False:
            totals["review_rows"] = int((batch_result.get("totals") or {}).get("valid_unique_rows") or 0)

    return result
