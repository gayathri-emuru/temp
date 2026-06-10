from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from core.models import JobPosting, SentEmailLog


DEFAULT_COMPANY_COOLDOWN_DAYS = 10


def _cutoff_date(batch_date, cooldown_days: int):
    cooldown_days = int(cooldown_days or 0)
    return batch_date - timedelta(days=cooldown_days)


def company_has_recent_job(*, canonical_company: str, batch_date, cooldown_days: int = DEFAULT_COMPANY_COOLDOWN_DAYS) -> bool:
    canonical_company = (canonical_company or "").strip()
    if not canonical_company:
        return False
    if int(cooldown_days or 0) <= 0:
        return False

    start_date = _cutoff_date(batch_date, cooldown_days)

    return JobPosting.objects.filter(
        canonical_company=canonical_company,
        daily_batch__batch_date__gte=start_date,
        daily_batch__batch_date__lt=batch_date + timedelta(days=1),
    ).exists()


def company_has_recent_real_send(
    *,
    canonical_company: str,
    cooldown_days: int = DEFAULT_COMPANY_COOLDOWN_DAYS,
) -> bool:
    canonical_company = (canonical_company or "").strip()
    if not canonical_company:
        return False
    if int(cooldown_days or 0) <= 0:
        return False

    now = timezone.now()
    cutoff = now - timedelta(days=int(cooldown_days or 0))

    return SentEmailLog.objects.filter(
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        sent_at__gte=cutoff,
        job_posting__canonical_company=canonical_company,
    ).exists()


def should_skip_new_job_for_company(
    *,
    canonical_company: str,
    batch_date,
    cooldown_days: int = DEFAULT_COMPANY_COOLDOWN_DAYS,
) -> tuple[bool, str]:
    """
    Returns (skip, reason).
    reason values:
      - recent_job
      - recent_real_send
      - none
    """
    if company_has_recent_real_send(canonical_company=canonical_company, cooldown_days=cooldown_days):
        return True, "recent_real_send"

    if company_has_recent_job(canonical_company=canonical_company, batch_date=batch_date, cooldown_days=cooldown_days):
        return True, "recent_job"

    return False, "none"
