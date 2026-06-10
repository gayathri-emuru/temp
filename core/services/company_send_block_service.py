from __future__ import annotations

from django.utils import timezone

from core.models import Company, JobPosting
from core.utils import safe_str


def company_is_send_blocked(job: JobPosting) -> bool:
    company_ref = getattr(job, "company_ref", None)
    if getattr(job, "company_ref_id", None) and company_ref:
        return bool(getattr(company_ref, "is_blocked", False))

    normalized = safe_str(getattr(job, "normalized_company", "")).strip()
    if normalized:
        return Company.objects.filter(normalized_name__iexact=normalized, is_blocked=True).exists()

    company_name = safe_str(getattr(job, "company", "")).strip()
    if company_name:
        return Company.objects.filter(raw_name_latest__iexact=company_name, is_blocked=True).exists()

    return False


def set_company_send_block(*, company: Company, blocked: bool, reason: str = "") -> Company:
    company.is_blocked = bool(blocked)
    reason_text = safe_str(reason).strip()
    if blocked:
        timestamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
        note = f"[{timestamp}] Send Control: stopped company sends"
        if reason_text:
            note = f"{note} - {reason_text}"
        existing = safe_str(company.notes).strip()
        if note not in existing:
            company.notes = f"{existing}\n{note}".strip()
        company.save(update_fields=["is_blocked", "notes", "updated_at"])
    else:
        company.save(update_fields=["is_blocked", "updated_at"])
    return company
