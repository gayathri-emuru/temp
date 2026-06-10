from __future__ import annotations

import ast
import json
from typing import Any, List, Dict

from django.db import transaction
from django.db.models import Count, Q

from core.models import Company
from core.services.normalization_service import normalize_company_name
from core.utils import extract_domain_from_url_or_host, safe_str


BLOCKED_EXACT_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "dice.com",
    "www.dice.com",
    "icims.com",
    "www.icims.com",
    "jobgether.com",
    "www.jobgether.com",
}

BLOCKED_SUFFIXES = (
    ".icims.com",
    ".linkedin.com",
    ".hsforms.com",
    ".vercel.app",
    ".sharepoint.com",
)


def normalize_domain_value(value: str) -> str:
    value = safe_str(value).lower()
    if not value:
        return ""

    domain = extract_domain_from_url_or_host(value)
    domain = safe_str(domain).lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain


def is_usable_company_domain(domain: str) -> bool:
    domain = normalize_domain_value(domain)
    if not domain:
        return False

    if domain in BLOCKED_EXACT_DOMAINS:
        return False

    for suffix in BLOCKED_SUFFIXES:
        if domain.endswith(suffix):
            return False

    return True


def _get_companies_needing_domain_rows() -> List[Dict]:
    qs = (
        Company.objects
        .filter(is_blocked=False, jobs__isnull=False, jobs__is_manual_email_job=False)
        .annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False), distinct=True))
        .order_by("normalized_name")
        .distinct()
    )

    companies = []

    for company in qs:
        current_domain = normalize_domain_value(company.active_domain)

        if is_usable_company_domain(current_domain):
            continue

        companies.append(
            {
                "normalized_name": company.normalized_name,
                "raw_name_latest": company.raw_name_latest,
                "current_domain": current_domain,
                "domain_status": company.domain_status,
                "job_count": company.job_count,
            }
        )

    return companies


def get_companies_needing_domain() -> dict:
    companies = _get_companies_needing_domain_rows()
    return {
        "total": len(companies),
        "companies": companies,
    }


def get_company_domain_mapping_template_text() -> str:
    companies = _get_companies_needing_domain_rows()

    payload = {}
    for item in companies:
        payload[item["normalized_name"]] = ""

    return json.dumps(payload, indent=2, ensure_ascii=False)


def _get_legacy_company_domain_rows(*, only_missing: bool = True) -> List[Dict]:
    qs = (
        Company.objects
        .filter(legacy=True, is_blocked=False)
        .order_by("normalized_name")
    )

    companies = []
    for position, company in enumerate(qs, start=1):
        current_domain = normalize_domain_value(company.active_domain)
        if only_missing and is_usable_company_domain(current_domain):
            continue

        companies.append(
            {
                "position": position,
                "normalized_name": company.normalized_name,
                "raw_name_latest": company.raw_name_latest,
                "current_domain": current_domain,
                "domain_status": company.domain_status,
            }
        )

    return companies


def get_legacy_company_domain_mapping_template_text(
    *,
    start_range: int,
    end_range: int,
    only_missing: bool = True,
) -> dict:
    start_range = max(int(start_range or 1), 1)
    end_range = max(int(end_range or start_range), start_range)

    rows = _get_legacy_company_domain_rows(only_missing=False)
    selected_range = rows[start_range - 1:end_range]
    selected = selected_range
    if only_missing:
        selected = [
            item
            for item in selected_range
            if not is_usable_company_domain(item["current_domain"])
        ]

    payload = {}
    for item in selected:
        payload[item["normalized_name"]] = item["current_domain"] or ""

    company_names_text = "\n".join(item["normalized_name"] for item in selected)

    return {
        "start_range": start_range,
        "end_range": end_range,
        "only_missing": bool(only_missing),
        "total_legacy_companies_in_scope": len(rows),
        "selected_range_count": len(selected_range),
        "returned": len(selected),
        "rows": selected,
        "company_names_text": company_names_text,
        "text": json.dumps(payload, indent=2, ensure_ascii=False),
    }


def _load_mapping_payload(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except (SyntaxError, ValueError) as py_exc:
            raise ValueError("Paste either a JSON object/list or a Python dict/list literal.") from py_exc


def _parse_mapping_payload(raw_text: str) -> List[Dict[str, Any]]:
    text = safe_str(raw_text)
    if not text:
        raise ValueError("Domain mapping JSON cannot be empty.")

    payload = _load_mapping_payload(text)

    rows: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        for company_name, domain_url in payload.items():
            rows.append(
                {
                    "company_name": safe_str(company_name),
                    "domain_url": domain_url,
                }
            )
        return rows

    if isinstance(payload, list):
        for idx, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"List item #{idx} must be an object.")
            rows.append(
                {
                    "company_name": safe_str(item.get("company_name")),
                    "domain_url": item["domain_url"] if "domain_url" in item else "",
                }
            )
        return rows

    raise ValueError("Paste either a JSON object or a JSON list of objects.")


def _delete_company_everywhere(company: Company) -> dict:
    deleted_company_name = company.normalized_name
    summary = {
        "company_name": deleted_company_name,
        "job_postings": company.jobs.count(),
        "recruiters": company.recruiters.count(),
    }
    deleted_count, deleted_by_model = company.delete()
    summary["deleted_objects"] = deleted_count
    summary["deleted_by_model"] = deleted_by_model
    return summary


@transaction.atomic
def apply_company_domain_mapping(raw_text: str) -> dict:
    rows = _parse_mapping_payload(raw_text)

    result = {
        "rows_seen": 0,
        "updated": 0,
        "removed": 0,
        "skipped_blank": 0,
        "not_found": 0,
        "invalid_domain": 0,
        "errors": 0,
        "details": [],
    }

    for row in rows:
        result["rows_seen"] += 1

        company_name = safe_str(row["company_name"])
        domain_url_raw = row["domain_url"]
        domain_url = safe_str(domain_url_raw)

        normalized_company = normalize_company_name(company_name)
        normalized_domain = normalize_domain_value(domain_url)

        if domain_url_raw is None:
            if not normalized_company:
                result["errors"] += 1
                result["details"].append(
                    {
                        "company_name": company_name,
                        "domain_url": None,
                        "status": "error",
                        "message": "company_name is empty after normalization",
                    }
                )
                continue

            try:
                company = Company.objects.get(normalized_name=normalized_company)
            except Company.DoesNotExist:
                result["not_found"] += 1
                result["details"].append(
                    {
                        "company_name": company_name,
                        "normalized_company": normalized_company,
                        "domain_url": None,
                        "status": "not_found",
                    }
                )
                continue

            delete_summary = _delete_company_everywhere(company)
            result["removed"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": None,
                    "status": "removed",
                    "delete_summary": delete_summary,
                }
            )
            continue

        # Allow partial mappings: blank or "NAN" means "skip this company" (no DB update).
        if safe_str(domain_url).strip().upper() == "NAN" or not normalized_domain:
            result["skipped_blank"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "normalized_domain": normalized_domain,
                    "status": "skipped_blank",
                }
            )
            continue

        if not normalized_company:
            result["errors"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "domain_url": domain_url,
                    "status": "error",
                    "message": "company_name is empty after normalization",
                }
            )
            continue

        try:
            company = Company.objects.get(normalized_name=normalized_company)
        except Company.DoesNotExist:
            result["not_found"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "status": "not_found",
                }
            )
            continue

        if not is_usable_company_domain(normalized_domain):
            result["invalid_domain"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "normalized_domain": normalized_domain,
                    "status": "invalid_domain",
                }
            )
            continue

        company.active_domain = normalized_domain
        company.domain_status = Company.DomainStatus.SET
        company.save(update_fields=["active_domain", "domain_status", "updated_at"])

        result["updated"] += 1
        result["details"].append(
            {
                "company_name": company_name,
                "normalized_company": normalized_company,
                "domain_url": domain_url,
                "normalized_domain": normalized_domain,
                "status": "updated",
            }
        )

    return result


@transaction.atomic
def apply_legacy_company_domain_mapping(raw_text: str) -> dict:
    rows = _parse_mapping_payload(raw_text)

    result = {
        "rows_seen": 0,
        "updated": 0,
        "removed": 0,
        "skipped_blank": 0,
        "not_found": 0,
        "not_legacy": 0,
        "invalid_domain": 0,
        "errors": 0,
        "details": [],
    }

    for row in rows:
        result["rows_seen"] += 1

        company_name = safe_str(row["company_name"])
        domain_url_raw = row["domain_url"]
        domain_url = safe_str(domain_url_raw)
        normalized_company = normalize_company_name(company_name)
        normalized_domain = normalize_domain_value(domain_url)

        if domain_url_raw is None:
            if not normalized_company:
                result["errors"] += 1
                result["details"].append(
                    {
                        "company_name": company_name,
                        "domain_url": None,
                        "status": "error",
                        "message": "company_name is empty after normalization",
                    }
                )
                continue

            try:
                company = Company.objects.get(normalized_name=normalized_company)
            except Company.DoesNotExist:
                result["not_found"] += 1
                result["details"].append(
                    {
                        "company_name": company_name,
                        "normalized_company": normalized_company,
                        "domain_url": None,
                        "status": "not_found",
                    }
                )
                continue

            if not company.legacy:
                result["not_legacy"] += 1
                result["details"].append(
                    {
                        "company_name": company_name,
                        "normalized_company": normalized_company,
                        "domain_url": None,
                        "status": "not_legacy",
                    }
                )
                continue

            delete_summary = _delete_company_everywhere(company)
            result["removed"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": None,
                    "status": "removed",
                    "delete_summary": delete_summary,
                }
            )
            continue

        if safe_str(domain_url).strip().upper() == "NAN" or not normalized_domain:
            result["skipped_blank"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "normalized_domain": normalized_domain,
                    "status": "skipped_blank",
                }
            )
            continue

        if not normalized_company:
            result["errors"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "domain_url": domain_url,
                    "status": "error",
                    "message": "company_name is empty after normalization",
                }
            )
            continue

        try:
            company = Company.objects.get(normalized_name=normalized_company)
        except Company.DoesNotExist:
            result["not_found"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "status": "not_found",
                }
            )
            continue

        if not company.legacy:
            result["not_legacy"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "status": "not_legacy",
                }
            )
            continue

        if not is_usable_company_domain(normalized_domain):
            result["invalid_domain"] += 1
            result["details"].append(
                {
                    "company_name": company_name,
                    "normalized_company": normalized_company,
                    "domain_url": domain_url,
                    "normalized_domain": normalized_domain,
                    "status": "invalid_domain",
                }
            )
            continue

        company.active_domain = normalized_domain
        company.domain_status = Company.DomainStatus.SET
        company.save(update_fields=["active_domain", "domain_status", "updated_at"])

        result["updated"] += 1
        result["details"].append(
            {
                "company_name": company_name,
                "normalized_company": normalized_company,
                "domain_url": domain_url,
                "normalized_domain": normalized_domain,
                "status": "updated",
            }
        )

    return result
