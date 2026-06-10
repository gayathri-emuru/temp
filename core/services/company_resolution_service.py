from __future__ import annotations

import os

from core.models import Company
from core.services.normalization_service import canonical_compact_text, extract_linkedin_company_slug, find_fuzzy_company_match
from core.utils import safe_str


def resolve_company_normalized_name(
    *,
    normalized_company_name: str,
    company_linkedin_url: str = "",
) -> tuple[str, str]:
    """
    Resolves the best Company.normalized_name to use for get_or_create, based on:
    1) LinkedIn company slug (preferred when available)
    2) Optional fuzzy match on normalized_name (when enabled)

    Returns: (resolved_normalized_name, linkedin_company_slug)
    """
    normalized_company_name = safe_str(normalized_company_name).strip().lower()
    linkedin_company_slug = extract_linkedin_company_slug(company_linkedin_url)

    if linkedin_company_slug:
        existing = (
            Company.objects
            .filter(linkedin_company_slug=linkedin_company_slug)
            .values_list("normalized_name", flat=True)
            .first()
        )
        if existing:
            return safe_str(existing).strip().lower(), linkedin_company_slug

    # Deterministic merge for spacing/punctuation variants ("cvshealth" vs "cvs health").
    compact_key = canonical_compact_text(normalized_company_name)
    if compact_key:
        first_char = normalized_company_name[0] if normalized_company_name else ""
        qs = Company.objects.all()
        if first_char:
            qs = qs.filter(normalized_name__startswith=first_char)
        candidates = list(qs.values_list("normalized_name", flat=True)[:5000])
        matches = [c for c in candidates if canonical_compact_text(c) == compact_key]
        if matches:
            # Prefer the most readable (longest) normalized name as the canonical stored form.
            chosen = sorted(matches, key=lambda x: (len(safe_str(x)), safe_str(x)), reverse=True)[0]
            return safe_str(chosen).strip().lower(), linkedin_company_slug

    # Default ON to avoid creating new Company rows for small name variants.
    fuzzy_enabled = os.getenv("COMPANY_FUZZY_MATCH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    if fuzzy_enabled and normalized_company_name:
        threshold = int(os.getenv("COMPANY_FUZZY_MATCH_THRESHOLD", "90") or "90")
        allow_acronyms = os.getenv("COMPANY_FUZZY_MATCH_ALLOW_ACRONYMS", "1").strip().lower() in {"1", "true", "yes", "on"}

        match = find_fuzzy_company_match(
            normalized_company_name,
            threshold=threshold,
            allow_acronym_merge=allow_acronyms,
        )
        if match:
            return safe_str(match).strip().lower(), linkedin_company_slug

    return normalized_company_name, linkedin_company_slug


def _acronym_of(text: str) -> str:
    tokens = [t for t in safe_str(text).lower().split() if t]
    if not tokens:
        return ""
    return "".join(t[0] for t in tokens if t and t[0].isalnum())


def resolve_company_normalized_name_strict(
    *,
    normalized_company_name: str,
    company_linkedin_url: str = "",
) -> tuple[str, str]:
    """
    Stricter resolver used for high-stakes matching (recruiter imports).

    Resolution order:
    1) LinkedIn company slug match (if provided)
    2) Deterministic compact-key match ("cvshealth" vs "cvs health")
    3) Acronym match ONLY when unique ("aws" -> "amazon web services")

    This intentionally does NOT use fuzzy token matching.
    """
    normalized_company_name = safe_str(normalized_company_name).strip().lower()
    linkedin_company_slug = extract_linkedin_company_slug(company_linkedin_url)

    if linkedin_company_slug:
        existing = (
            Company.objects
            .filter(linkedin_company_slug=linkedin_company_slug)
            .values_list("normalized_name", flat=True)
            .first()
        )
        if existing:
            return safe_str(existing).strip().lower(), linkedin_company_slug

    compact_key = canonical_compact_text(normalized_company_name)
    if compact_key:
        first_char = normalized_company_name[0] if normalized_company_name else ""
        qs = Company.objects.all()
        if first_char:
            qs = qs.filter(normalized_name__startswith=first_char)
        candidates = list(qs.values_list("normalized_name", flat=True)[:5000])
        matches = [c for c in candidates if canonical_compact_text(c) == compact_key]
        if matches:
            chosen = sorted(matches, key=lambda x: (len(safe_str(x)), safe_str(x)), reverse=True)[0]
            return safe_str(chosen).strip().lower(), linkedin_company_slug

    # Acronym matching: only attempt for short names, only accept a unique match.
    short = normalized_company_name
    if 2 <= len(short) <= 6 and short.isalnum():
        matches = []
        for c in Company.objects.all().values_list("normalized_name", flat=True)[:8000]:
            if _acronym_of(c) == short:
                matches.append(c)
                if len(matches) > 2:
                    break
        if len(matches) == 1:
            return safe_str(matches[0]).strip().lower(), linkedin_company_slug

    return normalized_company_name, linkedin_company_slug
