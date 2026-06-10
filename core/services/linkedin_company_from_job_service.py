from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import requests

from core.services.normalization_service import normalize_company_name
from core.utils import safe_str


REQUEST_TIMEOUT_SECONDS = 25
REQUEST_SLEEP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def normalize_company_linkedin_url(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""

    url = html.unescape(url)
    url = url.replace("\\u002F", "/").replace("\\/", "/")
    url = url.replace("\\u003A", ":")
    url = re.sub(r"[\"'<>),;]+$", "", url)

    if url.startswith("/company/"):
        url = "https://www.linkedin.com" + url

    match = re.search(r"https?://www\.linkedin\.com/company/[^/?#\"'<> ]+", url, flags=re.I)
    if not match:
        return ""

    final_url = match.group(0)
    final_url = re.sub(r"^http://", "https://", final_url, flags=re.I)

    if not final_url.endswith("/"):
        final_url += "/"

    return final_url


def is_valid_company_linkedin_url(url: str) -> bool:
    return bool(normalize_company_linkedin_url(url))


def fetch_public_job_page(job_url: str) -> tuple[int, str]:
    response = requests.get(
        job_url,
        headers=REQUEST_SLEEP_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    return response.status_code, response.text


def _slug_from_company_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        match = re.search(r"/company/([^/?#]+)/?", parsed.path, flags=re.I)
        if not match:
            return ""
        return match.group(1).strip().lower()
    except Exception:
        return ""


def _normalize_for_match(text: str) -> str:
    text = safe_str(text)
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("&amp;", " and ")
    return normalize_company_name(text)


def _extract_company_candidates(page_html: str):
    if not page_html:
        return []

    text = html.unescape(page_html)
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    text = text.replace("\\u003A", ":")

    pattern = re.compile(
        r'https?://www\.linkedin\.com/company/[^/?#"\'>\s]+|/company/[^/?#"\'>\s]+',
        flags=re.I,
    )

    candidates = []
    seen = set()

    for match in pattern.finditer(text):
        raw_url = match.group(0)
        normalized_url = normalize_company_linkedin_url(raw_url)
        if not normalized_url:
            continue
        if normalized_url in seen:
            continue

        seen.add(normalized_url)

        start = max(0, match.start() - 600)
        end = min(len(text), match.end() + 600)
        context = text[start:end]

        candidates.append(
            {
                "url": normalized_url,
                "slug": _slug_from_company_url(normalized_url),
                "context": context,
            }
        )

    return candidates


def _score_candidate(candidate: dict, expected_company_name: str) -> int:
    expected = _normalize_for_match(expected_company_name)
    if not expected:
        return 0

    slug_norm = _normalize_for_match(candidate.get("slug", "").replace("-", " "))
    context_norm = _normalize_for_match(candidate.get("context", ""))

    score = 0

    if slug_norm == expected:
        score += 100

    if expected and expected in slug_norm:
        score += 80

    if expected and expected in context_norm:
        score += 60

    expected_tokens = set(expected.split())
    slug_tokens = set(slug_norm.split())
    context_tokens = set(context_norm.split())

    token_overlap_slug = len(expected_tokens & slug_tokens)
    token_overlap_context = len(expected_tokens & context_tokens)

    score += token_overlap_slug * 20
    score += token_overlap_context * 5

    return score


def extract_company_linkedin_url_from_html(page_html: str, expected_company_name: str = "") -> str:
    candidates = _extract_company_candidates(page_html)
    if not candidates:
        return ""

    expected_company_name = safe_str(expected_company_name)

    if expected_company_name:
        scored = []
        for candidate in candidates:
            score = _score_candidate(candidate, expected_company_name)
            scored.append((score, candidate["url"]))

        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best_url = scored[0]
        if best_score > 0:
            return best_url

        # IMPORTANT:
        # if we had an expected company name but could not match anything,
        # return blank instead of returning a wrong company link
        return ""

    # No expected company name available:
    # only return if there is exactly one candidate
    if len(candidates) == 1:
        return candidates[0]["url"]

    return ""


def get_company_linkedin_url_from_job_url(job_url: str, expected_company_name: str = "") -> str:
    job_url = safe_str(job_url)
    if not job_url:
        return ""

    try:
        status_code, page_html = fetch_public_job_page(job_url)
        if status_code != 200:
            return ""
        return extract_company_linkedin_url_from_html(
            page_html=page_html,
            expected_company_name=expected_company_name,
        )
    except Exception:
        return ""
