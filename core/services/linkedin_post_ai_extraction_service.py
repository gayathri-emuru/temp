from __future__ import annotations

import json
import os
import re

from core.constants import DEFAULT_OPENAI_MODEL
from core.services.openai_filter_service import _extract_output_text, _get_openai_client
from core.utils import safe_str


DEFAULT_LINKEDIN_POST_EXTRACT_MODEL = (
    os.getenv("OPENAI_LINKEDIN_POST_EXTRACT_MODEL", "").strip()
    or os.getenv("OPENAI_COLD_EMAIL_MODEL", "").strip()
    or DEFAULT_OPENAI_MODEL
)


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", flags=re.I)


SYSTEM_PROMPT = """
You extract factual outreach details from LinkedIn post/profile text for a job-seeker email workflow.

Rules:
- Return only facts explicitly present in the supplied text.
- Do not guess, infer, or normalize beyond light cleanup.
- If a field is not present, return an empty string.
- If several emails are present, choose the email most clearly meant for job/contact outreach.
- Prefer a human poster/recruiter name over a company page name.
- Prefer the hiring company over a vendor/platform/LinkedIn name.
- Keep notes short and factual.
""".strip()


SCHEMA = {
    "type": "object",
    "properties": {
        "poster_name": {"type": "string"},
        "poster_email": {"type": "string"},
        "company_name": {"type": "string"},
        "role_title": {"type": "string"},
        "location": {"type": "string"},
        "job_url": {"type": "string"},
        "company_website": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low", ""]},
        "notes": {"type": "string"},
    },
    "required": [
        "poster_name",
        "poster_email",
        "company_name",
        "role_title",
        "location",
        "job_url",
        "company_website",
        "confidence",
        "notes",
    ],
    "additionalProperties": False,
}


def openai_linkedin_post_extraction_configured() -> bool:
    return bool(safe_str(os.getenv("OPENAI_API_KEY", "")).strip())


def extract_email_from_text(text: str) -> str:
    matches = EMAIL_RE.findall(safe_str(text))
    for email in matches:
        cleaned = safe_str(email).strip().lower()
        if cleaned and not cleaned.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return cleaned
    return ""


def _parse_json_output(raw_text: str) -> dict:
    text = safe_str(raw_text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"Could not parse ChatGPT extraction JSON: {exc} raw={text[:1000]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"ChatGPT extraction output was not an object: {text[:1000]}")
    return payload


def _clean_payload(payload: dict) -> dict:
    return {
        "poster_name": safe_str(payload.get("poster_name")).strip()[:160],
        "poster_email": safe_str(payload.get("poster_email")).strip().lower()[:254],
        "company_name": safe_str(payload.get("company_name")).strip()[:180],
        "role_title": safe_str(payload.get("role_title")).strip()[:220],
        "location": safe_str(payload.get("location")).strip()[:220],
        "job_url": safe_str(payload.get("job_url")).strip()[:1000],
        "company_website": safe_str(payload.get("company_website")).strip()[:1000],
        "confidence": safe_str(payload.get("confidence")).strip().lower()[:20],
        "notes": safe_str(payload.get("notes")).strip()[:500],
    }


def extract_linkedin_post_details_with_openai(*, source: dict, model_name: str = "") -> dict:
    api_key = safe_str(os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    selected_model = safe_str(model_name).strip() or DEFAULT_LINKEDIN_POST_EXTRACT_MODEL
    client = _get_openai_client(api_key)
    user_payload = {
        "source_url": safe_str(source.get("source_url")).strip(),
        "canonical_url": safe_str(source.get("canonical_url")).strip(),
        "page_title": safe_str(source.get("page_title")).strip(),
        "scraped_poster_name": safe_str(source.get("poster_name")).strip(),
        "scraped_poster_profile": safe_str(source.get("poster_linkedin_url")).strip(),
        "scraped_company_name": safe_str(source.get("company_name") or source.get("job_company")).strip(),
        "scraped_company_profile": safe_str(source.get("company_linkedin_url")).strip(),
        "profile_headline": safe_str(source.get("profile_headline")).strip(),
        "post_text": safe_str(source.get("post_text")).strip(),
        "shared_post_text": safe_str(source.get("shared_post_text")).strip(),
        "raw_text_preview": safe_str(source.get("raw_text_preview")).strip()[:3000],
        "scraped_job_title": safe_str(source.get("job_title")).strip(),
        "scraped_job_location": safe_str(source.get("job_location")).strip(),
        "scraped_job_url": safe_str(source.get("job_url")).strip(),
    }

    response = client.responses.create(
        model=selected_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "linkedin_post_outreach_extraction",
                "strict": True,
                "schema": SCHEMA,
            }
        },
        max_output_tokens=500,
    )
    payload = _clean_payload(_parse_json_output(_extract_output_text(response)))
    if not payload["poster_email"]:
        payload["poster_email"] = extract_email_from_text(
            "\n".join(
                [
                    safe_str(source.get("post_text")),
                    safe_str(source.get("shared_post_text")),
                    safe_str(source.get("raw_text_preview")),
                ]
            )
        )
    return {
        "status": "ok",
        "model": selected_model,
        **payload,
    }
