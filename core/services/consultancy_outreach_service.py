from __future__ import annotations

import ast
import json

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from core.services.manual_bulk_email_service import send_manual_bulk_email
from core.utils import safe_str


def _load_mapping(raw_text: str) -> dict:
    raw_text = safe_str(raw_text).strip()
    if not raw_text:
        raise ValueError("Consultancy JSON is required.")
    try:
        payload = json.loads(raw_text)
    except Exception:
        try:
            payload = ast.literal_eval(raw_text)
        except Exception as exc:
            raise ValueError(f"Could not parse consultancy JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Consultancy input must be a JSON object keyed by company name.")
    return payload


def _email_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [safe_str(item).strip() for item in value]
    return []


def parse_consultancy_outreach_json(raw_text: str) -> dict:
    payload = _load_mapping(raw_text)
    rows: list[dict] = []
    invalid_rows: list[dict] = []
    seen_emails: set[str] = set()
    duplicate_emails = 0

    for company_name, details in payload.items():
        company = safe_str(company_name).strip()
        if not company:
            invalid_rows.append({"company": "", "email": "", "reason": "missing_company_name"})
            continue
        if not isinstance(details, dict):
            invalid_rows.append({"company": company, "email": "", "reason": "company_value_must_be_object"})
            continue

        website = safe_str(details.get("website")).strip()
        emails = _email_values(details.get("email_address"))
        if not emails:
            invalid_rows.append({"company": company, "email": "", "reason": "missing_email_address"})
            continue

        for raw_email in emails:
            email = safe_str(raw_email).strip().strip("<>()[]{}\"'").lower()
            if not email:
                invalid_rows.append({"company": company, "email": "", "reason": "blank_email"})
                continue
            try:
                validate_email(email)
            except ValidationError:
                invalid_rows.append({"company": company, "email": email, "reason": "invalid_email"})
                continue
            if email in seen_emails:
                duplicate_emails += 1
                invalid_rows.append({"company": company, "email": email, "reason": "duplicate_email"})
                continue
            seen_emails.add(email)
            rows.append(
                {
                    "company": company,
                    "name": company,
                    "website": website,
                    "email": email,
                }
            )

    return {
        "ok": True,
        "rows": rows,
        "invalid_rows": invalid_rows,
        "totals": {
            "companies_input": len(payload),
            "valid_recipients": len(rows),
            "invalid_rows": len(invalid_rows),
            "duplicate_emails": duplicate_emails,
        },
    }


def send_consultancy_outreach(
    *,
    raw_json: str,
    subject: str,
    body: str,
    delay_seconds: int = 15,
) -> dict:
    parsed = parse_consultancy_outreach_json(raw_json)
    recipients = parsed["rows"]
    if not recipients:
        raise RuntimeError("No valid consultancy recipients were found.")
    result = send_manual_bulk_email(
        prepared_recipients=recipients,
        subject=subject,
        body=body,
        delay_seconds=delay_seconds,
    )
    result["consultancy_parse"] = parsed
    return result
