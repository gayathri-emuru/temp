from __future__ import annotations

import os
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Tuple

from django.conf import settings
from django.db import transaction

from core.models import Company, CompanyRecruiter, RecruiterJsonUpload
from core.services.logging_service import log_system_event
from core.services.normalization_service import (
    canonical_company_name,
    normalize_company_name,
    normalize_person_name,
)
from core.services.company_resolution_service import resolve_company_normalized_name_strict
from core.services.openai_company_normalization_service import normalize_company_name_with_gpt
from core.utils import safe_str


REVIEW_OUTPUT_DIR = Path(settings.MEDIA_ROOT) / "recruiter_review_outputs"
TOP_SUGGESTIONS = 5


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

    if not suggested or confidence < min_conf:
        return base
    if len(suggested) <= 2 and suggested not in {"ibm", "hp", "3m", "gm", "ge"}:
        return base

    return suggested


def _normalize_recruiters_payload(recruiters) -> dict:
    """
    Accepts either:
      - { "Person Name": "email@domain.com", ... }
      - [ ["Person Name", "email@domain.com"], ... ]
    Returns a dict person_name -> email.
    """
    if isinstance(recruiters, dict):
        return recruiters

    if isinstance(recruiters, list):
        out = {}
        for item in recruiters:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            person_name = safe_str(item[0]).strip()
            email = safe_str(item[1] if len(item) > 1 else "none").strip()
            if not person_name:
                continue
            out[person_name] = email or "none"
        return out

    return {}


@dataclass
class ParsedCompanyBlock:
    raw_company_names: List[str]
    normalized_company: str
    canonical_company: str
    recruiters: Dict[str, Dict[str, str]]


def parse_multiple_json_blocks(raw_text: str) -> List[dict]:
    text = raw_text.strip()
    decoder = json.JSONDecoder()
    idx = 0
    blocks = []

    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx >= len(text):
            break

        obj, end_idx = decoder.raw_decode(text, idx)
        if not isinstance(obj, dict):
            raise ValueError("Each pasted JSON block must be a JSON object.")

        blocks.append(obj)
        idx = end_idx

    return blocks


def _merge_company_blocks(blocks: List[dict]) -> Tuple[Dict[str, ParsedCompanyBlock], dict]:
    merged: Dict[str, ParsedCompanyBlock] = {}

    stats = {
        "json_blocks": len(blocks),
        "raw_company_keys": 0,
        "merged_company_buckets": 0,
        "raw_recruiter_entries": 0,
        "duplicate_recruiters_collapsed": 0,
    }

    for block in blocks:
        for raw_company, recruiters in block.items():
            stats["raw_company_keys"] += 1

            raw_company = safe_str(raw_company)
            if not raw_company:
                continue

            recruiters = _normalize_recruiters_payload(recruiters)
            if not isinstance(recruiters, dict):
                raise ValueError(
                    f"Company '{raw_company}' must map to a JSON object (person->email) "
                    f"or a list of [name, email] pairs."
                )

            normalized_company = _normalize_company_best_effort(raw_company)
            canonical_company = canonical_company_name(normalized_company or raw_company)

            merge_key = canonical_company or normalized_company or raw_company.lower()

            if merge_key not in merged:
                merged[merge_key] = ParsedCompanyBlock(
                    raw_company_names=[raw_company],
                    normalized_company=normalized_company,
                    canonical_company=canonical_company,
                    recruiters={},
                )
            else:
                if raw_company not in merged[merge_key].raw_company_names:
                    merged[merge_key].raw_company_names.append(raw_company)

            for person_name, email in recruiters.items():
                stats["raw_recruiter_entries"] += 1

                person_name = safe_str(person_name)
                email = safe_str(email, "none").lower() or "none"

                if not person_name:
                    continue

                normalized_person = normalize_person_name(person_name)

                if normalized_person in merged[merge_key].recruiters:
                    stats["duplicate_recruiters_collapsed"] += 1
                    existing = merged[merge_key].recruiters[normalized_person]

                    if existing["email"] == "none" and email != "none":
                        existing["email"] = email

                    if len(person_name) > len(existing["person_name"]):
                        existing["person_name"] = person_name
                else:
                    merged[merge_key].recruiters[normalized_person] = {
                        "person_name": person_name,
                        "email": email if email else "none",
                    }

    stats["merged_company_buckets"] = len(merged)
    return merged, stats


def _build_company_match_indexes():
    companies = list(Company.objects.all().order_by("normalized_name"))

    normalized_index = {}
    canonical_index = {}

    for company in companies:
        normalized_index[company.normalized_name] = company

        canonical_value = canonical_company_name(company.normalized_name or company.raw_name_latest)
        canonical_index.setdefault(canonical_value, []).append(company)

    return companies, normalized_index, canonical_index


def _similarity_score(a_norm: str, a_can: str, b_norm: str, b_can: str) -> float:
    norm_ratio = SequenceMatcher(None, a_norm, b_norm).ratio()
    can_ratio = SequenceMatcher(None, a_can, b_can).ratio()

    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    token_score = 0.0
    if a_tokens or b_tokens:
        union = a_tokens.union(b_tokens)
        if union:
            token_score = len(a_tokens.intersection(b_tokens)) / len(union)

    return max(norm_ratio, can_ratio, token_score)


def _top_company_suggestions(
    input_normalized: str,
    input_canonical: str,
    companies: List[Company],
    limit: int = TOP_SUGGESTIONS,
):
    scored = []
    for company in companies:
        company_norm = company.normalized_name
        company_can = canonical_company_name(company.normalized_name or company.raw_name_latest)

        score = _similarity_score(
            input_normalized,
            input_canonical,
            company_norm,
            company_can,
        )

        scored.append(
            {
                "normalized_name": company.normalized_name,
                "raw_name_latest": company.raw_name_latest,
                "canonical_name": company_can,
                "score": round(score, 4),
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def _upsert_recruiters_for_company(company: Company, recruiter_block: ParsedCompanyBlock):
    created_count = 0
    updated_count = 0
    unchanged_count = 0

    for normalized_person, payload in recruiter_block.recruiters.items():
        person_name = safe_str(payload["person_name"])
        email = safe_str(payload["email"], "none").lower() or "none"

        recruiter, created = CompanyRecruiter.objects.get_or_create(
            company=company,
            normalized_person_name=normalized_person,
            defaults={
                "person_name": person_name,
                "email": email,
                "email_sent": False,
                "email_sent_date": None,
            },
        )

        if created:
            created_count += 1
            continue

        changed = False

        if recruiter.person_name != person_name and person_name:
            recruiter.person_name = person_name
            changed = True

        if email != "none":
            if recruiter.email != email:
                recruiter.email = email
                changed = True

        if changed:
            recruiter.save()
            updated_count += 1
        else:
            unchanged_count += 1

    return created_count, updated_count, unchanged_count


def _write_json(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def process_recruiter_json_text(raw_text: str):
    REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    upload_record = RecruiterJsonUpload.objects.create(
        raw_json_text=raw_text,
        status=RecruiterJsonUpload.Status.PENDING,
    )

    try:
        blocks = parse_multiple_json_blocks(raw_text)
        merged, parse_stats = _merge_company_blocks(blocks)

        companies, normalized_index, canonical_index = _build_company_match_indexes()

        summary = {
            **parse_stats,
            "exact_normalized_matches": 0,
            "exact_canonical_matches": 0,
            "resolved_strict_matches": 0,
            "unmatched_companies": 0,
            "ambiguous_companies": 0,
            "recruiters_created": 0,
            "recruiters_updated": 0,
            "recruiters_unchanged": 0,
        }

        unresolved = []
        matched_report = []

        with transaction.atomic():
            for bucket in merged.values():
                normalized_input = bucket.normalized_company
                canonical_input = bucket.canonical_company

                matched_company = None
                match_type = ""

                resolved_norm, _ = resolve_company_normalized_name_strict(
                    normalized_company_name=normalized_input,
                    company_linkedin_url="",
                )
                resolved_norm = safe_str(resolved_norm).strip().lower()

                if resolved_norm and resolved_norm in normalized_index:
                    matched_company = normalized_index[resolved_norm]
                    match_type = "resolved_strict"
                elif normalized_input in normalized_index:
                    matched_company = normalized_index[normalized_input]
                    match_type = "exact_normalized"
                else:
                    canonical_matches = canonical_index.get(canonical_input, [])
                    if len(canonical_matches) == 1:
                        matched_company = canonical_matches[0]
                        match_type = "exact_canonical"
                    elif len(canonical_matches) > 1:
                        unresolved.append(
                            {
                                "raw_company_names": bucket.raw_company_names,
                                "normalized_company": normalized_input,
                                "canonical_company": canonical_input,
                                "reason": "ambiguous_canonical_match",
                                "candidate_companies": [
                                    {
                                        "normalized_name": c.normalized_name,
                                        "raw_name_latest": c.raw_name_latest,
                                    }
                                    for c in canonical_matches
                                ],
                                "suggestions": _top_company_suggestions(
                                    normalized_input,
                                    canonical_input,
                                    companies,
                                ),
                                "recruiters": {
                                    v["person_name"]: v["email"]
                                    for _, v in bucket.recruiters.items()
                                },
                                "decision": "",
                                "target_normalized_company": "",
                            }
                        )
                        summary["ambiguous_companies"] += 1
                        continue

                if matched_company:
                    created_count, updated_count, unchanged_count = _upsert_recruiters_for_company(
                        matched_company,
                        bucket,
                    )

                    summary["recruiters_created"] += created_count
                    summary["recruiters_updated"] += updated_count
                    summary["recruiters_unchanged"] += unchanged_count

                    if match_type == "exact_normalized":
                        summary["exact_normalized_matches"] += 1
                    elif match_type == "exact_canonical":
                        summary["exact_canonical_matches"] += 1
                    elif match_type == "resolved_strict":
                        summary["resolved_strict_matches"] += 1

                    matched_report.append(
                        {
                            "raw_company_names": bucket.raw_company_names,
                            "normalized_company": normalized_input,
                            "canonical_company": canonical_input,
                            "matched_company": matched_company.normalized_name,
                            "match_type": match_type,
                            "recruiter_count": len(bucket.recruiters),
                            "recruiters_created": created_count,
                            "recruiters_updated": updated_count,
                            "recruiters_unchanged": unchanged_count,
                        }
                    )
                    continue

                unresolved.append(
                    {
                        "raw_company_names": bucket.raw_company_names,
                        "normalized_company": normalized_input,
                        "canonical_company": canonical_input,
                        "reason": "no_exact_match",
                        "candidate_companies": [],
                        "suggestions": _top_company_suggestions(
                            normalized_input,
                            canonical_input,
                            companies,
                        ),
                        "recruiters": {
                            v["person_name"]: v["email"]
                            for _, v in bucket.recruiters.items()
                        },
                        "decision": "",
                        "target_normalized_company": "",
                    }
                )
                summary["unmatched_companies"] += 1

        timestamp = upload_record.uploaded_at.strftime("%Y%m%d_%H%M%S")

        merged_file = REVIEW_OUTPUT_DIR / f"merged_input_{timestamp}.json"
        matched_file = REVIEW_OUTPUT_DIR / f"matched_report_{timestamp}.json"
        unresolved_file = REVIEW_OUTPUT_DIR / f"unresolved_review_{timestamp}.json"
        summary_file = REVIEW_OUTPUT_DIR / f"summary_{timestamp}.json"

        merged_output = [
            {
                "raw_company_names": block.raw_company_names,
                "normalized_company": block.normalized_company,
                "canonical_company": block.canonical_company,
                "recruiters": {v["person_name"]: v["email"] for _, v in block.recruiters.items()},
            }
            for block in merged.values()
        ]

        _write_json(merged_file, merged_output)
        _write_json(matched_file, matched_report)
        _write_json(unresolved_file, unresolved)
        _write_json(summary_file, summary)

        upload_record.normalized_company_count = len(merged)
        upload_record.total_people_count = parse_stats["raw_recruiter_entries"]
        upload_record.status = RecruiterJsonUpload.Status.SUCCESS
        upload_record.notes = json.dumps(summary, ensure_ascii=False)
        upload_record.save()

        log_system_event(
            event_type="recruiter_json_uploaded",
            message=f"Recruiter JSON processed. Summary: {json.dumps(summary, ensure_ascii=False)}",
        )

        return {
            "summary": summary,
            "files": {
                "merged_input": str(merged_file),
                "matched_report": str(matched_file),
                "unresolved_review": str(unresolved_file),
                "summary": str(summary_file),
            },
        }

    except Exception as exc:
        upload_record.status = RecruiterJsonUpload.Status.FAILED
        upload_record.notes = str(exc)[:4000]
        upload_record.save(update_fields=["status", "notes"])

        log_system_event(
            event_type="failed",
            message=f"Recruiter JSON processing failed: {exc}",
        )
        raise
