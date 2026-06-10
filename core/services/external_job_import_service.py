from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from django.db import transaction
from django.db.utils import IntegrityError
from django.utils import timezone

from core.models import Company, DailyBatch, JobPosting
from core.services.app_settings_service import get_company_cooldown_days, get_max_people_per_company
from core.services.company_cooldown_service import should_skip_new_job_for_company
from core.services.company_domain_service import is_usable_company_domain, normalize_domain_value
from core.services.company_resolution_service import resolve_company_normalized_name
from core.services.dedupe_service import find_duplicate_reason, get_prior_7d_company_roles
from core.services.job_target_sync_service import sync_job_targets_for_job
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
from core.services.openai_filter_service import classify_job_apply_or_reject_with_reason
from core.utils import compact_spaces, normalize_job_or_generic_url, safe_str


DEFAULT_REQUEST_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class ExternalJobDetails:
    job_url: str
    final_url: str
    status_code: int
    page_html: str
    external_job_id: str
    title: str
    company: str
    location: str
    description_text: str
    apply_url: str
    company_domain: str
    company_website: str


def _sleep_seconds() -> float:
    try:
        return float(os.getenv("EXTERNAL_JOB_IMPORT_SLEEP_SECONDS", "0.8") or "0.8")
    except Exception:
        return 0.8


def _strip_tags(text: str) -> str:
    text = safe_str(text)
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ")
    return compact_spaces(text)


def split_external_job_urls(raw_text: str) -> list[str]:
    raw_text = safe_str(raw_text)
    if not raw_text:
        return []

    candidates = re.findall(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+)", raw_text, flags=re.I)
    urls: list[str] = []
    for item in candidates:
        cleaned = safe_str(item).strip().rstrip("),.;\"'<>")
        if cleaned:
            urls.append(cleaned)

    if urls:
        return urls

    for line in raw_text.splitlines():
        line = safe_str(line).strip().rstrip("),.;\"'<>")
        if line:
            urls.append(line)
    return urls


def canonicalize_external_job_url(raw_url: str) -> str:
    normalized = normalize_job_or_generic_url(raw_url)
    if not normalized:
        return ""

    try:
        parsed = urlparse(normalized)
        if not parsed.scheme or not parsed.netloc:
            return ""
        cleaned = parsed._replace(query="", fragment="")
        return urlunparse(cleaned).rstrip("/")
    except Exception:
        return normalized.rstrip("/")


def _host_matches_allowed(normalized_url: str, allowed_hosts: tuple[str, ...] | None) -> bool:
    if not allowed_hosts:
        return True
    try:
        host = (urlparse(normalized_url).netloc or "").lower().strip(".")
    except Exception:
        host = ""
    allowed = {safe_str(item).lower().strip(".") for item in allowed_hosts if safe_str(item)}
    if not host or not allowed:
        return False
    return host in allowed


def _is_dice_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().strip(".")
        return host in {"dice.com", "www.dice.com"} and "/job-detail/" in (parsed.path or "").lower()
    except Exception:
        return False


def _is_icims_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().strip(".")
        return bool(host.endswith(".icims.com") and "/jobs/" in (parsed.path or "").lower())
    except Exception:
        return False


def _icims_fetch_url(url: str) -> str:
    if not _is_icims_url(url):
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query or "")
    qs["in_iframe"] = ["1"]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True), fragment=""))


def _external_id_from_url(url: str) -> str:
    url = canonicalize_external_job_url(url)
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().strip(".")
    path = parsed.path or ""
    if host in {"dice.com", "www.dice.com"}:
        match = re.search(r"/job-detail/([^/?#]+)", path, flags=re.I)
        if match:
            return f"dice:{safe_str(match.group(1)).lower()}"[:255]
    if host.endswith(".icims.com"):
        match = re.search(r"/jobs/([^/]+)/", path, flags=re.I)
        if match:
            return f"icims:{host}:{safe_str(match.group(1))}"[:255]
    return f"external:{sha256(url.encode('utf-8')).hexdigest()}"[:255]


def _extract_json_ld_blocks(page_html: str) -> list[dict]:
    page_html = safe_str(page_html)
    if not page_html:
        return []

    blocks: list[dict] = []
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page_html,
        flags=re.I | re.S,
    ):
        raw = safe_str(match.group(1)).strip()
        if not raw:
            continue
        raw = re.sub(r"^\s*<!--", "", raw)
        raw = re.sub(r"-->\s*$", "", raw)
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            blocks.append(data)
        elif isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
    return blocks


def _flatten_graph(obj: dict) -> list[dict]:
    graph = obj.get("@graph") if isinstance(obj, dict) else None
    if isinstance(graph, list):
        return [item for item in graph if isinstance(item, dict)]
    return [obj] if isinstance(obj, dict) else []


def _find_job_posting_json_ld(page_html: str) -> dict:
    for block in _extract_json_ld_blocks(page_html):
        for obj in _flatten_graph(block):
            type_value = obj.get("@type")
            if isinstance(type_value, str) and type_value.lower() == "jobposting":
                return obj
            if isinstance(type_value, list) and any(str(t).lower() == "jobposting" for t in type_value):
                return obj
    return {}


def _extract_meta_content(page_html: str, *, attr: str, name: str) -> str:
    page_html = safe_str(page_html)
    if not page_html:
        return ""
    pattern = re.compile(
        rf"<meta[^>]+{attr}\s*=\s*[\"']{re.escape(name)}[\"'][^>]+content\s*=\s*[\"']([^\"']+)[\"']",
        flags=re.I,
    )
    match = pattern.search(page_html)
    if not match:
        pattern = re.compile(
            rf"<meta[^>]+content\s*=\s*[\"']([^\"']+)[\"'][^>]+{attr}\s*=\s*[\"']{re.escape(name)}[\"']",
            flags=re.I,
        )
        match = pattern.search(page_html)
    if not match:
        return ""
    return compact_spaces(html.unescape(safe_str(match.group(1))))


def _extract_title_tag(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", safe_str(page_html), flags=re.I | re.S)
    if not match:
        return ""
    return compact_spaces(_strip_tags(match.group(1)))


def _extract_h1(page_html: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", safe_str(page_html), flags=re.I | re.S)
    if not match:
        return ""
    return compact_spaces(_strip_tags(match.group(1)))


def _extract_icims_sd(page_html: str) -> dict:
    match = re.search(r"var\s+icimsSD\s*=\s*(\{.*?\});", safe_str(page_html), flags=re.I | re.S)
    if not match:
        return {}
    raw = safe_str(match.group(1))
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_organization(obj: dict) -> tuple[str, str]:
    hiring_org = obj.get("hiringOrganization") if isinstance(obj, dict) else {}
    if not isinstance(hiring_org, dict):
        return "", ""
    company = compact_spaces(safe_str(hiring_org.get("name")))
    same_as = hiring_org.get("sameAs")
    if isinstance(same_as, list):
        same_as = next((x for x in same_as if _clean_company_domain_for_storage(safe_str(x))), "") or next(
            (x for x in same_as if safe_str(x)),
            "",
        )
    company_website = safe_str(same_as)
    return company, company_website


def _clean_company_domain_for_storage(value: str) -> str:
    domain = normalize_domain_value(value)
    if not domain:
        return ""

    for prefix in ("careers.", "jobs.", "apply."):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
            break

    if not is_usable_company_domain(domain):
        return ""
    return domain


def _location_from_address(address: dict) -> str:
    if not isinstance(address, dict):
        return ""

    values = []
    locality = safe_str(address.get("addressLocality"))
    region = safe_str(address.get("addressRegion"))
    country = safe_str(address.get("addressCountry"))
    for value in (locality, region, country):
        value = value.strip()
        if value and value.upper() != "UNAVAILABLE":
            values.append(value)

    if len(values) >= 2 and values[0].lower() == "remote" and values[-1].upper() == "US":
        return "Remote, United States"
    if values:
        return ", ".join(values)
    return ""


def _extract_location_from_jobposting(obj: dict) -> str:
    job_location = obj.get("jobLocation") if isinstance(obj, dict) else None
    if isinstance(job_location, dict):
        job_location = [job_location]
    if not isinstance(job_location, list) or not job_location:
        return ""

    locations: list[str] = []
    for item in job_location:
        if not isinstance(item, dict):
            continue
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        location = _location_from_address(address)
        if location and location not in locations:
            locations.append(location)
    return " | ".join(locations[:4])


def _company_from_host(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower().strip(".")
    except Exception:
        host = ""
    host = re.sub(r"\.icims\.com$", "", host)
    host = re.sub(r"^(careers|jobs|homeoffice|homeoffice-na|us|volunteers)[-_]", "", host)
    host = host.replace("-", " ")
    return compact_spaces(host).title()


def parse_external_job_details_from_html(*, job_url: str, final_url: str, status_code: int, page_html: str) -> ExternalJobDetails:
    canonical_url = canonicalize_external_job_url(job_url)
    jobposting = _find_job_posting_json_ld(page_html)
    icims_sd = _extract_icims_sd(page_html)
    icims_job = icims_sd.get("job") if isinstance(icims_sd.get("job"), dict) else {}

    org_company, company_website = _extract_organization(jobposting)
    company = org_company or safe_str(icims_sd.get("companyName")).strip() or _company_from_host(canonical_url)

    title = (
        compact_spaces(safe_str(jobposting.get("title")))
        or compact_spaces(safe_str(icims_job.get("title")))
        or _extract_h1(page_html)
    )
    if not title:
        meta_title = _extract_meta_content(page_html, attr="property", name="og:title") or _extract_title_tag(page_html)
        title = re.split(r"\s+\|\s+", meta_title, maxsplit=1)[0].strip()

    location = (
        _extract_location_from_jobposting(jobposting)
        or compact_spaces(safe_str(icims_job.get("location")))
        or "United States"
    )

    description_html = safe_str(jobposting.get("description"))
    description = _strip_tags(description_html)
    if not description:
        description = (
            _extract_meta_content(page_html, attr="property", name="og:description")
            or _extract_meta_content(page_html, attr="name", name="description")
        )
    if not description:
        description = _strip_tags(page_html)[:12000]

    apply_url = canonicalize_external_job_url(safe_str(jobposting.get("url"))) or canonical_url
    company_domain = _clean_company_domain_for_storage(company_website)

    return ExternalJobDetails(
        job_url=canonical_url,
        final_url=canonicalize_external_job_url(final_url) or final_url,
        status_code=int(status_code or 0),
        page_html=page_html,
        external_job_id=_external_id_from_url(canonical_url),
        title=compact_spaces(title),
        company=compact_spaces(company),
        location=compact_spaces(location),
        description_text=compact_spaces(description),
        apply_url=apply_url,
        company_domain=company_domain,
        company_website=company_website,
    )


def fetch_external_job_details(job_url: str) -> ExternalJobDetails:
    canonical_url = canonicalize_external_job_url(job_url)
    if not canonical_url:
        raise ValueError("Invalid external job URL")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    fetch_urls = []
    iframe_url = _icims_fetch_url(canonical_url)
    for candidate in (iframe_url, canonical_url):
        if candidate and candidate not in fetch_urls:
            fetch_urls.append(candidate)

    def _fetch_with_powershell(fetch_url: str) -> tuple[int, str, str]:
        script = """
$ErrorActionPreference = 'Stop'
$Url = $env:CODEX_EXTERNAL_JOB_FETCH_URL
$resp = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 25
$responseUri = ''
try { $responseUri = $resp.BaseResponse.ResponseUri.AbsoluteUri } catch {}
@{status=[int]$resp.StatusCode; url=$responseUri; content=[string]$resp.Content} | ConvertTo-Json -Compress
""".strip()
        env = os.environ.copy()
        env["CODEX_EXTERNAL_JOB_FETCH_URL"] = fetch_url
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS + 10,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(safe_str(proc.stderr) or f"PowerShell fetch failed with code {proc.returncode}")
        payload = json.loads(safe_str(proc.stdout))
        return int(payload.get("status") or 0), safe_str(payload.get("url")) or fetch_url, safe_str(payload.get("content"))

    last_exc: Exception | None = None
    response_payload: tuple[int, str, str] | None = None
    for fetch_url in fetch_urls:
        try:
            candidate_response = requests.get(
                fetch_url,
                headers=headers,
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            candidate_response.raise_for_status()
            response_payload = (
                int(candidate_response.status_code or 0),
                safe_str(getattr(candidate_response, "url", "")) or fetch_url,
                safe_str(candidate_response.text),
            )
            break
        except requests.HTTPError as exc:
            last_exc = exc
            # iCIMS sometimes rejects the embedded iframe URL while allowing
            # the canonical job URL, so keep trying the fallback forms.
            continue

    if response_payload is None:
        for fetch_url in fetch_urls:
            try:
                response_payload = _fetch_with_powershell(fetch_url)
                break
            except Exception as exc:
                last_exc = exc
                continue

    if response_payload is None:
        if last_exc:
            raise last_exc
        raise RuntimeError("Could not fetch external job page")

    status_code, final_url, page_html = response_payload
    return parse_external_job_details_from_html(
        job_url=canonical_url,
        final_url=final_url,
        status_code=status_code,
        page_html=page_html,
    )


def _job_exists_by_url_path_suffix(normalized_url: str) -> bool:
    normalized_url = safe_str(normalized_url)
    if not normalized_url:
        return False
    try:
        parsed = urlparse(normalized_url)
        path = safe_str(parsed.path).rstrip("/")
        if path and JobPosting.objects.filter(normalized_linkedin_url__endswith=path).exists():
            return True
        if path and JobPosting.objects.filter(normalized_apply_url__endswith=path).exists():
            return True
    except Exception:
        pass
    return JobPosting.objects.filter(normalized_linkedin_url=normalized_url).exists()


def _normalize_company_best_effort(raw_company: str) -> str:
    return normalize_company_name(raw_company) or safe_str(raw_company).strip().lower()


def _row_for_details(details: ExternalJobDetails, *, status: str, detail: str = "", job_id: int | None = None) -> dict:
    row = {
        "job_url": details.job_url,
        "company": details.company,
        "title": details.title,
        "location": details.location,
        "company_domain": details.company_domain,
        "status": status,
        "detail": detail,
    }
    if job_id:
        row["job_id"] = int(job_id)
    return row


def _set_url_row_status(stats: dict, normalized_url: str, status: str, detail: str = "") -> None:
    for url_row in stats["url_rows"]:
        if url_row.get("normalized_url") == normalized_url and url_row.get("status") in {"queued", "processing"}:
            url_row["status"] = status
            if detail:
                url_row["detail"] = detail
            return


def run_external_job_url_import(
    *,
    raw_urls_text: str,
    cooldown_days: int | None = None,
    apply_cooldown_filters: bool = True,
    skip_blocked_companies: bool = True,
    use_openai_filter: bool = False,
    dry_run: bool = True,
    force_refetch: bool = False,
    source_platform: str = JobPosting.SourcePlatform.EXTERNAL,
    source_label: str = "External",
    allowed_hosts: tuple[str, ...] | None = None,
    log_path: str = "",
) -> dict:
    batch_date = timezone.localdate()
    cooldown_days = get_company_cooldown_days() if cooldown_days is None else int(cooldown_days or 0)
    sleep_s = _sleep_seconds()
    raw_urls = split_external_job_urls(raw_urls_text)

    stats: dict = {
        "batch_date": batch_date.isoformat(),
        "raw_lines": len(raw_urls),
        "invalid_urls": 0,
        "unique_urls": 0,
        "queued_urls": 0,
        "skipped_existing_url": 0,
        "skipped_existing_external_job_id": 0,
        "scrape_ok": 0,
        "scrape_failed": 0,
        "skipped_blocked_company": 0,
        "skipped_company_cooldown": 0,
        "skipped_duplicate": 0,
        "filtered_reject": 0,
        "created_jobs": 0,
        "updated_jobs": 0,
        "created_job_ids": [],
        "job_errors": 0,
        "dry_run": bool(dry_run),
        "use_openai_filter": bool(use_openai_filter),
        "source_platform": safe_str(source_platform) or JobPosting.SourcePlatform.EXTERNAL,
        "source_label": safe_str(source_label) or "External",
        "url_rows": [],
        "job_status_rows": [],
        "filter_decision_rows": [],
        "error_samples": [],
    }

    def _log(message: str) -> None:
        if not log_path:
            return
        try:
            from core.services.file_run_logger import append_and_print

            append_and_print(log_path, message)
        except Exception:
            return

    seen = set()
    candidates: list[tuple[str, str]] = []

    for raw_url in raw_urls:
        normalized_url = canonicalize_external_job_url(raw_url)
        row = {"raw_url": raw_url, "normalized_url": normalized_url, "status": "", "detail": ""}
        if not normalized_url:
            stats["invalid_urls"] += 1
            row["status"] = "invalid_url"
            stats["url_rows"].append(row)
            continue
        if not _host_matches_allowed(normalized_url, allowed_hosts):
            stats["invalid_urls"] += 1
            row["status"] = "invalid_url"
            row["detail"] = f"not a {source_label} URL"
            stats["url_rows"].append(row)
            continue
        if normalized_url in seen:
            row["status"] = "duplicate_in_input"
            stats["url_rows"].append(row)
            continue
        seen.add(normalized_url)
        if not force_refetch and _job_exists_by_url_path_suffix(normalized_url):
            stats["skipped_existing_url"] += 1
            row["status"] = "skipped_existing_url"
            stats["url_rows"].append(row)
            continue
        external_job_id = _external_id_from_url(normalized_url)
        if not force_refetch and external_job_id and JobPosting.objects.filter(external_job_id=external_job_id).exists():
            stats["skipped_existing_external_job_id"] += 1
            row["status"] = "skipped_existing_external_job_id"
            row["detail"] = external_job_id
            stats["url_rows"].append(row)
            continue
        row["status"] = "queued"
        stats["url_rows"].append(row)
        candidates.append((raw_url, normalized_url))

    stats["unique_urls"] = len(seen)
    stats["queued_urls"] = len(candidates)
    _log(
        f"{safe_str(source_label).upper()}_JOB_IMPORT_START "
        f"batch_date={batch_date} urls_total={len(raw_urls)} queued={len(candidates)} "
        f"dry_run={bool(dry_run)} use_openai_filter={bool(use_openai_filter)}"
    )

    daily_batch = None
    if not dry_run:
        daily_batch, _ = DailyBatch.objects.get_or_create(
            batch_date=batch_date,
            defaults={
                "lookback_hours": 24,
                "max_jobs_requested": 0,
                "apify_run_status": DailyBatch.RunStatus.SUCCESS,
                "notes": f"{safe_str(source_label) or 'External'} job URL import",
            },
        )

    for idx, (_, normalized_url) in enumerate(candidates, start=1):
        try:
            _set_url_row_status(stats, normalized_url, "processing")
            if sleep_s and idx > 1:
                time.sleep(sleep_s)
            details = fetch_external_job_details(normalized_url)
            stats["scrape_ok"] += 1

            if not details.title or not details.company or not details.description_text:
                stats["scrape_failed"] += 1
                _set_url_row_status(stats, normalized_url, "scrape_failed", "missing title/company/description")
                stats["job_status_rows"].append(_row_for_details(details, status="scrape_failed", detail="missing title/company/description"))
                continue

            if use_openai_filter:
                classifier_result = classify_job_apply_or_reject_with_reason(details.title, details.description_text)
                decision = safe_str(classifier_result.get("decision")).strip().upper()
                reason = safe_str(classifier_result.get("reason")).strip()
                stats["filter_decision_rows"].append(
                    {
                        "job_url": details.job_url,
                        "company": details.company,
                        "title": details.title,
                        "location": details.location,
                        "decision": decision,
                        "reason": reason,
                        "raw_output": safe_str(classifier_result.get("raw_output")).strip(),
                    }
                )
                if decision != "APPLY":
                    stats["filtered_reject"] += 1
                    _set_url_row_status(stats, normalized_url, "openai_reject", reason)
                    stats["job_status_rows"].append(_row_for_details(details, status="rejected", detail=reason))
                    continue

            normalized_company = _normalize_company_best_effort(details.company)
            resolved_company_name, linkedin_company_slug = resolve_company_normalized_name(
                normalized_company_name=normalized_company,
                company_linkedin_url="",
            )
            stored_normalized_company = resolved_company_name or normalized_company
            canonical_company_value = canonical_company_name(stored_normalized_company or details.company)
            canonical_title_value = canonical_title(details.title)
            canonical_location_value = canonical_location(details.location or "United States")
            normalized_title = normalize_title(details.title)
            normalized_location = normalize_location(details.location or "United States") or "united states"
            apply_url = canonicalize_external_job_url(details.apply_url) or normalized_url

            duplicate_reason = find_duplicate_reason(
                normalized_linkedin_url=normalized_url,
                normalized_apply_url=apply_url,
                canonical_company=canonical_company_value,
                canonical_title=canonical_title_value,
                canonical_location=canonical_location_value,
            )
            if duplicate_reason:
                stats["skipped_duplicate"] += 1
                _set_url_row_status(stats, normalized_url, "deduped", duplicate_reason)
                stats["job_status_rows"].append(_row_for_details(details, status="deduped", detail=duplicate_reason))
                continue

            if skip_blocked_companies and Company.objects.filter(normalized_name=stored_normalized_company, is_blocked=True).exists():
                stats["skipped_blocked_company"] += 1
                _set_url_row_status(stats, normalized_url, "skipped_blocked_company", stored_normalized_company)
                stats["job_status_rows"].append(_row_for_details(details, status="skipped_blocked_company", detail=stored_normalized_company))
                continue

            if apply_cooldown_filters:
                skip, skip_reason = should_skip_new_job_for_company(
                    canonical_company=canonical_company_value,
                    batch_date=batch_date,
                    cooldown_days=int(cooldown_days or 0),
                )
                if skip:
                    stats["skipped_company_cooldown"] += 1
                    _set_url_row_status(stats, normalized_url, "skipped_company_cooldown", skip_reason)
                    stats["job_status_rows"].append(_row_for_details(details, status="skipped_company_cooldown", detail=skip_reason))
                    continue

            if dry_run:
                _set_url_row_status(stats, normalized_url, "would_create")
                stats["job_status_rows"].append(_row_for_details(details, status="would_create"))
                continue

            assert daily_batch is not None
            prior_roles = get_prior_7d_company_roles(stored_normalized_company, batch_date)
            linkedin_geo_region_id, linkedin_people_search_urls = generate_linkedin_people_search_data(
                company_linkedin_url="",
                normalized_state=normalized_location,
            )
            dedupe_key = build_dedupe_key(stored_normalized_company or details.company, details.title, details.location or "United States")
            company_domain = normalize_domain_value(details.company_domain)
            company_domain_is_usable = is_usable_company_domain(company_domain)

            with transaction.atomic():
                company_obj, created = Company.objects.get_or_create(
                    normalized_name=stored_normalized_company,
                    defaults={"raw_name_latest": details.company},
                )
                changed_fields: set[str] = set()
                if not created and company_obj.raw_name_latest != details.company:
                    company_obj.raw_name_latest = details.company
                    changed_fields.add("raw_name_latest")
                if linkedin_company_slug and safe_str(company_obj.linkedin_company_slug).strip().lower() != linkedin_company_slug.strip().lower():
                    company_obj.linkedin_company_slug = linkedin_company_slug.strip().lower()
                    changed_fields.add("linkedin_company_slug")
                if company_domain_is_usable and not normalize_domain_value(company_obj.active_domain):
                    company_obj.active_domain = company_domain
                    company_obj.domain_status = Company.DomainStatus.SET
                    changed_fields.update({"active_domain", "domain_status"})
                if changed_fields:
                    changed_fields.add("updated_at")
                    company_obj.save(update_fields=sorted(changed_fields))

                existing_job = None
                if force_refetch:
                    existing_job = JobPosting.objects.filter(external_job_id=details.external_job_id).first()
                    if not existing_job:
                        existing_job = JobPosting.objects.filter(normalized_linkedin_url=normalized_url).first()

                if existing_job:
                    existing_job.title = details.title
                    existing_job.company = details.company
                    existing_job.location = details.location
                    existing_job.description = details.description_text
                    existing_job.description_fingerprint = build_description_fingerprint(details.description_text)
                    existing_job.apply_url = apply_url
                    existing_job.normalized_apply_url = apply_url
                    existing_job.normalized_company = stored_normalized_company
                    existing_job.normalized_title = normalized_title
                    existing_job.normalized_location = normalized_location
                    existing_job.source_platform = safe_str(source_platform) or JobPosting.SourcePlatform.EXTERNAL
                    existing_job.canonical_company = canonical_company_value
                    existing_job.canonical_title = canonical_title_value
                    existing_job.canonical_location = canonical_location_value
                    existing_job.dedupe_key = dedupe_key
                    existing_job.sort_company = build_sort_company(details.company)
                    existing_job.sort_title = build_sort_title(details.title)
                    existing_job.sort_location = build_sort_location(details.location)
                    existing_job.company_ref = company_obj
                    existing_job.save()
                    job_posting = existing_job
                    job_is_new = False
                else:
                    try:
                        job_posting = JobPosting.objects.create(
                            daily_batch=daily_batch,
                            is_manual_import=True,
                            is_manual_email_job=False,
                            source_platform=safe_str(source_platform) or JobPosting.SourcePlatform.EXTERNAL,
                            company_ref=company_obj,
                            external_job_id=safe_str(details.external_job_id),
                            linkedin_url=normalized_url,
                            apply_url=apply_url,
                            normalized_linkedin_url=normalized_url,
                            normalized_apply_url=apply_url,
                            title=details.title,
                            company=details.company,
                            location=details.location,
                            salary="",
                            description=details.description_text,
                            description_fingerprint=build_description_fingerprint(details.description_text),
                            apify_linkedin_org_url="",
                            apify_linkedin_org_slug="",
                            company_linkedin="",
                            recruiter_name="",
                            recruiter_title="",
                            recruiter_linkedin="",
                            ai_hiring_mgr_name="",
                            ai_hiring_mgr_email="",
                            normalized_company=stored_normalized_company,
                            normalized_title=normalized_title,
                            normalized_location=normalized_location,
                            canonical_company=canonical_company_value,
                            canonical_title=canonical_title_value,
                            canonical_location=canonical_location_value,
                            dedupe_key=dedupe_key,
                            sort_company=build_sort_company(details.company),
                            sort_title=build_sort_title(details.title),
                            sort_location=build_sort_location(details.location),
                            prior_7d_company_roles=prior_roles,
                            linkedin_geo_region_id=linkedin_geo_region_id,
                            linkedin_people_search_urls=linkedin_people_search_urls,
                            status=JobPosting.Status.RECRUITERS_PENDING,
                        )
                        job_is_new = True
                    except IntegrityError:
                        stats["skipped_duplicate"] += 1
                        _set_url_row_status(stats, normalized_url, "deduped", "db_unique_constraint")
                        stats["job_status_rows"].append(_row_for_details(details, status="deduped", detail="db_unique_constraint"))
                        continue

                auto_select = os.getenv("AUTO_SELECT_TARGETS_ON_IMPORT", "1").strip().lower() in {"1", "true", "yes", "on"}
                sync_stats = sync_job_targets_for_job(
                    job=job_posting,
                    max_targets=get_max_people_per_company(),
                    auto_select=auto_select,
                )
                if sync_stats.get("targets_upserted"):
                    job_posting.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
                    job_posting.save(update_fields=["status", "updated_at"])

            if job_is_new:
                stats["created_jobs"] += 1
                action = "created"
            else:
                stats["updated_jobs"] += 1
                action = "updated"
            stats["created_job_ids"].append(int(job_posting.id))
            _set_url_row_status(stats, normalized_url, action, f"job_id={job_posting.id}")
            stats["job_status_rows"].append(_row_for_details(details, status=action, detail=f"job_id={job_posting.id}", job_id=job_posting.id))
            log_system_event(
                event_type="imported_external_job_url",
                message=f"{action.title()} {safe_str(source_label).lower() or 'external'} job into batch {batch_date}: {details.company} | {details.title}",
                job_posting=job_posting,
            )

        except Exception as exc:
            stats["scrape_failed"] += 1
            stats["job_errors"] += 1
            _set_url_row_status(stats, normalized_url, "error", str(exc))
            if len(stats["error_samples"]) < 30:
                stats["error_samples"].append(f"url={normalized_url} error={exc}")
            stats["job_status_rows"].append({"job_url": normalized_url, "status": "error", "detail": str(exc)})
            _log(f"JOB_ERROR idx={idx} url={normalized_url} error={exc}")
            log_system_event(event_type="failed_external_job_import", message=f"url={normalized_url} error={exc}")

    stats["job_status_rows"] = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in stats["job_status_rows"]]
    stats["filter_decision_rows"] = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in stats["filter_decision_rows"]]
    _log(
        f"{safe_str(source_label).upper()}_JOB_IMPORT_DONE "
        f"created_jobs={stats['created_jobs']} updated_jobs={stats['updated_jobs']} "
        f"scrape_failed={stats['scrape_failed']} filtered_reject={stats['filtered_reject']}"
    )
    return stats


def result_summary_for_external_job_import(stats: dict | None) -> dict:
    if not isinstance(stats, dict):
        return {}
    skipped_count = (
        int(stats.get("invalid_urls") or 0)
        + int(stats.get("skipped_existing_url") or 0)
        + int(stats.get("skipped_existing_external_job_id") or 0)
        + int(stats.get("skipped_duplicate") or 0)
        + int(stats.get("skipped_blocked_company") or 0)
        + int(stats.get("skipped_company_cooldown") or 0)
        + int(stats.get("scrape_failed") or 0)
    )
    return {
        "input_urls": int(stats.get("raw_lines") or 0),
        "unique_urls": int(stats.get("unique_urls") or 0),
        "checked_urls": int(stats.get("queued_urls") or 0),
        "scraped_jobs": int(stats.get("scrape_ok") or 0),
        "created_jobs": int(stats.get("created_jobs") or 0),
        "updated_jobs": int(stats.get("updated_jobs") or 0),
        "filtered_reject": int(stats.get("filtered_reject") or 0),
        "skipped_jobs": skipped_count,
        "errors": int(stats.get("job_errors") or 0),
    }


def prepare_external_job_import_result_for_display(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return result
    result["manual_summary"] = result_summary_for_external_job_import(result)
    result["stored_job_rows"] = [
        row
        for row in list(result.get("job_status_rows") or [])
        if safe_str(row.get("status")).strip().lower() in {"created", "updated", "would_create"}
    ]
    result["not_imported_rows"] = [
        row
        for row in list(result.get("job_status_rows") or [])
        if safe_str(row.get("status")).strip().lower() not in {"created", "updated", "would_create"}
    ]
    known_urls = {safe_str(row.get("job_url")).strip() for row in result["job_status_rows"] if safe_str(row.get("job_url")).strip()}
    for url_row in result.get("url_rows") or []:
        status = safe_str(url_row.get("status")).strip()
        normalized_url = safe_str(url_row.get("normalized_url")).strip() or safe_str(url_row.get("raw_url")).strip()
        if status in {"queued", "processing", "would_create"} or normalized_url in known_urls:
            continue
        result["not_imported_rows"].append(
            {
                "job_url": normalized_url,
                "company": "",
                "title": "",
                "location": "",
                "status": status,
                "detail": safe_str(url_row.get("detail")).strip(),
            }
        )
    return result


def run_dice_job_import(**kwargs) -> dict:
    return run_external_job_url_import(
        **kwargs,
        source_platform=JobPosting.SourcePlatform.DICE,
        source_label="Dice",
        allowed_hosts=("dice.com", "www.dice.com"),
    )
