from __future__ import annotations

from django.db.models import Count, Prefetch, Q, prefetch_related_objects
from django.urls import reverse

from core.models import (
    ApprovalRecord,
    DailyBatch,
    GeneratedEmail,
    JobPosting,
    JobRecruiterTarget,
    SentEmailLog,
    TargetedPeopleLookupRun,
)
from core.services.email_composition_service import build_full_email_body
from core.services.app_settings_service import get_max_people_per_company
from core.services.job_filter_review_service import pending_job_filter_review_rows
from core.utils import safe_str


SQLITE_SAFE_BATCH_SIZE = 300


def _admin_change_url(obj) -> str:
    if not obj or not getattr(obj, "pk", None):
        return ""
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])


def _admin_delete_url(obj) -> str:
    if not obj or not getattr(obj, "pk", None):
        return ""
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_delete", args=[obj.pk])


def _admin_add_url(model, query: str = "") -> str:
    meta = model._meta
    url = reverse(f"admin:{meta.app_label}_{meta.model_name}_add")
    if query:
        return f"{url}?{query}"
    return url


def _latest_populated_batch() -> DailyBatch | None:
    return (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0)
        .order_by("-batch_date", "-id")
        .first()
    )


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


def _selected_targets_for_job(job: JobPosting):
    prefetched = getattr(job, "prefetched_selected_targets", None)
    if prefetched is not None:
        return list(prefetched)[: get_max_people_per_company()]

    targets = list(
        job.targets.filter(
            is_selected_for_job=True,
            recipient_email_snapshot__isnull=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter")
        .order_by("selection_order", "id")
    )
    return targets[: get_max_people_per_company()]


def _fetched_people_for_company(company_id: int | None, exclude_emails: set[str], limit_runs: int = 5) -> list[dict]:
    """Return all distinct people Apollo has fetched for this company across recent lookup runs,
    excluding ones already shown as selected recipients. Used so the user can see WHO was found
    but not selected, with the reason (skipped, error, etc.)."""
    if not company_id:
        return []
    runs = (
        TargetedPeopleLookupRun.objects.filter(company_id=company_id)
        .order_by("-created_at", "-id")[:limit_runs]
    )
    seen_keys: set[str] = set()
    rows: list[dict] = []
    for run in runs:
        result_rows = run.result_rows or []
        if not isinstance(result_rows, list):
            continue
        for raw in result_rows:
            if not isinstance(raw, dict):
                continue
            name = safe_str(raw.get("name") or raw.get("apollo_name")).strip()
            email = safe_str(raw.get("email")).strip().lower()
            if not name and not email:
                continue
            if email and email in exclude_emails:
                continue
            key = email or f"name::{name.lower()}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "name": name or "(unknown)",
                    "title": safe_str(raw.get("title")).strip(),
                    "email": email,
                    "status": safe_str(raw.get("status")).strip(),
                    "reason": safe_str(raw.get("reason")).strip(),
                    "already_real_sent": _already_real_sent(email) if email else False,
                    "source": safe_str(raw.get("source")).strip(),
                    "linkedin_url": safe_str(raw.get("linkedin_url")).strip(),
                    "email_status": safe_str(raw.get("email_status")).strip(),
                    "lookup_run_id": run.id,
                    "lookup_created_at": run.created_at,
                }
            )
    return rows


def _iter_jobs_with_selected_targets(jobs_qs):
    target_prefetch = Prefetch(
        "targets",
        queryset=(
            JobRecruiterTarget.objects.filter(
                is_selected_for_job=True,
                recipient_email_snapshot__isnull=False,
            )
            .exclude(recipient_email_snapshot__in=["", "none"])
            .select_related("company_recruiter")
            .order_by("selection_order", "id")
        ),
        to_attr="prefetched_selected_targets",
    )

    offset = 0
    while True:
        jobs = list(jobs_qs[offset : offset + SQLITE_SAFE_BATCH_SIZE])
        if not jobs:
            break
        prefetch_related_objects(jobs, target_prefetch)
        for job in jobs:
            yield job
        offset += SQLITE_SAFE_BATCH_SIZE


def build_read_only_review_dashboard_context(*, batch_date: str = "") -> dict:
    requested_batch_date = safe_str(batch_date).strip()
    if batch_date:
        batch = DailyBatch.objects.filter(batch_date=requested_batch_date).order_by("-id").first()
    else:
        batch = _latest_populated_batch()

    if not batch:
        return {
            "batch": None,
            "selected_batch_date": requested_batch_date,
            "jobs": [],
            "job_filter_reviews": [],
            "totals": {"jobs": 0, "selected_recipients": 0, "ready_jobs": 0},
        }

    jobs_qs = (
        JobPosting.objects.filter(daily_batch=batch, is_manual_email_job=False)
        .select_related("company_ref", "generated_email", "approval_record")
        .order_by("company_ref__normalized_name", "id")
    )

    rows = []
    totals = {
        "jobs": 0,
        "selected_recipients": 0,
        "ready_jobs": 0,
        "missing_generated_email": 0,
        "missing_selected_recipients": 0,
    }

    for job in _iter_jobs_with_selected_targets(jobs_qs):
        totals["jobs"] += 1

        try:
            generated = job.generated_email
        except GeneratedEmail.DoesNotExist:
            generated = None

        try:
            approval = job.approval_record
        except ApprovalRecord.DoesNotExist:
            approval = None

        targets = _selected_targets_for_job(job)
        if not generated or not safe_str(generated.subject).strip() or not safe_str(generated.body).strip():
            totals["missing_generated_email"] += 1
        if not targets:
            totals["missing_selected_recipients"] += 1

        recipient_rows = []
        for target in targets:
            email = safe_str(target.recipient_email_snapshot).strip().lower()
            name = safe_str(target.recipient_name_snapshot).strip() or "there"
            final_body = ""
            if generated:
                final_body = build_full_email_body(
                    recipient_name=name,
                    base_body=safe_str(generated.body),
                    job_linkedin_url=safe_str(job.normalized_linkedin_url) or safe_str(job.linkedin_url),
                    manual_job_reference_id=safe_str(getattr(job, "manual_job_reference_id", "")).strip(),
                )

            recruiter = target.company_recruiter
            recipient_rows.append(
                {
                    "target": target,
                    "target_admin_url": _admin_change_url(target),
                    "target_delete_url": _admin_delete_url(target),
                    "recruiter_admin_url": _admin_change_url(recruiter),
                    "recruiter_delete_url": _admin_delete_url(recruiter),
                    "name": name,
                    "email": email,
                    "selection_order": target.selection_order,
                    "legacy": bool(
                        getattr(recruiter, "legacy", False)
                        or safe_str(getattr(recruiter, "source", "")).strip().lower() == "legacy"
                    ),
                    "source": safe_str(getattr(recruiter, "source", "")).strip().lower(),
                    "email_status": safe_str(getattr(recruiter, "email_status", "")).strip().lower(),
                    "title_match": bool(getattr(recruiter, "title_match", False)),
                    "location_match": bool(getattr(recruiter, "location_match", False)),
                    "apollo_title": safe_str(getattr(recruiter, "apollo_title", "")).strip(),
                    "apollo_linkedin_url": safe_str(getattr(recruiter, "apollo_linkedin_url", "")).strip(),
                    "already_real_sent": _already_real_sent(email),
                    "subject": safe_str(generated.subject).strip() if generated else "",
                    "final_body": final_body,
                }
            )

        ready = bool(generated and safe_str(generated.subject).strip() and safe_str(generated.body).strip() and recipient_rows)
        if ready:
            totals["ready_jobs"] += 1
        totals["selected_recipients"] += len(recipient_rows)

        preview_recipient = recipient_rows[0] if recipient_rows else None

        selected_emails = {safe_str(r.get("email")).strip().lower() for r in recipient_rows if r.get("email")}
        fetched_not_selected = _fetched_people_for_company(
            company_id=job.company_ref_id,
            exclude_emails=selected_emails,
        )

        rows.append(
            {
                "job": job,
                "job_admin_url": _admin_change_url(job),
                "company_admin_url": _admin_change_url(job.company_ref),
                "generated": generated,
                "generated_admin_url": _admin_change_url(generated)
                or _admin_add_url(GeneratedEmail, query=f"job_posting={job.id}"),
                "approval": approval,
                "approval_admin_url": _admin_change_url(approval)
                or _admin_add_url(ApprovalRecord, query=f"job_posting={job.id}"),
                "is_approved": bool(approval and approval.is_approved),
                "recipients": recipient_rows,
                "fetched_not_selected": fetched_not_selected,
                "preview_recipient_name": safe_str((preview_recipient or {}).get("name")).strip(),
                "preview_subject": safe_str((preview_recipient or {}).get("subject")).strip()
                or (safe_str(generated.subject).strip() if generated else ""),
                "preview_body": safe_str((preview_recipient or {}).get("final_body")).strip(),
                "ready": ready,
                "selected_count": len(recipient_rows),
                "max_targets": get_max_people_per_company(),
            }
        )

    return {
        "batch": batch,
        "selected_batch_date": batch.batch_date.isoformat(),
        "jobs": rows,
        "job_filter_reviews": pending_job_filter_review_rows(batch_date=batch.batch_date.isoformat()),
        "totals": totals,
    }
