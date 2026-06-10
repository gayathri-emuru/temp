from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse
from typing import Optional

from core.constants import (
    COMPANY_SUFFIXES,
    ROMAN_NUMERAL_MAP,
    STATE_ABBREV_TO_NAME,
    STATE_NAME_TO_ABBREV,
    TITLE_DROP_TOKENS,
    TITLE_EXACT_REPLACEMENTS,
    TITLE_REGEX_REPLACEMENTS,
)
from core.utils import compact_spaces, safe_str


DOMAIN_TOKENS = {
    "com", "io", "ai", "co", "net", "org", "edu", "gov"
}


def _ascii_text(text: str) -> str:
    text = safe_str(text)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _strip_company_domain(text: str) -> str:
    text = _ascii_text(text).lower().strip()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    return text


def _canonical_no_space(text: str) -> str:
    text = safe_str(text).lower()
    text = re.sub(r"[^a-z0-9]", "", text)
    return text


def canonical_compact_text(text: str) -> str:
    """
    Lowercased alphanumeric-only key (removes spaces/punctuation).
    Useful for matching "cvshealth" vs "cvs health", etc.
    """
    return _canonical_no_space(text)


def normalize_company_name(company_name: str) -> str:
    text = _strip_company_domain(company_name)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = compact_spaces(text)

    if not text:
        return ""

    parts = text.split()

    # remove leading "the"
    while parts and parts[0] == "the":
        parts.pop(0)

    # remove trailing suffixes and trailing domain-like tokens (and common source noise)
    while parts and (parts[-1] in COMPANY_SUFFIXES or parts[-1] in DOMAIN_TOKENS or parts[-1] == "linkedin"):
        parts.pop()

    return compact_spaces(" ".join(parts))


def canonical_company_name(company_name: str) -> str:
    readable = normalize_company_name(company_name)
    return _canonical_no_space(readable)


def normalize_title(title: str) -> str:
    text = _ascii_text(title).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[()]", " ", text)
    text = re.sub(r"[-/]", " ", text)

    for pattern, replacement in TITLE_REGEX_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)

    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = compact_spaces(text)

    tokens = []
    for token in text.split():
        token = ROMAN_NUMERAL_MAP.get(token, token)
        tokens.append(token)

    tokens = [t for t in tokens if t not in TITLE_DROP_TOKENS]
    text = compact_spaces(" ".join(tokens))
    text = TITLE_EXACT_REPLACEMENTS.get(text, text)

    return compact_spaces(text)


def canonical_title(title: str) -> str:
    readable = normalize_title(title)
    return _canonical_no_space(readable)


def normalize_location(location: str) -> str:
    """
    State-only readable normalization.
    Returns full lowercase state name if recognized.
    """
    text = _ascii_text(location).lower()
    text = text.replace(".", " ")
    text = re.sub(r"[^a-z0-9,\s]", " ", text)
    text = compact_spaces(text)

    if not text:
        return ""

    if text in STATE_ABBREV_TO_NAME:
        return STATE_ABBREV_TO_NAME[text]

    if text in STATE_NAME_TO_ABBREV:
        return text

    parts = [compact_spaces(p) for p in text.split(",") if compact_spaces(p)]
    for part in reversed(parts):
        if part in STATE_ABBREV_TO_NAME:
            return STATE_ABBREV_TO_NAME[part]
        if part in STATE_NAME_TO_ABBREV:
            return part

    words = text.split()
    if len(words) == 1 and words[0] in STATE_ABBREV_TO_NAME:
        return STATE_ABBREV_TO_NAME[words[0]]

    return text


def canonical_location(location: str) -> str:
    readable = normalize_location(location)
    return _canonical_no_space(readable)


def normalize_person_name(person_name: str) -> str:
    text = _ascii_text(person_name).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return compact_spaces(text)


def normalize_email_address(email: str) -> str:
    """
    Normalizes email for storage / comparisons.

    - Trims leading/trailing whitespace
    - Lowercases entire address (practical case-insensitive dedupe)
    """
    text = safe_str(email).strip()
    if not text:
        return ""
    return text.lower()


def extract_linkedin_company_slug(value: str) -> str:
    """
    Extracts LinkedIn company slug from a URL or path.

    Examples:
    - https://www.linkedin.com/company/google/ -> google
    - linkedin.com/company/microsoft -> microsoft
    """
    text = safe_str(value).strip()
    if not text:
        return ""

    try:
        parsed = urlparse(text if "://" in text else f"https://{text.lstrip('/')}")
        path = safe_str(parsed.path).strip("/")
        if not path:
            return ""

        parts = [p for p in path.split("/") if p]
        # common forms: company/{slug}
        for i, part in enumerate(parts):
            if part.lower() == "company" and i + 1 < len(parts):
                slug = safe_str(parts[i + 1]).strip()
                # Some LinkedIn company URLs use numeric IDs; keep those too.
                slug = slug.strip("/").strip()
                return slug
    except Exception:
        return ""

    return ""


def _acronym_of(text: str) -> str:
    tokens = [t for t in re.split(r"[^a-z0-9]+", safe_str(text).lower()) if t]
    if not tokens:
        return ""
    return "".join(t[0] for t in tokens if t and t[0].isalnum())


def find_fuzzy_company_match(
    normalized_name: str,
    *,
    threshold: int = 90,
    allow_acronym_merge: bool = True,
) -> Optional[str]:
    """
    Checks existing Company.normalized_name values in DB for a fuzzy match.

    Notes:
    - Uses rapidfuzz when available; otherwise falls back to difflib (less accurate).
    - Designed to be used with already-normalized, lowercase readable names.
    """
    from core.models import Company  # local import to avoid circular

    normalized_name = safe_str(normalized_name).strip().lower()
    if not normalized_name or len(normalized_name) < 3:
        return None

    first_char = normalized_name[0]
    candidates = list(
        Company.objects
        .filter(normalized_name__startswith=first_char)
        .values_list("normalized_name", flat=True)
    )
    if not candidates:
        return None

    # Prefer rapidfuzz (token_set_ratio + token_sort_ratio).
    try:
        from rapidfuzz import fuzz  # type: ignore

        def _tokens(value: str) -> list[str]:
            raw = safe_str(value).lower()
            parts = [p for p in re.split(r"[^a-z0-9]+", raw) if p]
            # Drop low-signal tokens commonly appended by sources.
            drop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "linkedin"}
            return [p for p in parts if p not in drop]

        def _shared_token_count(a: str, b: str) -> int:
            ta = set(_tokens(a))
            tb = set(_tokens(b))
            return len(ta.intersection(tb))

        best_score = 0
        best_match = None

        for candidate in candidates:
            score1 = int(fuzz.token_sort_ratio(normalized_name, candidate))
            score2 = int(fuzz.token_set_ratio(normalized_name, candidate))
            score = max(score1, score2)
            if score > best_score:
                best_score = score
                best_match = candidate

        if not best_match:
            return None

        if best_score < int(threshold or 90):
            return None

        # Acronym handling: allow merges like "aws" -> "amazon web services"
        if allow_acronym_merge:
            short = normalized_name.replace(" ", "")
            if short.isalpha() and 2 <= len(short) <= 6:
                if short == _acronym_of(best_match):
                    return best_match

        # Require meaningful token overlap to avoid bad merges like "cs energy" vs "cps energy".
        # (Canonical no-space matches are handled elsewhere; this is for fuzzy-only cases.)
        if _shared_token_count(normalized_name, best_match) < 2:
            return None

        # Further guard: require the first token to match, unless it's an acronym merge.
        try:
            a0 = _tokens(normalized_name)[0] if _tokens(normalized_name) else ""
            b0 = _tokens(best_match)[0] if _tokens(best_match) else ""
            if a0 and b0 and a0 != b0:
                return None
        except Exception:
            return None

        # Guard against over-merging extremely different lengths unless it is an acronym merge.
        len_ratio = min(len(normalized_name), len(best_match)) / max(len(normalized_name), len(best_match), 1)
        if len_ratio < 0.5:
            return None

        return best_match

    except Exception:
        pass

    # Fallback: difflib (no extra dependency, but less robust)
    try:
        import difflib

        best_match = None
        best_score = 0
        for candidate in candidates:
            score = int(difflib.SequenceMatcher(None, normalized_name, candidate).ratio() * 100)
            if score > best_score:
                best_score = score
                best_match = candidate

        if best_match and best_score >= int(threshold or 90):
            return best_match
    except Exception:
        return None

    return None


def build_dedupe_key(company_name: str, title: str, location: str) -> str:
    company_key = canonical_company_name(company_name)
    title_key = canonical_title(title)
    location_key = canonical_location(location)
    return f"{company_key}||{title_key}||{location_key}"


def build_sort_company(company_name: str) -> str:
    return normalize_company_name(company_name)


def build_sort_title(title: str) -> str:
    return normalize_title(title)


def build_sort_location(location: str) -> str:
    return normalize_location(location)


def build_description_fingerprint(description: str) -> str:
    text = safe_str(description).lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = compact_spaces(text)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
