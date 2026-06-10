from __future__ import annotations

import re

from core.utils import safe_str


RECRUITING_TITLE_PATTERNS = (
    r"\brecruit(?:er|ing|ment)\b",
    r"\btalent acquisition\b",
    r"\btalent sourc(?:er|ing)\b",
    r"\btechnical sourc(?:er|ing)\b",
    r"\bsourc(?:er|ing)\b",
    r"\bhead of talent\b",
    r"\btalent executive\b",
    r"\btalent consultant\b",
    r"\btalent advisor\b",
    r"\btalent specialist\b",
    r"\btalent associate\b",
    r"\btalent partner\b",
    r"\btalent lead\b",
    r"\btalent manager\b",
    r"\btalent coordinator\b",
    r"\bhuman resources\b",
    r"\bhuman capital\b",
    r"\bhr\b",
    r"\bhrbp\b",
    r"\bpeople operations\b",
    r"\bpeople ops\b",
    r"\bpeople partner\b",
    r"\bpeople and culture\b",
    r"\bstaffing\b",
    r"\bstaffing manager\b",
    r"\bstaffing specialist\b",
    r"\bhiring manager\b",
    r"\bhiring specialist\b",
    r"\bhiring partner\b",
    r"\bhiring lead\b",
    r"\bhiring coordinator\b",
    r"\bcampus recruit(?:er|ing)\b",
    r"\buniversity recruit(?:er|ing)\b",
    r"\buniversity relations\b",
)

LOW_VALUE_TITLE_PATTERNS = (
    r"\bintern\b",
    r"\bassistant\b",
)

FALLBACK_CONTACT_BLOCK_PATTERNS = (
    r"\brecruit(?:er|ing|ment)\b",
    r"\btalent acquisition\b",
    r"\btalent sourc(?:er|ing)\b",
    r"\btechnical sourc(?:er|ing)\b",
    r"\bsourc(?:er|ing)\b",
    r"\bhead of talent\b",
    r"\bhuman resources\b",
    r"\bhuman capital\b",
    r"\bhr\b",
    r"\bhrbp\b",
    r"\bpeople operations\b",
    r"\bpeople ops\b",
    r"\bpeople partner\b",
    r"\bpeople and culture\b",
    r"\bstaffing\b",
    r"\bcampus recruit(?:er|ing)\b",
    r"\buniversity recruit(?:er|ing)\b",
    r"\buniversity relations\b",
    r"\battorney\b",
    r"\blegal\b",
    r"\bcounsel\b",
    r"\bcfo\b",
    r"\bchief financial officer\b",
    r"\bfinance\b",
    r"\bportfolio\b",
    r"\besg\b",
    r"\bfood safety\b",
    r"\bsales\b",
    r"\bmarketing\b",
)

DATA_DISCIPLINE_PATTERN = (
    r"(?:data science|machine learning|artificial intelligence|ai|ml|"
    r"data engineering|ml engineering|analytics|decision science|"
    r"data platform|data products?|data and analytics|data & analytics|"
    r"data analytics|business analytics|product analytics|advanced analytics|"
    r"predictive analytics|business intelligence|bi|insights|"
    r"applied ai|applied science|research science|"
    r"data architecture|data governance|data strategy|"
    r"data)"
)

DATA_SCIENCE_MANAGER_TITLE_PATTERNS = (
    rf"\b(?:associate |senior |sr\.? |group |principal |executive )?(?:manager|director) (?:of )?{DATA_DISCIPLINE_PATTERN}\b",
    rf"\b{DATA_DISCIPLINE_PATTERN} (?:associate |senior |sr\.? |group |principal |executive )?(?:manager|director)\b",
    rf"\b(?:head|lead) of {DATA_DISCIPLINE_PATTERN}\b",
    rf"\b{DATA_DISCIPLINE_PATTERN} (?:head|lead)\b",
    rf"\b(?:vp|vice president) of {DATA_DISCIPLINE_PATTERN}\b",
    rf"\b{DATA_DISCIPLINE_PATTERN} (?:vp|vice president)\b",
)

FALLBACK_CONTACT_TITLE_PATTERNS = (
    r"\bchief data officer\b",
    r"\bchief analytics officer\b",
    r"\bchief ai officer\b",
    r"\bchief artificial intelligence officer\b",
    rf"\bhead of {DATA_DISCIPLINE_PATTERN}\b",
    rf"\b(?:vp|vice president|director|senior director|executive director) of {DATA_DISCIPLINE_PATTERN}\b",
    rf"\b{DATA_DISCIPLINE_PATTERN} (?:vp|vice president|director|senior director|executive director)\b",
)

GENERAL_BUSINESS_LEADERSHIP_TITLE_PATTERNS = (
    r"\b(?:head|vp|vice president|director|senior director|manager|senior manager) of (?:operations|product|programs?|projects?|delivery|implementation|customer success|client success|client services|solutions|technology|information technology|it|engineering|software engineering)\b",
    r"\b(?:operations|product|program|project|delivery|implementation|customer success|client success|client services|solutions|technology|information technology|it|engineering|software engineering) (?:head|vp|vice president|director|senior director|manager|senior manager)\b",
    r"\b(?:senior |sr\.? )?(?:program|project|delivery|implementation|operations|product|customer success|client success|client services|solutions|technical account) manager\b",
    r"\b(?:senior |sr\.? )?solutions architect\b",
    r"\b(?:senior |sr\.? )?technical account manager\b",
)

DOMAIN_BUSINESS_OWNER_TITLE_PATTERNS = (
    r"\bmember of (?:the )?(?:executive committee|management committee|leadership team)\b",
    r"\b(?:co[- ]?)?(?:head|lead) of (?:secondaries|primaries|co[- ]?investment|co[- ]?investments|buyout|private equity|infrastructure|credit|growth|direct lending|funds?|fund of funds|portfolio|investment|investments|asset management|wealth|private wealth|investor relations|client solutions|business development|strategy|corporate development|transformation|business operations|analytics|research)\b",
    r"\b(?:global |regional |deputy |associate |senior |sr\.? |executive )?(?:managing director|director|partner|principal|vice president|vp|president|co[- ]?ceo|chief executive officer|ceo)\b",
    r"\b(?:senior |sr\.? |principal |lead |associate )?(?:investment|private equity|portfolio|fund|asset management|wealth management|credit|infrastructure|buyout|secondaries|primaries|co[- ]?investment|co[- ]?investments|capital markets|investor relations|client solutions|strategy|corporate development|business operations|business intelligence|analytics|research) (?:analyst|associate|manager|director|lead|principal|partner|vp|vice president)\b",
    r"\b(?:senior |sr\.? |principal |lead |associate )?(?:analyst|associate|manager|director|principal|partner|vp|vice president) (?:of |for )?(?:investment|investments|private equity|portfolio|funds?|asset management|wealth management|credit|infrastructure|buyout|secondaries|primaries|co[- ]?investment|co[- ]?investments|capital markets|investor relations|client solutions|strategy|corporate development|business operations|business intelligence|analytics|research)\b",
    r"\b(?:private equity|fund of funds|portfolio operations|portfolio management|investment management|asset management|wealth management|private wealth|client solutions|investor relations|capital markets|corporate development|business transformation|business operations)\b",
    r"\b(?:investment|portfolio|fund|asset|wealth|credit|infrastructure|buyout|secondaries|primaries|co[- ]?investment|co[- ]?investments) (?:team|group|platform|practice)\b",
)

LAST_RESORT_TECHNICAL_TITLE_PATTERNS = (
    r"\b(?:ceo|chief executive officer)\b",
    r"\b(?:cto|chief technology officer)\b",
    r"\b(?:founder|co founder|cofounder|founding partner)\b",
    r"\btechnical (?:founder|co founder|cofounder)\b",
    r"\b(?:head|vp|vice president|director) of engineering\b",
    r"\bengineering (?:head|vp|vice president|director|manager)\b",
    r"\bsoftware engineering (?:manager|director|lead)\b",
    r"\bfounding (?:engineer|data scientist|applied ai engineer|machine learning engineer|ml engineer)\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?data scientist\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?(?:machine learning|ml) engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?(?:ai|artificial intelligence) engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?applied ai engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?data engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?analytics engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?software engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?backend engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?frontend engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?full stack engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?computer vision engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?robotics(?: and mechatronics)? engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?automation engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?(?:devops|cloudops|cloud) engineer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?(?:data|business intelligence|bi|research|scientific research|data quality) analyst\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?(?:software|backend|back end|frontend|front end|full stack|cloud|devops|tibco|python|java|\.net) developer\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?applied scientist\b",
    r"\b(?:senior |sr\.? |staff |principal |lead )?research scientist\b",
    r"\bmember of technical staff\b",
)

LAST_RESORT_EXECUTIVE_TITLE_PATTERNS = (
    r"\b(?:ceo|chief executive officer)\b",
    r"\b(?:founder|co founder|cofounder|founding partner)\b",
)


def normalized_contact_title(title: str) -> str:
    text = safe_str(title).strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    text = re.sub(r"[/|,()]+", " ", text)
    text = re.sub(r"[^a-z0-9+\-. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_recruiting_or_hiring_contact_title(title: str, *, allow_missing: bool = False) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return bool(allow_missing)
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in RECRUITING_TITLE_PATTERNS)


def is_fallback_business_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    if any(re.search(pattern, text) for pattern in FALLBACK_CONTACT_BLOCK_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in FALLBACK_CONTACT_TITLE_PATTERNS)


def is_general_business_leadership_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    if any(re.search(pattern, text) for pattern in FALLBACK_CONTACT_BLOCK_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in GENERAL_BUSINESS_LEADERSHIP_TITLE_PATTERNS)


def is_domain_business_owner_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in DOMAIN_BUSINESS_OWNER_TITLE_PATTERNS)


def is_last_resort_technical_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    if any(re.search(pattern, text) for pattern in FALLBACK_CONTACT_BLOCK_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in LAST_RESORT_TECHNICAL_TITLE_PATTERNS)


def is_non_executive_technical_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LAST_RESORT_EXECUTIVE_TITLE_PATTERNS):
        return False
    return is_last_resort_technical_contact_title(title)


def is_data_science_manager_contact_title(title: str) -> bool:
    text = normalized_contact_title(title)
    if not text:
        return False
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return False
    if any(re.search(pattern, text) for pattern in FALLBACK_CONTACT_BLOCK_PATTERNS):
        return False
    return any(re.search(pattern, text) for pattern in DATA_SCIENCE_MANAGER_TITLE_PATTERNS)


def contact_title_rejection_reason(title: str) -> str:
    text = normalized_contact_title(title)
    if not text:
        return "missing_contact_title"
    if any(re.search(pattern, text) for pattern in LOW_VALUE_TITLE_PATTERNS):
        return "low_value_contact_title"
    if any(re.search(pattern, text) for pattern in RECRUITING_TITLE_PATTERNS):
        return "recruiting_or_talent_title_not_selected"
    return "non_data_science_manager_title"


def recruiter_contact_is_allowed(
    recruiter,
    *,
    allow_legacy_without_title: bool = False,
    allow_manual_without_title: bool = True,
    allow_fallback_business_title: bool = False,
    allow_paid_apollo_reveal: bool = True,
) -> bool:
    title = safe_str(getattr(recruiter, "apollo_title", "")).strip()
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    email = safe_str(getattr(recruiter, "email", "")).strip().lower()
    email_status = safe_str(getattr(recruiter, "email_status", "")).strip().lower()
    paid_apollo_reveal = bool(
        allow_paid_apollo_reveal
        and source == "apollo"
        and email
        and email != "none"
        and "@" in email
        and email_status == "verified"
    )
    if bool(getattr(recruiter, "manually_targeted", False)):
        if not title:
            return bool(allow_manual_without_title)
        return (
            is_data_science_manager_contact_title(title)
            or is_recruiting_or_hiring_contact_title(title)
            or is_fallback_business_contact_title(title)
            or paid_apollo_reveal
        )

    if title:
        if is_data_science_manager_contact_title(title) or is_recruiting_or_hiring_contact_title(title):
            return True
        if allow_fallback_business_title and is_fallback_business_contact_title(title):
            return True
        if paid_apollo_reveal:
            return True
        return False

    is_legacy = bool(getattr(recruiter, "legacy", False) or source == "legacy")
    is_manual = email_status.startswith("manual")

    if paid_apollo_reveal:
        return True
    if allow_legacy_without_title and is_legacy:
        return True
    if allow_manual_without_title and is_manual:
        return True
    return False
