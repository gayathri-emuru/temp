import os
import math
import re
from datetime import timedelta

from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Count
from django.utils import timezone

from core.constants import DEFAULT_APIFY_ACTOR_ID
from core.models import BlacklistedCompany, Company, DailyBatch, JobPosting, SentEmailLog
from core.services.apify_service import estimate_apify_dataset_cost_usd, fetch_jobs_from_apify_with_rotation, flatten_apify_job
from core.services.company_blacklist_service import build_company_blacklist_lookup, find_blacklisted_company_name
from core.services.dedupe_service import find_duplicate_reason, get_prior_7d_company_roles
from core.services.company_cooldown_service import DEFAULT_COMPANY_COOLDOWN_DAYS, should_skip_new_job_for_company
from core.services.job_target_sync_service import sync_job_targets_for_job
from core.services.linkedin_company_from_job_service import get_company_linkedin_url_from_job_url
from core.services.linkedin_people_url_service import generate_linkedin_people_search_data
from core.services.logging_service import console_log, log_system_event
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
from core.services.company_resolution_service import resolve_company_normalized_name
from core.services.openai_company_normalization_service import normalize_company_name_with_gpt
from core.services.openai_filter_service import classify_job_apply_or_reject
from core.utils import normalize_generic_url, normalize_job_or_generic_url, normalize_linkedin_job_url, safe_str


JOB_STATUS_TRACE_LIMIT = 500
HIGH_VOLUME_BATCH_SIZE = 10
HIGH_VOLUME_TARGET_CREATED_JOBS = 120
HIGH_VOLUME_MAX_RUNS = 40
APIFY_DYNAMIC_EXCLUSION_CAP = 1000
APIFY_SLUG_EXCLUSION_CAP = 1000
HARD_EXPERIENCE_REJECT_YEARS = 4
_EXPERIENCE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:years?|yrs?)\b.{0,80}\b(?:experience|exp|professional|relevant|hands[-\s]?on)\b"
    r"|\b(?:experience|exp|professional|relevant|hands[-\s]?on)\b.{0,80}\b(?:years?|yrs?)\b"
)
_PREFERRED_EXPERIENCE_RE = re.compile(r"(?i)\b(?:preferred|nice\s+to\s+have|plus|bonus|desired)\b")
_HARD_EXPERIENCE_PATTERNS = [
    re.compile(r"(?i)\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\b.{0,80}\b(?:experience|exp)\b"),
    re.compile(r"(?i)\b(?:experience|exp)\b.{0,80}\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\b"),
    re.compile(r"(?i)\b(?:minimum|min\.?|at\s+least|required|requires?|must\s+have)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b.{0,80}\b(?:experience|exp)\b"),
    re.compile(r"(?i)\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\b.{0,80}\b(?:experience|exp)\b"),
    re.compile(r"(?i)\b(?:experience|exp)\b.{0,80}\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\b"),
    re.compile(r"(?i)(?<![-–—to]\s)\b(\d{1,2})\s*(?:years?|yrs?)\b.{0,80}\b(?:experience|exp)\b"),
    re.compile(r"(?i)\b(?:experience|exp)\b.{0,80}\b(\d{1,2})\s*(?:years?|yrs?)\b"),
]


def _company_exclusion_term(value: str) -> str:
    text = safe_str(value).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    alnum = re.sub(r"[^a-z0-9]", "", text.lower())
    if len(alnum) < 4:
        return ""
    if text.endswith(":*"):
        return text
    return f"{text}:*"


def _add_exclusion_term(terms: list, seen: set, value: str) -> None:
    term = _company_exclusion_term(value)
    if not term:
        return
    key = term.lower()
    if key in seen:
        return
    seen.add(key)
    terms.append(term)


def _add_slug(slugs: list, seen: set, value: str) -> None:
    slug = safe_str(value).strip().strip("/").lower()
    if not slug:
        return
    if "/" in slug:
        slug = slug.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    if not slug or slug in seen:
        return
    seen.add(slug)
    slugs.append(slug)


def hard_reject_experience_requirement(description: str) -> str:
    text = safe_str(description)
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text)

    for pattern in _HARD_EXPERIENCE_PATTERNS:
        for match in pattern.finditer(compact):
            groups = [g for g in match.groups() if g is not None]
            if not groups:
                continue
            before = compact[max(0, match.start() - 6):match.start()]
            if re.search(r"(?:-|–|—|to)\s*$", before, flags=re.I):
                continue
            try:
                min_years = int(groups[0])
            except Exception:
                continue
            if min_years < HARD_EXPERIENCE_REJECT_YEARS:
                continue

            start = max(0, match.start() - 80)
            end = min(len(compact), match.end() + 80)
            window = compact[start:end]
            if not _EXPERIENCE_CONTEXT_RE.search(window):
                continue
            if _PREFERRED_EXPERIENCE_RE.search(window):
                continue
            return f"required_experience_min_{min_years}_years: {match.group(0).strip()[:180]}"

    return ""


def build_dynamic_apify_exclusions(*, batch_date=None, cooldown_days: int = DEFAULT_COMPANY_COOLDOWN_DAYS) -> dict:
    batch_date = batch_date or timezone.localdate()
    cutoff_date = batch_date - timedelta(days=int(cooldown_days or DEFAULT_COMPANY_COOLDOWN_DAYS))
    send_cutoff = timezone.now() - timedelta(days=int(cooldown_days or DEFAULT_COMPANY_COOLDOWN_DAYS))

    terms = []
    term_seen = set()
    slugs = []
    slug_seen = set()
    cooldown_days = max(0, int(cooldown_days or 0))

    blacklisted = (
        BlacklistedCompany.objects
        .select_related("company")
        .order_by("-updated_at", "normalized_name")
    )
    for row in blacklisted:
        _add_exclusion_term(terms, term_seen, row.raw_name_latest)
        _add_exclusion_term(terms, term_seen, row.normalized_name)
        if row.company_id and row.company:
            _add_exclusion_term(terms, term_seen, row.company.raw_name_latest)
            _add_exclusion_term(terms, term_seen, row.company.normalized_name)
            _add_slug(slugs, slug_seen, row.company.linkedin_company_slug)

    if cooldown_days > 0:
        recent_companies = (
            Company.objects
            .filter(jobs__daily_batch__batch_date__gte=cutoff_date, jobs__daily_batch__batch_date__lte=batch_date)
            .distinct()
            .order_by("-updated_at", "normalized_name")
        )
        for company in recent_companies:
            _add_exclusion_term(terms, term_seen, company.raw_name_latest)
            _add_exclusion_term(terms, term_seen, company.normalized_name)
            _add_slug(slugs, slug_seen, company.linkedin_company_slug)

        recent_sent_companies = (
            Company.objects
            .filter(jobs__sent_logs__send_type=SentEmailLog.SendType.REAL)
            .filter(jobs__sent_logs__status=SentEmailLog.SendStatus.SENT)
            .filter(jobs__sent_logs__message_type=SentEmailLog.MessageType.INITIAL)
            .filter(jobs__sent_logs__sent_at__gte=send_cutoff)
            .distinct()
            .order_by("-updated_at", "normalized_name")
        )
        for company in recent_sent_companies:
            _add_exclusion_term(terms, term_seen, company.raw_name_latest)
            _add_exclusion_term(terms, term_seen, company.normalized_name)
            _add_slug(slugs, slug_seen, company.linkedin_company_slug)

    full_cap_companies = (
        Company.objects
        .filter(jobs__sent_logs__send_type=SentEmailLog.SendType.REAL)
        .filter(jobs__sent_logs__status=SentEmailLog.SendStatus.SENT)
        .filter(jobs__sent_logs__message_type=SentEmailLog.MessageType.INITIAL)
        .annotate(real_initial_sent_count=Count("jobs__sent_logs", distinct=True))
        .filter(real_initial_sent_count__gte=10)
        .order_by("-updated_at", "normalized_name")
    )
    for company in full_cap_companies:
        _add_exclusion_term(terms, term_seen, company.raw_name_latest)
        _add_exclusion_term(terms, term_seen, company.normalized_name)
        _add_slug(slugs, slug_seen, company.linkedin_company_slug)

    return {
        "organization_exclusion_search": terms[:APIFY_DYNAMIC_EXCLUSION_CAP],
        "organization_slug_exclusion_filter": slugs[:APIFY_SLUG_EXCLUSION_CAP],
        "uncapped_organization_exclusion_count": len(terms),
        "uncapped_slug_exclusion_count": len(slugs),
    }


def build_today_apify_exclusions(*, batch_date=None) -> dict:
    batch_date = batch_date or timezone.localdate()
    terms = []
    term_seen = set()
    slugs = []
    slug_seen = set()

    today_companies = (
        Company.objects
        .filter(jobs__daily_batch__batch_date=batch_date)
        .distinct()
        .order_by("-updated_at", "normalized_name")
    )
    for company in today_companies:
        _add_exclusion_term(terms, term_seen, company.raw_name_latest)
        _add_exclusion_term(terms, term_seen, company.normalized_name)
        _add_slug(slugs, slug_seen, company.linkedin_company_slug)

    return {
        "organization_exclusion_search": terms[:APIFY_DYNAMIC_EXCLUSION_CAP],
        "organization_slug_exclusion_filter": slugs[:APIFY_SLUG_EXCLUSION_CAP],
        "uncapped_organization_exclusion_count": len(terms),
        "uncapped_slug_exclusion_count": len(slugs),
    }


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

    # Avoid collapsing to overly-short strings accidentally (except common acronyms).
    if len(suggested) <= 2 and suggested not in {"ibm", "hp", "3m", "gm", "ge"}:
        return base

    return suggested


def _safe_get(flat: dict, key: str, default: str = "") -> str:
    if not isinstance(flat, dict):
        return default
    value = flat.get(key, default)
    if value is None:
        return default
    return str(value)


def _add_job_status(stats: dict, flat: dict, status: str, detail: str = ""):
    if len(stats["job_status_rows"]) >= JOB_STATUS_TRACE_LIMIT:
        return

    stats["job_status_rows"].append({
        "company": _safe_get(flat, "company", "[unknown company]"),
        "title": _safe_get(flat, "title", "[unknown title]"),
        "location": _safe_get(flat, "location", ""),
        "linkedin_url": _safe_get(flat, "linkedin_url", ""),
        "status": status,
        "detail": detail,
    })


def _reject_blacklisted_job(stats: dict, flat: dict, matched_company: str):
    stats["rejected_jobs"] += 1
    stats["rejected_blacklisted_companies"] += 1
    log_system_event(
        event_type="filtered_reject",
        message=(
            f"Rejected blacklisted company from Apify import ({matched_company}): "
            f"{flat['company']} | {flat['title']} | {flat['location']}"
        ),
    )
    _add_job_status(stats, flat, "rejected_blacklisted_company", matched_company)


def run_import_pipeline(
    lookback_hours: int,
    max_jobs: int,
    actor_id: str = DEFAULT_APIFY_ACTOR_ID,
    organization_exclusion_search=None,
    organization_slug_exclusion_filter=None,
    skip_duplicate_companies_in_run: bool = True,
):
    batch_date = timezone.localdate()
    from core.services.app_settings_service import get_company_cooldown_days

    cooldown_days = get_company_cooldown_days()

    daily_batch, _ = DailyBatch.objects.get_or_create(
        batch_date=batch_date,
        defaults={
            "lookback_hours": lookback_hours,
            "max_jobs_requested": max_jobs,
            "apify_run_status": DailyBatch.RunStatus.PENDING,
        },
    )

    daily_batch.lookback_hours = lookback_hours
    daily_batch.max_jobs_requested = max_jobs
    daily_batch.apify_run_status = DailyBatch.RunStatus.RUNNING
    daily_batch.import_started_at = timezone.now()
    daily_batch.import_finished_at = None
    daily_batch.notes = ""
    daily_batch.save()

    stats = {
        "ok": True,
        "error": "",
        "raw_jobs": 0,
        "apply_jobs": 0,
        "rejected_jobs": 0,
        "rejected_blacklisted_companies": 0,
        "deduped_jobs": 0,
        "created_jobs": 0,
        "job_errors": 0,
        "skipped_missing_url": 0,
        "skipped_missing_core_fields": 0,
        "skipped_duplicate_company_in_run": 0,
        "hard_rejected_experience": 0,
        "apify_dynamic_exclusion_terms": len(organization_exclusion_search or []),
        "apify_dynamic_slug_exclusions": len(organization_slug_exclusion_filter or []),
        "apify_estimated_dataset_cost_usd": 0.0,
        "apify_reported_usage_total_usd": None,
        "apify_run_id": "",
        "apify_dataset_id": "",
        "apify_pricing_note": "$1.50 per 1,000 returned dataset jobs",
        "job_error_samples": [],
        "job_status_rows": [],
    }
    seen_companies_this_run = set()

    try:
        try:
            raw_jobs, apify_key_obj, apify_metadata = fetch_jobs_from_apify_with_rotation(
                lookback_hours=lookback_hours,
                max_jobs=max_jobs,
                actor_id=actor_id,
                organization_exclusion_search=organization_exclusion_search,
                organization_slug_exclusion_filter=organization_slug_exclusion_filter,
            )
        except Exception as exc:
            stats["ok"] = False
            stats["error"] = str(exc)[:4000]

            daily_batch.apify_run_status = DailyBatch.RunStatus.FAILED
            daily_batch.import_finished_at = timezone.now()
            daily_batch.notes = stats["error"]
            daily_batch.save()

            log_system_event(
                event_type="failed",
                message=f"Import pipeline failed for batch {batch_date}: {exc}",
            )
            return stats

        stats["raw_jobs"] = len(raw_jobs)
        stats["apify_estimated_dataset_cost_usd"] = estimate_apify_dataset_cost_usd(len(raw_jobs))
        stats["apify_reported_usage_total_usd"] = apify_metadata.get("usage_total_usd")
        stats["apify_run_id"] = safe_str(apify_metadata.get("run_id"))
        stats["apify_dataset_id"] = safe_str(apify_metadata.get("dataset_id"))
        blacklist_lookup = build_company_blacklist_lookup()

        for raw_job in raw_jobs:
            flat = {}

            try:
                flat = flatten_apify_job(raw_job)

                flat["linkedin_url"] = normalize_linkedin_job_url(flat["linkedin_url"])
                flat["apply_url"] = normalize_job_or_generic_url(flat["apply_url"])
                flat["apify_linkedin_org_url"] = normalize_generic_url(flat.get("apify_linkedin_org_url", ""))
                flat["location"] = flat.get("location") or "United States"

                if not flat["linkedin_url"]:
                    stats["skipped_missing_url"] += 1
                    _add_job_status(stats, flat, "skipped_missing_url", "linkedin_url missing after normalization")
                    continue

                if not flat["company"] or not flat["title"]:
                    stats["skipped_missing_core_fields"] += 1
                    _add_job_status(stats, flat, "skipped_missing_core_fields", "company or title missing")
                    continue

                current_run_company_key = canonical_company_name(flat["company"]) or normalize_company_name(flat["company"])
                if skip_duplicate_companies_in_run and current_run_company_key:
                    if current_run_company_key in seen_companies_this_run:
                        stats["deduped_jobs"] += 1
                        stats["skipped_duplicate_company_in_run"] += 1
                        log_system_event(
                            event_type="deduped",
                            message=(
                                f"Skipped duplicate company inside Apify run before OpenAI: "
                                f"{flat['company']} | {flat['title']} | {flat['location']}"
                            ),
                        )
                        _add_job_status(stats, flat, "skipped_duplicate_company_in_run", current_run_company_key)
                        continue
                    seen_companies_this_run.add(current_run_company_key)

                blacklisted_company = find_blacklisted_company_name(
                    raw_company=flat["company"],
                    lookup=blacklist_lookup,
                )
                if blacklisted_company:
                    _reject_blacklisted_job(stats, flat, blacklisted_company)
                    continue

                experience_reject_reason = hard_reject_experience_requirement(flat["description"])
                if experience_reject_reason:
                    stats["rejected_jobs"] += 1
                    stats["hard_rejected_experience"] += 1
                    log_system_event(
                        event_type="filtered_reject",
                        message=(
                            f"Rejected job by hard experience rule ({experience_reject_reason}): "
                            f"{flat['company']} | {flat['title']} | {flat['location']}"
                        ),
                    )
                    _add_job_status(stats, flat, "rejected_experience", experience_reject_reason)
                    continue

                decision = classify_job_apply_or_reject(
                    title=flat["title"],
                    description=flat["description"],
                )

                if decision != "APPLY":
                    stats["rejected_jobs"] += 1

                    log_system_event(
                        event_type="filtered_reject",
                        message=f"Rejected job: {flat['company']} | {flat['title']} | {flat['location']}",
                    )

                    _add_job_status(stats, flat, "rejected", f"classifier decision={decision}")
                    continue

                stats["apply_jobs"] += 1

                normalized_company = _normalize_company_best_effort(flat["company"])
                blacklisted_company = find_blacklisted_company_name(
                    raw_company=flat["company"],
                    normalized_company=normalized_company,
                    lookup=blacklist_lookup,
                )
                if blacklisted_company:
                    _reject_blacklisted_job(stats, flat, blacklisted_company)
                    continue
                normalized_title = normalize_title(flat["title"])
                normalized_location = normalize_location(flat["location"] or "United States")
                if not normalized_location:
                    normalized_location = "united states"

                # Resolve company name BEFORE computing canonical keys so acronym/fuzzy merges
                # ("aws" -> "amazon web services") share the same cooldown/dedupe keys.
                pre_resolved_company_name, _ = resolve_company_normalized_name(
                    normalized_company_name=normalized_company,
                    company_linkedin_url="",
                )

                stored_normalized_company = pre_resolved_company_name or normalized_company or normalize_company_name(flat["company"])
                canonical_company = canonical_company_name(stored_normalized_company or flat["company"])
                blacklisted_company = find_blacklisted_company_name(
                    raw_company=flat["company"],
                    normalized_company=stored_normalized_company,
                    canonical_company=canonical_company,
                    lookup=blacklist_lookup,
                )
                if blacklisted_company:
                    _reject_blacklisted_job(stats, flat, blacklisted_company)
                    continue
                canonical_title_value = canonical_title(flat["title"])
                canonical_location_value = canonical_location(flat["location"] or "United States")

                skip, skip_reason = should_skip_new_job_for_company(
                    canonical_company=canonical_company,
                    batch_date=batch_date,
                    cooldown_days=cooldown_days,
                )
                if skip:
                    stats["deduped_jobs"] += 1
                    log_system_event(
                        event_type="deduped",
                        message=(
                            f"Skipped job due to company cooldown ({skip_reason}): "
                            f"{flat['company']} | {flat['title']} | {flat['location']}"
                        ),
                    )
                    _add_job_status(stats, flat, "skipped_company_cooldown", skip_reason)
                    continue

                dedupe_key = build_dedupe_key(
                    stored_normalized_company or flat["company"],
                    flat["title"],
                    flat["location"] or "United States",
                )

                duplicate_reason = find_duplicate_reason(
                    normalized_linkedin_url=flat["linkedin_url"],
                    normalized_apply_url=flat["apply_url"],
                    canonical_company=canonical_company,
                    canonical_title=canonical_title_value,
                    canonical_location=canonical_location_value,
                )

                if duplicate_reason:
                    stats["deduped_jobs"] += 1

                    log_system_event(
                        event_type="deduped",
                        message=(
                            f"Skipped duplicate APPLY job by {duplicate_reason}: "
                            f"{flat['company']} | {flat['title']} | {flat['location']}"
                        ),
                    )

                    _add_job_status(stats, flat, "deduped", duplicate_reason)
                    continue

                # IMPORTANT:
                # Never use Apify for LinkedIn company page resolution.
                # Always scrape the LinkedIn company page from the LinkedIn job page.
                scraped_company_linkedin_url = get_company_linkedin_url_from_job_url(
                    job_url=flat["linkedin_url"],
                    expected_company_name=flat["company"],
                )

                # Re-resolve with scraped LinkedIn company URL when available.
                resolved_company_name, linkedin_company_slug = resolve_company_normalized_name(
                    normalized_company_name=normalized_company,
                    company_linkedin_url=scraped_company_linkedin_url,
                )
                stored_normalized_company = resolved_company_name or stored_normalized_company
                canonical_company = canonical_company_name(stored_normalized_company or flat["company"])
                blacklisted_company = find_blacklisted_company_name(
                    raw_company=flat["company"],
                    normalized_company=stored_normalized_company,
                    canonical_company=canonical_company,
                    lookup=blacklist_lookup,
                )
                if blacklisted_company:
                    _reject_blacklisted_job(stats, flat, blacklisted_company)
                    continue

                # Final cooldown guard after stronger company resolution.
                skip, skip_reason = should_skip_new_job_for_company(
                    canonical_company=canonical_company,
                    batch_date=batch_date,
                    cooldown_days=cooldown_days,
                )
                if skip:
                    stats["deduped_jobs"] += 1
                    log_system_event(
                        event_type="deduped",
                        message=(
                            f"Skipped job due to company cooldown ({skip_reason}): "
                            f"{flat['company']} | {flat['title']} | {flat['location']}"
                        ),
                    )
                    _add_job_status(stats, flat, "skipped_company_cooldown", f"post_resolve:{skip_reason}")
                    continue

                # Re-check duplicates using the final canonical_company (may differ after acronym/slug resolution).
                duplicate_reason = find_duplicate_reason(
                    normalized_linkedin_url=flat["linkedin_url"],
                    normalized_apply_url=flat["apply_url"],
                    canonical_company=canonical_company,
                    canonical_title=canonical_title_value,
                    canonical_location=canonical_location_value,
                )
                if duplicate_reason:
                    stats["deduped_jobs"] += 1
                    log_system_event(
                        event_type="deduped",
                        message=(
                            f"Skipped duplicate APPLY job by {duplicate_reason}: "
                            f"{flat['company']} | {flat['title']} | {flat['location']}"
                        ),
                    )
                    _add_job_status(stats, flat, "deduped", f"post_resolve:{duplicate_reason}")
                    continue

                prior_roles = get_prior_7d_company_roles(stored_normalized_company, batch_date)

                linkedin_geo_region_id, linkedin_people_search_urls = generate_linkedin_people_search_data(
                    company_linkedin_url=scraped_company_linkedin_url,
                    normalized_state=normalized_location,
                )

                with transaction.atomic():
                    company_obj, created = Company.objects.get_or_create(
                        normalized_name=stored_normalized_company,
                        defaults={
                            "raw_name_latest": flat["company"],
                        },
                    )

                    if not created and company_obj.raw_name_latest != flat["company"]:
                        company_obj.raw_name_latest = flat["company"]
                        company_obj.save(update_fields=["raw_name_latest", "updated_at"])

                    # Prefer scraped slug; fall back to Apify slug only if needed.
                    apify_slug = safe_str(flat.get("apify_linkedin_org_slug")).strip().lower()
                    chosen_slug = safe_str(linkedin_company_slug).strip().lower() or apify_slug
                    if chosen_slug and safe_str(company_obj.linkedin_company_slug).strip().lower() != chosen_slug:
                        # Set/refresh LinkedIn slug when available to strengthen future dedupe.
                        company_obj.linkedin_company_slug = chosen_slug
                        company_obj.save(update_fields=["linkedin_company_slug", "updated_at"])

                    try:
                        job_posting = JobPosting.objects.create(
                            daily_batch=daily_batch,
                            company_ref=company_obj,
                            external_job_id=flat["external_job_id"],
                            linkedin_url=flat["linkedin_url"],
                            apply_url=flat["apply_url"],
                            normalized_linkedin_url=flat["linkedin_url"],
                            normalized_apply_url=flat["apply_url"],
                            title=flat["title"],
                            company=flat["company"],
                            location=flat["location"],
                            salary=flat["salary"],
                            description=flat["description"],
                            description_fingerprint=build_description_fingerprint(flat["description"]),
                            apify_linkedin_org_url=flat["apify_linkedin_org_url"],
                            apify_linkedin_org_slug=apify_slug,
                            company_linkedin=scraped_company_linkedin_url,
                            recruiter_name=flat["recruiter_name"],
                            recruiter_title=flat["recruiter_title"],
                            recruiter_linkedin=flat["recruiter_linkedin"],
                            ai_hiring_mgr_name=flat["ai_hiring_mgr_name"],
                            ai_hiring_mgr_email=flat["ai_hiring_mgr_email"],
                            normalized_company=stored_normalized_company,
                            normalized_title=normalized_title,
                            normalized_location=normalized_location,
                            canonical_company=canonical_company,
                            canonical_title=canonical_title_value,
                            canonical_location=canonical_location_value,
                            dedupe_key=dedupe_key,
                            sort_company=build_sort_company(flat["company"]),
                            sort_title=build_sort_title(flat["title"]),
                            sort_location=build_sort_location(flat["location"]),
                            prior_7d_company_roles=prior_roles,
                            linkedin_geo_region_id=linkedin_geo_region_id,
                            linkedin_people_search_urls=linkedin_people_search_urls,
                            status=JobPosting.Status.RECRUITERS_PENDING,
                        )
                    except IntegrityError:
                        stats["deduped_jobs"] += 1
                        _add_job_status(stats, flat, "deduped", "db_unique_constraint")
                        continue

                    # If we already have recruiter emails for this company (directory import),
                    # create job targets immediately to make the job send-ready.
                    auto_select = os.getenv("AUTO_SELECT_TARGETS_ON_IMPORT", "1").strip().lower() in {"1", "true", "yes", "on"}
                    from core.services.app_settings_service import get_max_people_per_company
                    sync_stats = sync_job_targets_for_job(
                        job=job_posting,
                        max_targets=get_max_people_per_company(),
                        auto_select=auto_select,
                    )
                    if sync_stats.get("targets_upserted"):
                        job_posting.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
                        job_posting.save(update_fields=["status", "updated_at"])

                log_system_event(
                    event_type="filtered_apply",
                    message=f"Accepted job: {job_posting.company} | {job_posting.title} | {job_posting.location}",
                    job_posting=job_posting,
                )
                log_system_event(
                    event_type="imported",
                    message=f"Imported APPLY job into batch {batch_date}",
                    job_posting=job_posting,
                )

                if linkedin_people_search_urls:
                    _add_job_status(
                        stats,
                        flat,
                        "created",
                        f"people_urls={len(linkedin_people_search_urls)} geo_id={linkedin_geo_region_id} company_linkedin_source=scraped",
                    )
                else:
                    _add_job_status(
                        stats,
                        flat,
                        "created_no_people_urls",
                        f"geo_id={linkedin_geo_region_id} company_linkedin_source=scraped_missing_or_invalid",
                    )

                stats["created_jobs"] += 1

            except Exception as job_exc:
                stats["job_errors"] += 1

                error_message = (
                    f"{flat.get('company', '[unknown company]')} | "
                    f"{flat.get('title', '[unknown title]')} | "
                    f"{str(job_exc)}"
                )

                if len(stats["job_error_samples"]) < 20:
                    stats["job_error_samples"].append(error_message)

                print(f"JOB ERROR: {error_message}", flush=True)

                log_system_event(
                    event_type="failed",
                    message=f"Job import failed for batch {batch_date}: {error_message}",
                )

                _add_job_status(stats, flat, "error", str(job_exc))
                continue

        daily_batch.apify_run_status = DailyBatch.RunStatus.SUCCESS
        daily_batch.import_finished_at = timezone.now()
        daily_batch.notes = f"Import completed using Apify key: {apify_key_obj.key_name}"
        daily_batch.save()

        return stats

    except Exception as exc:
        stats["ok"] = False
        stats["error"] = str(exc)[:4000]

        daily_batch.apify_run_status = DailyBatch.RunStatus.FAILED
        daily_batch.import_finished_at = timezone.now()
        daily_batch.notes = stats["error"]
        daily_batch.save()

        log_system_event(
            event_type="failed",
            message=f"Import pipeline failed for batch {batch_date}: {exc}",
        )
        return stats


def run_high_volume_unique_company_import(
    *,
    lookback_hours: int,
    target_created_jobs: int = HIGH_VOLUME_TARGET_CREATED_JOBS,
    actor_id: str = DEFAULT_APIFY_ACTOR_ID,
    batch_size: int = HIGH_VOLUME_BATCH_SIZE,
    max_runs: int = HIGH_VOLUME_MAX_RUNS,
    exclusion_mode: str = "full",
) -> dict:
    target_created_jobs = max(101, int(target_created_jobs or HIGH_VOLUME_TARGET_CREATED_JOBS))
    batch_size = max(10, int(batch_size or HIGH_VOLUME_BATCH_SIZE))
    max_runs = max(1, int(max_runs or HIGH_VOLUME_MAX_RUNS))
    batch_date = timezone.localdate()

    exclusion_mode = safe_str(exclusion_mode).strip().lower() or "full"
    if exclusion_mode == "today":
        dynamic = build_today_apify_exclusions(batch_date=batch_date)
    else:
        exclusion_mode = "full"
        dynamic = build_dynamic_apify_exclusions(batch_date=batch_date)
    organization_exclusions = list(dynamic["organization_exclusion_search"])
    slug_exclusions = list(dynamic["organization_slug_exclusion_filter"])
    exclusion_seen = {safe_str(x).strip().lower() for x in organization_exclusions if safe_str(x).strip()}

    totals = {
        "ok": True,
        "error": "",
        "mode": "high_volume_unique_company",
        "exclusion_mode": exclusion_mode,
        "batch_date": batch_date.isoformat(),
        "lookback_hours": lookback_hours,
        "target_created_jobs": target_created_jobs,
        "batch_size": batch_size,
        "max_runs": max_runs,
        "runs_attempted": 0,
        "raw_jobs": 0,
        "apply_jobs": 0,
        "rejected_jobs": 0,
        "rejected_blacklisted_companies": 0,
        "deduped_jobs": 0,
        "created_jobs": 0,
        "job_errors": 0,
        "skipped_missing_url": 0,
        "skipped_missing_core_fields": 0,
        "skipped_duplicate_company_in_run": 0,
        "hard_rejected_experience": 0,
        "apify_estimated_dataset_cost_usd": 0.0,
        "apify_reported_usage_total_usd": 0.0,
        "initial_dynamic_exclusion_terms": len(organization_exclusions),
        "initial_dynamic_slug_exclusions": len(slug_exclusions),
        "final_dynamic_exclusion_terms": len(organization_exclusions),
        "final_dynamic_slug_exclusions": len(slug_exclusions),
        "empty_runs": 0,
        "run_rows": [],
    }

    console_log(
        (
            "HIGH_VOLUME_START "
            f"mode={exclusion_mode} batch_date={batch_date.isoformat()} target_created={target_created_jobs} "
            f"batch_size={batch_size} max_runs={max_runs} lookback_hours={lookback_hours} "
            f"initial_exclusions={len(organization_exclusions)} initial_slug_exclusions={len(slug_exclusions)}"
        )
    )

    for run_number in range(1, max_runs + 1):
        if totals["created_jobs"] >= target_created_jobs:
            console_log(
                f"HIGH_VOLUME_STOP reason=target_reached total_created={totals['created_jobs']} target={target_created_jobs}"
            )
            break

        console_log(
            (
                "HIGH_VOLUME_RUN_START "
                f"run={run_number}/{max_runs} total_created={totals['created_jobs']}/{target_created_jobs} "
                f"exclusions={len(organization_exclusions)} slug_exclusions={len(slug_exclusions)}"
            )
        )

        result = run_import_pipeline(
            lookback_hours=lookback_hours,
            max_jobs=batch_size,
            actor_id=actor_id,
            organization_exclusion_search=organization_exclusions,
            organization_slug_exclusion_filter=slug_exclusions,
            skip_duplicate_companies_in_run=True,
        )
        totals["runs_attempted"] += 1

        for key in [
            "raw_jobs",
            "apply_jobs",
            "rejected_jobs",
            "rejected_blacklisted_companies",
            "deduped_jobs",
            "created_jobs",
            "job_errors",
            "skipped_missing_url",
            "skipped_missing_core_fields",
            "skipped_duplicate_company_in_run",
            "hard_rejected_experience",
        ]:
            totals[key] += int(result.get(key) or 0)
        totals["apify_estimated_dataset_cost_usd"] = round(
            float(totals["apify_estimated_dataset_cost_usd"] or 0)
            + float(result.get("apify_estimated_dataset_cost_usd") or 0),
            4,
        )
        if result.get("apify_reported_usage_total_usd") is not None:
            totals["apify_reported_usage_total_usd"] = round(
                float(totals["apify_reported_usage_total_usd"] or 0)
                + float(result.get("apify_reported_usage_total_usd") or 0),
                4,
            )

        if not result.get("ok"):
            totals["ok"] = False
            totals["error"] = result.get("error", "")
            console_log(
                (
                    "HIGH_VOLUME_RUN_FAIL "
                    f"run={run_number}/{max_runs} error={safe_str(totals['error'])[:500]}"
                ),
                level="ERROR",
            )
            break

        returned_companies = []
        new_exclusions_this_run = 0
        for row in result.get("job_status_rows") or []:
            company_name = safe_str(row.get("company")).strip()
            if not company_name or company_name == "[unknown company]":
                continue
            returned_companies.append(company_name)
            term = _company_exclusion_term(company_name)
            if term and term.lower() not in exclusion_seen and len(organization_exclusions) < APIFY_DYNAMIC_EXCLUSION_CAP:
                exclusion_seen.add(term.lower())
                organization_exclusions.append(term)
                new_exclusions_this_run += 1

        if int(result.get("raw_jobs") or 0) <= 0:
            totals["empty_runs"] += 1
        else:
            totals["empty_runs"] = 0

        console_log(
            (
                "HIGH_VOLUME_RUN_DONE "
                f"run={run_number}/{max_runs} raw={int(result.get('raw_jobs') or 0)} "
                f"created={int(result.get('created_jobs') or 0)} apply={int(result.get('apply_jobs') or 0)} "
                f"rejected={int(result.get('rejected_jobs') or 0)} deduped={int(result.get('deduped_jobs') or 0)} "
                f"hard_exp={int(result.get('hard_rejected_experience') or 0)} "
                f"dup_company={int(result.get('skipped_duplicate_company_in_run') or 0)} "
                f"new_exclusions={new_exclusions_this_run} total_exclusions={len(organization_exclusions)} "
                f"total_created={totals['created_jobs']}/{target_created_jobs} empty_runs={totals['empty_runs']} "
                f"estimated_cost=${float(result.get('apify_estimated_dataset_cost_usd') or 0):.4f} "
                f"reported_usage=${float(result.get('apify_reported_usage_total_usd') or 0):.4f} "
                f"apify_run_id={safe_str(result.get('apify_run_id')) or '-'}"
            )
        )

        totals["run_rows"].append(
            {
                "run_number": run_number,
                "raw_jobs": int(result.get("raw_jobs") or 0),
                "created_jobs": int(result.get("created_jobs") or 0),
                "apply_jobs": int(result.get("apply_jobs") or 0),
                "deduped_jobs": int(result.get("deduped_jobs") or 0),
                "rejected_jobs": int(result.get("rejected_jobs") or 0),
                "skipped_duplicate_company_in_run": int(result.get("skipped_duplicate_company_in_run") or 0),
                "hard_rejected_experience": int(result.get("hard_rejected_experience") or 0),
                "apify_estimated_dataset_cost_usd": result.get("apify_estimated_dataset_cost_usd"),
                "apify_reported_usage_total_usd": result.get("apify_reported_usage_total_usd"),
                "apify_run_id": result.get("apify_run_id"),
                "returned_companies": sorted(set(returned_companies)),
                "exclusions_after_run": len(organization_exclusions),
            }
        )

        if totals["empty_runs"] >= 2:
            console_log("HIGH_VOLUME_STOP reason=two_empty_runs")
            break

    totals["final_dynamic_exclusion_terms"] = len(organization_exclusions)
    totals["final_dynamic_slug_exclusions"] = len(slug_exclusions)

    batch = DailyBatch.objects.filter(batch_date=batch_date).first()
    if batch:
        batch.max_jobs_requested = max(int(batch.max_jobs_requested or 0), totals["runs_attempted"] * batch_size)
        batch.notes = (
            f"High-volume unique-company import: runs={totals['runs_attempted']} "
            f"raw={totals['raw_jobs']} created={totals['created_jobs']} "
            f"dynamic_exclusions={totals['final_dynamic_exclusion_terms']}."
        )
        batch.save(update_fields=["max_jobs_requested", "notes", "updated_at"])

    console_log(
        (
            "HIGH_VOLUME_DONE "
            f"ok={totals['ok']} mode={exclusion_mode} runs={totals['runs_attempted']} raw={totals['raw_jobs']} "
            f"created={totals['created_jobs']} rejected={totals['rejected_jobs']} deduped={totals['deduped_jobs']} "
            f"hard_exp={totals['hard_rejected_experience']} final_exclusions={totals['final_dynamic_exclusion_terms']} "
            f"estimated_cost=${float(totals['apify_estimated_dataset_cost_usd'] or 0):.4f} "
            f"reported_usage=${float(totals['apify_reported_usage_total_usd'] or 0):.4f} "
            f"error={safe_str(totals['error'])[:500] or '-'}"
        ),
        level="SUCCESS" if totals["ok"] else "ERROR",
    )

    return totals
