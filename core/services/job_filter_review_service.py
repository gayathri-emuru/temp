from __future__ import annotations

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.models import ApprovalRecord, DailyBatch, JobFilterReview, JobPosting
from core.services.openai_filter_service import JOB_FILTER_PROMPT_NAME, classify_job_apply_or_reject_with_reason
from core.utils import safe_str


def _admin_change_url(obj) -> str:
    if not obj or not getattr(obj, "pk", None):
        return ""
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])


def _batch_for_date(batch_date: str) -> DailyBatch:
    batch = DailyBatch.objects.filter(batch_date=batch_date).order_by("-id").first()
    if not batch:
        raise RuntimeError(f"No batch found for {batch_date}.")
    return batch


def run_job_filter_review_for_batch(*, batch_date: str, limit: int | None = None) -> dict:
    batch = _batch_for_date(batch_date)
    qs = (
        JobPosting.objects
        .filter(daily_batch=batch, is_manual_email_job=False)
        .exclude(status__in=[JobPosting.Status.REAL_SENT, JobPosting.Status.BLOCKED])
        .order_by("company", "title", "id")
    )
    if limit:
        qs = qs[: int(limit)]

    result = {
        "batch_date": batch.batch_date.isoformat(),
        "jobs_seen": 0,
        "apply": 0,
        "reject": 0,
        "pending_reviews": 0,
        "errors": 0,
        "error_rows": [],
    }

    for job in qs:
        result["jobs_seen"] += 1
        try:
            decision = classify_job_apply_or_reject_with_reason(
                title=safe_str(job.title),
                description=safe_str(job.description),
            )
            decision_value = safe_str(decision.get("decision")).strip().upper()
            reason = safe_str(decision.get("reason")).strip()[:120]
            raw_output = safe_str(decision.get("raw_output")).strip()[:4000]

            if decision_value == JobFilterReview.Decision.REJECT:
                status = JobFilterReview.Status.PENDING
                result["reject"] += 1
                result["pending_reviews"] += 1
            else:
                status = JobFilterReview.Status.AUTO_KEEP
                result["apply"] += 1

            review, _ = JobFilterReview.objects.update_or_create(
                job_posting=job,
                defaults={
                    "daily_batch": batch,
                    "decision": decision_value,
                    "reason": reason,
                    "raw_output": raw_output,
                    "prompt_name": JOB_FILTER_PROMPT_NAME,
                    "status": status,
                    "reviewed_at": None,
                    "review_notes": "",
                },
            )
            review.save()
        except Exception as exc:
            result["errors"] += 1
            result["error_rows"].append(
                {
                    "job_id": job.id,
                    "company": safe_str(job.company),
                    "title": safe_str(job.title),
                    "error": str(exc)[:400],
                }
            )

    return result


def pending_job_filter_review_rows(*, batch_date: str) -> list[dict]:
    batch = _batch_for_date(batch_date)
    reviews = (
        JobFilterReview.objects
        .filter(
            daily_batch=batch,
            job_posting__is_manual_email_job=False,
            decision=JobFilterReview.Decision.REJECT,
            status=JobFilterReview.Status.PENDING,
        )
        .select_related("job_posting", "job_posting__company_ref", "daily_batch")
        .order_by("job_posting__company", "job_posting__title", "id")
    )

    rows = []
    for review in reviews:
        job = review.job_posting
        company = job.company_ref
        rows.append(
            {
                "review": review,
                "job": job,
                "company": safe_str(getattr(company, "normalized_name", "")).strip() or safe_str(job.company).strip(),
                "reason": safe_str(review.reason).strip(),
                "linkedin_url": safe_str(job.normalized_linkedin_url).strip() or safe_str(job.linkedin_url).strip(),
                "job_admin_url": _admin_change_url(job),
                "company_admin_url": _admin_change_url(company),
                "review_admin_url": _admin_change_url(review),
            }
        )
    return rows


@transaction.atomic
def accept_job_filter_reviews(review_ids: list[int]) -> dict:
    reviews = list(
        JobFilterReview.objects
        .select_for_update()
        .select_related("job_posting")
        .filter(
            id__in=review_ids,
            decision=JobFilterReview.Decision.REJECT,
            status=JobFilterReview.Status.PENDING,
        )
        .order_by("id")
    )

    result = {"requested": len(review_ids), "accepted": 0, "jobs_blocked": 0}
    now = timezone.now()
    for review in reviews:
        job = review.job_posting
        approval, _ = ApprovalRecord.objects.get_or_create(job_posting=job)
        approval.is_approved = False
        approval.approved_at = None
        approval.review_notes = (
            f"Blocked by job filter review: {safe_str(review.reason).strip()}"
        )[:4000]
        approval.save(update_fields=["is_approved", "approved_at", "review_notes", "updated_at"])

        if job.status != JobPosting.Status.BLOCKED:
            job.status = JobPosting.Status.BLOCKED
            job.save(update_fields=["status", "updated_at"])
            result["jobs_blocked"] += 1

        review.status = JobFilterReview.Status.ACCEPTED
        review.reviewed_at = now
        review.review_notes = "Accepted from read-only review dashboard."
        review.save(update_fields=["status", "reviewed_at", "review_notes", "updated_at"])
        result["accepted"] += 1

    return result


@transaction.atomic
def dismiss_job_filter_reviews(review_ids: list[int]) -> dict:
    reviews = list(
        JobFilterReview.objects
        .select_for_update()
        .filter(id__in=review_ids, status=JobFilterReview.Status.PENDING)
        .order_by("id")
    )
    now = timezone.now()
    for review in reviews:
        review.status = JobFilterReview.Status.DISMISSED
        review.reviewed_at = now
        review.review_notes = "Dismissed from read-only review dashboard."
        review.save(update_fields=["status", "reviewed_at", "review_notes", "updated_at"])
    return {"requested": len(review_ids), "dismissed": len(reviews)}
