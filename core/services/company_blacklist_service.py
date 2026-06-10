from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.db import transaction

from core.models import BlacklistedCompany, Company, DailyBatch
from core.services.normalization_service import canonical_company_name, normalize_company_name
from core.utils import safe_str


@dataclass(frozen=True)
class CompanyBlacklistLookup:
    normalized_names: frozenset[str]
    canonical_names: frozenset[str]


def build_company_blacklist_lookup() -> CompanyBlacklistLookup:
    rows = BlacklistedCompany.objects.values_list("normalized_name", "canonical_name")
    normalized_names = set()
    canonical_names = set()
    for normalized_name, canonical_name in rows:
        normalized = safe_str(normalized_name).strip().lower()
        canonical = safe_str(canonical_name).strip().lower()
        if normalized:
            normalized_names.add(normalized)
        if canonical:
            canonical_names.add(canonical)
    return CompanyBlacklistLookup(
        normalized_names=frozenset(normalized_names),
        canonical_names=frozenset(canonical_names),
    )


def find_blacklisted_company_name(
    *,
    raw_company: str = "",
    normalized_company: str = "",
    canonical_company: str = "",
    lookup: CompanyBlacklistLookup | None = None,
) -> str:
    lookup = lookup or build_company_blacklist_lookup()

    normalized_candidates = {
        safe_str(normalized_company).strip().lower(),
        normalize_company_name(raw_company),
    }
    canonical_candidates = {
        safe_str(canonical_company).strip().lower(),
        canonical_company_name(normalized_company),
        canonical_company_name(raw_company),
    }

    for value in normalized_candidates:
        if value and value in lookup.normalized_names:
            return value

    for value in canonical_candidates:
        if value and value in lookup.canonical_names:
            return value

    return ""


def blacklist_zero_usable_recipient_companies(
    *,
    batch: DailyBatch,
    company_rows: Iterable[dict],
) -> dict:
    rows_to_blacklist = []
    seen_company_ids = set()
    for row in company_rows:
        company = row.get("company")
        if not company or not getattr(company, "id", None):
            continue
        if company.id in seen_company_ids:
            continue
        try:
            usable_count = int(row.get("usable_recipient_count") or 0)
        except Exception:
            usable_count = 0
        if usable_count != 0:
            continue
        rows_to_blacklist.append(row)
        seen_company_ids.add(company.id)

    result = {
        "batch_date": batch.batch_date.isoformat(),
        "companies_seen": len(rows_to_blacklist),
        "created": 0,
        "updated": 0,
        "blocked_companies": 0,
        "company_names": [],
    }

    with transaction.atomic():
        for row in rows_to_blacklist:
            company: Company = row["company"]
            normalized_name = safe_str(company.normalized_name).strip().lower()
            raw_name = safe_str(company.raw_name_latest or row.get("raw_name_latest") or normalized_name).strip()
            canonical_name = canonical_company_name(normalized_name or raw_name)
            reason = (
                f"Blacklisted from pipeline dashboard batch {batch.batch_date.isoformat()}: "
                "0 usable recipients found for latest-batch company."
            )

            _, created = BlacklistedCompany.objects.update_or_create(
                normalized_name=normalized_name,
                defaults={
                    "company": company,
                    "raw_name_latest": raw_name,
                    "canonical_name": canonical_name,
                    "reason": reason,
                    "source": "zero_usable_recipients",
                },
            )
            if created:
                result["created"] += 1
            else:
                result["updated"] += 1

            if not company.is_blocked:
                company.is_blocked = True
                company.save(update_fields=["is_blocked", "updated_at"])
                result["blocked_companies"] += 1

            result["company_names"].append(normalized_name)

    result["company_names"].sort()
    return result
