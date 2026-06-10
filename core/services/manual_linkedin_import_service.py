from __future__ import annotations

import os
import time
from dataclasses import asdict
from urllib.parse import urlparse

from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone
import re

from core.models import Company, DailyBatch, JobPosting
from core.services.app_settings_service import get_company_cooldown_days
from core.services.company_cooldown_service import DEFAULT_COMPANY_COOLDOWN_DAYS, should_skip_new_job_for_company
from core.services.dedupe_service import find_duplicate_reason, get_prior_7d_company_roles
from core.services.job_target_sync_service import sync_job_targets_for_job
from core.services.linkedin_company_from_job_service import extract_company_linkedin_url_from_html
from core.services.linkedin_job_scrape_service import fetch_linkedin_job_details
from core.services.linkedin_people_url_service import generate_linkedin_people_search_data
from core.services.logging_service import log_system_event
from core.services.normalization_service import (
    build_dedupe_key,
    build_description_fingerprint,
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
from core.services.openai_company_normalization_service import normalize_company_name_with_gpt
from core.services.openai_filter_service import classify_job_apply_or_reject_with_reason
from core.services.company_resolution_service import resolve_company_normalized_name
from core.utils import normalize_generic_url, normalize_job_or_generic_url, normalize_linkedin_job_url, safe_str


_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)", flags=re.IGNORECASE)


def _sleep_seconds() -> float:
    try:
        return float(os.getenv("LINKEDIN_IMPORT_SLEEP_SECONDS", "1.0") or "1.0")
    except Exception:
        return 1.0


def _sleep_with_jitter(base_seconds: float) -> None:
    """Sleep for base_seconds plus a small random jitter to avoid bot-pattern timing."""
    import random
    jitter = random.uniform(0.5, 2.0)
    time.sleep(base_seconds + jitter)


def _canonicalize_linkedin_job_url_for_storage(raw_url: str) -> str:
    """
    Produce a stable normalized LinkedIn job URL for dedupe/storage.
    Ensures https + www.linkedin.com when possible and strips query/fragment.
    """
    normalized = normalize_linkedin_job_url(raw_url)
    if not normalized:
        return ""

    try:
        parsed = urlparse(normalized)
        host = (parsed.netloc or "").lower().strip(".")
        path = parsed.path or ""

        if host:
            is_linkedin_host = host in {"linkedin.com", "www.linkedin.com"} or host.endswith(".linkedin.com")
            if not is_linkedin_host:
                return ""

        if "/jobs/view/" in path.lower():
            return f"https://www.linkedin.com{path}".rstrip("/")
        return normalized.rstrip("/")
    except Exception:
        return normalized.rstrip("/")


def _normalize_company_best_effort(raw_company: str) -> str:
    base = normalize_company_name(raw_company)

    enabled = os.getenv("OPENAI_COMPANY_NORMALIZATION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return base

    min_conf = float(os.getenv("OPENAI_COMPANY_NORMALIZATION_MIN_CONFIDENCE", "0.82") or "0.82")

    try:
        gpt_result = normalize_company_name_with_gpt(raw_company)
    except Exception:
        return base

    suggested = normalize_company_name(gpt_result.get("normalized_company") or "")
    confidence = float(gpt_result.get("confidence") or 0.0)

    if not suggested:
        return base
    if confidence < min_conf:
        return base

    if len(suggested) <= 2 and suggested not in {"ibm", "hp", "3m", "gm", "ge"}:
        return base

    return suggested


def _job_exists_by_url_path_suffix(normalized_url: str) -> bool:
    """
    Catches duplicates even if older rows stored a different linkedin host (www vs non-www).
    """
    normalized_url = safe_str(normalized_url)
    if not normalized_url:
        return False

    try:
        parsed = urlparse(normalized_url)
        path = safe_str(parsed.path)
        if path:
            path_no_slash = path.rstrip("/")
            if path_no_slash and JobPosting.objects.filter(normalized_linkedin_url__endswith=path_no_slash).exists():
                return True
            if path_no_slash and JobPosting.objects.filter(normalized_linkedin_url__endswith=path_no_slash + "/").exists():
                return True
    except Exception:
        pass

    return JobPosting.objects.filter(normalized_linkedin_url=normalized_url).exists()


def _extract_job_id_from_url(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""
    match = _JOB_ID_RE.search(url)
    if not match:
        return ""
    return safe_str(match.group(1))


def _split_urls(raw_text: str) -> list[str]:
    raw_text = safe_str(raw_text)
    if not raw_text:
        return []

    # Prefer extracting URLs anywhere in the text (handles pasted bullets/commas).
    candidates = re.findall(r"(https?://[^\s<>\"']+|www\.linkedin\.com/[^\s<>\"']+|linkedin\.com/[^\s<>\"']+)", raw_text, flags=re.I)
    urls: list[str] = []
    for item in candidates:
        cleaned = safe_str(item).strip()
        cleaned = cleaned.rstrip("),.;\"'<>")
        if cleaned:
            urls.append(cleaned)

    if urls:
        return urls

    # Fallback: line-based.
    for line in raw_text.splitlines():
        line = safe_str(line).strip().rstrip("),.;\"'<>")
        if line:
            urls.append(line)
    return urls


_ANY_URL_RE = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", flags=re.I)
_LINKEDIN_PROFILE_URL_RE = re.compile(
    r"(https?://(?:www\.)?linkedin\.com/in/[^\s<>\"')]+|www\.linkedin\.com/in/[^\s<>\"')]+)",
    flags=re.I,
)


def _normalize_hiring_team_line(line: str) -> str:
    text = safe_str(line).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -\t|")
    text = re.sub(r"\b(?:1st|2nd|3rd)\b\.?$", "", text, flags=re.I).strip()
    return text


def _hiring_team_lines(text: str) -> list[str]:
    text = safe_str(text)
    if not text:
        return []
    text = text.replace("\r", "\n").replace("|", "\n").replace("\t", "\n")
    rows: list[str] = []
    for raw_line in text.splitlines():
        cleaned = _normalize_hiring_team_line(raw_line)
        if cleaned:
            rows.append(cleaned)
    return rows


def _looks_like_person_name(line: str) -> bool:
    text = _normalize_hiring_team_line(line)
    if not text:
        return False
    lower = text.lower()
    if "linkedin.com/" in lower or "http://" in lower or "https://" in lower:
        return False
    if lower in {"meet the hiring team", "job poster", "message", "actively reviewing applicants"}:
        return False
    if any(
        token in lower
        for token in [
            "recruiter",
            "talent",
            "human resources",
            "people operations",
            "vice president",
            "president",
            "director",
            "manager",
            "engineer",
            "scientist",
            "specialist",
            "coordinator",
            "officer",
            " at ",
        ]
    ):
        return False
    words = [w for w in re.split(r"\s+", text) if w]
    if len(words) < 2 or len(words) > 5:
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,4}$", text))


def _looks_like_hiring_title(line: str) -> bool:
    text = _normalize_hiring_team_line(line)
    if not text:
        return False
    lower = text.lower()
    if lower in {"meet the hiring team", "job poster", "message"}:
        return False
    if "linkedin.com/" in lower or "jobs/view/" in lower:
        return False
    if _looks_like_person_name(text):
        return False
    return len(text) >= 4


def _extract_hiring_team_lead_from_text(text: str) -> dict:
    lines = _hiring_team_lines(text)
    if not lines:
        return {}

    profile_url = ""
    profile_match = _LINKEDIN_PROFILE_URL_RE.search(safe_str(text))
    if profile_match:
        profile_url = normalize_generic_url(profile_match.group(1))

    stripped_lines = []
    for line in lines:
        without_urls = _ANY_URL_RE.sub("", line).strip(" -|")
        cleaned = _normalize_hiring_team_line(without_urls)
        if cleaned:
            stripped_lines.append(cleaned)

    for idx, line in enumerate(stripped_lines):
        if not _looks_like_person_name(line):
            continue
        title = ""
        for candidate in stripped_lines[idx + 1 : idx + 5]:
            if _looks_like_hiring_title(candidate):
                title = candidate
                break
        return {
            "name": line[:255],
            "title": title[:255],
            "linkedin": profile_url[:1000],
        }

    return {}


def parse_hiring_team_leads_from_text(raw_text: str, job_urls: list[str] | None = None) -> dict[str, dict]:
    """
    Parse optional LinkedIn "Meet the hiring team" paste text into job-url keyed leads.

    Preferred format is a block containing the LinkedIn job URL, person name, title,
    and optional profile URL. If there is exactly one job URL being imported, a bare
    card paste without the job URL is accepted too.
    """
    text = safe_str(raw_text)
    if not text:
        return {}

    normalized_job_urls = []
    for url in job_urls or []:
        normalized = _canonicalize_linkedin_job_url_for_storage(url)
        if normalized and normalized not in normalized_job_urls:
            normalized_job_urls.append(normalized)

    leads: dict[str, dict] = {}
    matches = list(re.finditer(r"https?://[^\s<>\"']*linkedin\.com/jobs/view/[^\s<>\"']+", text, flags=re.I))
    for index, match in enumerate(matches):
        normalized_url = _canonicalize_linkedin_job_url_for_storage(match.group(0))
        if not normalized_url:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lead = _extract_hiring_team_lead_from_text(text[match.end() : end])
        if lead.get("name"):
            lead["source"] = "pasted_text"
            leads[normalized_url] = lead

    if leads:
        return leads

    if len(normalized_job_urls) == 1:
        lead = _extract_hiring_team_lead_from_text(text)
        if lead.get("name"):
            lead["source"] = "pasted_text"
        return {normalized_job_urls[0]: lead} if lead.get("name") else {}

    blocks = [block for block in re.split(r"\n\s*\n", text) if safe_str(block).strip()]
    if len(blocks) == len(normalized_job_urls):
        for url, block in zip(normalized_job_urls, blocks):
            lead = _extract_hiring_team_lead_from_text(block)
            if lead.get("name"):
                lead["source"] = "pasted_text"
                leads[url] = lead

    return leads


def _find_existing_job_by_normalized_url(normalized_url: str) -> JobPosting | None:
    normalized_url = safe_str(normalized_url).strip()
    if not normalized_url:
        return None

    job = JobPosting.objects.filter(normalized_linkedin_url=normalized_url).order_by("id").first()
    if job:
        return job

    try:
        parsed = urlparse(normalized_url)
        path = safe_str(parsed.path).rstrip("/")
        if path:
            return (
                JobPosting.objects.filter(normalized_linkedin_url__endswith=path).order_by("id").first()
                or JobPosting.objects.filter(normalized_linkedin_url__endswith=path + "/").order_by("id").first()
            )
    except Exception:
        return None

    return None


def _set_job_hiring_team_lead_fields(job: JobPosting, lead: dict) -> list[str]:
    if not job or not lead.get("name"):
        return []

    changed_fields: list[str] = []
    values = {
        "recruiter_name": safe_str(lead.get("name")).strip()[:255],
        "recruiter_title": safe_str(lead.get("title")).strip()[:255],
        "recruiter_linkedin": safe_str(lead.get("linkedin")).strip()[:1000],
    }
    for field, value in values.items():
        if value and getattr(job, field) != value:
            setattr(job, field, value)
            changed_fields.append(field)

    resettable_statuses = {
        JobPosting.Status.IMPORTED,
        JobPosting.Status.RECRUITERS_PENDING,
        JobPosting.Status.EMAIL_DISCOVERY_READY,
        JobPosting.Status.EMAIL_DISCOVERY_DONE,
    }
    if changed_fields and job.status in resettable_statuses and job.status != JobPosting.Status.RECRUITERS_PENDING:
        job.status = JobPosting.Status.RECRUITERS_PENDING
        changed_fields.append("status")

    return changed_fields


def _record_hiring_team_lead(stats: dict, *, job: JobPosting | None, normalized_url: str, lead: dict, status: str) -> None:
    stats["hiring_team_leads_stored"] = int(stats.get("hiring_team_leads_stored") or 0) + 1
    stats.setdefault("hiring_team_lead_rows", []).append(
        {
            "job_id": int(job.id) if job and job.id else "",
            "linkedin_url": normalized_url,
            "name": safe_str(lead.get("name")).strip(),
            "title": safe_str(lead.get("title")).strip(),
            "profile_url": safe_str(lead.get("linkedin")).strip(),
            "source": safe_str(lead.get("source")).strip() or "unknown",
            "status": status,
        }
    )


def _manual_result_summary(stats: dict) -> dict:
    apply_count = 0
    reject_count = 0
    for row in stats.get("filter_decision_rows") or []:
        decision = safe_str(row.get("decision")).strip().upper()
        if decision == "APPLY":
            apply_count += 1
        elif decision:
            reject_count += 1

    skipped_count = (
        int(stats.get("invalid_urls") or 0)
        + int(stats.get("skipped_existing_url") or 0)
        + int(stats.get("skipped_existing_external_job_id") or 0)
        + int(stats.get("skipped_duplicate") or 0)
        + int(stats.get("skipped_blocked_company") or 0)
        + int(stats.get("skipped_company_cooldown") or 0)
        + int(stats.get("scrape_failed") or 0)
    )
    not_useful_count = int(stats.get("filtered_reject") or 0) + skipped_count

    return {
        "input_urls": int(stats.get("raw_lines") or 0),
        "unique_urls": int(stats.get("unique_urls") or 0),
        "checked_urls": int(stats.get("queued_urls") or 0),
        "scraped_jobs": int(stats.get("scrape_ok") or 0),
        "apply_jobs": apply_count,
        "reject_jobs": reject_count,
        "created_jobs": int(stats.get("created_jobs") or 0),
        "not_useful_jobs": not_useful_count,
        "skipped_jobs": skipped_count,
        "errors": int(stats.get("job_errors") or 0),
        "pasted_person_leads": int(stats.get("hiring_team_leads_input") or 0),
        "scraped_public_cards": int(stats.get("hiring_team_leads_scraped") or 0),
        "stored_person_leads": int(stats.get("hiring_team_leads_stored") or 0),
    }


def _manual_result_audit_tables(stats: dict) -> None:
    filter_rows = list(stats.get("filter_decision_rows") or [])
    stats["apply_decision_rows"] = [
        row for row in filter_rows if safe_str(row.get("decision")).strip().upper() == "APPLY"
    ]
    stats["reject_decision_rows"] = [
        row for row in filter_rows if safe_str(row.get("decision")).strip().upper() != "APPLY"
    ]

    job_rows = list(stats.get("job_status_rows") or [])
    stats["stored_job_rows"] = [
        row for row in job_rows if safe_str(row.get("status")).strip().lower() in {"created", "updated"}
    ]
    stored_with_lead = [
        row
        for row in stats["stored_job_rows"]
        if safe_str(row.get("hiring_team_name")).strip()
    ]
    stats["poster_coverage"] = {
        "stored_jobs": len(stats["stored_job_rows"]),
        "stored_jobs_with_exact_person": len(stored_with_lead),
        "stored_jobs_needing_regular_fallback": max(0, len(stats["stored_job_rows"]) - len(stored_with_lead)),
        "pasted_person_leads": int(stats.get("hiring_team_leads_input") or 0),
        "scraped_public_cards": int(stats.get("hiring_team_leads_scraped") or 0),
        "stored_person_leads": int(stats.get("hiring_team_leads_stored") or 0),
    }

    not_imported_rows = [
        row for row in job_rows if safe_str(row.get("status")).strip().lower() not in {"created", "updated"}
    ]
    known_job_urls = {safe_str(row.get("linkedin_url")).strip() for row in job_rows if safe_str(row.get("linkedin_url")).strip()}
    for url_row in stats.get("url_rows") or []:
        status = safe_str(url_row.get("status")).strip()
        linkedin_url = safe_str(url_row.get("normalized_url")).strip() or safe_str(url_row.get("raw_url")).strip()
        if status in {"queued", "processing"} or linkedin_url in known_job_urls:
            continue
        not_imported_rows.append(
            {
                "linkedin_url": linkedin_url,
                "company": "",
                "title": "",
                "location": "",
                "status": status,
                "detail": safe_str(url_row.get("detail")).strip(),
            }
        )
    stats["not_imported_rows"] = not_imported_rows
    stats["manual_summary"] = _manual_result_summary(stats)


def prepare_manual_linkedin_result_for_display(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return result
    if result.get("manual_summary") and "stored_job_rows" in result and "not_imported_rows" in result:
        return result
    _manual_result_audit_tables(result)
    return result


def run_manual_linkedin_import(
    *,
    raw_urls_text: str,
    cooldown_days: int,
    apply_cooldown_filters: bool,
    skip_blocked_companies: bool,
    use_openai_filter: bool,
    dry_run: bool,
    force_refetch: bool = False,
    hiring_team_text: str = "",
    log_path: str = "",
) -> dict:
    batch_date = timezone.localdate()
    sleep_s = _sleep_seconds()
    # Never allow disabling cooldown; enforce at least 10 days globally.
    global_cooldown_days = get_company_cooldown_days()
    apply_cooldown_filters = True

    raw_urls = _split_urls(raw_urls_text)
    hiring_team_leads_by_url = parse_hiring_team_leads_from_text(hiring_team_text, job_urls=raw_urls)

    stats: dict = {
        "batch_date": batch_date.isoformat(),
        "raw_lines": len(raw_urls),
        "hiring_team_leads_input": len(hiring_team_leads_by_url),
        "hiring_team_leads_scraped": 0,
        "hiring_team_leads_stored": 0,
        "hiring_team_lead_rows": [],
        "invalid_urls": 0,
        "unique_urls": 0,
        "skipped_existing_url": 0,
        "skipped_existing_external_job_id": 0,
        "scrape_ok": 0,
        "scrape_failed": 0,
        "skipped_blocked_company": 0,
        "skipped_company_cooldown": 0,
        "skipped_duplicate": 0,
        "filtered_reject": 0,
        "created_jobs": 0,
        "created_job_ids": [],
        "job_errors": 0,
        "dry_run": bool(dry_run),
        "url_rows": [],
        "job_status_rows": [],
        "filter_decision_rows": [],
        "error_samples": [],
    }

    daily_batch, _ = DailyBatch.objects.get_or_create(
        batch_date=batch_date,
        defaults={
            "lookback_hours": 24,
            "max_jobs_requested": 0,
            "apify_run_status": DailyBatch.RunStatus.SUCCESS,
            "notes": "Manual LinkedIn import",
        },
    )

    seen = set()
    candidates: list[tuple[str, str]] = []  # (raw_url, normalized_url)

    def _log(message: str) -> None:
        if not log_path:
            return
        try:
            from core.services.file_run_logger import append_and_print

            append_and_print(log_path, message)
        except Exception:
            return

    def _set_url_row_status(normalized_url: str, status: str, detail: str = "") -> None:
        for url_row in stats["url_rows"]:
            if url_row.get("normalized_url") == normalized_url and url_row.get("status") in {"queued", "processing"}:
                url_row["status"] = status
                if detail:
                    url_row["detail"] = detail
                return

    for raw_url in raw_urls:
        normalized_url = _canonicalize_linkedin_job_url_for_storage(raw_url)
        row = {"raw_url": raw_url, "normalized_url": normalized_url, "status": "", "detail": ""}

        if not normalized_url or "linkedin.com/jobs/view/" not in normalized_url.lower():
            stats["invalid_urls"] += 1
            row["status"] = "invalid_url"
            stats["url_rows"].append(row)
            continue

        if normalized_url in seen:
            row["status"] = "duplicate_in_input"
            stats["url_rows"].append(row)
            continue
        seen.add(normalized_url)

        existing_by_url = None if force_refetch else _find_existing_job_by_normalized_url(normalized_url)
        if not force_refetch and existing_by_url:
            lead = hiring_team_leads_by_url.get(normalized_url) or {}
            changed_fields = _set_job_hiring_team_lead_fields(existing_by_url, lead)
            if changed_fields:
                existing_by_url.save(update_fields=list(dict.fromkeys(changed_fields + ["updated_at"])))
                _record_hiring_team_lead(
                    stats,
                    job=existing_by_url,
                    normalized_url=normalized_url,
                    lead=lead,
                    status="updated_existing_url",
                )
                row["detail"] = f"hiring_team={lead.get('name')}"
            stats["skipped_existing_url"] += 1
            row["status"] = "skipped_existing_url"
            stats["url_rows"].append(row)
            continue

        job_id = _extract_job_id_from_url(normalized_url)
        existing_by_external_id = (
            JobPosting.objects.filter(external_job_id=job_id).order_by("id").first()
            if not force_refetch and job_id
            else None
        )
        if not force_refetch and existing_by_external_id:
            lead = hiring_team_leads_by_url.get(normalized_url) or {}
            changed_fields = _set_job_hiring_team_lead_fields(existing_by_external_id, lead)
            if changed_fields:
                existing_by_external_id.save(update_fields=list(dict.fromkeys(changed_fields + ["updated_at"])))
                _record_hiring_team_lead(
                    stats,
                    job=existing_by_external_id,
                    normalized_url=normalized_url,
                    lead=lead,
                    status="updated_existing_external_job_id",
                )
                row["detail"] = f"{job_id} hiring_team={lead.get('name')}"
            stats["skipped_existing_external_job_id"] += 1
            row["status"] = "skipped_existing_external_job_id"
            row["detail"] = row["detail"] or job_id
            stats["url_rows"].append(row)
            continue

        row["status"] = "queued"
        stats["url_rows"].append(row)
        candidates.append((raw_url, normalized_url))

    stats["unique_urls"] = len(seen)
    stats["queued_urls"] = len(candidates)

    _log(
        "MANUAL_LINKEDIN_IMPORT_START "
        f"batch_date={batch_date} urls_total={stats['raw_lines']} queued={len(candidates)} "
        f"hiring_team_leads={len(hiring_team_leads_by_url)} "
        f"apply_cooldown=FORCED cooldown_days={int(cooldown_days or 0)} global_cooldown_days={global_cooldown_days} "
        f"skip_blocked_companies={bool(skip_blocked_companies)} use_openai_filter={bool(use_openai_filter)} sleep_s={sleep_s}"
    )

    if dry_run:
        _log("MANUAL_LINKEDIN_IMPORT_DRY_RUN return_only=true")
        _manual_result_audit_tables(stats)
        return stats

    for idx, (raw_url, normalized_url) in enumerate(candidates, start=1):
        try:
            _log(f"JOB_START idx={idx}/{len(candidates)} url={normalized_url}")
            _set_url_row_status(normalized_url, "processing")
            if sleep_s and idx > 1:
                _sleep_with_jitter(sleep_s)

            details = fetch_linkedin_job_details(normalized_url)
            stats["scrape_ok"] += 1
            _log(
                f"JOB_SCRAPE_OK idx={idx} status={details.status_code} final_url={details.final_url} "
                f"title={details.title[:80]!r} company={details.company[:60]!r}"
            )

            ext_id = safe_str(details.external_job_id)
            if not force_refetch and ext_id and JobPosting.objects.filter(external_job_id=ext_id).exists():
                stats["skipped_existing_external_job_id"] += 1
                _set_url_row_status(normalized_url, "skipped_existing_external_job_id", ext_id)
                stats["job_status_rows"].append(
                    {
                        "linkedin_url": normalized_url,
                        "company": company_raw,
                        "title": title_raw,
                        "location": location_raw,
                        "status": "skipped_existing_external_job_id",
                        "detail": ext_id,
                    }
                )
                _log(f"JOB_SKIP_EXISTING_EXTERNAL_ID idx={idx} external_job_id={ext_id}")
                continue

            company_raw = safe_str(details.company)
            title_raw = safe_str(details.title)
            location_raw = safe_str(details.location) or "United States"
            description_raw = safe_str(details.description_text)
            apply_url = normalize_job_or_generic_url(details.apply_url) or normalized_url
            manual_hiring_team_lead = hiring_team_leads_by_url.get(normalized_url) or {}
            scraped_hiring_team_lead = {}
            if safe_str(getattr(details, "recruiter_name", "")).strip():
                scraped_hiring_team_lead = {
                    "name": safe_str(getattr(details, "recruiter_name", "")).strip(),
                    "title": safe_str(getattr(details, "recruiter_title", "")).strip(),
                    "linkedin": safe_str(getattr(details, "recruiter_linkedin", "")).strip(),
                    "source": "public_linkedin_card",
                }
                stats["hiring_team_leads_scraped"] += 1
                _log(
                    f"JOB_POSTER_SCRAPED idx={idx} "
                    f"name={scraped_hiring_team_lead['name'][:80]!r} "
                    f"profile={'yes' if scraped_hiring_team_lead.get('linkedin') else 'no'}"
                )
            if manual_hiring_team_lead:
                manual_hiring_team_lead = {**manual_hiring_team_lead, "source": safe_str(manual_hiring_team_lead.get("source")).strip() or "pasted_text"}
            hiring_team_lead = manual_hiring_team_lead or scraped_hiring_team_lead

            if use_openai_filter:
                classifier_result = classify_job_apply_or_reject_with_reason(title_raw, description_raw)
                decision = safe_str(classifier_result.get("decision")).strip().upper()
                reason = safe_str(classifier_result.get("reason")).strip()
                raw_output = safe_str(classifier_result.get("raw_output")).strip()
                stats["filter_decision_rows"].append(
                    {
                        "linkedin_url": normalized_url,
                        "company": company_raw,
                        "title": title_raw,
                        "location": location_raw,
                        "decision": decision,
                        "reason": reason,
                        "raw_output": raw_output,
                    }
                )
                _log(f"JOB_CLASSIFIER idx={idx} decision={decision} reason={reason!r}")
                if decision != "APPLY":
                    stats["filtered_reject"] += 1
                    _set_url_row_status(normalized_url, "openai_reject", reason)
                    stats["job_status_rows"].append(
                        {
                            "linkedin_url": normalized_url,
                            "company": company_raw,
                            "title": title_raw,
                            "location": location_raw,
                            "status": "rejected",
                            "detail": f"classifier decision={decision} reason={reason}",
                        }
                    )
                    log_system_event(
                        event_type="filtered_reject_manual",
                        message=f"Rejected job: {company_raw} | {title_raw} | {location_raw} | reason={reason}",
                    )
                    continue

            normalized_company = _normalize_company_best_effort(company_raw) or normalize_company_name(company_raw) or safe_str(company_raw).strip().lower()
            normalized_title = normalize_title(title_raw)
            normalized_location = normalize_location(location_raw)
            if not normalized_location:
                normalized_location = "united states"

            # Resolve company to a stable stored normalized name so acronym/fuzzy merges
            # share the same cooldown/dedupe keys.
            pre_resolved_company_name, _ = resolve_company_normalized_name(
                normalized_company_name=normalized_company,
                company_linkedin_url="",
            )
            stored_normalized_company = pre_resolved_company_name or normalized_company or normalize_company_name(company_raw)

            canonical_title_value = canonical_title(title_raw)
            canonical_location_value = canonical_location(location_raw or "United States")

            duplicate_reason = find_duplicate_reason(
                normalized_linkedin_url=normalized_url,
                normalized_apply_url=apply_url,
                canonical_company=canonical_company_name(stored_normalized_company or company_raw),
                canonical_title=canonical_title_value,
                canonical_location=canonical_location_value,
            )
            if duplicate_reason:
                stats["skipped_duplicate"] += 1
                _set_url_row_status(normalized_url, "deduped", duplicate_reason)
                stats["job_status_rows"].append(
                    {
                        "linkedin_url": normalized_url,
                        "company": company_raw,
                        "title": title_raw,
                        "location": location_raw,
                        "status": "deduped",
                        "detail": duplicate_reason,
                    }
                )
                _log(f"JOB_SKIP_DUPLICATE idx={idx} reason={duplicate_reason}")
                log_system_event(
                    event_type="deduped_manual",
                    message=f"Skipped duplicate job by {duplicate_reason}: {company_raw} | {title_raw}",
                )
                continue

            prior_roles = get_prior_7d_company_roles(stored_normalized_company, batch_date)

            scraped_company_linkedin_url = extract_company_linkedin_url_from_html(
                page_html=safe_str(details.page_html),
                expected_company_name=company_raw,
            )
            if not scraped_company_linkedin_url:
                from core.services.linkedin_company_from_job_service import get_company_linkedin_url_from_job_url

                scraped_company_linkedin_url = get_company_linkedin_url_from_job_url(
                    job_url=normalized_url,
                    expected_company_name=company_raw,
                )

            # Final resolve with scraped company URL.
            resolved_company_name, linkedin_company_slug = resolve_company_normalized_name(
                normalized_company_name=normalized_company,
                company_linkedin_url=scraped_company_linkedin_url,
            )
            stored_normalized_company = resolved_company_name or stored_normalized_company
            canonical_company = canonical_company_name(stored_normalized_company or company_raw)

            if skip_blocked_companies:
                try:
                    if Company.objects.filter(normalized_name=stored_normalized_company, is_blocked=True).exists():
                        stats["skipped_blocked_company"] += 1
                        _set_url_row_status(normalized_url, "skipped_blocked_company", stored_normalized_company)
                        stats["job_status_rows"].append(
                            {
                                "linkedin_url": normalized_url,
                                "company": company_raw,
                                "title": title_raw,
                                "location": location_raw,
                                "status": "skipped_blocked_company",
                                "detail": stored_normalized_company,
                            }
                        )
                        _log(f"JOB_SKIP_BLOCKED idx={idx} normalized_company={stored_normalized_company}")
                        continue
                except Exception:
                    pass

            effective_cooldown_days = int(cooldown_days or 0)
            skip, skip_reason = should_skip_new_job_for_company(
                canonical_company=canonical_company,
                batch_date=batch_date,
                cooldown_days=effective_cooldown_days,
            )
            if skip:
                stats["skipped_company_cooldown"] += 1
                _set_url_row_status(normalized_url, "skipped_company_cooldown", skip_reason)
                stats["job_status_rows"].append(
                    {
                        "linkedin_url": normalized_url,
                        "company": company_raw,
                        "title": title_raw,
                        "location": location_raw,
                        "status": "skipped_company_cooldown",
                        "detail": skip_reason,
                    }
                )
                _log(f"JOB_SKIP_COOLDOWN idx={idx} reason={skip_reason} canonical_company={canonical_company}")
                log_system_event(
                    event_type="deduped_manual",
                    message=f"Skipped job due to company cooldown ({skip_reason}): {company_raw} | {title_raw}",
                )
                continue

            linkedin_geo_region_id, linkedin_people_search_urls = generate_linkedin_people_search_data(
                company_linkedin_url=scraped_company_linkedin_url,
                normalized_state=normalized_location,
            )
            _log(
                f"JOB_COMPANY_LINKEDIN idx={idx} company_linkedin={'yes' if scraped_company_linkedin_url else 'no'} "
                f"people_urls={len(linkedin_people_search_urls or {})}"
            )

            dedupe_key = build_dedupe_key(
                stored_normalized_company or company_raw,
                title_raw,
                location_raw or "United States",
            )

            with transaction.atomic():
                company_obj, created = Company.objects.get_or_create(
                    normalized_name=stored_normalized_company,
                    defaults={"raw_name_latest": company_raw},
                )
                if not created and company_obj.raw_name_latest != company_raw:
                    company_obj.raw_name_latest = company_raw
                    company_obj.save(update_fields=["raw_name_latest", "updated_at"])

                if linkedin_company_slug and safe_str(company_obj.linkedin_company_slug).strip().lower() != linkedin_company_slug.strip().lower():
                    company_obj.linkedin_company_slug = linkedin_company_slug.strip().lower()
                    company_obj.save(update_fields=["linkedin_company_slug", "updated_at"])

                # When force_refetch, find and update the existing job instead of creating.
                existing_job = None
                if force_refetch:
                    if ext_id:
                        existing_job = JobPosting.objects.filter(external_job_id=ext_id).first()
                    if not existing_job:
                        existing_job = JobPosting.objects.filter(normalized_linkedin_url=normalized_url).first()

                if existing_job:
                    existing_job.title = title_raw
                    existing_job.company = company_raw
                    existing_job.location = location_raw
                    existing_job.description = description_raw
                    existing_job.description_fingerprint = build_description_fingerprint(description_raw)
                    existing_job.apply_url = apply_url
                    existing_job.normalized_apply_url = apply_url
                    existing_job.company_linkedin = safe_str(scraped_company_linkedin_url) or existing_job.company_linkedin
                    existing_job.normalized_company = stored_normalized_company
                    existing_job.normalized_title = normalized_title
                    existing_job.normalized_location = normalized_location
                    existing_job.canonical_company = canonical_company
                    existing_job.canonical_title = canonical_title_value
                    existing_job.canonical_location = canonical_location_value
                    existing_job.dedupe_key = dedupe_key
                    existing_job.sort_company = build_sort_company(company_raw)
                    existing_job.sort_title = build_sort_title(title_raw)
                    existing_job.sort_location = build_sort_location(location_raw)
                    existing_job.company_ref = company_obj
                    changed_hiring_team_fields = _set_job_hiring_team_lead_fields(existing_job, hiring_team_lead)
                    existing_job.save()
                    if changed_hiring_team_fields:
                        _record_hiring_team_lead(
                            stats,
                            job=existing_job,
                            normalized_url=normalized_url,
                            lead=hiring_team_lead,
                            status="updated_refetched_job",
                        )
                    job_posting = existing_job
                    job_is_new = False
                    # Re-sync targets so force_refetch picks up recruiters added after original import.
                    from core.services.app_settings_service import get_max_people_per_company
                    if not hiring_team_lead.get("name"):
                        sync_stats = sync_job_targets_for_job(
                            job=job_posting,
                            max_targets=get_max_people_per_company(),
                            auto_select=True,
                        )
                        if sync_stats.get("targets_upserted") and job_posting.status == JobPosting.Status.RECRUITERS_PENDING:
                            job_posting.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
                            job_posting.save(update_fields=["status", "updated_at"])
                else:
                    try:
                        job_posting = JobPosting.objects.create(
                            daily_batch=daily_batch,
                            is_manual_import=True,
                            company_ref=company_obj,
                            external_job_id=safe_str(details.external_job_id),
                            linkedin_url=normalized_url,
                            apply_url=apply_url,
                            normalized_linkedin_url=normalized_url,
                            normalized_apply_url=apply_url,
                            title=title_raw,
                            company=company_raw,
                            location=location_raw,
                            salary="",
                            description=description_raw,
                            description_fingerprint=build_description_fingerprint(description_raw),
                            apify_linkedin_org_url="",
                            apify_linkedin_org_slug="",
                            company_linkedin=safe_str(scraped_company_linkedin_url),
                            recruiter_name=safe_str(hiring_team_lead.get("name")).strip()[:255],
                            recruiter_title=safe_str(hiring_team_lead.get("title")).strip()[:255],
                            recruiter_linkedin=safe_str(hiring_team_lead.get("linkedin")).strip()[:1000],
                            ai_hiring_mgr_name="",
                            ai_hiring_mgr_email="",
                            normalized_company=stored_normalized_company,
                            normalized_title=normalized_title,
                            normalized_location=normalized_location,
                            canonical_company=canonical_company,
                            canonical_title=canonical_title_value,
                            canonical_location=canonical_location_value,
                            dedupe_key=dedupe_key,
                            sort_company=build_sort_company(company_raw),
                            sort_title=build_sort_title(title_raw),
                            sort_location=build_sort_location(location_raw),
                            prior_7d_company_roles=prior_roles,
                            linkedin_geo_region_id=linkedin_geo_region_id,
                            linkedin_people_search_urls=linkedin_people_search_urls,
                            status=JobPosting.Status.RECRUITERS_PENDING,
                        )
                        job_is_new = True
                        if hiring_team_lead.get("name"):
                            _record_hiring_team_lead(
                                stats,
                                job=job_posting,
                                normalized_url=normalized_url,
                                lead=hiring_team_lead,
                                status="stored_new_job",
                            )
                    except IntegrityError:
                        stats["skipped_duplicate"] += 1
                        _set_url_row_status(normalized_url, "deduped", "db_unique_constraint")
                        stats["job_status_rows"].append(
                            {
                                "linkedin_url": normalized_url,
                                "company": company_raw,
                                "title": title_raw,
                                "location": location_raw,
                                "status": "deduped",
                                "detail": "db_unique_constraint",
                            }
                        )
                        _log(f"JOB_DEDUPED_DB_CONSTRAINT idx={idx} url={normalized_url}")
                        continue

                if job_is_new:
                    auto_select = os.getenv("AUTO_SELECT_TARGETS_ON_IMPORT", "1").strip().lower() in {"1", "true", "yes", "on"}
                    from core.services.app_settings_service import get_max_people_per_company
                    if not hiring_team_lead.get("name"):
                        sync_stats = sync_job_targets_for_job(
                            job=job_posting,
                            max_targets=get_max_people_per_company(),
                            auto_select=auto_select,
                        )
                        if sync_stats.get("targets_upserted"):
                            job_posting.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
                            job_posting.save(update_fields=["status", "updated_at"])

            stats["created_jobs"] += 1
            try:
                stats["created_job_ids"].append(int(job_posting.id))
            except Exception:
                pass
            action = "updated" if not job_is_new else "created"
            _set_url_row_status(normalized_url, action, f"job_id={job_posting.id}")
            stats["job_status_rows"].append(
                {
                    "job_id": int(job_posting.id),
                    "linkedin_url": normalized_url,
                    "company": company_raw,
                    "title": title_raw,
                    "location": location_raw,
                    "status": action,
                    "detail": f"company_linkedin={'yes' if scraped_company_linkedin_url else 'no'} people_urls={len(linkedin_people_search_urls or {})}",
                    "hiring_team_name": safe_str(hiring_team_lead.get("name")).strip(),
                    "hiring_team_title": safe_str(hiring_team_lead.get("title")).strip(),
                    "hiring_team_profile_url": safe_str(hiring_team_lead.get("linkedin")).strip(),
                }
            )
            _log(f"JOB_{action.upper()} idx={idx} job_id={job_posting.id} status={job_posting.status}")

            log_system_event(
                event_type="imported_manual",
                message=f"{'Refetched and updated' if not job_is_new else 'Imported'} manual job into batch {batch_date}",
                job_posting=job_posting,
            )

        except Exception as exc:
            stats["scrape_failed"] += 1
            stats["job_errors"] += 1
            message = f"url={normalized_url} error={exc}"
            _set_url_row_status(normalized_url, "error", str(exc))
            if len(stats["error_samples"]) < 30:
                stats["error_samples"].append(message)
            stats["job_status_rows"].append({"linkedin_url": normalized_url, "status": "error", "detail": str(exc)})
            _log(f"JOB_ERROR idx={idx} url={normalized_url} error={exc}")
            log_system_event(event_type="failed_manual_import", message=message)

    # Convert dataclasses (if any) to pure dicts for template rendering safety.
    stats["job_status_rows"] = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in stats["job_status_rows"]]
    stats["filter_decision_rows"] = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in stats["filter_decision_rows"]]
    _manual_result_audit_tables(stats)

    _log(
        "MANUAL_LINKEDIN_IMPORT_DONE "
        f"created_jobs={stats['created_jobs']} skipped_existing_url={stats['skipped_existing_url']} "
        f"skipped_existing_external_job_id={stats['skipped_existing_external_job_id']} "
        f"skipped_duplicate={stats['skipped_duplicate']} skipped_company_cooldown={stats['skipped_company_cooldown']} "
        f"skipped_blocked_company={stats['skipped_blocked_company']} "
        f"scrape_failed={stats['scrape_failed']} filtered_reject={stats['filtered_reject']}"
    )
    return stats


def sync_manual_jobs_with_existing_recruiters() -> dict:
    """
    Re-sync all manual LinkedIn jobs stuck at RECRUITERS_PENDING with recruiters
    already in the DB (legacy or Apollo). Jobs that were imported before the
    company's recruiters existed — or before force_refetch re-linked them — are
    fixed here in one batch without re-scraping LinkedIn.
    """
    from core.services.app_settings_service import get_max_people_per_company

    max_targets = get_max_people_per_company()
    auto_select = os.getenv("AUTO_SELECT_TARGETS_ON_IMPORT", "1").strip().lower() in {"1", "true", "yes", "on"}

    pending_jobs = (
        JobPosting.objects.filter(
            is_manual_import=True,
            status=JobPosting.Status.RECRUITERS_PENDING,
        )
        .select_related("company_ref")
    )

    stats = {
        "total_jobs": 0,
        "synced": 0,
        "no_recruiters": 0,
        "already_had_targets": 0,
        "errors": 0,
        "synced_job_ids": [],
    }

    for job in pending_jobs:
        stats["total_jobs"] += 1
        try:
            sync_result = sync_job_targets_for_job(
                job=job,
                max_targets=max_targets,
                auto_select=auto_select,
            )
            upserted = sync_result.get("targets_upserted", 0)
            if upserted:
                job.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
                job.save(update_fields=["status", "updated_at"])
                stats["synced"] += 1
                stats["synced_job_ids"].append(int(job.id))
            else:
                stats["no_recruiters"] += 1
        except Exception as exc:
            stats["errors"] += 1
            log_system_event(
                event_type="manual_sync_legacy_error",
                message=f"sync_manual_jobs_with_existing_recruiters job_id={job.id} error={exc}",
            )

    return stats
