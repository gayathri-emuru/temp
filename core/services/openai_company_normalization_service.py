from __future__ import annotations

import json
import os
from functools import lru_cache

import requests

from core.utils import safe_str


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT = 45
DEFAULT_MODEL = os.getenv("OPENAI_COMPANY_NORMALIZATION_MODEL", "gpt-5-mini")


def _extract_output_text(payload: dict) -> str:
    output_text = safe_str(payload.get("output_text"))
    if output_text:
        return output_text

    output = payload.get("output") or []
    for item in output:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text":
                text = safe_str(block.get("text"))
                if text:
                    return text
    return ""


@lru_cache(maxsize=2000)
def normalize_company_name_with_gpt(raw_company_name: str) -> dict:
    """
    Returns:
      {
        "normalized_company": str,
        "confidence": float,
        "reason": str,
      }
    """
    raw_company_name = safe_str(raw_company_name).strip()
    if not raw_company_name:
        return {"normalized_company": "", "confidence": 0.0, "reason": "empty_input"}

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    schema = {
        "type": "object",
        "properties": {
            "normalized_company": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["normalized_company", "confidence", "reason"],
        "additionalProperties": False,
    }

    developer_message = (
        "You normalize company names ONLY. "
        "Goal: map messy variants from different sources to a consistent company name. "
        "Rules: "
        "- Output a short, clean company name (brand name) without legal suffixes (Inc/LLC/Ltd/Corp). "
        "- Remove obvious geography/business-unit suffixes if they do not change the company identity "
        "  (e.g., 'North America', 'USA', 'US', 'EMEA', 'APAC'). "
        "- Do NOT invent a different company. If unsure, return the input company name cleaned minimally. "
        "- Output should be plain text (no punctuation), in lowercase words."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": DEFAULT_MODEL,
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": developer_message}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": raw_company_name}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "company_name_normalization",
                "strict": True,
                "schema": schema,
            }
        },
    }

    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers=headers,
        json=body,
        timeout=OPENAI_TIMEOUT,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI Responses API error. status={response.status_code} body={response.text[:4000]}"
        )

    payload = response.json()
    raw_text = _extract_output_text(payload)
    if not raw_text:
        raise RuntimeError(f"OpenAI response missing output_text. payload={json.dumps(payload)[:3000]}")

    try:
        parsed = json.loads(raw_text)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse OpenAI JSON output. error={exc} raw_text={raw_text[:2000]}")

    return {
        "normalized_company": safe_str(parsed.get("normalized_company")).strip(),
        "confidence": float(parsed.get("confidence") or 0.0),
        "reason": safe_str(parsed.get("reason")).strip(),
    }

