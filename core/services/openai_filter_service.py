import os
import json
from functools import lru_cache

from core.utils import safe_str
from core.constants import DEFAULT_OPENAI_MODEL

JOB_FILTER_PROMPT_NAME = "applyreject_job"
DEFAULT_JOB_FILTER_MODEL = os.getenv("OPENAI_JOB_FILTER_MODEL", "").strip() or os.getenv("OPENAI_COLD_EMAIL_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
DEFAULT_JOB_FILTER_SYSTEM_PROMPT = """
You are a strict job application gatekeeper for one specific candidate.

Your task: read the job title and job description, then decide whether the candidate should APPLY or REJECT.

Optimization priority:
- This filter is only for hard blockers.
- Do not reject just because the title, role family, or job function looks imperfect.
- When there is no hard blocker, return APPLY and let later manual review decide whether it is useful.
- If uncertain about role fit, still APPLY unless a hard reject rule is clearly present.

CANDIDATE PROFILE
- International student with work authorization/EAD.
- For this filter, ignore normal sponsorship concerns unless the job excludes F-1/OPT/EAD candidates.
- Education: Bachelor's / Master's degree.
- No PhD, MD, JD, or postdoctoral background.
- Individual contributor only.
- No people-management roles.
- No security clearance.
- Open to relocation anywhere in the United States.
- No CPA, CFA, PE, Series 7/63/65/66, medical, nursing, or pharmacy license.

TARGET / PREFERRED ROLES
These are preferred roles, but they are NOT required for APPLY:
- Data Analyst
- Data Scientist
- Data Engineer
- Analytics Engineer
- Business Intelligence Analyst / BI Analyst
- Machine Learning Engineer
- AI Engineer
- Research Analyst
- Quantitative Analyst
- Reporting Analyst
- Product/Data/Business Analyst when the work is mostly data, analytics, SQL, Python, BI, ML, statistics, or reporting

DO NOT REJECT just because a job is outside the preferred list.
The following role families may be lower priority, but should still APPLY unless a hard reject rule appears:
- Sales, marketing, recruiter, HR, legal, accounting, finance-only, insurance sales, real estate
- Nursing, pharmacy, medical, clinical license roles
- Mechanical, civil, electrical, hardware, manufacturing, field, technician, warehouse, retail, call center
- Cybersecurity, SOC, GRC, cloud infrastructure, DevOps, network admin, systems admin
- Frontend, backend, full-stack, mobile, embedded, QA-only, SDET-only software engineering
- Product manager, project manager, program manager, scrum master
- Customer support, implementation consultant, solutions consultant, teaching/training

HARD REJECT RULES
Reject if any one of these is present as required, preferred, desired, ability to obtain, eligibility, or "nice to have":

1. Clearance / government restriction
- security clearance
- Secret, Top Secret, TS/SCI, SCI
- polygraph, CI polygraph, Full Scope polygraph
- public trust
- DoD, DHS, federal clearance
- ITAR, export control
- US citizen only, US persons only
- permanent resident / green card required

2. Experience too high
- Requires, expects, or strongly asks for 4+ years of professional experience.
- Reject examples: 4+ years, 5 years, minimum 4 years, at least 4 years, 3-5 years, 4-6 years, extensive experience, senior-level experience, proven track record with multiple years.
- APPLY is allowed only when required experience is clearly 0-3 years, entry-level, associate-level, new grad, early career, internship, or not specified.

3. Education mismatch
- PhD / doctorate required
- MD required
- JD required
- Postdoctoral position
- Reject if Master's plus PhD-like research background is effectively required.
- Do not reject if the job says Bachelor's or Master's.
- Do not reject if it says Master's preferred.

4. Seniority / management
- Director or above
- VP, head of, executive, founder, partner
- Principal, staff, architect
- Manager roles that primarily manage people
- Product Manager, Project Manager, Program Manager
- Reject senior roles if the description requires 4+ years.
- Allow "Senior Analyst" only if experience requirement is 0-3 years or not specified.

5. Licenses / credentials
- CPA, CFA, PE
- Series 7, 63, 65, 66
- Active medical, nursing, pharmacy, clinical license

6. Work authorization exclusions
- Reject only if the job explicitly says F-1, OPT, CPT, EAD, student visa, or similar candidates are not eligible.
- Ignore ordinary sponsorship wording otherwise.

7. Salary
- Reject only if the minimum salary/package is clearly above 150,000.
- Do not reject based on maximum salary.
- Do not reject if salary is missing or unclear.

DECISION RULES
- Do not reject from title or role family alone.
- Reject if the title alone clearly triggers a listed hard reject, such as Director, VP, Principal, Staff, Architect, Manager, Product Manager, Project Manager, Program Manager, clearance, US citizen only, license, or 4+ years.
- Reject if the description clearly triggers a hard reject.
- Do not reject because the job is an odd/outside role.
- Do not reject because the job is mostly business operations, sales operations, customer support, marketing, finance, accounting, HR, legal, software engineering, project/product/program management, or IT infrastructure unless it also triggers a hard reject rule.
- Apply whenever no hard reject condition appears.
- If the description is too short to know role fit, APPLY unless the title clearly triggers a hard reject.

Return exactly one JSON object and nothing else:
{"decision":"APPLY_OR_REJECT","reason":"MAX_5_WORDS"}

Examples:
{"decision":"REJECT","reason":"requires security clearance"}
{"decision":"REJECT","reason":"needs 5 years experience"}
{"decision":"APPLY","reason":"no hard blocker"}
{"decision":"APPLY","reason":"matches data analyst role"}
""".strip()


@lru_cache(maxsize=4)
def _get_openai_client(api_key: str):
    # Lazy import so Django management commands can run even if the `openai` package
    # isn't installed in the active environment.
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "OpenAI classifier requires the `openai` Python package. "
            "Install it (pip install openai) or disable OpenAI filtering."
        ) from exc

    return OpenAI(api_key=api_key)


def _extract_output_text(response) -> str:
    if getattr(response, "output_text", None):
        return response.output_text.strip()

    chunks = []
    for item in getattr(response, "output", []):
        for content in getattr(item, "content", []):
            if getattr(content, "type", "") == "output_text":
                chunks.append(content.text)
    return "\n".join(chunks).strip()


def _load_job_filter_system_prompt() -> str:
    """
    Prefer the active DB prompt so the APPLY/REJECT filter can be tuned from Admin.
    Fall back to the legacy constant for fresh databases or unapplied migrations.
    """
    try:
        from core.models import PromptTemplate

        active = (
            PromptTemplate.objects
            .filter(purpose=PromptTemplate.Purpose.JOB_FILTER, is_active=True)
            .order_by("-updated_at", "-id")
            .first()
        )
        if active and safe_str(active.content).strip():
            return safe_str(active.content).strip()
    except Exception:
        pass

    return DEFAULT_JOB_FILTER_SYSTEM_PROMPT


def _build_user_prompt(title: str, description: str) -> str:
    return f"""
Job title:
{title or ""}

Job description:
{description or ""}
""".strip()


def _limit_reason_words(reason: str, max_words: int = 5) -> str:
    words = safe_str(reason).strip().split()
    return " ".join(words[:max_words])


def _parse_classifier_result(raw_result: str) -> dict:
    text = safe_str(raw_result).strip()
    decision = ""
    reason = ""

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            decision = safe_str(payload.get("decision")).strip().upper()
            reason = _limit_reason_words(payload.get("reason") or "")
    except Exception:
        pass

    upper = text.upper()
    if decision not in {"APPLY", "REJECT"}:
        if "REJECT" in upper:
            decision = "REJECT"
        elif "APPLY" in upper:
            decision = "APPLY"

    if decision not in {"APPLY", "REJECT"}:
        raise RuntimeError(f"Unexpected OpenAI classifier output: {text}")

    if not reason:
        reason = "api returned no reason"

    return {"decision": decision, "reason": _limit_reason_words(reason), "raw_output": text}


def classify_job_apply_or_reject_with_reason(
    title: str,
    description: str,
    model_name: str = DEFAULT_JOB_FILTER_MODEL,
) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing in .env")

    client = _get_openai_client(api_key)

    response = client.responses.create(
        model=model_name,
        input=[
            {"role": "system", "content": _load_job_filter_system_prompt()},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ],
        max_output_tokens=40,
    )

    return _parse_classifier_result(_extract_output_text(response))


def classify_job_apply_or_reject(title: str, description: str, model_name: str = DEFAULT_JOB_FILTER_MODEL) -> str:
    return classify_job_apply_or_reject_with_reason(title, description, model_name=model_name)["decision"]
