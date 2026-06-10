from __future__ import annotations

import re

from core.services.email_sending_control_service import get_resume_attachment_state
from core.utils import normalize_linkedin_job_url, safe_str


DEFAULT_SIGNATURE_BLOCK = (
    "Best regards,\n"
    "Gayathri Emuru.\n"
    "\n"
    # "Portfolio: https://gayathri-emuru.github.io/\n"
    # "Linkedin: https://www.linkedin.com/in/gayathri-emuru/\n"
)

RESUME_ATTACHMENT_SENTENCE = "I have attached my resume for context."
APPLICATION_OPENER = "I hope this message finds you well."
# APPLICATION_READ_TIME_LINE = "Job Link: "
APPLICATION_RESUME_ASK = ""
APPLICATION_SEARCH_WITH_RESUME_OFFER_LINE = 'If helpful, you can find more about my work by searching "Gayathri Emuru", and I\'d be happy to send my resume as well.'
APPLICATION_SEARCH_WITH_ATTACHED_RESUME_LINE = 'If helpful, you can find more about my work by searching "Gayathri Emuru". I have attached my resume for your reference.'
APPLICATION_SIGNATURE_BLOCK = (
    "Kind regards,\n"
    "Gayathri Emuru\n"
    "\n"
    # "Portfolio: https://gayathri-emuru.github.io/\n"
    # "LinkedIn: https://www.linkedin.com/in/gayathri-emuru/"
)
RECRUITER_SCREEN_CTA = (
    "Would you be open to a quick chat about the role this week?"
)
BODY_STYLE_REPLACEMENTS = (
    (r"\bI'm reaching out to express my interest in\b", "I'm interested in"),
    (r"\bI am reaching out to express my interest in\b", "I'm interested in"),
    (r"\bI'm reaching out to express interest in\b", "I'm interested in"),
    (r"\bI am reaching out to express interest in\b", "I'm interested in"),
    (r"\bI'm reaching out to\b", "I'm applying for"),
    (r"\bI'm reaching out about\b", "I'm applying for"),
    (r"\bI am reaching out to\b", "I'm applying for"),
    (r"\bI am reaching out about\b", "I'm applying for"),
    (r"\bI bring\b", "I've worked with"),
    (r"\bMy background also includes\b", "I've also worked on"),
    (
        r"\bWould you be open to a brief conversation to see if there may be a fit\?\s*",
        RECRUITER_SCREEN_CTA,
    ),
    (
        r"\bWould you be open to a 10[-\s]*minute recruiter screen this week,\s*or is there someone better I should contact\?\s*",
        RECRUITER_SCREEN_CTA,
    ),
    (
        r"\bWould you be open to a 10[-\s]*minute (?:call|chat|conversation|screen) this week\?\s*",
        RECRUITER_SCREEN_CTA,
    ),
    (r"\bPlease see my attached Resume\.\s*", ""),
    (r"\bI have attached my resume for context\.\s*", ""),
)


def remove_ai_dash_punctuation(text: str) -> str:
    text = safe_str(text)
    if not text:
        return ""

    text = re.sub(r"\s*(?:--|\u2014|\u2013|\u00e2\u20ac[\u201c\u201d])\s*", ", ", text)
    text = re.sub(r"\s+-\s+", ", ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,+", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def remove_quick_note_opener(text: str) -> str:
    body = safe_str(text).strip()
    if not body:
        return ""

    opener = re.match(
        r"^quick note (?:on|about)\s+the\s+(.+?)\s+opening\s+at\s+([^,.!?]+),\s+(.+)$",
        body,
        flags=re.I | re.S,
    )
    if opener:
        role = opener.group(1).strip()
        company = opener.group(2).strip()
        rest = opener.group(3).strip()
        first_sentence = re.match(r"^(.+?[.!?])(\s+.*)?$", rest, flags=re.S)
        if first_sentence:
            detail = first_sentence.group(1).strip()
            tail = first_sentence.group(2) or ""
            stripped_detail = re.sub(r"\s+stood out(?:\s+to me)?([.!?])$", r"\1", detail, flags=re.I).strip()
            if stripped_detail != detail:
                stripped_detail = stripped_detail.rstrip(".!?").strip()
                if stripped_detail:
                    stripped_detail = stripped_detail[0].lower() + stripped_detail[1:]
                    return (
                        f"The {role} opening at {company} stood out because it centers on "
                        f"{stripped_detail}.{tail}"
                    ).strip()
        if rest:
            rest = rest[0].lower() + rest[1:]
        return f"The {role} opening at {company} stood out because {rest}".strip()

    body = re.sub(r"^quick note (?:on|about)\s+", "", body, flags=re.I).strip()
    if body:
        body = body[0].upper() + body[1:]
    return body


def remove_readiness_risk_education_phrases(text: str) -> str:
    body = safe_str(text).strip()
    if not body:
        return ""

    degree = r"(?:an?\s+)?(?:M\.?\s*S\.?|Master'?s|Masters|MSDS)(?:\s+(?:degree\s+)?in\s+Data Science)?"
    progress = r"(?:finishing|completing|wrapping\s+up|currently\s+pursuing|currently\s+completing|soon\s+to\s+graduate\s+from)"

    body = re.sub(
        rf"\bI(?:'m| am)\s+{progress}\s+{degree}\s+and\s+have\b",
        "I have",
        body,
        flags=re.I,
    )
    body = re.sub(
        rf"\bI(?:'m| am)\s+{progress}\s+{degree}\s+and\s+",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        rf"\bI(?:'m| am)\s+{progress}\s+{degree}\s*[,.]?\s*",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        rf"\s*,?\s*(?:while\s+)?{progress}\s+{degree}\s*,?\s*",
        " ",
        body,
        flags=re.I,
    )
    body = re.sub(r"\b(?:M\.?\s*S\.?|MSDS|Master'?s|Masters)\s+in\s+Data Science\b", "", body, flags=re.I)
    body = re.sub(r"\s+,", ",", body)
    body = re.sub(r"\s+\.", ".", body)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body


def _clean_first_token(name: str) -> str:
    token = safe_str(name).strip().split(" ", 1)[0] if safe_str(name).strip() else ""
    token = token.strip().strip(",").strip(".").strip()
    # Keep letters, apostrophes, hyphens (so O'Neil / Anne-Marie survive).
    token = re.sub(r"[^A-Za-z'\-]", "", token)
    return token


def recipient_first_name(name: str) -> str:
    token = _clean_first_token(name)
    if token:
        return token
    return "Hiring Team"


def canonical_linkedin_job_url_for_email(url: str) -> str:
    """
    Convert any LinkedIn job URL shape into:
      https://www.linkedin.com/jobs/view/<job_id>/
    If a job_id can't be extracted, returns a cleaned URL (no query/fragment) or empty.
    """
    normalized = normalize_linkedin_job_url(url)
    if not normalized:
        return ""

    match = re.search(r"/jobs/view/(\d+)/?$", normalized, flags=re.I)
    if match and safe_str(match.group(1)).isdigit():
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"

    # If normalize_linkedin_job_url returned a non-canonical path, keep it but add trailing slash for consistency.
    return normalized.rstrip("/") + "/"


def build_standard_footer(*, job_linkedin_url: str, manual_job_reference_id: str = "") -> str:
    job_reference = build_job_reference_line(
        job_linkedin_url=job_linkedin_url,
        manual_job_reference_id=manual_job_reference_id,
    )
    if job_reference:
        return job_reference

    return DEFAULT_SIGNATURE_BLOCK


def build_job_reference_line(*, job_linkedin_url: str, manual_job_reference_id: str = "") -> str:
    reference_id = safe_str(manual_job_reference_id).strip()
    if reference_id:
        return f"Job ID: {reference_id}"

    job_url = canonical_linkedin_job_url_for_email(job_linkedin_url)
    if not job_url:
        return ""
    return f"Job posting: {job_url}"


def _strip_leading_personal_salutation(body: str) -> str:
    body = re.sub(
        r"^\s*(?:dear|hi|hello|hey)\s+[A-Za-z][A-Za-z'’\-]*(?:\s+[A-Za-z][A-Za-z'’\-]*)*\s*[,—:-]\s*",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r"^\s*(?:dear|hi|hello|hey)\s+[A-Za-z][A-Za-z'’\-]*(?:\s+[A-Za-z][A-Za-z'’\-]*)*\s*$",
        "",
        body,
        flags=re.I,
    )
    return body


def _compact_body_style(base_body: str) -> str:
    body = safe_str(base_body).strip()
    if not body:
        return body

    for pattern, replacement in BODY_STYLE_REPLACEMENTS:
        body = re.sub(pattern, replacement, body, flags=re.I)

    body = _strip_leading_personal_salutation(body)
    body = remove_ai_dash_punctuation(body)
    body = remove_quick_note_opener(body)
    body = remove_readiness_risk_education_phrases(body)
    body = re.sub(r"\s+", " ", body).strip()
    sentences = [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", body) if piece.strip()]
    if len(sentences) > 4:
        body = " ".join(sentences[:4]).strip()
    return body


def _strip_app_owned_application_text(body: str) -> str:
    text = safe_str(body).strip()
    if not text:
        return ""

    patterns = (
        r"^I hope this message finds you well\.?\s*",
        r"\bI really appreciate you taking the time to read this\.?\s*",
        r"\bWould you be open to a quick chat about the role this week\??\s*",
        r"\bWould you be open to a 10[-\s]*minute (?:recruiter screen|call|chat|conversation|screen).*?\?\s*",
        r"\bPlease find my attached resume\.?\s*",
        r"\bI have attached my resume for context\.?\s*",
        r"\bIf my profile seems like a good fit, would you be kind enough to forward my resume to the hiring manager\??\s*",
        r"\bThank you so much,?\s+[A-Za-z][A-Za-z'\-]*\.?\s*I really appreciate your time and help\.?\s*",
        r"\b(?:Best|Warm) regards,?\s*Gayathri(?:\s+Emuru)?\.?\s*",
        r"\bPortfolio:\s*\S+\s*",
        r"\bLinkedin:\s*\S+\s*",
        r"\bLinkedIn:\s*\S+\s*",
        r"\bJob posting:\s*\S+\s*",
        r"\bWarm regards,?\s*Gayathri(?:\s+Emuru)?\.?\s*",
    )
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.I)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def _application_middle_paragraph(base_body: str) -> str:
    body = safe_str(base_body).strip()
    if not body:
        return ""

    body = _strip_leading_personal_salutation(body)
    for pattern, replacement in BODY_STYLE_REPLACEMENTS:
        body = re.sub(pattern, replacement, body, flags=re.I)
    body = remove_ai_dash_punctuation(body)
    body = remove_quick_note_opener(body)
    body = remove_readiness_risk_education_phrases(body)
    body = _strip_app_owned_application_text(body)
    body = re.sub(r"\s+", " ", body).strip()
    return body


def _split_application_middle_for_readability(middle: str) -> str:
    text = safe_str(middle).strip()
    if not text:
        return ""

    background_match = re.search(r"\bMy background includes\b", text)
    if background_match and background_match.start() > 0:
        first_part = text[: background_match.start()].strip()
        second_part = text[background_match.start() :].strip()
        if first_part and second_part:
            return f"{first_part}\n\n{second_part}"

    sentences = [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", text) if piece.strip()]
    if len(sentences) >= 3:
        first_part = " ".join(sentences[:2]).strip()
        second_part = " ".join(sentences[2:]).strip()
        if first_part and second_part:
            return f"{first_part}\n\n{second_part}"

    return text


def _application_search_line(*, resume_attached: bool) -> str:
    if resume_attached:
        return APPLICATION_SEARCH_WITH_ATTACHED_RESUME_LINE
    return APPLICATION_SEARCH_WITH_RESUME_OFFER_LINE


def build_initial_application_email_body(
    *,
    recipient_name: str,
    base_body: str,
    linkedin_url: str,
    resume_attached: bool = False,
) -> str:
    first = recipient_first_name(recipient_name)
    middle = _application_middle_paragraph(base_body)
    parts = [
        f"Hi {first},",
        APPLICATION_OPENER,
    ]
    if middle:
        parts.append(_split_application_middle_for_readability(middle))
    if linkedin_url:
        parts.append(linkedin_url)
    parts.extend(
        [
            f"I really appreciate your time and help, {first}. {_application_search_line(resume_attached=resume_attached)}",
            APPLICATION_SIGNATURE_BLOCK,
        ]
    )
    return "\n\n".join(parts)


def _has_recruiter_screen_cta(body: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", safe_str(body).lower()).strip()
    if not normalized:
        return False

    has_ten_minute = bool(re.search(r"\b10\s*minute\b|\bten\s*minute\b", normalized))
    has_screen_call = any(
        token in normalized
        for token in ("recruiter screen", "phone screen", "quick call", "brief call", "quick conversation", "quick chat")
    )
    has_contact_referral = (
        "someone better" in normalized
        or "right contact" in normalized
        or "best person" in normalized
        or "better contact" in normalized
    )

    if has_ten_minute and (has_screen_call or has_contact_referral):
        return True
    if "would you be open" in normalized and (has_screen_call or has_contact_referral or "about the role" in normalized):
        return True
    return False


def ensure_recruiter_screen_cta(base_body: str) -> str:
    base = safe_str(base_body).strip()
    if not base:
        return RECRUITER_SCREEN_CTA

    if _has_recruiter_screen_cta(base):
        return base

    return f"{base.rstrip()} {RECRUITER_SCREEN_CTA}"


def append_resume_attachment_sentence(base_body: str) -> str:
    base = safe_str(base_body).strip()
    if not base:
        return RESUME_ATTACHMENT_SENTENCE

    if RESUME_ATTACHMENT_SENTENCE.lower() in base.lower():
        return base

    return f"{base.rstrip()} {RESUME_ATTACHMENT_SENTENCE}"


def build_full_email_body(
    *,
    recipient_name: str,
    base_body: str,
    job_linkedin_url: str,
    manual_job_reference_id: str = "",
    include_job_reference: bool = True,
    include_resume_attachment_sentence: bool = True,
) -> str:
    """
    Produces the final outbound email body with a personalized salutation when available.
    """
    first = recipient_first_name(recipient_name)
    linkedin_url = (
        build_job_reference_line(
            job_linkedin_url=job_linkedin_url,
            manual_job_reference_id=manual_job_reference_id,
        )
        if include_job_reference
        else ""
    )
    if include_resume_attachment_sentence:
        resume_attached = bool(get_resume_attachment_state()["enabled"])
        return build_initial_application_email_body(
            recipient_name=recipient_name,
            base_body=base_body,
            linkedin_url=linkedin_url,
            resume_attached=resume_attached,
        )

    base = ensure_recruiter_screen_cta(_compact_body_style(base_body))
    footer = (
        build_standard_footer(
            job_linkedin_url=job_linkedin_url,
            manual_job_reference_id=manual_job_reference_id,
        )
        if include_job_reference
        else DEFAULT_SIGNATURE_BLOCK
    )
    return f"Dear {first},\n\n{base}\n\n{footer}"
