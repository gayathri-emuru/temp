from __future__ import annotations

import os
import re
import time
from datetime import timedelta

import requests
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import ApolloRejectedEmail, Company, CompanyRecruiter, DailyBatch, JobPosting, JobRecruiterTarget, SentEmailLog
from core.services.app_settings_service import get_company_cooldown_days
from core.services.company_domain_service import is_usable_company_domain, normalize_domain_value
from core.services.file_run_logger import append_and_print, create_run_log_path, append_exception
from core.services.job_target_sync_service import sync_job_targets_for_company_pending_jobs
from core.services.normalization_service import normalize_person_name
from core.services.openai_location_service import extract_us_state_from_location
from core.services.recruiter_title_guard_service import (
    contact_title_rejection_reason,
    is_general_business_leadership_contact_title,
    is_data_science_manager_contact_title,
    is_domain_business_owner_contact_title,
    is_fallback_business_contact_title,
    is_last_resort_technical_contact_title,
    is_recruiting_or_hiring_contact_title,
)
from core.utils import safe_str


APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
APOLLO_SEARCH_URL = f"{APOLLO_BASE_URL}/mixed_people/api_search"
APOLLO_BULK_MATCH_URL = f"{APOLLO_BASE_URL}/people/bulk_match?reveal_personal_emails=false&reveal_phone_number=false"
APOLLO_MATCH_URL = f"{APOLLO_BASE_URL}/people/match"

APOLLO_TIMEOUT = 45
APOLLO_BACKOFF_SECS = 2

APOLLO_PEOPLE_PER_COMPANY = 20
DEFAULT_MAX_PEOPLE = APOLLO_PEOPLE_PER_COMPANY
DEFAULT_MAX_CREDITS_NOT_CONVERTED_PER_COMPANY = 0
DEFAULT_APOLLO_BULK_MATCH_BATCH_SIZE = 1
APOLLO_EMAIL_SKIP_STATUSES = {"unavailable", "invalid", "bounced", "spammy"}
DEFAULT_APOLLO_CATEGORY_CAP = 10
APOLLO_CATEGORY_RECRUITING = "recruiting"
APOLLO_CATEGORY_PEOPLE_HR = "people_hr"
APOLLO_CATEGORY_DATA = "data_analytics"
APOLLO_CATEGORY_FALLBACK = "fallback"

RECRUITING_PERSON_TITLES = [
    "recruiter",
    "technical recruiter",
    "senior recruiter",
    "corporate recruiter",
    "recruiting manager",
    "recruitment manager",
    "talent acquisition",
    "talent acquisition specialist",
    "talent acquisition partner",
    "talent acquisition manager",
    "talent partner",
    "talent specialist",
    "talent sourcer",
    "technical sourcer",
    "sourcer",
    "head of talent",
    "human resources",
    "hr",
    "hrbp",
    "people operations",
    "people partner",
    "staffing specialist",
    "staffing manager",
    "hiring manager",
    "hiring specialist",
    "hiring partner",
    "hiring lead",
    "hiring coordinator",
]

DATA_MANAGER_PERSON_TITLES = [
    "director of data science",
    "data science director",
    "senior director of data science",
    "associate director of data science",
    "manager of data science",
    "data science manager",
    "senior manager data science",
    "group manager data science",
    "principal manager data science",
    "machine learning manager",
    "manager of machine learning",
    "director of machine learning",
    "machine learning director",
    "senior director of machine learning",
    "ml engineering manager",
    "manager of ml engineering",
    "director of ml engineering",
    "machine learning engineering manager",
    "director of machine learning engineering",
    "ai manager",
    "director of ai",
    "artificial intelligence manager",
    "director of artificial intelligence",
    "applied ai manager",
    "director of applied ai",
    "applied science manager",
    "director of applied science",
    "research science manager",
    "director of research science",
    "data engineering manager",
    "manager of data engineering",
    "director of data engineering",
    "senior manager data engineering",
    "senior director data engineering",
    "analytics manager",
    "manager of analytics",
    "director of analytics",
    "data analytics manager",
    "director of data analytics",
    "business analytics manager",
    "director of business analytics",
    "product analytics manager",
    "director of product analytics",
    "decision science manager",
    "director of decision science",
    "business intelligence manager",
    "director of business intelligence",
    "bi manager",
    "director of bi",
    "insights manager",
    "director of insights",
    "data product manager",
    "director of data products",
    "data platform manager",
    "director of data platform",
    "data architecture manager",
    "director of data architecture",
    "data governance manager",
    "director of data governance",
]

DEFAULT_PERSON_TITLES = [
    *RECRUITING_PERSON_TITLES,
    *DATA_MANAGER_PERSON_TITLES,
]

FALLBACK_PERSON_TITLES = [
    "head of data science",
    "head of machine learning",
    "head of artificial intelligence",
    "head of ai",
    "head of data",
    "head of analytics",
    "head of data analytics",
    "head of business intelligence",
    "head of data engineering",
    "head of data platform",
    "head of applied science",
    "head of research science",
    "vp of data science",
    "vice president of data science",
    "vp of machine learning",
    "vice president of machine learning",
    "vp of artificial intelligence",
    "vice president of artificial intelligence",
    "vp of ai",
    "vice president of ai",
    "vp of data engineering",
    "vice president of data engineering",
    "vp of data",
    "vice president of data",
    "vp of analytics",
    "vice president of analytics",
    "vp of business intelligence",
    "vice president of business intelligence",
    "director of data",
    "data director",
    "senior director of data",
    "executive director of data",
    "manager of data",
    "data manager",
    "data platform manager",
    "director of data platform",
    "data products manager",
    "director of data products",
    "director of data strategy",
    "data strategy manager",
    "director of insights",
    "insights director",
    "director of advanced analytics",
    "advanced analytics manager",
    "director of predictive analytics",
    "predictive analytics manager",
]

LEADERSHIP_FALLBACK_PERSON_TITLES = [
    "chief data officer",
    "chief analytics officer",
    "chief ai officer",
    "chief artificial intelligence officer",
    "head of data science",
    "head of machine learning",
    "head of artificial intelligence",
    "head of ai",
    "head of data",
    "head of analytics",
    "head of data analytics",
    "head of data engineering",
    "head of business intelligence",
    "head of applied science",
    "head of research science",
    "director of data science",
    "director of machine learning",
    "director of ml engineering",
    "director of artificial intelligence",
    "director of ai",
    "director of applied science",
    "director of research science",
    "director of data engineering",
    "vp of data",
    "vice president of data",
    "vp of data science",
    "vice president of data science",
    "vp of machine learning",
    "vice president of machine learning",
    "vp of data engineering",
    "vice president of data engineering",
    "director of data",
    "vp of analytics",
    "vice president of analytics",
    "director of analytics",
    "vp of business intelligence",
    "vice president of business intelligence",
    "director of business intelligence",
    "director of data strategy",
]

LAST_RESORT_PERSON_TITLES = [
    "ceo",
    "chief executive officer",
    "founder",
    "co-founder",
    "cofounder",
    "technical founder",
    "cto",
    "chief technology officer",
    "head of engineering",
    "vp of engineering",
    "vice president of engineering",
    "director of engineering",
    "engineering manager",
    "software engineering manager",
    "data scientist",
    "senior data scientist",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "applied ai engineer",
    "applied scientist",
    "research scientist",
    "data engineer",
    "analytics engineer",
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "computer vision engineer",
    "robotics engineer",
    "robotics and mechatronics engineer",
    "automation engineer",
    "devops engineer",
    "cloudops engineer",
    "cloud engineer",
    "data analyst",
    "data quality analyst",
    "research analyst",
    "scientific researcher",
    "business intelligence analyst",
    "software developer",
    "backend developer",
    "back end developer",
    "frontend developer",
    "front end developer",
    "full stack developer",
    "tibco developer",
    "founding engineer",
    "founding applied ai engineer",
    "member of technical staff",
]

BROAD_FALLBACK_PERSON_TITLES = [
    "data scientist",
    "senior data scientist",
    "data engineer",
    "senior data engineer",
    "analytics engineer",
    "data analyst",
    "senior data analyst",
    "business intelligence analyst",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "applied scientist",
    "research scientist",
    "director of engineering",
    "engineering manager",
    "software engineering manager",
    "director of technology",
    "technology manager",
    "director of information technology",
    "information technology manager",
    "it manager",
    "solutions architect",
    "technical account manager",
    "director of product",
    "product manager",
    "senior product manager",
    "director of operations",
    "operations manager",
    "senior operations manager",
    "program manager",
    "senior program manager",
    "project manager",
    "senior project manager",
    "delivery manager",
    "implementation manager",
    "director of customer success",
    "customer success manager",
    "director of client services",
    "client services manager",
    "ceo",
    "chief executive officer",
    "founder",
    "co-founder",
    "cofounder",
]

DOMAIN_BUSINESS_OWNER_PERSON_TITLES = [
    "managing director",
    "senior managing director",
    "executive director",
    "director",
    "partner",
    "principal",
    "vice president",
    "vp",
    "member of executive committee",
    "head of investments",
    "head of investment",
    "head of secondaries",
    "head of primaries",
    "head of co-investment",
    "head of co-investments",
    "head of buyout",
    "head of private equity",
    "head of infrastructure",
    "head of credit",
    "head of portfolio",
    "head of portfolio operations",
    "head of investor relations",
    "head of client solutions",
    "head of strategy",
    "co-head of investments",
    "co-head of secondaries",
    "co-head of primaries",
    "co-head of co-investment",
    "co-head of co-investments",
    "deputy head of investments",
    "deputy head of co-investment",
    "investment manager",
    "senior investment manager",
    "investment director",
    "investment analyst",
    "investment associate",
    "private equity analyst",
    "private equity associate",
    "private equity director",
    "portfolio manager",
    "portfolio director",
    "portfolio operations manager",
    "fund manager",
    "fund of funds",
    "asset management director",
    "wealth management director",
    "private wealth director",
    "investor relations director",
    "investor relations analyst",
    "client solutions director",
    "client solutions associate",
    "strategy director",
    "corporate development director",
    "business operations director",
    "business operations manager",
    "analytics director",
    "research director",
]

DEFAULT_PERSON_NOT_TITLES = [
    "intern",
    "assistant",
    "attorney",
    "lawyer",
    "counsel",
    "chief financial officer",
    "cfo",
    "portfolio manager",
    "quantitative trader",
    "quantitative researcher",
    "sales",
    "account executive",
    "food safety",
    "esg",
]


def _apollo_people_limit() -> int:
    from core.services.app_settings_service import get_max_people_per_company
    return get_max_people_per_company()


def _max_credit_waste_per_company() -> int:
    try:
        return max(
            0,
            int(
                os.getenv(
                    "APOLLO_MAX_CREDITS_NOT_CONVERTED_PER_COMPANY",
                    str(DEFAULT_MAX_CREDITS_NOT_CONVERTED_PER_COMPANY),
                )
                or DEFAULT_MAX_CREDITS_NOT_CONVERTED_PER_COMPANY
            ),
        )
    except Exception:
        return DEFAULT_MAX_CREDITS_NOT_CONVERTED_PER_COMPANY


def _apollo_category_cap() -> int:
    try:
        return max(1, int(os.getenv("APOLLO_CATEGORY_CAP", str(DEFAULT_APOLLO_CATEGORY_CAP)) or DEFAULT_APOLLO_CATEGORY_CAP))
    except Exception:
        return DEFAULT_APOLLO_CATEGORY_CAP


def _apollo_bulk_match_batch_size() -> int:
    try:
        return max(1, min(10, int(os.getenv("APOLLO_BULK_MATCH_BATCH_SIZE", str(DEFAULT_APOLLO_BULK_MATCH_BATCH_SIZE)) or DEFAULT_APOLLO_BULK_MATCH_BATCH_SIZE)))
    except Exception:
        return DEFAULT_APOLLO_BULK_MATCH_BATCH_SIZE


def _get_apollo_api_key() -> str:
    key = os.getenv("APOLLO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("APOLLO_API_KEY is missing in .env (single-key mode).")
    return key


def _apollo_post(url: str, payload: dict) -> dict:
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "accept": "application/json",
        "x-api-key": _get_apollo_api_key(),
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=APOLLO_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _extract_people(payload: dict) -> list[dict]:
    """
    Best-effort parser for Apollo people search results.
    Apollo responses can vary by endpoint/version; we try common shapes.
    """
    if not isinstance(payload, dict):
        return []

    candidates = []

    # Common: {"people": [...]}
    people = payload.get("people")
    if isinstance(people, list):
        candidates.extend([p for p in people if isinstance(p, dict)])

    # Common: {"contacts": [...]}
    contacts = payload.get("contacts")
    if isinstance(contacts, list):
        candidates.extend([p for p in contacts if isinstance(p, dict)])

    # Common: {"persons": [...]}
    persons = payload.get("persons")
    if isinstance(persons, list):
        candidates.extend([p for p in persons if isinstance(p, dict)])

    # Nested: {"data": {"people": [...]}}
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("people", "contacts", "persons"):
            items = data.get(key)
            if isinstance(items, list):
                candidates.extend([p for p in items if isinstance(p, dict)])

    # De-dupe by id/name
    seen = set()
    output = []
    for person in candidates:
        pid = safe_str(person.get("id"))
        name = safe_str(person.get("name"))
        key = pid or name.lower().strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        output.append(person)
    return output


def search_people_from_apollo(
    *,
    domain: str,
    locations: list[str],
    max_people: int,
    person_titles: list[str] | None = None,
) -> list[dict]:
    domain = normalize_domain_value(domain)
    if not is_usable_company_domain(domain):
        return []

    per_page = max(1, min(int(max_people or 1) * 2, 50))

    cleaned_locations = [safe_str(x).strip() for x in (locations or []) if safe_str(x).strip()]
    if not cleaned_locations:
        cleaned_locations = ["United States"]

    payload = {
        "q_organization_domains_list": [domain],
        "person_locations": cleaned_locations,
        "person_titles": list(person_titles or DEFAULT_PERSON_TITLES),
        "person_not_titles": list(DEFAULT_PERSON_NOT_TITLES),
        "include_similar_titles": False,
        "has_email": True,
        # No person_seniorities: the title filter already asks for manager/director
        # data titles, and Apollo title matching is stricter than seniority labels.
        "per_page": per_page,
    }

    out = []
    seen: set[str] = set()

    for page_num in range(1, 3):  # Up to 2 pages — searches are free
        payload["page"] = page_num
        result = _apollo_post(APOLLO_SEARCH_URL, payload)
        page_people = _extract_people(result)
        for p in page_people:
            pid = safe_str(p.get("id")).strip()
            if pid and pid not in seen:
                seen.add(pid)
                out.append(p)
        # If Apollo returned fewer than a full page, there are no more results
        if len(page_people) < per_page:
            break

    return out


def search_company_people_from_apollo(
    *,
    domain: str,
    locations: list[str],
    max_people: int,
) -> list[dict]:
    domain = normalize_domain_value(domain)
    if not is_usable_company_domain(domain):
        return []

    max_people = max(1, min(int(max_people or 1), 50))
    cleaned_locations = [safe_str(x).strip() for x in (locations or []) if safe_str(x).strip()]
    if not cleaned_locations:
        cleaned_locations = ["United States"]

    payload = {
        "q_organization_domains_list": [domain],
        "person_locations": cleaned_locations,
        "person_not_titles": list(DEFAULT_PERSON_NOT_TITLES),
        "include_similar_titles": True,
        "has_email": True,
        "page": 1,
        "per_page": max_people,
    }

    result = _apollo_post(APOLLO_SEARCH_URL, payload)
    people = _extract_people(result)

    out = []
    seen = set()
    for p in people:
        pid = safe_str(p.get("id")).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(p)
        if len(out) >= max_people:
            break
    return out


def bulk_match_people_from_apollo(person_ids: list[str]) -> dict:
    ids = [safe_str(x).strip() for x in (person_ids or []) if safe_str(x).strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {"matches": [], "credits_consumed": 0, "missing_records": 0}

    payload = {
        "reveal_personal_emails": False,
        "reveal_phone_number": False,
        "details": [{"id": pid} for pid in ids[:10]],
    }

    return _apollo_post(APOLLO_BULK_MATCH_URL, payload)


def match_person_email_from_apollo(*, first_name: str, last_name: str, organization_name: str) -> dict:
    """
    Single-person match for Apify named recruiter/hiring-manager leads.
    """
    payload = {
        "first_name": safe_str(first_name),
        "last_name": safe_str(last_name),
        "organization_name": safe_str(organization_name),
        "reveal_personal_emails": False,
    }
    return _apollo_post(APOLLO_MATCH_URL, payload)


def match_person_email_from_apollo_linkedin_url(*, linkedin_url: str) -> dict:
    """
    Single-person match by LinkedIn profile URL. Use this when the profile URL is
    available so we do not need a company-name guess to spend one enrichment attempt.
    """
    payload = {
        "linkedin_url": safe_str(linkedin_url).strip(),
        "reveal_personal_emails": False,
    }
    return _apollo_post(APOLLO_MATCH_URL, payload)


def _split_name(full_name: str) -> tuple[str, str]:
    parts = safe_str(full_name).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _extract_best_email(person: dict) -> tuple[str, str]:
    email = safe_str(person.get("email")).strip().lower()
    email_status = safe_str(person.get("email_status")).strip().lower()
    # Guard: Apollo search responses sometimes include placeholders.
    if "email_not_unlocked" in email or email.endswith("@domain.com"):
        return "", email_status
    return email, email_status


def _extract_match_email(match_payload: dict) -> tuple[str, str]:
    if not isinstance(match_payload, dict):
        return "", ""
    person = match_payload.get("person") if isinstance(match_payload.get("person"), dict) else match_payload
    return _extract_best_email(person if isinstance(person, dict) else {})


def _match_has_prelim_accepted_email(
    match_payload: dict,
    *,
    domain: str,
    allow_alternate_domain_emails: bool,
) -> bool:
    """
    Cheap post-enrichment check used by the credit guard.
    The full persistence loop still applies every final rule; this only prevents
    alternate-domain or non-verified reveals from being treated as successful
    credit conversions.
    """
    email, email_status = _extract_match_email(match_payload)
    if not email or email_status != "verified":
        return False
    return allow_alternate_domain_emails or _email_matches_domain(email, domain)


def _person_location_string(person: dict) -> str:
    city = safe_str(person.get("city")).strip()
    state = safe_str(person.get("state")).strip()
    country = safe_str(person.get("country")).strip()
    parts = [p for p in [city, state, country] if p]
    return ", ".join(parts)


def _person_is_us_based(person: dict) -> bool:
    country = safe_str(person.get("country")).strip().lower()
    location = _person_location_string(person).strip().lower()
    if country in {"united states", "us", "usa", "united states of america"}:
        return True
    return bool(
        location.endswith(", united states")
        or location.endswith(", us")
        or location == "united states"
        or location == "us"
    )


def _person_has_email_before_reveal(person: dict) -> bool:
    return person.get("has_email") is True


def _apollo_search_email_skip_reason(person: dict) -> str:
    """
    Search is called with has_email=True, but Apollo does not consistently echo
    has_email on every returned profile. Treat unknown as worth one cautious
    enrichment attempt, but skip explicit no-email/bad-status records.
    """
    raw_email = safe_str((person or {}).get("email")).strip().lower()
    email_status = safe_str((person or {}).get("email_status")).strip().lower()
    has_email = (person or {}).get("has_email")
    email, _ = _extract_best_email(person or {})
    if email:
        return ""
    if "email_not_unlocked" in raw_email or raw_email.endswith("@domain.com"):
        return ""
    if has_email is True:
        return ""
    if email_status in APOLLO_EMAIL_SKIP_STATUSES:
        return f"apollo_{email_status}_email"
    if has_email is False:
        return "apollo_search_no_email_available"
    return ""


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _increment_counter(target: dict, key: str, amount: int = 1) -> None:
    key = safe_str(key).strip() or "unknown"
    target[key] = int(target.get(key) or 0) + int(amount or 0)


def _record_seen_title(stats: dict, title: str) -> None:
    title = safe_str(title).strip() or "[NO TITLE]"
    if len(title) > 160:
        title = f"{title[:157]}..."
    _increment_counter(stats.setdefault("seen_title_counts", {}), title)


def _missing_email_waste_reason(person: dict, email_status: str) -> str:
    raw_email = safe_str((person or {}).get("email")).strip().lower()
    status = safe_str(email_status).strip().lower()
    if status == "unavailable":
        return "unavailable_email"
    if "email_not_unlocked" in raw_email or raw_email.endswith("@domain.com"):
        return "unusable_placeholder"
    return "no_email_returned"


def _record_wasted_credit(
    stats: dict,
    *,
    reason: str,
    person_name: str = "",
    email: str = "",
    email_status: str = "",
    apollo_person_id: str = "",
    title: str = "",
    note: str = "",
    run_log_path: str = "",
) -> None:
    item = {
        "company": safe_str(stats.get("company")),
        "reason": safe_str(reason),
        "person": safe_str(person_name),
        "email": safe_str(email),
        "email_status": safe_str(email_status),
        "apollo_person_id": safe_str(apollo_person_id),
        "title": safe_str(title),
        "note": safe_str(note),
    }
    stats.setdefault("wasted_credit_people", []).append(item)
    if run_log_path:
        append_and_print(
            run_log_path,
            (
                f"CREDIT_WASTE company={item['company'] or '[NONE]'} reason={item['reason'] or '[NONE]'} "
                f"person={item['person'] or '[NONE]'} email={item['email'] or '[NONE]'} "
                f"status={item['email_status'] or '[NONE]'} apollo_id={item['apollo_person_id'] or '[NONE]'} "
                f"title={item['title'] or '[NONE]'} note={item['note'] or '[NONE]'}"
            ),
        )


def _store_rejected_apollo_email(
    *,
    company: Company | None,
    job: JobPosting | None = None,
    person_name: str = "",
    title: str = "",
    email: str = "",
    email_status: str = "",
    apollo_person_id: str = "",
    reason: str = "",
    source_workflow: str = "",
    run_log_path: str = "",
    raw_payload: dict | None = None,
) -> None:
    email = safe_str(email).strip().lower()
    if not company or not company.id or not email or email == "none" or "@" not in email:
        return
    ApolloRejectedEmail.objects.create(
        company=company,
        job_posting=job if job and getattr(job, "id", None) else None,
        person_name=safe_str(person_name).strip(),
        title=safe_str(title).strip(),
        email=email,
        email_status=safe_str(email_status).strip().lower() or "unknown",
        apollo_person_id=safe_str(apollo_person_id).strip(),
        reason=safe_str(reason).strip(),
        source_workflow=safe_str(source_workflow).strip(),
        run_log_path=safe_str(run_log_path).strip(),
        raw_payload=raw_payload if isinstance(raw_payload, dict) else {},
    )


def _email_matches_domain(email: str, domain: str) -> bool:
    email = safe_str(email).strip().lower()
    domain = normalize_domain_value(domain)
    if "@" not in email or not domain:
        return False
    email_domain = email.rsplit("@", 1)[-1]
    return email_domain == domain or email_domain.endswith(f".{domain}")


def _email_domain(email: str) -> str:
    email = safe_str(email).strip().lower()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip()


def _email_has_prior_real_initial_send(email: str) -> bool:
    email = safe_str(email).strip().lower()
    if not email:
        return False
    return SentEmailLog.objects.filter(
        to_email=email,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


def _company_real_initial_sent_count(company: Company) -> int:
    normalized = safe_str(getattr(company, "normalized_name", "")).strip()
    if not normalized:
        return 0
    cooldown_days = get_company_cooldown_days()
    if cooldown_days <= 0:
        return 0
    qs = SentEmailLog.objects.filter(
        job_posting__company_ref__normalized_name=normalized,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
        sent_at__gte=timezone.now() - timedelta(days=cooldown_days),
    )
    return qs.count()


def _person_company_domain_matches(person: dict, domain: str) -> bool:
    domain = normalize_domain_value(domain)
    if not domain:
        return False
    org = person.get("organization") if isinstance(person.get("organization"), dict) else {}
    possible_domains = [
        person.get("organization_website_url"),
        person.get("organization_domain"),
        org.get("website_url"),
        org.get("primary_domain"),
        org.get("domain"),
    ]
    for value in possible_domains:
        candidate = normalize_domain_value(value)
        if candidate and (candidate == domain or candidate.endswith(f".{domain}") or domain.endswith(f".{candidate}")):
            return True
    return False


def _person_has_conflicting_company_domain(person: dict, domain: str) -> bool:
    domain = normalize_domain_value(domain)
    if not domain:
        return False
    org = person.get("organization") if isinstance(person.get("organization"), dict) else {}
    possible_domains = [
        person.get("organization_website_url"),
        person.get("organization_domain"),
        org.get("website_url"),
        org.get("primary_domain"),
        org.get("domain"),
    ]
    candidates = [normalize_domain_value(value) for value in possible_domains]
    candidates = [value for value in candidates if value]
    if not candidates:
        return False
    return not any(
        candidate == domain or candidate.endswith(f".{domain}") or domain.endswith(f".{candidate}")
        for candidate in candidates
    )


def get_apify_person_lead(job: JobPosting) -> dict:
    recruiter_name = safe_str(getattr(job, "recruiter_name", "")).strip()
    if recruiter_name:
        return {
            "name": recruiter_name,
            "title": safe_str(getattr(job, "recruiter_title", "")).strip() or "Recruiter",
            "linkedin": safe_str(getattr(job, "recruiter_linkedin", "")).strip(),
            "kind": "apify_recruiter",
        }

    hiring_manager_name = safe_str(getattr(job, "ai_hiring_mgr_name", "")).strip()
    if hiring_manager_name:
        return {
            "name": hiring_manager_name,
            "title": "Hiring Manager",
            "linkedin": "",
            "kind": "apify_hiring_manager",
        }

    return {}


def job_has_apify_person_lead(job: JobPosting) -> bool:
    return bool(get_apify_person_lead(job).get("name"))


def _exact_person_lead_title_is_allowed(title: str) -> bool:
    if not safe_str(title).strip():
        return False
    return is_data_science_manager_contact_title(title) or is_fallback_business_contact_title(title)


@transaction.atomic
def upsert_apify_person_recruiter_from_apollo(
    *,
    job: JobPosting,
    run_log_path: str = "",
) -> dict:
    company = job.company_ref
    lead = get_apify_person_lead(job)
    stats = {
        "job_id": job.id,
        "company": safe_str(getattr(company, "normalized_name", "")),
        "person": safe_str(lead.get("name")),
        "kind": safe_str(lead.get("kind")),
        "created": 0,
        "updated": 0,
        "emails_found": 0,
        "verified_emails": 0,
        "unverified_emails": 0,
        "apollo_email_status_counts": {},
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "accepted_alternate_domain_emails": 0,
        "accepted_non_us_emails": 0,
        "accepted_paid_nonmatching_title_emails": 0,
        "accepted_last_resort_title_emails": 0,
        "accepted_broad_fallback_title_emails": 0,
        "skip_reasons": {},
        "seen_title_counts": {},
        "wasted_credit_people": [],
        "errors": 0,
        "status": "",
        "run_log_path": run_log_path,
    }

    if not company or not company.id:
        stats["errors"] = 1
        stats["status"] = "missing_company"
        return stats

    if not lead:
        stats["status"] = "no_apify_person_lead"
        return stats

    if not _exact_person_lead_title_is_allowed(lead.get("title", "")):
        stats["status"] = "non_data_science_manager_lead_title"
        _increment_counter(stats["skip_reasons"], stats["status"])
        if run_log_path:
            append_and_print(
                run_log_path,
                (
                    f"APIFY_PERSON_PREFLIGHT_SKIP job_id={job.id} status={stats['status']} "
                    f"person={lead['name']} title={lead.get('title') or '[NONE]'}"
                ),
            )
        return stats

    domain = normalize_domain_value(company.active_domain)
    if not is_usable_company_domain(domain):
        stats["errors"] = 1
        stats["status"] = "missing_domain"
        return stats

    first_name, last_name = _split_name(lead["name"])
    if not first_name or not last_name:
        stats["errors"] = 1
        stats["status"] = "person_name_not_matchable"
        return stats

    # Pre-flight DB check: if we already have a real email for this person, skip Apollo.
    # Saves 1 credit when the same Apify recruiter appears across multiple jobs.
    norm_person_preflight = normalize_person_name(lead["name"])
    pre_existing = (
        CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_person_preflight)
        .order_by("id")
        .first()
    )
    if pre_existing and pre_existing.email_sent:
        stats["status"] = "person_already_contacted_preflight"
        _increment_counter(stats["skip_reasons"], stats["status"])
        if run_log_path:
            append_and_print(
                run_log_path,
                f"APIFY_PERSON_PREFLIGHT_SKIP job_id={job.id} status={stats['status']} person={lead['name']}",
            )
        return stats
    if pre_existing and _has_real_email(pre_existing.email):
        pre_existing_source = safe_str(getattr(pre_existing, "source", "")).strip().lower()
        pre_existing_status = safe_str(getattr(pre_existing, "email_status", "")).strip().lower()
        pre_existing_apollo_id = safe_str(getattr(pre_existing, "apollo_person_id", "")).strip()
        if (pre_existing_source == CompanyRecruiter.Source.APOLLO or pre_existing_apollo_id) and pre_existing_status != "verified":
            stats["status"] = f"apollo_existing_non_verified_email:{pre_existing_status or 'unknown'}"
            _increment_counter(stats["skip_reasons"], stats["status"])
            stats["unverified_emails"] = 1
            _increment_counter(stats["apollo_email_status_counts"], pre_existing_status or "unknown")
            if run_log_path:
                append_and_print(
                    run_log_path,
                    (
                        f"APIFY_PERSON_PREFLIGHT_SKIP job_id={job.id} status={stats['status']} "
                        f"person={lead['name']} email={pre_existing.email}"
                    ),
                )
            return stats
        stats["status"] = "person_email_already_in_db_preflight"
        _increment_counter(stats["skip_reasons"], stats["status"])
        if run_log_path:
            append_and_print(
                run_log_path,
                f"APIFY_PERSON_PREFLIGHT_SKIP job_id={job.id} status={stats['status']} person={lead['name']} email={pre_existing.email}",
            )
        # Reuse existing email — create/update job target without an Apollo call.
        changed_existing_fields = []
        if not pre_existing.manually_targeted:
            pre_existing.manually_targeted = True
            changed_existing_fields.append("manually_targeted")
        if safe_str(lead.get("title")).strip() and not safe_str(pre_existing.apollo_title).strip():
            pre_existing.apollo_title = safe_str(lead.get("title")).strip()
            changed_existing_fields.append("apollo_title")
        if safe_str(lead.get("linkedin")).strip() and not safe_str(pre_existing.apollo_linkedin_url).strip():
            pre_existing.apollo_linkedin_url = safe_str(lead.get("linkedin")).strip()
            changed_existing_fields.append("apollo_linkedin_url")
        if changed_existing_fields:
            pre_existing.save(update_fields=list(dict.fromkeys(changed_existing_fields + ["updated_at"])))

        JobRecruiterTarget.objects.filter(job_posting=job).exclude(company_recruiter=pre_existing).delete()
        JobRecruiterTarget.objects.update_or_create(
            job_posting=job,
            company_recruiter=pre_existing,
            defaults={
                "recipient_email_snapshot": pre_existing.email,
                "recipient_name_snapshot": pre_existing.person_name,
                "selection_order": 1,
                "is_selected_for_job": True,
                "is_verified_for_job": True,
            },
        )
        job.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
        job.save(update_fields=["status", "updated_at"])
        stats["emails_found"] = 1
        stats["verified_emails"] = 1
        return stats

    if run_log_path:
        append_and_print(
            run_log_path,
            f"APIFY_PERSON_START job_id={job.id} company={company.normalized_name} person={lead['name']} kind={lead['kind']}",
        )

    try:
        payload = match_person_email_from_apollo(
            first_name=first_name,
            last_name=last_name,
            organization_name=company.raw_name_latest or company.normalized_name,
        )
    except Exception as exc:
        stats["errors"] = 1
        stats["status"] = "apollo_match_error"
        if run_log_path:
            append_exception(run_log_path, f"APIFY_PERSON_MATCH_ERROR job_id={job.id}", exc)
        return stats

    stats["credits_consumed"] = int((payload or {}).get("credits_consumed") or 0)
    person_payload = payload.get("person") if isinstance(payload, dict) and isinstance(payload.get("person"), dict) else payload
    if run_log_path:
        append_and_print(
            run_log_path,
            f"APIFY_PERSON_MATCH_DONE job_id={job.id} credits={stats['credits_consumed']} payload_has_person={isinstance(person_payload, dict)}",
        )
    email, email_status = _extract_match_email(payload)
    if email:
        _increment_counter(stats["apollo_email_status_counts"], email_status or "unknown")
    if not email:
        stats["status"] = f"no_real_email:{email_status or 'unknown'}"
        _increment_counter(stats["skip_reasons"], stats["status"])
        stats["credits_not_converted_to_email"] = stats["credits_consumed"]
        if stats["credits_consumed"]:
            _record_wasted_credit(
                stats,
                reason=_missing_email_waste_reason(person_payload if isinstance(person_payload, dict) else {}, email_status),
                person_name=lead["name"],
                email_status=email_status,
                apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
                title=lead["title"],
                run_log_path=run_log_path,
            )
        if run_log_path:
            append_and_print(run_log_path, f"APIFY_PERSON_SKIP job_id={job.id} status={stats['status']}")
        return stats
    if email_status != "verified":
        stats["status"] = f"apollo_non_verified_email:{email_status or 'unknown'}"
        _increment_counter(stats["skip_reasons"], stats["status"])
        stats["unverified_emails"] = 1
        stats["credits_not_converted_to_email"] = stats["credits_consumed"]
        _store_rejected_apollo_email(
            company=company,
            job=job,
            person_name=lead["name"],
            title=lead["title"],
            email=email,
            email_status=email_status,
            apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
            reason=stats["status"],
            source_workflow="apollo_exact_person",
            run_log_path=run_log_path,
            raw_payload=person_payload if isinstance(person_payload, dict) else {},
        )
        if stats["credits_consumed"]:
            _record_wasted_credit(
                stats,
                reason=stats["status"],
                person_name=lead["name"],
                email=email,
                email_status=email_status,
                apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
                title=lead["title"],
                run_log_path=run_log_path,
            )
        if run_log_path:
            append_and_print(
                run_log_path,
                (
                    f"APIFY_PERSON_SKIP job_id={job.id} status={stats['status']} "
                    f"email={email}"
                ),
            )
        return stats
    if _email_has_prior_real_initial_send(email):
        stats["status"] = "email_already_contacted"
        _increment_counter(stats["skip_reasons"], stats["status"])
        stats["credits_not_converted_to_email"] = stats["credits_consumed"]
        if stats["credits_consumed"]:
            _record_wasted_credit(
                stats,
                reason="already_contacted_email",
                person_name=lead["name"],
                email=email,
                email_status=email_status,
                apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
                title=lead["title"],
                run_log_path=run_log_path,
            )
        if run_log_path:
            append_and_print(run_log_path, f"APIFY_PERSON_SKIP job_id={job.id} status={stats['status']} email={email}")
        return stats

    actual_title = (
        safe_str((person_payload or {}).get("title")).strip()
        or safe_str((person_payload or {}).get("headline")).strip()
        if isinstance(person_payload, dict)
        else ""
    ) or safe_str(lead.get("title")).strip()
    if not _exact_person_lead_title_is_allowed(actual_title):
        stats["status"] = contact_title_rejection_reason(actual_title)
        _increment_counter(stats["skip_reasons"], stats["status"])
        stats["credits_not_converted_to_email"] = stats["credits_consumed"]
        if stats["credits_consumed"]:
            _record_wasted_credit(
                stats,
                reason=stats["status"],
                person_name=lead["name"],
                email=email,
                email_status=email_status,
                apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
                title=actual_title,
                run_log_path=run_log_path,
            )
        if run_log_path:
            append_and_print(
                run_log_path,
                (
                    f"APIFY_PERSON_SKIP job_id={job.id} status={stats['status']} "
                    f"email={email} title={actual_title or '[NONE]'}"
                ),
            )
        return stats

    if not _person_is_us_based(person_payload if isinstance(person_payload, dict) else {}):
        stats["accepted_non_us_emails"] += 1
        if run_log_path:
            append_and_print(
                run_log_path,
                f"APIFY_PERSON_LOCATION_NOTE job_id={job.id} accepted=true email={email} location={_person_location_string(person_payload if isinstance(person_payload, dict) else {}) or '[NONE]'}",
            )
    if not _email_matches_domain(email, domain):
        stats["accepted_alternate_domain_emails"] += 1
        if run_log_path:
            append_and_print(
                run_log_path,
                f"APIFY_PERSON_DOMAIN_NOTE job_id={job.id} accepted=true email={email} "
                f"email_domain={_email_domain(email) or '[NONE]'} searched_domain={domain or '[NONE]'}",
            )

    norm_person = normalize_person_name(lead["name"])
    recruiter = CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_person).order_by("id").first()
    created = False
    if recruiter is None:
        recruiter = CompanyRecruiter(company=company, person_name=lead["name"], is_active=True)
        created = True

    changed = False
    fields = {
        "person_name": lead["name"],
        "apollo_title": actual_title,
        "apollo_location": _person_location_string(person_payload if isinstance(person_payload, dict) else {}),
        "apollo_linkedin_url": (
            safe_str((person_payload or {}).get("linkedin_url")).strip()
            if isinstance(person_payload, dict)
            else ""
        )
        or safe_str(lead.get("linkedin")).strip(),
        "email": email,
        "source": CompanyRecruiter.Source.APOLLO,
        "email_status": email_status,
        "title_match": True,
        "location_match": False,
        "manually_targeted": True,
    }
    for field, value in fields.items():
        if getattr(recruiter, field) != value:
            setattr(recruiter, field, value)
            changed = True

    if created:
        recruiter.save()
        stats["created"] = 1
    elif changed:
        recruiter.save()
        stats["updated"] = 1

    if recruiter.email_sent:
        JobRecruiterTarget.objects.filter(job_posting=job, company_recruiter=recruiter).delete()
        stats["status"] = "person_already_contacted"
        _increment_counter(stats["skip_reasons"], stats["status"])
        stats["credits_not_converted_to_email"] = stats["credits_consumed"]
        if stats["credits_consumed"]:
            _record_wasted_credit(
                stats,
                reason="already_contacted_person",
                person_name=lead["name"],
                email=email,
                email_status=email_status,
                apollo_person_id=safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else "",
                title=lead["title"],
                run_log_path=run_log_path,
            )
        if run_log_path:
            append_and_print(run_log_path, f"APIFY_PERSON_SKIP job_id={job.id} status={stats['status']} email={email}")
        return stats

    JobRecruiterTarget.objects.filter(job_posting=job).exclude(company_recruiter=recruiter).delete()
    JobRecruiterTarget.objects.update_or_create(
        job_posting=job,
        company_recruiter=recruiter,
        defaults={
            "recipient_email_snapshot": email,
            "recipient_name_snapshot": recruiter.person_name,
            "selection_order": 1,
            "is_selected_for_job": True,
            "is_verified_for_job": True,
        },
    )
    job.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
    job.save(update_fields=["status", "updated_at"])

    stats["emails_found"] = 1
    stats["verified_emails"] = 1
    stats["credits_not_converted_to_email"] = max(0, stats["credits_consumed"] - stats["emails_found"])
    stats["status"] = "real_person_target_created"
    if run_log_path:
        append_and_print(run_log_path, f"APIFY_PERSON_DONE job_id={job.id} email={email} status={stats['status']}")
    return stats


def _apollo_title_matches(title: str) -> bool:
    return is_data_science_manager_contact_title(title) or is_recruiting_or_hiring_contact_title(title)


def _apollo_contact_category(title: str) -> str:
    text = safe_str(title).strip().lower()
    if re.search(r"\brecruit(?:er|ing|ment)\b|\btalent acquisition\b|\btalent sourc(?:er|ing)\b|\btechnical sourc(?:er|ing)\b|\bsourc(?:er|ing)\b|\bstaffing\b|\bhead of talent\b|\bhiring\b", text):
        return APOLLO_CATEGORY_RECRUITING
    if re.search(r"\bhuman resources\b|\bhuman capital\b|\bhr\b|\bhrbp\b|\bpeople operations\b|\bpeople ops\b|\bpeople partner\b|\bpeople and culture\b", text):
        return APOLLO_CATEGORY_PEOPLE_HR
    if is_data_science_manager_contact_title(title) or is_fallback_business_contact_title(title):
        return APOLLO_CATEGORY_DATA
    return APOLLO_CATEGORY_FALLBACK


def _apollo_contact_priority(title: str) -> int:
    category = _apollo_contact_category(title)
    if category in {APOLLO_CATEGORY_RECRUITING, APOLLO_CATEGORY_PEOPLE_HR}:
        return 0
    if category == APOLLO_CATEGORY_DATA:
        return 1
    return 2


def _apollo_candidate_priority(title: str) -> int:
    text = safe_str(title).strip().lower()
    if is_recruiting_or_hiring_contact_title(title):
        return 0
    if re.search(r"\bhuman resources\b|\bhuman capital\b|\bhr\b|\bhrbp\b|\bpeople operations\b|\bpeople ops\b|\bpeople partner\b|\bpeople and culture\b", text):
        return 1
    if is_data_science_manager_contact_title(title):
        return 2
    if is_fallback_business_contact_title(title):
        return 3
    if is_last_resort_technical_contact_title(title) and re.search(
        r"\b(data|analytics|business intelligence|bi|machine learning|ml|ai|artificial intelligence|applied scientist|research scientist)\b",
        text,
    ):
        return 4
    if is_domain_business_owner_contact_title(title):
        return 5
    if is_last_resort_technical_contact_title(title) and not re.search(r"\b(ceo|chief executive officer|founder|co founder|cofounder)\b", text):
        return 6
    if is_general_business_leadership_contact_title(title):
        return 7
    if re.search(r"\b(ceo|chief executive officer|founder|co founder|cofounder)\b", text):
        return 8
    return 9


def _apollo_leadership_fallback_title_matches(title: str) -> bool:
    return is_fallback_business_contact_title(title)


def _apollo_broad_fallback_title_matches(title: str) -> bool:
    return (
        is_domain_business_owner_contact_title(title)
        or is_general_business_leadership_contact_title(title)
        or is_last_resort_technical_contact_title(title)
    )


def _apollo_last_resort_title_matches(title: str) -> bool:
    return is_last_resort_technical_contact_title(title)


def _apollo_location_matches(location_hint: str, person_location: str) -> bool:
    hint = safe_str(location_hint).strip().lower()
    location = safe_str(person_location).strip().lower()
    if not hint or not location:
        return False
    if hint in {"remote", "anywhere", "global"}:
        return True
    if hint in location:
        return True
    if "united states" in hint and ("united states" in location or location.endswith(", us")):
        return True
    hint_parts = [p.strip() for p in hint.replace(",", " ").split() if len(p.strip()) >= 3]
    return any(part in location for part in hint_parts)


def _bounded_people_limit(max_people: int = DEFAULT_MAX_PEOPLE) -> int:
    try:
        requested = int(max_people or DEFAULT_MAX_PEOPLE)
    except Exception:
        requested = DEFAULT_MAX_PEOPLE
    return max(1, min(requested, _apollo_people_limit()))


def _search_people_api_per_page(max_people: int) -> int:
    try:
        value = int(max_people or 1)
    except Exception:
        value = 1
    return max(1, min(value * 2, 50))


def _record_search_trace(
    *,
    stats: dict,
    run_log_path: str,
    label: str,
    locations: list[str],
    api_per_page: int,
    returned: int,
) -> None:
    trace = {
        "label": label,
        "locations": [safe_str(x).strip() for x in locations if safe_str(x).strip()],
        "api_per_page": int(api_per_page or 0),
        "returned_people": int(returned or 0),
    }
    stats.setdefault("search_trace", []).append(trace)
    stats["search_calls"] = int(stats.get("search_calls") or 0) + 1
    stats["search_api_per_page_total"] = int(stats.get("search_api_per_page_total") or 0) + trace["api_per_page"]
    stats["search_returned_people"] = int(stats.get("search_returned_people") or 0) + trace["returned_people"]
    append_and_print(
        run_log_path,
        f"APOLLO_SEARCH_TRACE label={label} locations={trace['locations'] or ['[NONE]']} "
        f"api_per_page={trace['api_per_page']} returned_people={trace['returned_people']}",
    )


def _legacy_recruiters_with_email(company: Company, max_people: int = DEFAULT_MAX_PEOPLE) -> list[CompanyRecruiter]:
    max_people = _bounded_people_limit(max_people)
    qs = (
        CompanyRecruiter.objects
        .filter(company=company, is_active=True, email_sent=False)
        .filter(Q(legacy=True) | Q(source=CompanyRecruiter.Source.LEGACY))
        .order_by("-id")
    )
    recruiters = [r for r in qs if _has_real_email(r.email)]
    return recruiters[:max_people]


def _apollo_recruiters_with_email(company: Company, max_people: int = DEFAULT_MAX_PEOPLE) -> list[CompanyRecruiter]:
    max_people = _bounded_people_limit(max_people)
    qs = (
        CompanyRecruiter.objects
        .filter(
            company=company,
            is_active=True,
            email_status__iexact="verified",
            email_sent=False,
        )
        .filter(Q(source=CompanyRecruiter.Source.APOLLO) | (Q(apollo_person_id__isnull=False) & ~Q(apollo_person_id="")))
        .order_by("-title_match", "-location_match", "normalized_person_name", "id")
    )
    recruiters = [
        r
        for r in qs
        if _has_real_email(r.email)
    ]
    recruiters.sort(
        key=lambda r: (
            _apollo_contact_priority(safe_str(r.apollo_title).strip()),
            -int(bool(r.title_match)),
            -int(bool(r.location_match)),
            safe_str(r.normalized_person_name).strip(),
            r.id,
        )
    )
    if len(recruiters) < max_people:
        selected_ids = {r.id for r in recruiters}
        recruiters.extend([
            r
            for r in qs
            if r.id not in selected_ids
            and _has_real_email(r.email)
        ])
    return recruiters[:max_people]


@transaction.atomic
def upsert_company_recruiters_from_apollo(
    *,
    company: Company,
    location_hint: str = "",
    max_people: int = DEFAULT_MAX_PEOPLE,
    run_log_path: str = "",
    exclude_person_names: list[str] | None = None,
    allow_last_resort_titles: bool = True,
    allow_paid_nonmatching_titles: bool = True,
    allow_alternate_domain_emails: bool = True,
    allow_broad_fallback_titles: bool = False,
) -> dict:
    if not run_log_path:
        run_log_path = create_run_log_path("apollo_recruiter_fetch_company", company.normalized_name)

    domain = normalize_domain_value(company.active_domain)

    stats = {
        "company": company.normalized_name,
        "domain": domain,
        "location_hint": safe_str(location_hint).strip(),
        "max_people": _bounded_people_limit(max_people),
        "searched_local": 0,
        "searched_us": 0,
        "legacy_reused": 0,
        "enriched": 0,
        "created": 0,
        "updated": 0,
        "emails_found": 0,
        "verified_emails": 0,
        "unverified_emails": 0,
        "apollo_email_status_counts": {},
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "accepted_alternate_domain_emails": 0,
        "accepted_non_us_emails": 0,
        "accepted_paid_nonmatching_title_emails": 0,
        "accepted_last_resort_title_emails": 0,
        "accepted_broad_fallback_title_emails": 0,
        "skip_reasons": {},
        "seen_title_counts": {},
        "wasted_credit_people": [],
        "credit_batches": [],
        "search_calls": 0,
        "search_api_per_page_total": 0,
        "search_returned_people": 0,
        "search_trace": [],
        "prior_real_initial_sends": 0,
        "remaining_send_capacity": 0,
        "errors": 0,
        "run_log_path": run_log_path,
    }

    append_and_print(
        run_log_path,
        f"START company={company.normalized_name} domain={domain or '[NONE]'} location={stats['location_hint'] or '[NONE]'} max_people={stats['max_people']}",
    )

    max_people = _bounded_people_limit(max_people)
    stats["max_people"] = max_people

    legacy_recruiters = _legacy_recruiters_with_email(company, max_people=max_people)
    existing_apollo_recruiters = _apollo_recruiters_with_email(company, max_people=max_people)
    prior_real_initial_sends = _company_real_initial_sent_count(company)
    remaining_send_capacity = max(0, max_people - prior_real_initial_sends)
    stats["prior_real_initial_sends"] = prior_real_initial_sends
    stats["remaining_send_capacity"] = remaining_send_capacity
    append_and_print(
        run_log_path,
        (
            f"SEND_CAP company={company.normalized_name} max_people={max_people} "
            f"prior_real_initial_sends={prior_real_initial_sends} remaining={remaining_send_capacity}"
        ),
    )
    existing_apollo_ids = {
        safe_str(recruiter.apollo_person_id).strip()
        for recruiter in existing_apollo_recruiters
        if safe_str(recruiter.apollo_person_id).strip()
    }
    # All Apollo person IDs ever stored for this company — including already-emailed ones.
    # Used to skip re-enriching people we've already contacted, preventing wasted credits.
    # Exclude both empty-string and NULL so seen_ids is never polluted with None/empty values.
    all_known_apollo_ids = set(
        CompanyRecruiter.objects.filter(company=company)
        .exclude(apollo_person_id="")
        .exclude(apollo_person_id__isnull=True)
        .values_list("apollo_person_id", flat=True)
    )
    existing_allowed_count = min(max_people, len(legacy_recruiters) + len(existing_apollo_recruiters))
    if legacy_recruiters:
        stats["legacy_reused"] = len(legacy_recruiters)
        append_and_print(
            run_log_path,
            f"LEGACY_REUSE company={company.normalized_name} recruiters={len(legacy_recruiters)}",
        )
    if existing_apollo_recruiters:
        append_and_print(
            run_log_path,
            f"APOLLO_REUSE company={company.normalized_name} recruiters={len(existing_apollo_recruiters)}",
        )
    if remaining_send_capacity <= 0:
        append_and_print(run_log_path, "COMPANY_SEND_CAP_FULL skip_apollo=true")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    if existing_allowed_count >= remaining_send_capacity:
        sync_job_targets_for_company_pending_jobs(
            company=company,
            max_targets=remaining_send_capacity,
            auto_select=True,
            allow_fallback_contacts=True,
        )
        company.jobs.filter(status=JobPosting.Status.RECRUITERS_PENDING, is_manual_email_job=False).update(status=JobPosting.Status.EMAIL_DISCOVERY_DONE)
        append_and_print(run_log_path, "EXISTING_FULL skip_apollo=true")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    apollo_slots = min(max_people - existing_allowed_count, remaining_send_capacity - existing_allowed_count)
    append_and_print(run_log_path, f"APOLLO_TOP_UP slots={apollo_slots}")

    if not is_usable_company_domain(domain):
        stats["errors"] += 1
        append_and_print(run_log_path, "ERROR reason=missing_or_blocked_domain")
        if legacy_recruiters:
            sync_job_targets_for_company_pending_jobs(
                company=company,
                max_targets=max_people,
                auto_select=True,
                allow_fallback_contacts=True,
            )
            company.jobs.filter(status=JobPosting.Status.RECRUITERS_PENDING, is_manual_email_job=False).update(status=JobPosting.Status.EMAIL_DISCOVERY_DONE)
            append_and_print(run_log_path, "LEGACY_PARTIAL_USED reason=apollo_domain_missing")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    # Build the location list from the hint.
    hint = safe_str(stats["location_hint"]).strip()
    if hint and hint.lower() not in {"remote", "anywhere", "global", "united states"}:
        primary_locations: list[str] = [f"{hint}, United States"]
        local_is_us = False
    else:
        primary_locations = ["United States"]
        local_is_us = True

    # Shared seen-IDs set — starts from all known Apollo IDs so we never
    # re-enrich someone already in the DB.
    seen_ids: set[str] = set(all_known_apollo_ids)
    excluded_names: set[str] = {
        normalize_person_name(name)
        for name in (exclude_person_names or [])
        if normalize_person_name(name)
    }
    people: list[dict] = []
    category_cap = min(_apollo_category_cap(), apollo_slots)
    category_counts = {
        APOLLO_CATEGORY_RECRUITING: 0,
        APOLLO_CATEGORY_PEOPLE_HR: 0,
        APOLLO_CATEGORY_DATA: 0,
        APOLLO_CATEGORY_FALLBACK: 0,
    }
    stats["category_cap"] = category_cap
    stats["candidate_category_counts"] = dict(category_counts)
    category_overflow: list[dict] = []
    category_overflow_ids: set[str] = set()

    def _can_add_person_category(title: str) -> bool:
        category = _apollo_contact_category(title)
        return category_counts.get(category, 0) < category_cap

    def _remember_category_overflow(person: dict) -> None:
        pid = safe_str(person.get("id")).strip()
        if not pid or pid in seen_ids or pid in category_overflow_ids:
            return
        category_overflow.append(person)
        category_overflow_ids.add(pid)

    def _add_person_candidate(person: dict, title: str) -> None:
        category = _apollo_contact_category(title)
        people.append(person)
        category_counts[category] = category_counts.get(category, 0) + 1
        stats["candidate_category_counts"] = dict(category_counts)

    def _do_search(
        locations: list[str],
        titles: list[str],
        label: str,
        *,
        allow_leadership_fallback: bool = False,
        allow_last_resort_fallback: bool = False,
        allow_broad_fallback: bool = False,
    ) -> None:
        """Run one Apollo people search and merge qualifying results into `people`."""
        if len(people) >= apollo_slots:
            return
        try:
            results = search_people_from_apollo(
                domain=domain,
                locations=locations,
                max_people=apollo_slots,
                person_titles=titles,
            )
            _record_search_trace(
                stats=stats,
                run_log_path=run_log_path,
                label=label,
                locations=locations,
                api_per_page=_search_people_api_per_page(apollo_slots),
                returned=len(results),
            )
            added = 0
            for p in results:
                if len(people) >= apollo_slots:
                    break
                pid = safe_str(p.get("id")).strip()
                person_name = safe_str(p.get("name")).strip()
                title = safe_str(p.get("title")).strip() or safe_str(p.get("headline")).strip()
                _record_seen_title(stats, title)
                if (
                    not pid
                    or pid in seen_ids
                    or normalize_person_name(person_name) in excluded_names
                ):
                    continue
                title_allowed = (
                    _apollo_title_matches(title)
                    or (allow_leadership_fallback and _apollo_leadership_fallback_title_matches(title))
                    or (allow_last_resort_fallback and _apollo_last_resort_title_matches(title))
                    or (allow_broad_fallback and _apollo_broad_fallback_title_matches(title))
                )
                if not title_allowed:
                    _increment_counter(stats["skip_reasons"], f"search:{contact_title_rejection_reason(title)}")
                    append_and_print(
                        run_log_path,
                        (
                            f"SEARCH_{label.upper()}_SKIP person={person_name or '[NONE]'} "
                            f"reason={contact_title_rejection_reason(title)} title={title or '[NONE]'}"
                        ),
                    )
                    continue
                email_skip_reason = _apollo_search_email_skip_reason(p)
                if email_skip_reason:
                    _increment_counter(stats["skip_reasons"], f"search:{email_skip_reason}")
                    append_and_print(
                        run_log_path,
                        (
                            f"SEARCH_{label.upper()}_SKIP person={person_name or '[NONE]'} "
                            f"reason={email_skip_reason} title={title or '[NONE]'}"
                        ),
                    )
                    continue
                if not _can_add_person_category(title):
                    category = _apollo_contact_category(title)
                    _remember_category_overflow(p)
                    _increment_counter(stats["skip_reasons"], f"search:{category}_category_cap")
                    append_and_print(
                        run_log_path,
                        (
                            f"SEARCH_{label.upper()}_SKIP person={person_name or '[NONE]'} "
                            f"reason={category}_category_cap title={title or '[NONE]'}"
                        ),
                    )
                    continue
                _add_person_candidate(p, title)
                seen_ids.add(pid)
                added += 1
            append_and_print(run_log_path, f"SEARCH_{label.upper()}_DONE returned={len(results)} added={added} total={len(people)}")
        except Exception as exc:
            stats["errors"] += 1
            append_exception(run_log_path, f"SEARCH_{label.upper()}_ERROR company={company.normalized_name}", exc)

    # Round 1 - recruiting, talent, hiring, and HR titles.
    _do_search(primary_locations, RECRUITING_PERSON_TITLES, "recruiting_local")
    # US-wide only when local wasn't already US - avoids an identical duplicate search
    if len(people) < apollo_slots and not local_is_us:
        _do_search(["United States"], RECRUITING_PERSON_TITLES, "recruiting_us")

    # Round 2 - data/AI/ML manager and director titles.
    _do_search(primary_locations, DATA_MANAGER_PERSON_TITLES, "primary_local")
    if len(people) < apollo_slots and not local_is_us:
        _do_search(["United States"], DATA_MANAGER_PERSON_TITLES, "primary_us")

    # Round 3 - broader data leadership titles.
    # Fires whenever we are short of slots, not just when zero.
    if len(people) < apollo_slots:
        _do_search(primary_locations, FALLBACK_PERSON_TITLES, "fallback_local")
        if len(people) < apollo_slots and not local_is_us:
            _do_search(["United States"], FALLBACK_PERSON_TITLES, "fallback_us")

    # Round 4 - data-specific executive fallback only when manager/director
    # searches did not fill the requested slots.
    if len(people) < apollo_slots:
        _do_search(
            primary_locations,
            LEADERSHIP_FALLBACK_PERSON_TITLES,
            "leadership_local",
            allow_leadership_fallback=True,
        )
        if len(people) < apollo_slots and not local_is_us:
            _do_search(
                ["United States"],
                LEADERSHIP_FALLBACK_PERSON_TITLES,
                "leadership_us",
                allow_leadership_fallback=True,
            )

    # Round 5 - last-resort founder, executive, engineering, and hands-on
    # technical/data titles. Used only after the data/AI leadership searches fail.
    if allow_last_resort_titles and len(people) < apollo_slots:
        _do_search(
            primary_locations,
            LAST_RESORT_PERSON_TITLES,
            "last_resort_local",
            allow_last_resort_fallback=True,
        )
        if len(people) < apollo_slots and not local_is_us:
            _do_search(
                ["United States"],
                LAST_RESORT_PERSON_TITLES,
                "last_resort_us",
                allow_last_resort_fallback=True,
            )

    # Round 6 - broader non-executive fallback titles for strict batch top-up.
    if allow_broad_fallback_titles and len(people) < apollo_slots:
        _do_search(
            primary_locations,
            BROAD_FALLBACK_PERSON_TITLES,
            "broad_fallback_local",
            allow_broad_fallback=True,
        )
        if len(people) < apollo_slots and not local_is_us:
            _do_search(
                ["United States"],
                BROAD_FALLBACK_PERSON_TITLES,
                "broad_fallback_us",
                allow_broad_fallback=True,
            )

    # Round 7 - domain/business owner fallback for finance, investment, and operational roles.
    if allow_broad_fallback_titles and len(people) < apollo_slots:
        _do_search(
            primary_locations,
            DOMAIN_BUSINESS_OWNER_PERSON_TITLES,
            "domain_owner_local",
            allow_broad_fallback=True,
        )
        if len(people) < apollo_slots and not local_is_us:
            _do_search(
                ["United States"],
                DOMAIN_BUSINESS_OWNER_PERSON_TITLES,
                "domain_owner_us",
                allow_broad_fallback=True,
            )

    # Round 8 - broad unfiltered company scan.
    # Fetches up to 50 people with no title filter, then keeps only allowed titles
    # before spending credits.
    if len(people) < apollo_slots:
        append_and_print(run_log_path, "SEARCH_BROAD_START")
        try:
            broad_people = search_company_people_from_apollo(
                domain=domain,
                locations=["United States"],
                max_people=50,
            )
            _record_search_trace(
                stats=stats,
                run_log_path=run_log_path,
                label="broad_company_scan",
                locations=["United States"],
                api_per_page=50,
                returned=len(broad_people),
            )
        except Exception as exc:
            stats["errors"] += 1
            append_exception(run_log_path, f"SEARCH_BROAD_ERROR company={company.normalized_name}", exc)
            broad_people = []

        # Keep only data/AI/ML manager/director, data-leadership, or final
        # founder/technical/data results. Broad search is free, but enriching
        # unrelated titles would waste Apollo credits and send volume.
        broad_title_match: list[dict] = []
        for p in broad_people:
            pid = safe_str(p.get("id")).strip()
            person_name = safe_str(p.get("name")).strip()
            t = safe_str(p.get("title")).strip() or safe_str(p.get("headline")).strip()
            _record_seen_title(stats, t)
            if (
                not pid
                or pid in seen_ids
                or normalize_person_name(person_name) in excluded_names
            ):
                continue
            if (
                _apollo_title_matches(t)
                or _apollo_leadership_fallback_title_matches(t)
                or (allow_last_resort_titles and _apollo_last_resort_title_matches(t))
                or (allow_broad_fallback_titles and _apollo_broad_fallback_title_matches(t))
            ):
                email_skip_reason = _apollo_search_email_skip_reason(p)
                if email_skip_reason:
                    _increment_counter(stats["skip_reasons"], f"broad:{email_skip_reason}")
                    append_and_print(
                        run_log_path,
                        (
                            f"SEARCH_BROAD_SKIP person={person_name or '[NONE]'} "
                            f"reason={email_skip_reason} title={t or '[NONE]'}"
                        ),
                    )
                    continue
                broad_title_match.append(p)
            else:
                _increment_counter(stats["skip_reasons"], f"broad:{contact_title_rejection_reason(t)}")
                append_and_print(
                    run_log_path,
                    (
                        f"SEARCH_BROAD_SKIP person={person_name or '[NONE]'} "
                        f"reason={contact_title_rejection_reason(t)} title={t or '[NONE]'}"
                    ),
                )

        for p in broad_title_match:
            if len(people) >= apollo_slots:
                break
            pid = safe_str(p.get("id")).strip()
            t = safe_str(p.get("title")).strip() or safe_str(p.get("headline")).strip()
            if not _can_add_person_category(t):
                category = _apollo_contact_category(t)
                _remember_category_overflow(p)
                _increment_counter(stats["skip_reasons"], f"broad:{category}_category_cap")
                append_and_print(
                    run_log_path,
                    (
                        f"SEARCH_BROAD_SKIP person={safe_str(p.get('name')).strip() or '[NONE]'} "
                        f"reason={category}_category_cap title={t or '[NONE]'}"
                    ),
                )
                continue
            _add_person_candidate(p, t)
            seen_ids.add(pid)
        append_and_print(run_log_path, f"SEARCH_BROAD_DONE returned={len(broad_people)} kept={len(people)}")

    if len(people) < apollo_slots and category_overflow:
        overflow_added = 0
        for p in category_overflow:
            if len(people) >= apollo_slots:
                break
            pid = safe_str(p.get("id")).strip()
            if not pid or pid in seen_ids:
                continue
            title = safe_str(p.get("title")).strip() or safe_str(p.get("headline")).strip()
            _add_person_candidate(p, title)
            seen_ids.add(pid)
            overflow_added += 1
        if overflow_added:
            append_and_print(
                run_log_path,
                f"CATEGORY_OVERFLOW_FILL added={overflow_added} total={len(people)} counts={category_counts}",
            )

    people.sort(
        key=lambda p: (
            _apollo_candidate_priority(safe_str(p.get("title")).strip() or safe_str(p.get("headline")).strip()),
            safe_str(p.get("name")).strip().lower(),
            safe_str(p.get("id")).strip(),
        )
    )
    stats["searched_local"] = len(people)
    people = people[:apollo_slots]

    # Split people into two buckets before spending any credits:
    #
    # 1. free_email_people  — Apollo already revealed the email in the free search
    #    result. We can use it directly; no bulk_match call needed, 0 credits.
    #
    # 2. needs_bulk         — Email not yet revealed. Only enrich people whose
    #    free-search email_status is NOT "unavailable" or "invalid" — those two
    #    statuses mean Apollo knows it cannot find an email, so bulk_match would
    #    spend a credit and return nothing.
    # These statuses in the free search result mean Apollo cannot/will not produce
    # a usable email for this person — skip them before paying bulk_match credits.
    free_email_people: list[dict] = []
    needs_bulk: list[dict] = []
    skipped_bad_status = 0

    for p in people:
        e, _ = _extract_best_email(p)
        if e:
            free_email_people.append(p)
        else:
            status = safe_str(p.get("email_status")).strip().lower()
            if status in APOLLO_EMAIL_SKIP_STATUSES:
                skipped_bad_status += 1
                append_and_print(
                    run_log_path,
                    f"PRE_ENRICH_STATUS_SKIP person={safe_str(p.get('name')).strip()} status={status}",
                )
            else:
                needs_bulk.append(p)

    if free_email_people:
        append_and_print(run_log_path, f"PRE_ENRICH_FREE_EMAILS count={len(free_email_people)} (no credits needed)")
    if skipped_bad_status:
        append_and_print(run_log_path, f"PRE_ENRICH_STATUS_SKIPPED count={skipped_bad_status} (saved {skipped_bad_status} credit(s))")

    # Start matches with the zero-credit free emails.
    matches: list[dict] = list(free_email_people)
    usable_matches_so_far = sum(
        1
        for p in free_email_people
        if _match_has_prelim_accepted_email(
            p,
            domain=domain,
            allow_alternate_domain_emails=allow_alternate_domain_emails,
        )
    )
    max_credit_waste = _max_credit_waste_per_company()

    person_ids = [safe_str(p.get("id")).strip() for p in needs_bulk if safe_str(p.get("id")).strip()]

    bulk_batch_size = _apollo_bulk_match_batch_size()
    append_and_print(
        run_log_path,
        (
            f"ENRICH_START ids_count={len(person_ids)} free_email_count={len(free_email_people)} "
            f"batch_size={bulk_batch_size} max_credit_waste={max_credit_waste}"
        ),
    )

    for i in range(0, len(person_ids), bulk_batch_size):
        batch = person_ids[i : i + bulk_batch_size]
        if not batch:
            continue
        try:
            time.sleep(0.8)
            match_payload = bulk_match_people_from_apollo(batch)
        except Exception as exc:
            stats["errors"] += 1
            append_exception(run_log_path, f"BULK_MATCH_ERROR company={company.normalized_name} batch={batch}", exc)
            time.sleep(APOLLO_BACKOFF_SECS)
            continue

        credits = int((match_payload or {}).get("credits_consumed") or 0)
        stats["credits_consumed"] += credits

        batch_matches = (match_payload or {}).get("matches")
        batch_match_count = len(batch_matches) if isinstance(batch_matches, list) else 0
        batch_usable_emails = 0
        if isinstance(batch_matches, list):
            batch_usable_emails = sum(
                1
                for m in batch_matches
                if isinstance(m, dict)
                and _match_has_prelim_accepted_email(
                    m,
                    domain=domain,
                    allow_alternate_domain_emails=allow_alternate_domain_emails,
                )
            )
        usable_matches_so_far += batch_usable_emails
        stats["credit_batches"].append(
            {
                "person_ids": batch,
                "requested": len(batch),
                "matches": batch_match_count,
                "usable_emails": batch_usable_emails,
                "credits": credits,
            }
        )
        append_and_print(
            run_log_path,
            f"ENRICH_BATCH_DONE requested={len(batch)} matches={batch_match_count} credits={credits} ids={batch}",
        )
        if isinstance(batch_matches, list):
            matches.extend([m for m in batch_matches if isinstance(m, dict)])
        if stats["credits_consumed"] > usable_matches_so_far + max_credit_waste:
            stats["credits_not_converted_to_email"] = max(0, stats["credits_consumed"] - usable_matches_so_far)
            _increment_counter(stats["skip_reasons"], "credit_guard_stopped_enrichment")
            append_and_print(
                run_log_path,
                (
                    "CREDIT_GUARD_STOP "
                    f"credits={stats['credits_consumed']} usable_emails={usable_matches_so_far} "
                    f"max_waste={max_credit_waste}"
                ),
            )
            break

    stats["enriched"] = len(matches)
    append_and_print(run_log_path, f"ENRICH_DONE matches={len(matches)} credits={stats['credits_consumed']}")

    # Fallback: if bulk_match is unavailable, try legacy /people/match per person.
    # Only iterate needs_bulk — free_email_people already have emails, bad-status
    # people were already excluded to avoid spending credits on guaranteed failures.
    if not matches and needs_bulk:
        append_and_print(run_log_path, "ENRICH_FALLBACK using=people_match")
        for p in needs_bulk[:apollo_slots]:
            full_name = safe_str(p.get("name")).strip() or f"{safe_str(p.get('first_name'))} {safe_str(p.get('last_name'))}".strip()
            if normalize_person_name(full_name) in excluded_names:
                _increment_counter(stats["skip_reasons"], "excluded_targeted_person_fallback")
                append_and_print(
                    run_log_path,
                    f"ENRICH_FALLBACK_SKIP person={full_name} reason=excluded_targeted_person",
                )
                continue
            first_name, last_name = _split_name(full_name)
            if not first_name or not last_name:
                continue
            # Pre-flight: skip if we already emailed this person — avoid credit spend.
            norm_p = normalize_person_name(full_name)
            existing_rec = (
                CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_p)
                .order_by("id")
                .first()
            )
            if existing_rec and existing_rec.email_sent:
                _increment_counter(stats["skip_reasons"], "person_already_contacted_fallback")
                append_and_print(
                    run_log_path,
                    f"ENRICH_FALLBACK_SKIP person={full_name} reason=person_already_contacted",
                )
                continue
            if existing_rec and _email_has_prior_real_initial_send(safe_str(existing_rec.email).strip().lower()):
                _increment_counter(stats["skip_reasons"], "email_already_contacted_fallback")
                append_and_print(
                    run_log_path,
                    f"ENRICH_FALLBACK_SKIP person={full_name} reason=email_already_contacted",
                )
                continue
            try:
                time.sleep(0.6)
                mp = match_person_email_from_apollo(
                    first_name=first_name,
                    last_name=last_name,
                    organization_name=company.raw_name_latest or company.normalized_name,
                )
                credits = int((mp or {}).get("credits_consumed") or 0)
                stats["credits_consumed"] += credits
                stats["credit_batches"].append(
                    {
                        "person": full_name,
                        "requested": 1,
                        "matches": 1 if isinstance(mp, dict) and mp else 0,
                        "credits": credits,
                        "fallback": True,
                    }
                )
                append_and_print(
                    run_log_path,
                    f"ENRICH_FALLBACK_PERSON_DONE person={full_name} credits={credits}",
                )
                email, email_status = _extract_match_email(mp)
            except Exception as exc:
                stats["errors"] += 1
                append_exception(run_log_path, f"MATCH_ERROR company={company.normalized_name} person={full_name}", exc)
                time.sleep(APOLLO_BACKOFF_SECS)
                continue

            merged = dict(p)
            if email:
                merged["email"] = email
            if email_status:
                merged["email_status"] = email_status
            matches.append(merged)

        stats["enriched"] = len(matches)
        append_and_print(run_log_path, f"ENRICH_FALLBACK_DONE matches={len(matches)}")

    seen_emails: dict[str, str] = {}
    for person in matches:
        apollo_id = safe_str(person.get("id")).strip()
        full_name = safe_str(person.get("name")).strip() or f"{safe_str(person.get('first_name'))} {safe_str(person.get('last_name'))}".strip()
        full_name = full_name.strip()
        if not full_name:
            _increment_counter(stats["skip_reasons"], "missing_person_name")
            continue

        norm_person = normalize_person_name(full_name)
        title = safe_str(person.get("title")).strip() or safe_str(person.get("headline")).strip()
        loc_str = _person_location_string(person)
        email, email_status = _extract_best_email(person)
        if email:
            _increment_counter(stats["apollo_email_status_counts"], email_status or "unknown")
        title_match = _apollo_title_matches(title)
        fallback_title_match = _apollo_leadership_fallback_title_matches(title)
        broad_fallback_title_match = _apollo_broad_fallback_title_matches(title)
        last_resort_title_match = _apollo_last_resort_title_matches(title)
        title_allowed = (
            title_match
            or fallback_title_match
            or (allow_broad_fallback_titles and broad_fallback_title_match)
            or (allow_last_resort_titles and last_resort_title_match)
        )
        location_match = _apollo_location_matches(stats["location_hint"], loc_str)

        if not title_allowed and (not email or not allow_paid_nonmatching_titles):
            reason = contact_title_rejection_reason(title)
            _increment_counter(stats["skip_reasons"], reason)
            _record_wasted_credit(
                stats,
                reason=reason,
                person_name=full_name,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                (
                    f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"reason={reason} title={title or '[NONE]'}"
                ),
            )
            continue
        if last_resort_title_match and email and allow_last_resort_titles:
            stats["accepted_last_resort_title_emails"] += 1
            append_and_print(
                run_log_path,
                (
                    f"PERSON_TITLE_NOTE person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"accepted=true reason=last_resort_title title={title or '[NONE]'}"
                ),
            )
        elif broad_fallback_title_match and email and allow_broad_fallback_titles:
            stats["accepted_broad_fallback_title_emails"] += 1
            append_and_print(
                run_log_path,
                (
                    f"PERSON_TITLE_NOTE person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"accepted=true reason=broad_fallback_title title={title or '[NONE]'}"
                ),
            )
        elif not (title_match or fallback_title_match) and email:
            stats["accepted_paid_nonmatching_title_emails"] += 1
            append_and_print(
                run_log_path,
                (
                    f"PERSON_TITLE_NOTE person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"accepted=true reason=paid_email_revealed title={title or '[NONE]'}"
                ),
            )

        if not email:
            _increment_counter(stats["skip_reasons"], "missing_or_placeholder_email")
            _record_wasted_credit(
                stats,
                reason=_missing_email_waste_reason(person, email_status),
                person_name=full_name,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} reason=missing_or_placeholder_email status={email_status or '[NONE]'}",
            )
            continue

        if email_status != "verified":
            stats["unverified_emails"] += 1
            reason = f"apollo_non_verified_email:{email_status or 'unknown'}"
            _increment_counter(stats["skip_reasons"], reason)
            _store_rejected_apollo_email(
                company=company,
                person_name=full_name,
                title=title,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                reason=reason,
                source_workflow="apollo_company_topup",
                run_log_path=run_log_path,
                raw_payload=person,
            )
            _record_wasted_credit(
                stats,
                reason=reason,
                person_name=full_name,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                (
                    f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"reason=apollo_non_verified_email status={email_status or '[NONE]'} email={email}"
                ),
            )
            continue

        if email in seen_emails:
            _increment_counter(stats["skip_reasons"], "duplicate_email_in_batch")
            _record_wasted_credit(
                stats,
                reason="duplicate_email",
                person_name=full_name,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                note=f"first_person={seen_emails.get(email) or ''}",
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                (
                    f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} "
                    f"reason=duplicate_email_in_batch email={email} "
                    f"first_person={seen_emails.get(email) or '[NONE]'}"
                ),
            )
            continue
        seen_emails[email] = full_name

        if _email_has_prior_real_initial_send(email):
            _increment_counter(stats["skip_reasons"], "email_already_contacted")
            _record_wasted_credit(
                stats,
                reason="already_contacted_email",
                person_name=full_name,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} reason=email_already_contacted email={email}",
            )
            continue

        if not _email_matches_domain(email, domain):
            if not allow_alternate_domain_emails:
                _increment_counter(stats["skip_reasons"], "alternate_domain_email_blocked")
                _record_wasted_credit(
                    stats,
                    reason="alternate_domain_email_blocked",
                    person_name=full_name,
                    email=email,
                    email_status=email_status,
                    apollo_person_id=apollo_id,
                    title=title,
                    run_log_path=run_log_path,
                )
                append_and_print(
                    run_log_path,
                    (
                        f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} "
                        f"reason=alternate_domain_email_blocked email={email} "
                        f"email_domain={_email_domain(email) or '[NONE]'} searched_domain={domain or '[NONE]'}"
                    ),
                )
                continue
            stats["accepted_alternate_domain_emails"] += 1
            append_and_print(
                run_log_path,
                f"PERSON_DOMAIN_NOTE person_id={apollo_id or '[NONE]'} person={full_name} accepted=true email={email} "
                f"email_domain={_email_domain(email) or '[NONE]'} searched_domain={domain or '[NONE]'}",
            )

        if not _person_is_us_based(person):
            stats["accepted_non_us_emails"] += 1
            append_and_print(
                run_log_path,
                f"PERSON_LOCATION_NOTE person_id={apollo_id or '[NONE]'} person={full_name} accepted=true email={email} location={loc_str or '[NONE]'}",
            )

        recruiter = None
        created = False
        if apollo_id:
            recruiter = CompanyRecruiter.objects.filter(company=company, apollo_person_id=apollo_id).order_by("id").first()

        if recruiter is None:
            recruiter = CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_person).order_by("id").first()

        if recruiter is not None and recruiter.email_sent:
            _increment_counter(stats["skip_reasons"], "person_already_contacted")
            _record_wasted_credit(
                stats,
                reason="already_contacted_person",
                person_name=full_name,
                email=email,
                email_status=email_status,
                apollo_person_id=apollo_id,
                title=title,
                run_log_path=run_log_path,
            )
            append_and_print(
                run_log_path,
                f"PERSON_SKIP person_id={apollo_id or '[NONE]'} person={full_name} reason=person_already_contacted email={email}",
            )
            continue

        if recruiter is None:
            recruiter = CompanyRecruiter(
                company=company,
                person_name=full_name,
                email="none",
                email_sent=False,
                email_sent_date=None,
                is_active=True,
                source=CompanyRecruiter.Source.APOLLO,
            )
            created = True

        changed_fields = set()
        if recruiter.person_name != full_name and len(full_name) > len(recruiter.person_name or ""):
            recruiter.person_name = full_name
            changed_fields.add("person_name")

        if apollo_id and recruiter.apollo_person_id != apollo_id:
            recruiter.apollo_person_id = apollo_id
            changed_fields.add("apollo_person_id")

        if title and recruiter.apollo_title != title:
            recruiter.apollo_title = title
            changed_fields.add("apollo_title")

        if loc_str and recruiter.apollo_location != loc_str:
            recruiter.apollo_location = loc_str
            changed_fields.add("apollo_location")

        linkedin_url = safe_str(person.get("linkedin_url")).strip()
        if linkedin_url and recruiter.apollo_linkedin_url != linkedin_url:
            recruiter.apollo_linkedin_url = linkedin_url
            changed_fields.add("apollo_linkedin_url")

        if email and recruiter.email != email:
            recruiter.email = email
            changed_fields.add("email")

        if recruiter.source != CompanyRecruiter.Source.APOLLO:
            recruiter.source = CompanyRecruiter.Source.APOLLO
            changed_fields.add("source")

        if recruiter.email_status != email_status:
            recruiter.email_status = email_status
            changed_fields.add("email_status")

        if recruiter.title_match != title_match:
            recruiter.title_match = title_match
            changed_fields.add("title_match")

        if recruiter.location_match != location_match:
            recruiter.location_match = location_match
            changed_fields.add("location_match")

        if created:
            recruiter.save()
            stats["created"] += 1
        elif changed_fields:
            # Do not use update_fields here: CompanyRecruiter.save() normalizes fields and may
            # adjust email_sent flags when email changes.
            recruiter.save()
            stats["updated"] += 1

        if email:
            stats["emails_found"] += 1
            stats["verified_emails"] += 1

        append_and_print(
            run_log_path,
            f"PERSON_DONE person_id={apollo_id or '[NONE]'} person={full_name} title={title or '[NONE]'} title_match={title_match} location={loc_str or '[NONE]'} location_match={location_match} email={email or '[NONE]'} status={email_status or '[NONE]'}",
        )

        if stats["emails_found"] >= apollo_slots:
            break

    stats["credits_not_converted_to_email"] = max(0, stats["credits_consumed"] - stats["emails_found"])
    append_and_print(
        run_log_path,
        f"CREDIT_SUMMARY company={company.normalized_name} credits={stats['credits_consumed']} "
        f"emails_found={stats['emails_found']} not_converted={stats['credits_not_converted_to_email']} "
        f"verified={stats['verified_emails']} unverified={stats['unverified_emails']} "
        f"email_status_counts={stats['apollo_email_status_counts']} "
        f"accepted_alternate_domain={stats['accepted_alternate_domain_emails']} "
        f"accepted_non_us={stats['accepted_non_us_emails']} "
        f"accepted_paid_nonmatching_title={stats['accepted_paid_nonmatching_title_emails']} "
        f"accepted_last_resort_title={stats['accepted_last_resort_title_emails']} "
        f"accepted_broad_fallback_title={stats['accepted_broad_fallback_title_emails']} "
        f"skip_reasons={stats['skip_reasons']}",
    )

    # Advance when we have allowed recipients: legacy emails and/or Apollo people with real emails.
    if legacy_recruiters or stats["emails_found"] > 0:
        sync_job_targets_for_company_pending_jobs(
            company=company,
            max_targets=max_people,
            auto_select=True,
            allow_fallback_contacts=True,
        )
        company.jobs.filter(status=JobPosting.Status.RECRUITERS_PENDING, is_manual_email_job=False).update(status=JobPosting.Status.EMAIL_DISCOVERY_DONE)

    # Persist per-company Apollo run stats so the dashboard can show them without re-running.
    try:
        from django.utils import timezone as _tz
        company.last_apollo_run_at = _tz.now()
        company.last_apollo_emails_found = int(stats.get("emails_found") or 0)
        company.last_apollo_verified_emails_found = int(stats.get("verified_emails") or 0)
        company.last_apollo_unverified_emails_found = int(stats.get("unverified_emails") or 0)
        company.last_apollo_email_status_counts = dict(stats.get("apollo_email_status_counts") or {})
        company.last_apollo_credits_consumed = int(stats.get("credits_consumed") or 0)
        company.save(update_fields=[
            "last_apollo_run_at",
            "last_apollo_emails_found",
            "last_apollo_verified_emails_found",
            "last_apollo_unverified_emails_found",
            "last_apollo_email_status_counts",
            "last_apollo_credits_consumed",
            "updated_at",
        ])
    except Exception:
        pass

    append_and_print(run_log_path, f"END stats={stats}")
    return stats


def fetch_apollo_credits_info() -> dict:
    """
    Calls Apollo's free people-search endpoint with a zero-result query to extract
    any credit/rate-limit metadata Apollo embeds in the response.
    Returns a dict with whatever Apollo provides; empty dict on failure.
    """
    try:
        result = _apollo_post(
            APOLLO_SEARCH_URL,
            {
                "q_organization_domains_list": ["__no_such_domain_probe__.invalid"],
                "per_page": 1,
                "page": 1,
            },
        )
        info = {}
        for key in (
            "credits_remaining",
            "credits_used",
            "credits_used_for_request",
            "total_credits",
            "rate_limit_minute",
            "rate_limit_hourly",
            "num_fetch_result",
        ):
            val = result.get(key)
            if val is not None:
                info[key] = val
        return info
    except Exception:
        return {}


def run_apollo_recruiter_fetch_for_pending_companies(
    *,
    company_name: str = "",
    max_people: int = DEFAULT_MAX_PEOPLE,
) -> dict:
    max_people = _apollo_people_limit()

    if company_name:
        companies = list(Company.objects.filter(is_blocked=False, normalized_name=company_name).order_by("normalized_name"))
        pending_jobs_qs = (
            JobPosting.objects.filter(
                company_ref__in=companies,
                status=JobPosting.Status.RECRUITERS_PENDING,
                company_ref__isnull=False,
                is_manual_email_job=False,
            )
            .select_related("company_ref")
            .order_by("company_ref__normalized_name", "id")
        )
        initial_company_ids = {
            company.id
            for company in companies
            if company.id
        }
    else:
        latest_batch = DailyBatch.objects.order_by("-batch_date", "-id").first()
        if not latest_batch:
            return {"totals": {"companies_seen": 0, "companies_errors": 0, "master_log_path": ""}, "companies": []}

        # Only companies with pending jobs in the latest batch.
        jobs_qs = (
            JobPosting.objects
            .filter(daily_batch=latest_batch, status=JobPosting.Status.RECRUITERS_PENDING, company_ref__isnull=False, is_manual_email_job=False)
            .select_related("company_ref")
        )

        company_ids = list(jobs_qs.values_list("company_ref_id", flat=True).distinct())
        base_qs = Company.objects.filter(is_blocked=False, id__in=company_ids).order_by("normalized_name")

        companies = list(base_qs)
        pending_jobs_qs = jobs_qs.order_by("company_ref__normalized_name", "id")
        initial_company_ids = set(company_ids)

    master_log_path = create_run_log_path("apollo_recruiter_fetch_master", company_name or "all")
    append_and_print(
        master_log_path,
        f"MASTER_START company_filter={company_name or '[LATEST_BATCH_MISSING]'} max_people={max_people}",
    )
    append_and_print(master_log_path, f"MASTER_SELECTION companies={[c.normalized_name for c in companies]}")

    totals = {
        "companies_seen": 0,
        "companies_errors": 0,
        "companies_with_emails": 0,
        "exact_person_jobs_seen": 0,
        "exact_person_emails": 0,
        "exact_person_email_companies": 0,
        "exact_person_fallback_companies": 0,
        "recruiters_created": 0,
        "recruiters_updated": 0,
        "emails_found": 0,
        "verified_emails": 0,
        "unverified_emails": 0,
        "credits_consumed": 0,
        "master_log_path": master_log_path,
    }
    per_company = []
    exact_person_jobs = []
    fallback_company_ids: set[int] = set()
    exact_email_company_ids: set[int] = set()
    exact_person_names_by_company: dict[int, set[str]] = {}

    pending_jobs = list(pending_jobs_qs)
    exact_jobs = [job for job in pending_jobs if job_has_apify_person_lead(job)]
    for job in exact_jobs:
        company = job.company_ref
        if not company or not company.id:
            continue
        totals["exact_person_jobs_seen"] += 1
        lead = get_apify_person_lead(job)
        lead_name = safe_str(lead.get("name")).strip()
        if lead_name:
            exact_person_names_by_company.setdefault(company.id, set()).add(lead_name)
        try:
            stats = upsert_apify_person_recruiter_from_apollo(
                job=job,
                run_log_path=create_run_log_path("apollo_exact_person_fetch", f"job_{job.id}"),
            )
        except Exception as exc:
            totals["companies_errors"] += 1
            stats = {"job_id": job.id, "company": job.company, "person": lead_name, "error": str(exc)[:4000]}

        exact_person_jobs.append(stats)
        totals["exact_person_emails"] += int(stats.get("emails_found") or 0)
        totals["emails_found"] += int(stats.get("emails_found") or 0)
        totals["verified_emails"] += int(stats.get("verified_emails") or 0)
        totals["unverified_emails"] += int(stats.get("unverified_emails") or 0)
        totals["credits_consumed"] += int(stats.get("credits_consumed") or 0)
        if int(stats.get("emails_found") or 0):
            exact_email_company_ids.add(company.id)
        if stats.get("created"):
            totals["recruiters_created"] += int(stats.get("created") or 0)
        if stats.get("updated"):
            totals["recruiters_updated"] += int(stats.get("updated") or 0)
        if not int(stats.get("emails_found") or 0):
            fallback_company_ids.add(company.id)

    if fallback_company_ids:
        totals["exact_person_fallback_companies"] = len(fallback_company_ids)
    if exact_email_company_ids:
        totals["exact_person_email_companies"] = len(exact_email_company_ids)

    remaining_pending_jobs = list(pending_jobs_qs.filter(status=JobPosting.Status.RECRUITERS_PENDING))
    remaining_company_ids = {job.company_ref_id for job in remaining_pending_jobs if job.company_ref_id}
    companies_by_id = {
        company.id: company
        for company in Company.objects.filter(is_blocked=False, id__in=(initial_company_ids | remaining_company_ids)).order_by("normalized_name")
    }
    company_locations = {}
    for company_id in remaining_company_ids:
        raw_loc = safe_str(
            pending_jobs_qs.filter(company_ref_id=company_id)
            .order_by("-updated_at", "-id")
            .values_list("location", flat=True)
            .first()
        ).strip()
        company_locations[company_id] = extract_us_state_from_location(raw_loc)

    companies = [
        companies_by_id[company_id]
        for company_id in sorted(
            remaining_company_ids,
            key=lambda cid: safe_str(getattr(companies_by_id.get(cid), "normalized_name", "")),
        )
        if company_id in companies_by_id
    ]

    for company in companies:
        if not pending_jobs_qs.filter(company_ref=company, status=JobPosting.Status.RECRUITERS_PENDING).exists():
            continue
        totals["companies_seen"] += 1
        log_path = create_run_log_path("apollo_recruiter_fetch_company", company.normalized_name)
        try:
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint=company_locations.get(company.id, ""),
                max_people=max_people,
                run_log_path=log_path,
                exclude_person_names=list(exact_person_names_by_company.get(company.id, set())),
            )
            per_company.append(stats)

            totals["recruiters_created"] += stats["created"]
            totals["recruiters_updated"] += stats["updated"]
            totals["emails_found"] += stats["emails_found"]
            totals["verified_emails"] += stats["verified_emails"]
            totals["unverified_emails"] += int(stats.get("unverified_emails") or 0)
            totals["credits_consumed"] += stats.get("credits_consumed", 0) or 0
            if (stats.get("emails_found") or 0) > 0 or (stats.get("legacy_reused") or 0) > 0:
                exact_email_company_ids.add(company.id)

        except Exception as exc:
            totals["companies_errors"] += 1
            append_exception(master_log_path, f"COMPANY_ERROR company={company.normalized_name}", exc)
            per_company.append({"company": company.normalized_name, "error": str(exc), "run_log_path": log_path})

    totals["companies_with_emails"] = len(exact_email_company_ids)
    append_and_print(master_log_path, f"MASTER_END totals={totals}")
    return {"totals": totals, "exact_person_jobs": exact_person_jobs, "companies": per_company}

