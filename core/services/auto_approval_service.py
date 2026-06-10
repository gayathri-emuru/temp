from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.models import ApprovalRecord, DailyBatch, JobPosting, JobRecruiterTarget, SentEmailLog
from core.utils import safe_str


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _already_real_sent(to_email: str) -> bool:
    to_email = safe_str(to_email).strip().lower()
    if not to_email:
        return False
    return SentEmailLog.objects.filter(
        to_email=to_email,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


def _safe_unsent_selected_recipient_count(job: JobPosting) -> int:
    count = 0
    for target in job.targets.all():
        email = safe_str(target.recipient_email_snapshot).strip().lower()
        if not target.is_selected_for_job:
            continue
        if not _has_real_email(email):
            continue
        if getattr(target.company_recruiter, "email_sent", False):
            continue
        if _already_real_sent(email):
            continue
        count += 1
    return count


def _safe_unsent_selected_recipient_count_from_targets(targets: list[JobRecruiterTarget]) -> int:
    count = 0
    for target in targets:
        email = safe_str(target.recipient_email_snapshot).strip().lower()
        if not target.is_selected_for_job:
            continue
        if not _has_real_email(email):
            continue
        if getattr(target.company_recruiter, "email_sent", False):
            continue
        if _already_real_sent(email):
            continue
        count += 1
    return count


def _has_generated_email(job: JobPosting) -> bool:
    try:
        generated = job.generated_email
    except Exception:
        return False
    return bool(safe_str(generated.subject).strip() and safe_str(generated.body).strip())


def _company_is_blocked(job: JobPosting) -> bool:
    return bool(job.company_ref_id and job.company_ref and getattr(job.company_ref, "is_blocked", False))


def _approval_reason(job: JobPosting) -> tuple[bool, str, int]:
    if _company_is_blocked(job):
        return False, "company_blocked", 0
    if not _has_generated_email(job):
        return False, "missing_generated_email", 0

    recipient_count = _safe_unsent_selected_recipient_count(job)
    if recipient_count <= 0:
        return False, "no_unsent_selected_recipients", 0

    return True, "safe_to_send", recipient_count


def _approval_reason_for_targets(job: JobPosting, targets: list[JobRecruiterTarget]) -> tuple[bool, str, int]:
    if _company_is_blocked(job):
        return False, "company_blocked", 0
    if not _has_generated_email(job):
        return False, "missing_generated_email", 0

    recipient_count = _safe_unsent_selected_recipient_count_from_targets(targets)
    if recipient_count <= 0:
        return False, "no_unsent_selected_recipients", 0

    return True, "safe_to_send", recipient_count


@transaction.atomic
def auto_approve_batch(batch: DailyBatch) -> dict:
    jobs = list(
        JobPosting.objects.filter(daily_batch=batch, is_manual_email_job=False)
        .select_related("company_ref", "generated_email", "approval_record")
        .order_by("company_ref__normalized_name", "id")
    )
    job_ids = [job.id for job in jobs]
    targets_by_job: dict[int, list[JobRecruiterTarget]] = {job_id: [] for job_id in job_ids}
    for start in range(0, len(job_ids), 200):
        chunk = job_ids[start : start + 200]
        for target in (
            JobRecruiterTarget.objects
            .filter(job_posting_id__in=chunk)
            .select_related("company_recruiter")
            .order_by("job_posting_id", "selection_order", "id")
        ):
            targets_by_job.setdefault(target.job_posting_id, []).append(target)

    summary = {
        "batch_date": batch.batch_date.isoformat(),
        "jobs_seen": len(jobs),
        "approved": 0,
        "unapproved": 0,
        "created_records": 0,
        "changed_records": 0,
        "safe_recipient_count": 0,
        "reasons": {},
        "approved_job_ids": [],
        "unapproved_job_ids": [],
    }

    now = timezone.now()
    for job in jobs:
        should_approve, reason, recipient_count = _approval_reason_for_targets(job, targets_by_job.get(job.id, []))
        summary["reasons"][reason] = int(summary["reasons"].get(reason, 0)) + 1

        approval, created = ApprovalRecord.objects.get_or_create(
            job_posting=job,
            defaults={
                "is_approved": should_approve,
                "approved_at": now if should_approve else None,
            },
        )
        if created:
            summary["created_records"] += 1

        changed = created
        if approval.is_approved != should_approve:
            approval.is_approved = should_approve
            changed = True
        if should_approve and approval.approved_at is None:
            approval.approved_at = now
            changed = True
        if not should_approve and approval.approved_at is not None:
            approval.approved_at = None
            changed = True

        if changed and not created:
            approval.save(update_fields=["is_approved", "approved_at", "updated_at"])
            summary["changed_records"] += 1
        elif created:
            summary["changed_records"] += 1

        if should_approve:
            summary["approved"] += 1
            summary["safe_recipient_count"] += recipient_count
            summary["approved_job_ids"].append(job.id)
            if job.status != JobPosting.Status.APPROVED:
                job.status = JobPosting.Status.APPROVED
                job.save(update_fields=["status", "updated_at"])
        else:
            summary["unapproved"] += 1
            summary["unapproved_job_ids"].append(job.id)
            if job.status == JobPosting.Status.APPROVED:
                job.status = JobPosting.Status.EMAIL_GENERATED if _has_generated_email(job) else JobPosting.Status.EMAIL_DISCOVERY_DONE
                job.save(update_fields=["status", "updated_at"])

    return summary


def latest_populated_batch() -> DailyBatch | None:
    return (
        DailyBatch.objects
        .filter(jobs__isnull=False)
        .distinct()
        .order_by("-batch_date", "-id")
        .first()
    )


def auto_approve_latest_batch() -> dict:
    batch = latest_populated_batch()
    if not batch:
        raise RuntimeError("No populated batch found.")
    return auto_approve_batch(batch)
