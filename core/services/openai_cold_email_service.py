from __future__ import annotations

import html
import json
import os
import hashlib
import re
import time
from pathlib import Path
import requests

from core.services.email_composition_service import remove_readiness_risk_education_phrases
from core.utils import safe_str


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OPENAI_TIMEOUT = 90
ANTHROPIC_TIMEOUT = 90
ANTHROPIC_RATE_LIMIT_RETRY_SECONDS = float(os.getenv("ANTHROPIC_RATE_LIMIT_RETRY_SECONDS", "65"))
ANTHROPIC_RATE_LIMIT_MAX_RETRIES = int(os.getenv("ANTHROPIC_RATE_LIMIT_MAX_RETRIES", "2"))
DEFAULT_MODEL = os.getenv("OPENAI_COLD_EMAIL_MODEL", "gpt-5.4")
DEFAULT_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_COLD_EMAIL_MODEL", "claude-sonnet-4-6")
DEFAULT_SUBJECT_PREFIX = os.getenv("COLD_EMAIL_SUBJECT_PREFIX", "").strip()
if DEFAULT_SUBJECT_PREFIX and not DEFAULT_SUBJECT_PREFIX.endswith((" ", "-", ":")):
    DEFAULT_SUBJECT_PREFIX = f"{DEFAULT_SUBJECT_PREFIX} "
COMPACT_SUBJECT_MAX_LENGTH = 62
COMPACT_SUBJECT_ROLE_MAX_WORDS = 4
MAX_MODEL_BODY_SENTENCES = 4
MAX_MODEL_BODY_WORDS = 105

MODEL_OWNED_FORBIDDEN_PHRASES = (
    "i would be grateful if you could consider my resume",
    "consider my resume or point me to the right hiring contact",
    "point me to the right hiring contact",
    "point me to the right contact",
    "right hiring contact",
    "please see my attached resume",
    "my resume is attached",
    "attached resume",
)
INVALID_RESUME_SOURCE_PHRASES = (
    "trendlyne",
    "trendlyne.com",
)


def _repo_root() -> Path:
    # core/services/<file>.py -> repo root is 2 parents up.
    return Path(__file__).resolve().parents[2]


def _load_developer_prompt() -> tuple[str, str]:
    """
    Returns (prompt_text, prompt_version).
    prompt_version is stable for a given prompt content (hash-based).
    """
    # Prefer DB prompt if available (admin-editable). Fall back to file prompt.
    try:
        from core.models import PromptTemplate  # type: ignore

        active = (
            PromptTemplate.objects
            .filter(purpose=PromptTemplate.Purpose.COLD_EMAIL, is_active=True)
            .order_by("-updated_at", "-id")
            .first()
        )
        if active and safe_str(active.content).strip():
            prompt_text = safe_str(active.content).strip()
            digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:10]
            return prompt_text, f"db:{active.id}:{digest}"
    except Exception:
        # DB table might not exist yet (unapplied migrations) or Django not ready.
        pass

    raw_path = os.getenv("COLD_EMAIL_PROMPT_PATH", "prompts/cold_email_dev_prompt.txt").strip()
    candidates = []
    if raw_path:
        candidates.append(Path(raw_path))
        candidates.append(_repo_root() / raw_path)

    prompt_text = ""
    chosen = None
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                prompt_text = p.read_text(encoding="utf-8").strip()
                chosen = p
                break
        except Exception:
            continue

    if not prompt_text:
        raise RuntimeError(
            "Cold email prompt is missing. "
            "Set an active PromptTemplate (purpose=cold_email) in Admin, "
            "or create a prompt file and point COLD_EMAIL_PROMPT_PATH to it. "
            f"(checked: {', '.join(str(c) for c in candidates)})"
        )

    chosen_name = chosen.name if chosen else "prompt_file"

    digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:10]
    return prompt_text, f"{chosen_name}:{digest}"


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


def apply_cold_email_subject_prefix(subject: str) -> str:
    subject = safe_str(subject).strip()
    if not subject:
        return subject
    if not DEFAULT_SUBJECT_PREFIX:
        return subject
    lowered = subject.lower()
    if lowered.startswith("gayathri - ") or lowered.startswith("gayathri emuru - "):
        return subject
    return f"{DEFAULT_SUBJECT_PREFIX}{subject}"


def _clean_subject_text(value: str) -> str:
    value = html.unescape(safe_str(value)).replace("&", " and ")
    value = value.replace("–", "-").replace("—", "-").replace("#", "")
    value = re.sub(r"\s*[/|]+\s*", " and ", value)
    value = re.sub(r"\s+", " ", value).strip(" -,.")
    return value


def _remove_subject_dash_punctuation(value: str) -> str:
    value = safe_str(value)
    if not value:
        return ""

    value = re.sub(r"\s*(?:--|\u2014|\u2013|\u00e2\u20ac[\u201c\u201d]|â€“|â€”)\s*", ", ", value)
    value = re.sub(r"\s+-\s+", ", ", value)
    value = re.sub(r"\s+,", ",", value)
    value = re.sub(r",\s*,+", ", ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


LEGAL_COMPANY_SUFFIX_RE = (
    r"(?:incorporated|inc\.?|llc|l\.l\.c\.?|ltd\.?|limited|corp\.?|corporation|"
    r"llp|l\.l\.p\.?|lp|l\.p\.?|plc|p\.l\.c\.?)"
)


def humanize_company_name(company_name: str) -> str:
    company = html.unescape(safe_str(company_name)).strip()
    if not company:
        return ""

    company = company.replace("â€“", "-").replace("â€”", "-")
    company = re.sub(r"\s+", " ", company).strip(" -,.")
    company = re.sub(r"^careers\s+(?:at|with)\s+", "", company, flags=re.I).strip(" -,.")
    if company.lower() in {"manual", "unknown", "unknown company", "[unknown company]"}:
        return ""
    if company.lower().startswith("manual job email "):
        return ""

    previous = ""
    while company and company != previous:
        previous = company
        company = re.sub(rf"(?:,\s*|\s+){LEGAL_COMPANY_SUFFIX_RE}\s*$", "", company, flags=re.I).strip(" -,.")

    return company


def _strip_legal_company_suffixes_from_text(value: str) -> str:
    text = safe_str(value)
    if not text:
        return ""

    nameish = r"[A-Za-z0-9][A-Za-z0-9&'.+-]*(?:\s+[A-Za-z0-9][A-Za-z0-9&'.+-]*){0,6}"
    text = re.sub(rf"\b({nameish}),?\s+{LEGAL_COMPANY_SUFFIX_RE}\b\.?", r"\1", text, flags=re.I)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _replace_company_mentions_with_human_name(value: str, company_name: str) -> str:
    text = safe_str(value)
    if not text:
        return ""

    human_company = humanize_company_name(company_name)
    raw_company = html.unescape(safe_str(company_name)).strip()
    if human_company and raw_company and human_company.lower() != raw_company.lower():
        variants = {raw_company, raw_company.rstrip(".")}
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                text = re.sub(re.escape(variant), human_company, text, flags=re.I)

    return _strip_legal_company_suffixes_from_text(text)


def _remove_subject_slashes(value: str) -> str:
    value = safe_str(value).strip()
    if not value:
        return ""
    value = re.sub(r"\s*/\s*", " and ", value)
    value = re.sub(r"\s+", " ", value).strip(" -,.")
    return value


def _strip_subject_hiring_noise(value: str) -> str:
    value = safe_str(value).strip()
    if not value:
        return ""

    value = re.sub(r"^[^\w#]+", "", value).strip()
    replacements = (
        r"^(?:(?:we|i)(?:['’]re|['’]m| are| am)\s+)?hiring\b\s*[:!,.]?\s*(?:for\s+|an?\s+)?",
        r"^(?:now\s+)?hiring\b\s*[:!,.]?\s*(?:for\s+|an?\s+)?",
        r"^(?:looking\s+for|seeking)\s+(?:an?\s+)?",
        r"^(?:job\s+opening|open\s+role|opportunity)\b\s*[:!,-]?\s*",
    )
    previous = ""
    while value and value != previous:
        previous = value
        for pattern in replacements:
            value = re.sub(pattern, "", value, flags=re.I).strip(" -,.")

    value = re.split(r"[.!?]\s+", value, maxsplit=1)[0].strip()
    value = re.sub(r"\s+(?:on|for)\s+the\s+.+?\s+team\b.*$", "", value, flags=re.I).strip()
    value = re.sub(r"\s+to\s+join\s+(?:my|our|the)\s+team\b.*$", "", value, flags=re.I).strip()
    value = re.sub(r"[^\w)]+$", "", value).strip()
    return value


def _has_core_role_word(value: str) -> bool:
    return bool(
        re.search(
            r"\b(analyst|architect|associate|developer|engineer|manager|scientist|specialist)\b",
            safe_str(value),
            flags=re.I,
        )
    )


def _compact_role_title(job_title: str) -> str:
    role = _strip_subject_hiring_noise(_clean_subject_text(job_title))
    if not role:
        return ""

    role = re.sub(r"^entry\s+level\s*-\s*", "", role, flags=re.I).strip()
    leading_role_match = re.match(
        r"^(Analyst|Architect|Associate|Developer|Engineer|Manager|Scientist|Specialist),\s+(.+)$",
        role,
        flags=re.I,
    )
    if leading_role_match:
        role_word = leading_role_match.group(1).strip()
        specialty = leading_role_match.group(2).strip(" -,.")
        if specialty and not re.search(rf"\b{re.escape(role_word)}\b", specialty, flags=re.I):
            role = f"{specialty} {role_word}"

    parts = re.split(r"\s+-\s+|\(", role, maxsplit=1)
    if len(parts) > 1:
        first = parts[0].strip()
        rest = parts[1].strip(" -,.")
        if re.fullmatch(r"analyst", first, flags=re.I) and rest:
            role = f"{rest} Analyst" if "analyst" not in rest.lower() else rest
        elif _has_core_role_word(first) or not _has_core_role_word(rest):
            role = first
        else:
            role = rest
    role = re.split(r"\s+OR\s+", role, maxsplit=1, flags=re.I)[0].strip()
    replacements = (
        (r"\bArtificial Intelligence\b", "AI"),
        (r"\bData (?:&|and) Reporting\b", "Data Reporting"),
        (r"\bEntry Level\b", ""),
        (r"\bFull Time\b", ""),
        (r"\bUS Based\b", ""),
    )
    for pattern, replacement in replacements:
        role = re.sub(pattern, replacement, role, flags=re.I)

    if re.fullmatch(r"Analyst,?\s+Data Science", role, flags=re.I):
        role = "Data Science Analyst"
    role = re.sub(r"^(Associate|Senior|Sr\.?|Junior|Jr\.?)\s+", "", role, flags=re.I)
    role = re.sub(r"\bBusiness Intelligence Analysts\b", "Business Intelligence Analyst", role, flags=re.I)
    role = re.sub(r"\s+", " ", role).strip(" -,.")
    role = re.sub(r"\bRole\b$", "", role, flags=re.I).strip()
    return role


def _short_subject_company_name(company_name: str) -> str:
    company = humanize_company_name(company_name)
    if not company:
        return ""

    company = html.unescape(company).replace("&amp;", "and").replace(" and amp ", " and ")
    company = re.split(r"\s+\|\s+", company, maxsplit=1)[0].strip()
    company = re.sub(r"\s*\([^)]*\)", "", company).strip()
    company = re.sub(r"^The\s+", "", company, flags=re.I).strip()
    company = re.sub(r"\bfamily\s+of\s+companies\b", "", company, flags=re.I).strip()
    company = re.sub(r"\s+(?:Corporation|Company|Companies|Group|Limited)\b.*$", "", company, flags=re.I).strip()
    company = re.sub(r"\.com$", "", company, flags=re.I).strip()
    company = re.sub(r"\bBlueCross\s+BlueShield\s+of\s+(.+)$", r"BlueCross \1", company, flags=re.I)
    company = re.sub(r"\bBlue\s+Cross\s+(?:and|&)\s+Blue\s+Shield\s+of\s+(.+)$", r"BlueCross \1", company, flags=re.I)
    if re.fullmatch(r"TALENT\s+Software\s+Services", company, flags=re.I):
        company = "TALENT"
    if re.fullmatch(r"McDermott\s+Will\s+and\s+Schulte", company, flags=re.I):
        company = "McDermott"
    if re.fullmatch(r"McDermott\s+Will\s+&\s+Schulte", company, flags=re.I):
        company = "McDermott"
    company = re.sub(r"\s+", " ", company).strip(" -,.")
    return company


def _strip_role_level_suffix(role: str) -> str:
    role = safe_str(role)
    role = re.sub(r"\b(?:I|II|III|IV|V|1|2|3|4|5)\b$", "", role, flags=re.I).strip()
    role = re.sub(r"\b(?:AMZ|REQ|JR|Job)\s*[-#]?\s*\d+\b", "", role, flags=re.I).strip()
    return role


def _extract_short_core_role(text: str) -> str:
    value = html.unescape(safe_str(text)).replace("&amp;", " and ").replace("/", " and ")
    value = re.sub(r"\bSr\.\s+", "Senior ", value, flags=re.I)
    value = re.sub(r"\bAssoc\b", "Associate", value, flags=re.I)
    value = re.sub(r"\bRes\b", "Research", value, flags=re.I)
    value = re.sub(r"\bArtificial Intelligence\b", "AI", value, flags=re.I)
    value = re.sub(r"\bMachine Learning\b", "ML", value, flags=re.I)
    value = re.sub(r"\bBusiness Intelligence\b", "BI", value, flags=re.I)
    value = re.sub(r"\bAI\s+and\s+ML\b", "AI/ML", value, flags=re.I)
    ordered_patterns = (
        (r"\bComputer Vision Data Scientist\b", "Computer Vision Data Scientist"),
        (r"\bFraud Data Scientist\b", "Fraud Data Scientist"),
        (r"\bPricing Data Engineer\b", "Pricing Data Engineer"),
        (r"\bBio[-\s]?Stat(?:istical)?\s+(?:Research\s+)?Analyst\b", "Bio-Stat Analyst"),
        (r"\bData Science Analyst\b", "Data Science Analyst"),
        (r"\bData Scientist(?:s)?\b", "Data Scientist"),
        (r"\bData Engineer(?:s)?\b", "Data Engineer"),
        (r"\bData Analyst(?:s)?\b", "Data Analyst"),
        (r"\bBI Analyst(?:s)?\b", "BI Analyst"),
        (r"\bAI/ML Engineer(?:s)?\b", "AI/ML Engineer"),
        (r"\bGen AI Engineer(?:s)?\b", "Gen AI Engineer"),
        (r"\bApplied AI Engineer(?:s)?\b", "Applied AI Engineer"),
        (r"\bAI Engineer(?:s)?\b", "AI Engineer"),
        (r"\bML Engineer(?:s)?\b", "ML Engineer"),
        (r"\bSoftware Engineer(?:s)?\b", "Software Engineer"),
        (r"\bBackend Engineer(?:s)?\b", "Backend Engineer"),
        (r"\bFrontend Engineer(?:s)?\b", "Frontend Engineer"),
        (r"\bFull Stack Engineer(?:s)?\b", "Full Stack Engineer"),
        (r"\bResearch Analyst(?:s)?\b", "Research Analyst"),
        (r"\bBusiness Operations Analyst(?:s)?\b", "Business Operations Analyst"),
        (r"\bOperations Analyst(?:s)?\b", "Operations Analyst"),
        (r"\bAnalytics Engineer(?:s)?\b", "Analytics Engineer"),
        (r"\bAnalytics Analyst(?:s)?\b", "Analytics Analyst"),
        (r"\bResearch Scientist(?:s)?\b", "Research Scientist"),
        (r"\bAI Analyst(?:s)?\b", "AI Analyst"),
        (r"\bDeveloper(?:s)?\b", "Developer"),
        (r"\bAnalyst(?:s)?\b", "Analyst"),
    )
    for pattern, replacement in ordered_patterns:
        if re.search(pattern, value, flags=re.I):
            return replacement
    return ""


def _short_subject_role(role: str) -> str:
    raw_role = safe_str(role)
    raw_role = re.sub(r"\bSr\.\s+", "Senior ", raw_role, flags=re.I)
    role = _compact_role_title(raw_role)
    role = html.unescape(role).replace("&amp;", "and").replace(" and amp ", " and ")
    role = re.sub(r"^\(?USA\)?\s+", "", role, flags=re.I).strip()
    role = re.sub(r"^New\s+Grad\s+", "", role, flags=re.I).strip()
    role = re.sub(r"\bAssoc\b", "Associate", role, flags=re.I)
    role = re.sub(r"\bRes\b", "Research", role, flags=re.I)
    role = re.sub(r"\bArtificial Intelligence\b", "AI", role, flags=re.I)
    role = re.sub(r"\bMachine Learning\b", "ML", role, flags=re.I)
    role = re.sub(r"\bBusiness Intelligence\b", "BI", role, flags=re.I)
    role = role.replace("/", " and ")
    role = _strip_role_level_suffix(role)
    raw_core_role = _extract_short_core_role(raw_role)

    leading_specialty = re.match(
        r"^(Senior|Sr\.?|Junior|Jr\.?|Staff|Principal|Lead|Associate|Assistant|AD),\s+(.+)$",
        role,
        flags=re.I,
    )
    if leading_specialty:
        seniority = leading_specialty.group(1).strip()
        specialty = leading_specialty.group(2).strip(" -,.")
        if re.search(r"\bData\s+Science\b", specialty, flags=re.I):
            role = "Data Scientist" if seniority.upper() == "AD" else f"{seniority} Data Scientist"
        elif re.search(r"\b(Analyst|Developer|Engineer|Scientist|Specialist|Manager)\b", specialty, flags=re.I):
            role = specialty if seniority.upper() == "AD" else f"{seniority} {specialty}"

    role = re.split(r"\s*[,;]\s*", role, maxsplit=1)[0].strip(" -,.")
    role = re.sub(r"\s+\band\b$", "", role, flags=re.I).strip(" -,.")
    role = re.split(
        r"\s+-\s+|\s+\|\s+|\s+(?:Remote|Hybrid|Grant\s+Funded|Graduate)\b",
        role,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" -,.")
    role = re.sub(r"\s+\band\b$", "", role, flags=re.I).strip(" -,.")

    if re.search(r"\bdata\s+science\b", role, flags=re.I) and not re.search(r"\b(scientist|analyst)\b", role, flags=re.I):
        role = "Data Scientist"
    role = re.sub(r"\bData\s+Science\s+Associate\b", "Data Scientist", role, flags=re.I)
    role = re.sub(r"\bData\s+Analytics\s+(?:and\s+)?Reporting\s+Developer\b", "Reporting Developer", role, flags=re.I)
    role = re.sub(r"\bBio[-\s]?Stat(?:istical)?\s+Research\s+Analyst\b", "Bio-Stat Analyst", role, flags=re.I)
    role = re.sub(r"\bAssociate\s+Bio[-\s]?Stat(?:istical)?\s+(?:Research\s+)?Analyst\b", "Bio-Stat Analyst", role, flags=re.I)
    role = re.sub(r"\bAnalyst,\s*Maritime\s+Data\b", "Data Analyst", role, flags=re.I)
    role = re.sub(r"\bAI\s+and\s+ML\b", "AI/ML", role, flags=re.I)
    role = re.sub(r"\s+", " ", role).strip(" -,.")

    if raw_core_role and not re.search(r"\b(Analyst|Developer|Engineer|Scientist|Specialist|Manager)\b", role, flags=re.I):
        role = raw_core_role

    # If the title names two separate roles, keep the first clear role.
    multi_role = re.match(
        r"^(.+?\b(?:Analyst|Developer|Engineer|Scientist|Specialist|Manager))\s+and\s+.+\b(?:Analyst|Developer|Engineer|Scientist|Specialist|Manager)\b",
        role,
        flags=re.I,
    )
    if multi_role:
        role = multi_role.group(1).strip(" -,.")

    words = role.split()
    if len(words) > COMPACT_SUBJECT_ROLE_MAX_WORDS:
        core_match = re.search(
            r"\b((?:Senior|Sr\.?|Junior|Jr\.?|Staff|Principal|Lead|Associate)?\s*"
            r"(?:AI/ML|AI|ML|Data|Software|Backend|Frontend|Full Stack|Computer Vision|Research|BI|Business|Pricing|Fraud|Robotics|Analytics|Applied|Gen AI)?\s*"
            r"(?:Analyst|Developer|Engineer|Scientist|Specialist|Manager))\b",
            role,
            flags=re.I,
        )
        if core_match:
            role = re.sub(r"\s+", " ", core_match.group(1)).strip()
        else:
            role = " ".join(words[:COMPACT_SUBJECT_ROLE_MAX_WORDS]).strip()

    role = _strip_role_level_suffix(role)
    return re.sub(r"\s+", " ", role).strip(" -,.")


def _subject_has_bad_source_noise(subject: str) -> bool:
    subject = safe_str(subject).strip().lower()
    if not subject:
        return True
    bad_patterns = (
        r"\bwe(?:'re|’re| are)\s+hiring\b",
        r"\bi(?:'m|’m| am)\s+hiring\b",
        r"\b#?hiring\b",
        r"\blooking\s+for\b",
        r"\bjoin\s+(?:my|our|the)\s+team\b",
        r"\bcome\s+build\b",
        r"\bcompany\s+logo\b",
        r"\bcareers\s+at\b",
        r"\bback\s+to\s+jobs?\b",
        r"\bskip\s+to\b",
        r"\bsign\s+in\b",
    )
    return any(re.search(pattern, subject, flags=re.I) for pattern in bad_patterns)


def _clean_model_subject_candidate(subject: str) -> str:
    subject = safe_str(subject).strip()
    if subject.lower().startswith(DEFAULT_SUBJECT_PREFIX.lower()) and DEFAULT_SUBJECT_PREFIX:
        subject = subject[len(DEFAULT_SUBJECT_PREFIX) :].strip()
    subject = _remove_subject_slashes(subject)
    subject = _remove_subject_dash_punctuation(subject)
    subject = _strip_legal_company_suffixes_from_text(subject)
    subject = re.sub(r"\s+", " ", subject).strip(" -,.")
    subject = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", subject).strip()
    return subject


def _clean_final_subject_text(subject: str) -> str:
    subject = _clean_model_subject_candidate(subject)
    subject = re.sub(r"\b(?:I|II|III|IV|V|1|2|3|4|5)\s+role\s+at\b", "at", subject, flags=re.I)
    subject = re.sub(r"\s+role\s+at\s+", " at ", subject, flags=re.I)
    subject = re.sub(r"\s+", " ", subject).strip(" -,.")
    return subject


def _is_generic_application_subject(subject: str) -> bool:
    subject = _clean_model_subject_candidate(subject)
    if not subject:
        return False
    generic_patterns = (
        r"^\s*(?:re:\s*)?application\s+for\b",
        r"^\s*applying\s+for\b",
        r"^\s*job\s+application\b",
        r"\bapplication\s+for\b",
        r"\bmy\s+application\b",
        r"\bcandidate\s+for\b",
        r"\bresume\s+for\b",
    )
    return any(re.search(pattern, subject, flags=re.I) for pattern in generic_patterns)


def _role_from_generic_application_subject(subject: str) -> str:
    subject = _repair_application_style_subject(_clean_model_subject_candidate(subject))
    if not subject:
        return ""

    subject = re.sub(r"^\s*(?:re:\s*)?(?:application|applying)\s+for\s+", "", subject, flags=re.I)
    subject = re.sub(r"\bapplication\s+for\s+", "", subject, flags=re.I)
    subject = re.sub(r"\b(my\s+)?application\b", "", subject, flags=re.I)
    subject = re.sub(r"\b(candidate|resume)\s+for\s+", "", subject, flags=re.I)
    subject = re.sub(r"\s+", " ", subject).strip(" -,.")
    return _clean_subject_text(subject)


def _human_subject_from_role(role: str, company_name: str = "") -> str:
    role = safe_str(role).strip(" -,.")
    if not role:
        return ""
    role = re.sub(r"\bRole\b", "role", role)
    role = re.sub(r"\b(position|opportunity|opening|job)\b$", "", role, flags=re.I).strip(" -,.")
    role = re.sub(r"\brole\b$", "", role, flags=re.I).strip(" -,.")
    has_role_word = bool(re.search(r"\brole\b", role, flags=re.I))

    company = humanize_company_name(company_name)
    if company and not re.search(rf"\b{re.escape(company)}\b", role, flags=re.I):
        if re.search(r"\bat\s+[A-Za-z0-9]", role, flags=re.I):
            return role
        return f"{role} at {company}" if has_role_word else f"{role} role at {company}"
    return role if has_role_word else f"{role} role"


def _application_style_subject_from_role(role: str) -> str:
    return _human_subject_from_role(role)


def _repair_application_style_subject(subject: str) -> str:
    subject = _remove_subject_slashes(subject)
    if not subject:
        return ""

    # Repair copied job-title order such as:
    # "Application for Analyst, Business Strategy & Analytics Role"
    # -> "Application for Business Strategy & Analytics Analyst Role"
    pattern = (
        r"\b(Application for|Applying for)\s+"
        r"(Analyst|Architect|Associate|Developer|Engineer|Manager|Scientist|Specialist),\s+"
        r"(.+?)\s+"
        r"(Role|Position|Opportunity)\b"
    )

    def repl(match: re.Match) -> str:
        prefix = match.group(1)
        role_word = match.group(2)
        specialty = match.group(3).strip(" -,.")
        suffix = match.group(4)
        if not specialty or re.search(rf"\b{re.escape(role_word)}\b", specialty, flags=re.I):
            return match.group(0)
        return f"{prefix} {specialty} {role_word} {suffix}"

    subject = re.sub(pattern, repl, subject, flags=re.I)
    subject = _remove_subject_slashes(subject)
    subject = re.sub(r"\s+", " ", subject).strip(" -,.")
    return subject


def _subject_mentions_company(subject: str, company_name: str) -> bool:
    subject = safe_str(subject)
    company = humanize_company_name(company_name)
    if not subject or not company:
        return False
    return bool(re.search(rf"\b{re.escape(company)}\b", subject, flags=re.I))


def _is_role_only_subject(subject: str, role: str) -> bool:
    subject = _clean_subject_text(subject).lower()
    role = _clean_subject_text(role).lower()
    if not subject or not role:
        return False

    subject = re.sub(r"\b(role|position|opening|job)\b", "", subject, flags=re.I)
    role = re.sub(r"\b(role|position|opening|job)\b", "", role, flags=re.I)
    subject_words = set(re.findall(r"[a-z0-9]+", subject))
    role_words = set(re.findall(r"[a-z0-9]+", role))
    if not subject_words or not role_words:
        return False
    return subject_words.issubset(role_words)


def _is_usable_human_subject(subject: str, *, company_name: str, role: str) -> bool:
    subject = _clean_model_subject_candidate(_replace_company_mentions_with_human_name(subject, company_name))
    if not subject or _subject_has_bad_source_noise(subject):
        return False
    if len(subject) > 115:
        return False
    if _is_generic_application_subject(subject):
        return False
    if _is_role_only_subject(subject, role) and humanize_company_name(company_name):
        return False
    if _subject_mentions_company(subject, company_name):
        return True
    if re.search(r"\d", subject):
        return True
    subject_words = re.findall(r"[A-Za-z0-9]+", subject)
    role_words = re.findall(r"[A-Za-z0-9]+", role)
    return len(subject_words) >= len(role_words) + 2


def _specific_subject_from_title(job_title: str) -> str:
    title = _clean_subject_text(job_title)
    lowered = title.lower()
    if "data analyst" in lowered and re.search(r"\bmsd\b", lowered, flags=re.I):
        return "Data Analyst role at MSD"
    if "data scientist" in lowered and re.search(r"\bmbis\b", lowered, flags=re.I):
        return "Data Scientist role at MBIS"
    if "people insights" in lowered:
        return "People Insights Data Analyst"
    if "ai and ml infrastructure" in lowered or "ai ml infrastructure" in lowered:
        return "AI and ML Infrastructure role"
    if "hr and finance" in lowered or "hr finance" in lowered:
        return "HR and Finance BI Analyst"
    if "internal audit" in lowered:
        return "Internal Audit Data Scientist"
    if "web analytics" in lowered:
        return "Web Analytics role"
    if "digital engagement" in lowered:
        return "Digital Engagement Data Analyst"
    if "crop insurance" in lowered:
        return "Crop Insurance Data Scientist"
    if "ml resources" in lowered:
        return "ML Resources Engineer"
    if "credit risk" in lowered:
        return "Credit Risk Data Science role"
    return ""


def build_compact_cold_email_subject(*, company_name: str, job_title: str, fallback_subject: str = "") -> str:
    specific = _short_subject_role(_specific_subject_from_title(job_title))
    role = _short_subject_role(job_title)
    fallback = _replace_company_mentions_with_human_name(safe_str(fallback_subject).strip(), company_name)
    application_role = _short_subject_role(_role_from_generic_application_subject(fallback)) if _is_generic_application_subject(fallback) else ""
    fallback_role = _short_subject_role(fallback)
    role = specific or role or application_role or fallback_role or "Role"
    company = _short_subject_company_name(company_name)

    if company:
        base = f"{role} at {company}"
    else:
        base = f"{role} role"

    base = _replace_company_mentions_with_human_name(base, company_name)
    base = _remove_subject_dash_punctuation(base)
    base = re.sub(r"\s+", " ", base).strip(" -,.")
    if not base:
        return ""

    max_body_len = COMPACT_SUBJECT_MAX_LENGTH - len(DEFAULT_SUBJECT_PREFIX)
    if len(base) > max_body_len:
        words = base.split()
        shortened = []
        for word in words:
            candidate = " ".join(shortened + [word])
            if len(candidate) > max_body_len:
                break
            shortened.append(word)
        base = " ".join(shortened) or base[:max_body_len].rstrip()

    return apply_cold_email_subject_prefix(base)


def forbidden_model_body_phrases(email_body: str) -> list[str]:
    text = safe_str(email_body).strip().lower()
    if not text:
        return []
    return [phrase for phrase in MODEL_OWNED_FORBIDDEN_PHRASES if phrase in text]


def invalid_resume_source_phrases(email_body: str) -> list[str]:
    text = safe_str(email_body).strip().lower()
    if not text:
        return []
    return [phrase for phrase in INVALID_RESUME_SOURCE_PHRASES if phrase in text]


def _body_words(email_body: str) -> list[str]:
    return re.findall(r"\b[\w'’.-]+\b", safe_str(email_body))


def _body_sentences(email_body: str) -> list[str]:
    body = re.sub(r"\s+", " ", safe_str(email_body)).strip()
    if not body:
        return []
    pieces = re.split(r"(?<=[.!?])\s+", body)
    return [piece.strip() for piece in pieces if piece.strip()]


def model_body_style_issues(email_body: str) -> list[str]:
    body = safe_str(email_body).strip()
    if not body:
        return ["empty_body"]

    issues = []
    word_count = len(_body_words(body))
    sentence_count = len(_body_sentences(body))
    if word_count > MAX_MODEL_BODY_WORDS:
        issues.append(f"too_many_words:{word_count}")
    if sentence_count > MAX_MODEL_BODY_SENTENCES:
        issues.append(f"too_many_sentences:{sentence_count}")
    if "\n" in body:
        issues.append("multiple_paragraphs")
    return issues


def _compact_model_body(email_body: str) -> str:
    sentences = _body_sentences(email_body)
    if not sentences:
        return safe_str(email_body).strip()

    compact = " ".join(sentences[:MAX_MODEL_BODY_SENTENCES]).strip()
    words = compact.split()
    if len(words) > MAX_MODEL_BODY_WORDS:
        compact = " ".join(words[:MAX_MODEL_BODY_WORDS]).strip(" ,;:")
        if compact and compact[-1] not in ".!?":
            compact += "."
    return compact


def _parse_cold_email_response(response: requests.Response) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI Responses API error. "
            f"status={response.status_code} "
            f"body={response.text[:4000]}"
        )

    payload = response.json()
    raw_text = _extract_output_text(payload)
    if not raw_text:
        raise RuntimeError(
            f"OpenAI response did not contain usable output_text. "
            f"payload={json.dumps(payload)[:4000]}"
        )

    try:
        parsed = json.loads(raw_text)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse OpenAI JSON output. "
            f"error={exc} raw_text={raw_text[:4000]}"
        )

    if not isinstance(parsed, dict):
        raise RuntimeError(f"OpenAI JSON output was not an object. raw_text={raw_text[:4000]}")
    return parsed


def _json_from_model_text(raw_text: str, *, provider: str) -> dict:
    text = safe_str(raw_text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to parse {provider} JSON output. error={exc} raw_text={raw_text[:4000]}"
                ) from exc
        else:
            raise RuntimeError(f"{provider} response did not contain a JSON object. raw_text={raw_text[:4000]}")

    if not isinstance(parsed, dict):
        raise RuntimeError(f"{provider} JSON output was not an object. raw_text={raw_text[:4000]}")
    return parsed


def _parse_anthropic_cold_email_response(
    response: requests.Response,
    *,
    allow_plain_email: bool = False,
    fallback_subject: str = "",
) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(
            f"Anthropic Messages API error. "
            f"status={response.status_code} "
            f"body={response.text[:4000]}"
        )

    payload = response.json()
    chunks = []
    for block in payload.get("content") or []:
        if block.get("type") == "text" and safe_str(block.get("text")):
            chunks.append(safe_str(block.get("text")))
    raw_text = "\n".join(chunks).strip()
    if not raw_text:
        raise RuntimeError(
            f"Anthropic response did not contain usable text content. "
            f"payload={json.dumps(payload)[:4000]}"
        )
    try:
        return _json_from_model_text(raw_text, provider="Anthropic")
    except RuntimeError:
        if allow_plain_email and "{" not in raw_text and "}" not in raw_text:
            return {"subject": fallback_subject, "email": raw_text}
        raise


def _retry_body_instruction(*, previous_email: str, matched_phrases: list[str]) -> dict:
    phrases = ", ".join(f'"{phrase}"' for phrase in matched_phrases)
    return {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "Rewrite the previous draft because it included wording owned by the app footer/resume block. "
                    f"Problem phrase(s): {phrases}. "
                    "Return a fresh JSON object with the same schema. "
                    "Do not include resume-attachment wording, right-contact wording, or a generic resume-consideration line. "
                    "Keep only the role-specific outreach body; the application will append the standard resume/footer text later.\n\n"
                    f"Previous draft:\n{safe_str(previous_email)[:3000]}"
                ),
            }
        ],
    }


def _retry_short_body_instruction(*, previous_email: str, style_issues: list[str]) -> dict:
    issues = ", ".join(style_issues)
    return {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "Rewrite the previous draft because it is longer than the requested application-email style. "
                    f"Problem(s): {issues}. "
                    "Return a fresh JSON object with the same schema. "
                    "The email field must be one compact paragraph, 3 to 4 sentences, and no more than "
                    f"{MAX_MODEL_BODY_WORDS} words. "
                    "Keep it like a concise application note, not a cover letter. "
                    "Do not include greeting, sign-off, links, bullets, or resume-attachment wording.\n\n"
                    f"Previous draft:\n{safe_str(previous_email)[:3000]}"
                ),
            }
        ],
    }


def _retry_invalid_source_instruction(*, previous_email: str, matched_phrases: list[str]) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "Rewrite the previous draft because it mentioned an employer/source that is not allowed. "
                    "Return a fresh JSON object with the same schema. "
                    "Do not mention any source not present in the provided resume_text. "
                    "Use only an employer/source that appears in the provided resume_text, or source-free wording when needed.\n\n"
                    f"Previous draft:\n{safe_str(previous_email)[:3000]}"
                ),
            }
        ],
    }


def _send_cold_email_request(*, headers: dict, body: dict) -> dict:
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers=headers,
        json=body,
        timeout=OPENAI_TIMEOUT,
    )
    return _parse_cold_email_response(response)


def _finalize_model_cold_email(
    *,
    parsed: dict,
    headers: dict,
    body: dict,
    prompt_version: str,
    company_name: str = "",
    allow_retry: bool = True,
) -> dict:
    matched = forbidden_model_body_phrases(parsed.get("email", ""))
    if matched and allow_retry:
        retry_body = dict(body)
        retry_body["input"] = list(body.get("input") or []) + [
            _retry_body_instruction(previous_email=parsed.get("email", ""), matched_phrases=matched)
        ]
        parsed = _send_cold_email_request(headers=headers, body=retry_body)
        matched = forbidden_model_body_phrases(parsed.get("email", ""))

    if matched:
        raise RuntimeError(
            "Model generated footer/resume wording that belongs to the app-controlled footer. "
            f"matched_phrases={matched}"
        )

    invalid_sources = invalid_resume_source_phrases(parsed.get("email", ""))
    if invalid_sources and allow_retry:
        retry_body = dict(body)
        retry_body["input"] = list(body.get("input") or []) + [
            _retry_invalid_source_instruction(previous_email=parsed.get("email", ""), matched_phrases=invalid_sources)
        ]
        parsed = _send_cold_email_request(headers=headers, body=retry_body)
        invalid_sources = invalid_resume_source_phrases(parsed.get("email", ""))

    if invalid_sources:
        raise RuntimeError(
            "Model generated an invalid resume source that is not allowed. "
            f"matched_phrases={invalid_sources}"
        )

    style_issues = model_body_style_issues(parsed.get("email", ""))
    if style_issues and allow_retry:
        retry_body = dict(body)
        retry_body["input"] = list(body.get("input") or []) + [
            _retry_short_body_instruction(previous_email=parsed.get("email", ""), style_issues=style_issues)
        ]
        parsed = _send_cold_email_request(headers=headers, body=retry_body)
        matched = forbidden_model_body_phrases(parsed.get("email", ""))
        if matched:
            raise RuntimeError(
                "Model generated footer/resume wording that belongs to the app-controlled footer. "
                f"matched_phrases={matched}"
            )
        invalid_sources = invalid_resume_source_phrases(parsed.get("email", ""))
        if invalid_sources:
            raise RuntimeError(
                "Model generated an invalid resume source that is not allowed. "
                f"matched_phrases={invalid_sources}"
            )
        style_issues = model_body_style_issues(parsed.get("email", ""))

    if style_issues:
        parsed["email"] = _compact_model_body(parsed.get("email", ""))

    parsed["email"] = remove_readiness_risk_education_phrases(parsed.get("email", ""))
    parsed["email"] = _replace_company_mentions_with_human_name(parsed.get("email", ""), company_name)
    parsed["subject"] = apply_cold_email_subject_prefix(
        _clean_final_subject_text(_replace_company_mentions_with_human_name(parsed.get("subject", ""), company_name))
    )
    parsed.setdefault("prompt_version", prompt_version)
    return parsed


def generate_cold_email_with_gpt(
    *,
    model: str = "",
    sender_name: str,
    sender_role: str,
    sender_signature: str = "",
    company_name: str,
    recipient_name: str,
    recipient_role: str,
    job_title: str,
    job_description: str,
    company_context: str,
    value_proposition: str,
    resume_text: str,
):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")
    selected_model = safe_str(model).strip() or DEFAULT_MODEL

    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "email": {"type": "string"},
        },
        "required": ["subject", "email"],
        "additionalProperties": False,
    }

    developer_message, prompt_version = _load_developer_prompt()

    user_payload = {
        "sender_name": sender_name,
        "sender_role": sender_role,
        "sender_signature": sender_signature,
        "company_name": company_name,
        "recipient_name": recipient_name,
        "recipient_role": recipient_role,
        "job_title": job_title,
        "job_description": job_description,
        "company_context": company_context,
        "value_proposition": value_proposition,
        "resume_text": resume_text,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": selected_model,
        "input": [
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": developer_message,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(user_payload, ensure_ascii=False),
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "cold_email_generation",
                "strict": True,
                "schema": schema,
            }
        },
    }

    parsed = _send_cold_email_request(headers=headers, body=body)
    result = _finalize_model_cold_email(
        parsed=parsed,
        headers=headers,
        body=body,
        prompt_version=f"openai:{selected_model[:50]}:{prompt_version}",
        company_name=company_name,
    )
    result["subject"] = build_compact_cold_email_subject(
        company_name=company_name,
        job_title=job_title,
        fallback_subject=result.get("subject", ""),
    )
    return result


def generate_cold_email_with_gpt_custom_prompt(
    *,
    prompt_text: str,
    model: str = "gpt-5.4",
    sender_name: str,
    sender_role: str,
    sender_signature: str = "",
    company_name: str,
    recipient_name: str,
    recipient_role: str,
    job_title: str,
    job_description: str,
    company_context: str,
    value_proposition: str,
    resume_text: str,
):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")
    selected_model = safe_str(model).strip() or DEFAULT_MODEL

    prompt_text = safe_str(prompt_text).strip()
    if not prompt_text:
        raise RuntimeError("Custom prompt text is empty.")

    digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:10]
    prompt_version = f"openai:{selected_model[:50]}:custom:{digest}"

    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "email": {"type": "string"},
        },
        "required": ["subject", "email"],
        "additionalProperties": False,
    }

    user_payload = {
        "sender_name": sender_name,
        "sender_role": sender_role,
        "sender_signature": sender_signature,
        "company_name": company_name,
        "recipient_name": recipient_name,
        "recipient_role": recipient_role,
        "job_title": job_title,
        "job_description": job_description,
        "company_context": company_context,
        "value_proposition": value_proposition,
        "resume_text": resume_text,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": selected_model,
        "input": [
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt_text,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(user_payload, ensure_ascii=False),
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "cold_email_generation",
                "strict": True,
                "schema": schema,
            }
        },
    }

    parsed = _send_cold_email_request(headers=headers, body=body)
    result = _finalize_model_cold_email(
        parsed=parsed,
        headers=headers,
        body=body,
        prompt_version=prompt_version,
        company_name=company_name,
    )
    result["subject"] = build_compact_cold_email_subject(
        company_name=company_name,
        job_title=job_title,
        fallback_subject=result.get("subject", ""),
    )
    return result


def _anthropic_retry_after_seconds(response: requests.Response) -> float:
    raw_retry_after = safe_str(response.headers.get("retry-after")).strip()
    if raw_retry_after:
        try:
            return max(0.0, float(raw_retry_after))
        except Exception:
            pass
    return max(0.0, ANTHROPIC_RATE_LIMIT_RETRY_SECONDS)


def _send_anthropic_cold_email_request(
    *,
    headers: dict,
    body: dict,
    allow_plain_email: bool = False,
    fallback_subject: str = "",
) -> dict:
    max_attempts = max(1, ANTHROPIC_RATE_LIMIT_MAX_RETRIES + 1)
    last_response = None
    for attempt in range(max_attempts):
        response = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers=headers,
            json=body,
            timeout=ANTHROPIC_TIMEOUT,
        )
        if response.status_code != 429:
            return _parse_anthropic_cold_email_response(
                response,
                allow_plain_email=allow_plain_email,
                fallback_subject=fallback_subject,
            )
        last_response = response
        if attempt >= max_attempts - 1:
            break
        time.sleep(_anthropic_retry_after_seconds(response))

    return _parse_anthropic_cold_email_response(
        last_response,
        allow_plain_email=allow_plain_email,
        fallback_subject=fallback_subject,
    )


def generate_cold_email_with_anthropic_custom_prompt(
    *,
    prompt_text: str,
    model: str = "",
    sender_name: str,
    sender_role: str,
    sender_signature: str = "",
    company_name: str,
    recipient_name: str,
    recipient_role: str,
    job_title: str,
    job_description: str,
    company_context: str,
    value_proposition: str,
    resume_text: str,
):
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing in .env")

    prompt_text = safe_str(prompt_text).strip()
    if not prompt_text:
        raise RuntimeError("Custom prompt text is empty.")

    selected_model = safe_str(model).strip() or DEFAULT_ANTHROPIC_MODEL
    digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:10]
    prompt_version = f"anthropic:{selected_model[:50]}:custom:{digest}"

    user_payload = {
        "sender_name": sender_name,
        "sender_role": sender_role,
        "sender_signature": sender_signature,
        "company_name": company_name,
        "recipient_name": recipient_name,
        "recipient_role": recipient_role,
        "job_title": job_title,
        "job_description": job_description,
        "company_context": company_context,
        "value_proposition": value_proposition,
        "resume_text": resume_text,
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": selected_model,
        "max_tokens": 700,
        "temperature": 0.2,
        "system": [
            {
                "type": "text",
                "text": prompt_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            }
        ],
    }

    parsed = _send_anthropic_cold_email_request(
        headers=headers,
        body=body,
        allow_plain_email=True,
        fallback_subject=job_title,
    )
    result = _finalize_model_cold_email(
        parsed=parsed,
        headers={},
        body={},
        prompt_version=prompt_version,
        company_name=company_name,
        allow_retry=False,
    )
    result["subject"] = build_compact_cold_email_subject(
        company_name=company_name,
        job_title=job_title,
        fallback_subject=result.get("subject", ""),
    )
    return result


def generate_cold_email_with_anthropic(
    *,
    model: str = "",
    sender_name: str,
    sender_role: str,
    sender_signature: str = "",
    company_name: str,
    recipient_name: str,
    recipient_role: str,
    job_title: str,
    job_description: str,
    company_context: str,
    value_proposition: str,
    resume_text: str,
):
    developer_message, prompt_version = _load_developer_prompt()
    result = generate_cold_email_with_anthropic_custom_prompt(
        prompt_text=developer_message,
        model=model,
        sender_name=sender_name,
        sender_role=sender_role,
        sender_signature=sender_signature,
        company_name=company_name,
        recipient_name=recipient_name,
        recipient_role=recipient_role,
        job_title=job_title,
        job_description=job_description,
        company_context=company_context,
        value_proposition=value_proposition,
        resume_text=resume_text,
    )
    selected_model = safe_str(model).strip() or DEFAULT_ANTHROPIC_MODEL
    result["prompt_version"] = f"anthropic:{selected_model[:50]}:{prompt_version}"
    return result


def generate_cold_email_with_provider(
    *,
    provider: str,
    model: str = "",
    **kwargs,
):
    if safe_str(provider).strip().lower() == "anthropic":
        return generate_cold_email_with_anthropic(model=model, **kwargs)
    return generate_cold_email_with_gpt(model=model, **kwargs)


def generate_cold_email_with_provider_custom_prompt(
    *,
    provider: str,
    model: str = "",
    prompt_text: str,
    **kwargs,
):
    if safe_str(provider).strip().lower() == "anthropic":
        return generate_cold_email_with_anthropic_custom_prompt(prompt_text=prompt_text, model=model, **kwargs)
    return generate_cold_email_with_gpt_custom_prompt(prompt_text=prompt_text, model=model, **kwargs)


def get_cold_email_prompt_info() -> dict:
    """
    Returns non-sensitive prompt metadata for UI/debug (no prompt text).
    """
    try:
        _, prompt_version = _load_developer_prompt()
    except Exception as exc:
        return {"prompt_version": "", "error": str(exc)}
    return {"prompt_version": prompt_version, "error": ""}


def get_cold_email_prompt_text() -> dict:
    """
    Returns the actual prompt text (for server-side debugging/logging).
    Avoid returning this to end users in templates.
    """
    prompt_text, prompt_version = _load_developer_prompt()
    return {"prompt_text": prompt_text, "prompt_version": prompt_version}
