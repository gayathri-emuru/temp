import html
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import requests

from core.constants import DEFAULT_APIFY_ACTOR_ID, DEFAULT_TITLES
from core.utils import safe_str
from core.services.normalization_service import extract_linkedin_company_slug


APIFY_POLL_SECS = 10
APIFY_TIMEOUT_SECS = 600
STATIC_ORGANIZATION_EXCLUSION_SEARCH = [
    "remotehunter",
    "lensa",
    "talentify",
    "jobot",
    "cybercoders",
    "ziprecruiter",
    "dice",
    "snagajob",
    "careerbuilder",
    "monster",
    "indeed",
    "glassdoor",
    "jobvite",
    "getwork",
    "jooble",
    "simplyhired",
    "joblist",
    "jobsearcher",
    "jobrapido",
    "jobhat",
    "nexxt",
    "jobspikr",
    "jobsyn",
    "recruitology",
    "appcast",
    "jobcase",
    "adzuna",
    "jobs2careers",
    "jobthread",
    "beyond",
    "jobscore",
]


class ApifyBadRequestError(RuntimeError):
    pass


class ApifyUnauthorizedError(RuntimeError):
    pass


class ApifyPaymentError(RuntimeError):
    pass


class ApifyForbiddenError(RuntimeError):
    pass


class ApifyHttpError(RuntimeError):
    pass


def _looks_like_usage_limit(response_text: str) -> bool:
    text = safe_str(response_text).lower()
    if not text:
        return False
    return ("hard limit exceeded" in text) or ("usage hard limit" in text) or ("monthly usage" in text)


def extract_location(job: dict) -> str:
    locs = job.get("locations_derived") or []
    if locs and isinstance(locs, list) and isinstance(locs[0], dict):
        state = safe_str(locs[0].get("admin"))
        if state:
            return state

    regions = job.get("regions_derived") or []
    if regions:
        return safe_str(regions[0])

    return ""


def extract_salary(job: dict) -> str:
    raw = job.get("salary_raw") or {}
    if isinstance(raw, dict) and raw:
        lo = raw.get("minValue") or raw.get("value")
        hi = raw.get("maxValue")
        unit = raw.get("unitText", "")
        try:
            if lo and hi:
                return f"${int(float(lo)):,} - ${int(float(hi)):,}/{unit}"
            if lo:
                return f"${int(float(lo)):,}+/{unit}"
        except Exception:
            pass

    ai_min = job.get("ai_salary_minvalue")
    ai_max = job.get("ai_salary_maxvalue")
    ai_unit = safe_str(job.get("ai_salary_unittext"), "YEAR")
    try:
        if ai_min and ai_max:
            return f"${int(float(ai_min)):,} - ${int(float(ai_max)):,}/{ai_unit}"
    except Exception:
        pass

    return ""


def flatten_apify_job(job: dict) -> dict:
    linkedin_url = safe_str(job.get("url"))
    org_url = safe_str(job.get("linkedin_org_url"))
    org_slug = safe_str(job.get("linkedin_org_slug")) or safe_str(job.get("organization_slug"))
    if not org_slug:
        org_slug = extract_linkedin_company_slug(org_url)

    location = extract_location(job) or "United States"

    return {
        "external_job_id": safe_str(job.get("id")),
        "linkedin_url": linkedin_url,
        "apply_url": safe_str(job.get("external_apply_url")) or linkedin_url,
        "title": html.unescape(safe_str(job.get("title"))),
        "company": html.unescape(safe_str(job.get("organization"))),
        "location": location,
        "salary": extract_salary(job),
        "description": safe_str(job.get("description_text")),
        "apify_linkedin_org_url": org_url,
        "apify_linkedin_org_slug": org_slug,
        "company_linkedin_profile_url": safe_str(job.get("organization_url")),
        "recruiter_name": safe_str(job.get("recruiter_name")),
        "recruiter_title": safe_str(job.get("recruiter_title")),
        "recruiter_linkedin": safe_str(job.get("recruiter_url")),
        "ai_hiring_mgr_name": safe_str(job.get("ai_hiring_manager_name")),
        "ai_hiring_mgr_email": safe_str(job.get("ai_hiring_manager_email_address")),
    }


def _get_apify_token() -> str:
    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is missing in .env")
    return token


def _request_with_token(method: str, url: str, token: str, **kwargs):
    headers = kwargs.pop("headers", {}) or {}
    headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })

    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )

    if response.status_code == 400:
        raise ApifyBadRequestError(
            f"Apify 400 Bad Request.\nURL: {url}\nResponse:\n{response.text[:4000]}"
        )

    if response.status_code == 401:
        raise ApifyUnauthorizedError(
            f"Apify token is invalid or unauthorized.\nResponse:\n{response.text[:4000]}"
        )

    if response.status_code == 402:
        raise ApifyPaymentError(
            f"Apify account/payment/credits issue.\nResponse:\n{response.text[:4000]}"
        )

    if response.status_code == 403:
        # Apify sometimes returns 403 for account-feature/usage-limit situations.
        # Treat those like a quota/payment exhaustion so rotation can move on.
        try:
            data = response.json() or {}
            err = data.get("error") or {}
            err_type = safe_str(err.get("type")).lower()
            err_msg = safe_str(err.get("message")).lower()
            if err_type == "platform-feature-disabled" or _looks_like_usage_limit(err_msg):
                raise ApifyPaymentError(
                    f"Apify usage limit/feature disabled.\nResponse:\n{response.text[:4000]}"
                )
        except ValueError:
            # Non-JSON; fall through to heuristic.
            pass

        if _looks_like_usage_limit(response.text):
            raise ApifyPaymentError(
                f"Apify usage limit/feature disabled.\nResponse:\n{response.text[:4000]}"
            )

        raise ApifyForbiddenError(
            f"Apify token is forbidden for this actor/account.\nResponse:\n{response.text[:4000]}"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise ApifyHttpError(
            f"Apify HTTP error {response.status_code}.\nURL: {url}\nResponse:\n{response.text[:4000]}"
        )

    return response


def _clean_string_list(values):
    out = []
    seen = set()
    for value in values or []:
        text = safe_str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def estimate_apify_dataset_cost_usd(job_count: int, rate_per_1000: float = 1.50) -> float:
    try:
        count = max(0, int(job_count or 0))
    except Exception:
        count = 0
    return round((count * float(rate_per_1000 or 0)) / 1000.0, 4)


def _fetch_jobs_with_single_token(
    token: str,
    lookback_hours: int,
    max_jobs: int,
    actor_id: str,
    organization_exclusion_search=None,
    organization_slug_exclusion_filter=None,
):
    now_utc = datetime.now(timezone.utc)
    effective_lookback_hours = max(int(lookback_hours or 0), 1)
    date_posted_after = (now_utc - timedelta(hours=effective_lookback_hours)).strftime("%Y-%m-%d")
    time_range = f"{effective_lookback_hours}h"
    max_jobs = int(max_jobs or 0)
    if max_jobs <= 0:
        max_jobs = 1

    payload = {
        "datePostedAfter": date_posted_after,
        "maxItems": max_jobs,
        "limit": max_jobs,
        "titleSearch": DEFAULT_TITLES,
        "locationSearch": ["United States"],
        "descriptionType": "text",
        "includeAi": False,
        "timeRange": time_range,
        "titleExclusionSearch": [
            "Recruiter",
            "Sales",
            "Marketing",
            "Manager",
            "Director",
            "Vice President",
            "VP ",
            "Principal",
            "Staff ",
            "Distinguished",
        ],
        "removeAgency": True,
        "organizationExclusionSearch": _clean_string_list(
            [*STATIC_ORGANIZATION_EXCLUSION_SEARCH, *(organization_exclusion_search or [])]
        ),
        "EmploymentTypeFilter": ["FULL_TIME"],
        "seniorityFilter": [
            "Entry level",
            "Associate",
            "Mid-Senior level",
            "Not Applicable",
        ],
    }
    slug_exclusions = _clean_string_list(organization_slug_exclusion_filter or [])
    if slug_exclusions:
        payload["organizationSlugExclusionFilter"] = slug_exclusions

    start_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    start_response = _request_with_token("POST", start_url, token, json=payload, timeout=30)
    start_json = start_response.json()

    if "data" not in start_json or "id" not in start_json["data"]:
        raise RuntimeError(f"Unexpected Apify start response: {start_json}")

    run_id = start_json["data"]["id"]

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline = time.time() + APIFY_TIMEOUT_SECS
    status_data = {}

    while time.time() < deadline:
        time.sleep(APIFY_POLL_SECS)
        status_response = _request_with_token("GET", status_url, token, timeout=20)
        status_data = status_response.json()["data"]
        status = status_data["status"]

        if status == "SUCCEEDED":
            break

        if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
            raise RuntimeError(f"Apify run ended with status: {status}")

    else:
        raise RuntimeError("Apify run timed out")

    dataset_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json&limit={max_jobs}"
    dataset_response = _request_with_token("GET", dataset_url, token, timeout=60)
    data = dataset_response.json()
    items = data[: int(max_jobs or 0)] if isinstance(data, list) and int(max_jobs or 0) > 0 else data
    if not isinstance(items, list):
        items = []
    metadata = {
        "run_id": run_id,
        "dataset_id": status_data.get("defaultDatasetId"),
        "status": status_data.get("status"),
        "returned_jobs": len(items),
        "usage_total_usd": status_data.get("usageTotalUsd"),
        "estimated_dataset_cost_usd": estimate_apify_dataset_cost_usd(len(items)),
        "pricing_note": "$1.50 per 1,000 returned dataset jobs",
    }
    return items, metadata


def fetch_jobs_from_apify_with_rotation(
    lookback_hours: int,
    max_jobs: int,
    actor_id: str = DEFAULT_APIFY_ACTOR_ID,
    organization_exclusion_search=None,
    organization_slug_exclusion_filter=None,
):
    """
    Tries Apify API keys stored in DB (rotation), falling back to APIFY_API_TOKEN env var.

    Returns: (jobs_json, key_obj_with_key_name)
    """
    tried_key_ids = set()
    tried_key_names = []

    def _pick_next_db_key():
        from django.db import transaction
        from django.utils import timezone

        from core.models import ApifyApiKey

        with transaction.atomic():
            # Prefer active, non-exhausted keys first.
            qs = (
                ApifyApiKey.objects
                .select_for_update()
                .filter(is_active=True, is_exhausted=False)
                .exclude(id__in=tried_key_ids)
                .order_by("last_used_at", "rotation_order", "id")
            )
            key_obj = qs.first()
            if not key_obj:
                # If the user accidentally deactivated all keys in admin, still try them
                # (rotation will deactivate/mark exhausted again on failures).
                qs = (
                    ApifyApiKey.objects
                    .select_for_update()
                    .filter(is_exhausted=False)
                    .exclude(id__in=tried_key_ids)
                    .order_by("last_used_at", "rotation_order", "id")
                )
                key_obj = qs.first()
            if not key_obj:
                return None

            key_obj.last_used_at = timezone.now()
            key_obj.save(update_fields=["last_used_at", "updated_at"])
            tried_key_ids.add(int(key_obj.id))
            tried_key_names.append(safe_str(getattr(key_obj, "key_name", "")) or f"key_{key_obj.id}")
            return key_obj

    def _record_success(key_obj):
        from django.utils import timezone

        key_obj.last_success_at = timezone.now()
        key_obj.last_error = ""
        key_obj.save(update_fields=["last_success_at", "last_error", "updated_at"])

    def _record_error(key_obj, error: Exception, *, deactivate: bool = False, exhaust: bool = False):
        key_obj.last_error = safe_str(str(error))[:4000]
        if deactivate:
            key_obj.is_active = False
        if exhaust:
            key_obj.is_exhausted = True
        fields = ["last_error", "updated_at"]
        if deactivate:
            fields.append("is_active")
        if exhaust:
            fields.append("is_exhausted")
        key_obj.save(update_fields=fields)

    # 1) DB rotation (preferred if configured)
    try:
        from core.models import ApifyApiKey

        has_db_keys = ApifyApiKey.objects.exists()

        while True:
            key_obj = _pick_next_db_key()
            if not key_obj:
                break

            token = safe_str(getattr(key_obj, "api_key", "")).strip()
            if not token:
                _record_error(key_obj, RuntimeError("Empty api_key"), deactivate=True)
                continue

            try:
                jobs, metadata = _fetch_jobs_with_single_token(
                    token=token,
                    lookback_hours=lookback_hours,
                    max_jobs=max_jobs,
                    actor_id=actor_id,
                    organization_exclusion_search=organization_exclusion_search,
                    organization_slug_exclusion_filter=organization_slug_exclusion_filter,
                )
                _record_success(key_obj)
                return jobs, key_obj, metadata
            except ApifyBadRequestError as e:
                _record_error(key_obj, e)
                raise
            except ApifyPaymentError as e:
                _record_error(key_obj, e, exhaust=True)
                continue
            except (ApifyUnauthorizedError, ApifyForbiddenError) as e:
                _record_error(key_obj, e, deactivate=True)
                continue
            except Exception as e:
                _record_error(key_obj, e)
                continue
    except Exception:
        raise

    # 2) Env fallback (only if DB keys are not configured, or explicitly allowed)
    allow_env_fallback = os.getenv("APIFY_ALLOW_ENV_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
    if has_db_keys and not allow_env_fallback:
        try:
            from core.models import ApifyApiKey

            rows = list(
                ApifyApiKey.objects
                .order_by("rotation_order", "key_name", "id")
                .values("key_name", "is_active", "is_exhausted", "last_error")
            )
        except Exception:
            rows = []

        summary = "; ".join(
            [
                f"{safe_str(r.get('key_name'))} active={r.get('is_active')} exhausted={r.get('is_exhausted')} err={safe_str(r.get('last_error'))[:120]}"
                for r in (rows[:10] if rows else [])
            ]
        )
        raise RuntimeError(
            "No usable ApifyApiKey in admin. "
            f"Tried={', '.join([n for n in tried_key_names if n]) or '[none]'}; "
            f"Keys={summary or '[no rows]'}; "
            "To allow falling back to APIFY_API_TOKEN, set APIFY_ALLOW_ENV_FALLBACK=1."
        )

    token = _get_apify_token()
    jobs, metadata = _fetch_jobs_with_single_token(
        token=token,
        lookback_hours=lookback_hours,
        max_jobs=max_jobs,
        actor_id=actor_id,
        organization_exclusion_search=organization_exclusion_search,
        organization_slug_exclusion_filter=organization_slug_exclusion_filter,
    )

    fake_key_obj = SimpleNamespace(key_name="ENV_APIFY_API_TOKEN")
    return jobs, fake_key_obj, metadata
