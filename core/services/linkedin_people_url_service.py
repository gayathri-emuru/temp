from __future__ import annotations

import re
from urllib.parse import urlencode, urlparse

from core.constants import LINKEDIN_PEOPLE_KEYWORDS, STATE_GEO_REGION_IDS
from core.utils import safe_str


DEFAULT_US_GEO_REGION_ID = "103644278"


def get_state_geo_region_id(normalized_state: str) -> str:
    normalized_state = safe_str(normalized_state).lower()

    if not normalized_state:
        return DEFAULT_US_GEO_REGION_ID

    if normalized_state in {"us", "usa", "united states"}:
        return DEFAULT_US_GEO_REGION_ID

    return STATE_GEO_REGION_IDS.get(normalized_state, DEFAULT_US_GEO_REGION_ID)


def build_company_people_base_url(company_linkedin_url: str) -> str:
    url = safe_str(company_linkedin_url)
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        if "linkedin.com" not in parsed.netloc.lower():
            return ""

        match = re.search(r"/company/([^/?#]+)/?", parsed.path, flags=re.IGNORECASE)
        if not match:
            return ""

        slug = match.group(1).strip()
        if not slug:
            return ""

        return f"https://www.linkedin.com/company/{slug}/people/"
    except Exception:
        return ""


def build_people_search_url(base_people_url: str, geo_region_id: str, keyword: str) -> str:
    if not base_people_url or not geo_region_id or not keyword:
        return ""

    query = urlencode({
        "facetGeoRegion": geo_region_id,
        "keywords": keyword,
    })
    return f"{base_people_url}?{query}"


def generate_linkedin_people_search_data(company_linkedin_url: str, normalized_state: str) -> tuple[str, dict]:
    geo_region_id = get_state_geo_region_id(normalized_state)
    base_people_url = build_company_people_base_url(company_linkedin_url)

    if not base_people_url:
        return geo_region_id, {}

    urls = {}
    for keyword in LINKEDIN_PEOPLE_KEYWORDS:
        urls[keyword] = build_people_search_url(base_people_url, geo_region_id, keyword.replace("_", " "))

    return geo_region_id, urls
