import json
from pathlib import Path

from django.db import transaction

from core.models import Company
from core.services.logging_service import log_system_event
from core.services.normalization_service import canonical_company_name, normalize_person_name
from core.services.recruiter_import_service import ParsedCompanyBlock, _upsert_recruiters_for_company


def _build_recruiter_block(raw_company_names, normalized_company, recruiters):
    normalized_recruiters = {}

    for person_name, email in (recruiters or {}).items():
        display_name = (person_name or "").strip()
        if not display_name:
            continue

        normalized_recruiters[normalize_person_name(display_name)] = {
            "person_name": display_name,
            "email": (email or "none").strip().lower() if isinstance(email, str) else "none",
        }

    canonical_base = normalized_company or (raw_company_names[0] if raw_company_names else "")

    return ParsedCompanyBlock(
        raw_company_names=raw_company_names or [],
        normalized_company=normalized_company or "",
        canonical_company=canonical_company_name(canonical_base),
        recruiters=normalized_recruiters,
    )


@transaction.atomic
def apply_recruiter_decisions(decision_file_path: str):
    path = Path(decision_file_path)
    if not path.exists():
        raise FileNotFoundError(f"Decision file not found: {decision_file_path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Decision file must be a JSON list.")

    stats = {
        "rows_seen": 0,
        "mapped": 0,
        "ignored": 0,
        "created_new_company": 0,
        "errors": 0,
        "recruiters_created": 0,
        "recruiters_updated": 0,
        "recruiters_unchanged": 0,
    }

    errors = []

    for row in data:
        stats["rows_seen"] += 1

        decision = (row.get("decision") or "").strip().lower()
        raw_company_names = row.get("raw_company_names") or []
        normalized_company = (row.get("normalized_company") or "").strip()
        recruiters = row.get("recruiters") or {}

        if decision == "ignore":
            stats["ignored"] += 1
            continue

        try:
            if decision == "map":
                target_normalized_company = (row.get("target_normalized_company") or "").strip()
                if not target_normalized_company:
                    raise ValueError("target_normalized_company is required when decision='map'.")

                target_company_obj = Company.objects.get(normalized_name=target_normalized_company)
                stats["mapped"] += 1

            elif decision == "create_new":
                if not normalized_company:
                    raise ValueError("normalized_company is required when decision='create_new'.")

                target_company_obj, created = Company.objects.get_or_create(
                    normalized_name=normalized_company,
                    defaults={
                        "raw_name_latest": raw_company_names[0] if raw_company_names else normalized_company,
                    },
                )
                if created:
                    stats["created_new_company"] += 1

            else:
                raise ValueError(f"Unsupported decision: {decision}")

            recruiter_block = _build_recruiter_block(
                raw_company_names=raw_company_names,
                normalized_company=normalized_company or target_company_obj.normalized_name,
                recruiters=recruiters,
            )

            created_count, updated_count, unchanged_count = _upsert_recruiters_for_company(
                target_company_obj,
                recruiter_block,
            )

            stats["recruiters_created"] += created_count
            stats["recruiters_updated"] += updated_count
            stats["recruiters_unchanged"] += unchanged_count

            log_system_event(
                event_type="recruiter_json_uploaded",
                message=(
                    f"Applied recruiter decision '{decision}' for company '{normalized_company}' "
                    f"-> '{target_company_obj.normalized_name}'"
                ),
            )

        except Exception as exc:
            stats["errors"] += 1
            errors.append(
                {
                    "row": row,
                    "error": str(exc),
                }
            )
            log_system_event(
                event_type="failed",
                message=f"Applying recruiter decision failed: {exc}",
            )

    return {
        "stats": stats,
        "errors": errors,
    }
