from __future__ import annotations

import json
import os
from functools import lru_cache

import requests

from core.utils import safe_str


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT = 30
DEFAULT_MODEL = os.getenv("OPENAI_LOCATION_MODEL", "gpt-4o-mini")

_US_STATE_ABBR = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

_FULL_STATE_NAMES = {v.lower() for v in _US_STATE_ABBR.values()}


def _fast_extract_state(location: str) -> str:
    """
    Try to extract a US state without a GPT call.
    Handles patterns like 'Chicago, IL', 'New York, NY', 'Illinois', 'Remote'.
    Returns full state name or '' if uncertain.
    """
    loc = safe_str(location).strip()
    if not loc:
        return ""

    lower = loc.lower()

    if lower in {"remote", "anywhere", "global", "united states", "us", "usa"}:
        return "United States"

    # Already a full state name
    if lower in _FULL_STATE_NAMES:
        return loc.title()

    # Pattern: "City, ST" — grab the last 2-char token
    parts = [p.strip() for p in loc.replace(",", " ").split()]
    for part in reversed(parts):
        if part.upper() in _US_STATE_ABBR:
            return _US_STATE_ABBR[part.upper()]

    return ""


@lru_cache(maxsize=2000)
def extract_us_state_from_location(location: str) -> str:
    """
    Given any location string, return the US state in full form (e.g. 'Illinois').
    Returns 'United States' if no specific state is found (remote/national/unknown).
    Falls back to fast extraction first to avoid unnecessary GPT calls.
    """
    location = safe_str(location).strip()
    if not location:
        return "United States"

    fast = _fast_extract_state(location)
    if fast:
        return fast

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "United States"

    schema = {
        "type": "object",
        "properties": {
            "us_state": {"type": "string"},
        },
        "required": ["us_state"],
        "additionalProperties": False,
    }

    developer_message = (
        "Extract the US state from the given location string. "
        "Return the full state name (e.g. 'Illinois', 'New York', 'California'). "
        "If the location is remote, national, or not a US location, return 'United States'. "
        "Never return abbreviations. Never return a city name. Only return the state name or 'United States'."
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
                "content": [{"type": "input_text", "text": location}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "us_state_extraction",
                "strict": True,
                "schema": schema,
            }
        },
    }

    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers=headers,
            json=body,
            timeout=OPENAI_TIMEOUT,
        )
        if response.status_code >= 400:
            return "United States"

        payload = response.json()
        output = payload.get("output") or []
        raw_text = ""
        for item in output:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    raw_text = safe_str(block.get("text"))
                    break
            if raw_text:
                break
        raw_text = raw_text or safe_str(payload.get("output_text"))

        if not raw_text:
            return "United States"

        parsed = json.loads(raw_text)
        state = safe_str(parsed.get("us_state")).strip()
        return state if state else "United States"

    except Exception:
        return "United States"
