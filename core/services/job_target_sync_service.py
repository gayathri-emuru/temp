from __future__ import annotations

from django.db import transaction

from core.models import Company, CompanyRecruiter, JobPosting, JobRecruiterTarget, SentEmailLog, TargetedPeopleLookupRun
from core.services.recruiter_title_guard_service import (
    is_data_science_manager_contact_title,
    is_fallback_business_contact_title,
    is_recruiting_or_hiring_contact_title,
    recruiter_contact_is_allowed,
)
from core.utils import safe_str


def _email_has_prior_real_initial_send(email: str) -> bool:
    """True if a real initial email has already been sent to this address (any company, any job).
    Used to prevent re-emailing the same person twice across jobs."""
    email = safe_str(email).strip().lower()
    if not email:
        return False
    return SentEmailLog.objects.filter(
        to_email=email,
        send_type=SentEmailLog.SendType.REAL,
        status=SentEmailLog.SendStatus.SENT,
        message_type=SentEmailLog.MessageType.INITIAL,
    ).exists()


DEFAULT_MAX_TARGETS_PER_JOB = 20


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _recruiter_priority_tuple(recruiter: CompanyRecruiter):
    status_rank = 0
    if not recruiter.email_sent and _has_real_email(recruiter.email):
        status_rank = 2
    elif _has_real_email(recruiter.email):
        status_rank = 1
    legacy_rank = 1 if _is_legacy_recruiter(recruiter) else 0
    apollo_rank = 1 if _is_usable_apollo_recruiter(recruiter) else 0
    primary_title_rank = 1 if is_data_science_manager_contact_title(safe_str(recruiter.apollo_title)) else 0
    recruiting_title_rank = 1 if is_recruiting_or_hiring_contact_title(safe_str(recruiter.apollo_title)) else 0
    fallback_title_rank = 1 if is_fallback_business_contact_title(safe_str(recruiter.apollo_title)) else 0
    legacy_recency_rank = recruiter.id if legacy_rank else 0
    return (
        legacy_rank,
        apollo_rank,
        primary_title_rank,
        recruiting_title_rank,
        fallback_title_rank,
        1 if getattr(recruiter, "title_match", False) else 0,
        1 if getattr(recruiter, "location_match", False) else 0,
        status_rank,
        legacy_recency_rank,
        len((recruiter.person_name or "").strip()),
        recruiter.normalized_person_name,
    )


def _is_legacy_recruiter(recruiter: CompanyRecruiter) -> bool:
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    return bool(getattr(recruiter, "legacy", False) or source == "legacy")


def _is_usable_apollo_recruiter(recruiter: CompanyRecruiter) -> bool:
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    email_status = safe_str(getattr(recruiter, "email_status", "")).strip().lower()
    return bool(
        (source == "apollo" or safe_str(getattr(recruiter, "apollo_person_id", "")).strip())
        and _has_real_email(recruiter.email)
        and email_status == "verified"
    )


def _recruiter_has_usable_email_for_targeting(recruiter: CompanyRecruiter) -> bool:
    # Block if this specific recruiter record has already been emailed.
    if recruiter.email_sent:
        return False
    # Block if no usable email is on file.
    if not _has_real_email(recruiter.email):
        return False
    # Block if this email address has been sent any prior real initial email
    # anywhere in the system (any company, any job). Prevents emailing the same
    # person twice across different jobs or companies.
    if _email_has_prior_real_initial_send(recruiter.email):
        return False
    return _is_legacy_recruiter(recruiter) or _is_usable_apollo_recruiter(recruiter)


def _is_allowed_primary_recruiter(recruiter: CompanyRecruiter) -> bool:
    return _recruiter_has_usable_email_for_targeting(recruiter)


def _is_allowed_fallback_recruiter(recruiter: CompanyRecruiter) -> bool:
    return _recruiter_has_usable_email_for_targeting(recruiter)


def _promote_lookup_history_fallback_contacts(company: Company, *, max_targets: int) -> int:
    promoted = 0
    seen_emails: set[str] = set()
    runs = TargetedPeopleLookupRun.objects.filter(company=company).order_by("-created_at", "-id")[:10]
    for run in runs:
        result_rows = run.result_rows or []
        if not isinstance(result_rows, list):
            continue
        for raw in result_rows:
            if promoted >= max_targets:
                return promoted
            if not isinstance(raw, dict):
                continue

            email = safe_str(raw.get("email")).strip().lower()
            title = safe_str(raw.get("title")).strip()
            if not _has_real_email(email) or email in seen_emails:
                continue
            seen_emails.add(email)
            if not (is_fallback_business_contact_title(title) or is_recruiting_or_hiring_contact_title(title)):
                continue

            apollo_person_id = safe_str(raw.get("apollo_person_id")).strip()
            recruiter = None
            if apollo_person_id:
                recruiter = CompanyRecruiter.objects.filter(
                    company=company,
                    apollo_person_id=apollo_person_id,
                ).order_by("id").first()
            if recruiter is None:
                recruiter = CompanyRecruiter.objects.filter(company=company, email__iexact=email).order_by("id").first()

            defaults = {
                "person_name": safe_str(raw.get("apollo_name") or raw.get("name")).strip() or email,
                "email": email,
                "source": CompanyRecruiter.Source.APOLLO,
                "email_status": safe_str(raw.get("email_status")).strip() or "verified",
                "apollo_title": title,
                "apollo_location": safe_str(raw.get("location")).strip(),
                "apollo_linkedin_url": safe_str(raw.get("linkedin_url")).strip(),
                "title_match": False,
                "location_match": False,
                "manually_targeted": True,
                "is_active": True,
            }
            if apollo_person_id:
                defaults["apollo_person_id"] = apollo_person_id

            if recruiter is None:
                recruiter = CompanyRecruiter(company=company, **defaults)
            else:
                for field, value in defaults.items():
                    if value not in ("", None):
                        setattr(recruiter, field, value)
            recruiter.save()
            promoted += 1
    return promoted


def _best_recruiters_for_company(
    company: Company,
    max_targets: int,
    *,
    allow_fallback_contacts: bool = False,
) -> list[CompanyRecruiter]:
    qs = company.recruiters.filter(is_active=True).order_by("normalized_person_name")
    recruiters = [r for r in qs if _is_allowed_primary_recruiter(r)]
    if allow_fallback_contacts and len(recruiters) < max_targets:
        _promote_lookup_history_fallback_contacts(company, max_targets=max_targets)
        qs = company.recruiters.filter(is_active=True).order_by("normalized_person_name")
        selected_ids = {r.id for r in recruiters}
        fallback_recruiters = [
            r
            for r in qs
            if r.id not in selected_ids and _is_allowed_fallback_recruiter(r)
        ]
        recruiters.extend(fallback_recruiters)
    recruiters.sort(key=_recruiter_priority_tuple, reverse=True)
    selected = recruiters[:max_targets]

    # max_targets controls how many people we try to discover from Apollo, but
    # once Apollo has already revealed an unsent email, selecting it avoids
    # leaving paid contacts stranded in the database.
    selected_ids = {r.id for r in selected}
    paid_revealed_overflow = [
        r
        for r in recruiters[max_targets:]
        if r.id not in selected_ids and _is_usable_apollo_recruiter(r)
    ]
    return selected + paid_revealed_overflow


@transaction.atomic
def sync_job_targets_for_job(
    *,
    job: JobPosting,
    max_targets: int | None = None,
    auto_select: bool = False,
    allow_fallback_contacts: bool = False,
) -> dict:
    if not job.company_ref_id or not job.company_ref:
        return {"targets_upserted": 0, "stale_targets_deleted": 0}

    if max_targets is None:
        from core.services.app_settings_service import get_max_people_per_company
        max_targets = get_max_people_per_company()
    max_targets = int(max_targets or DEFAULT_MAX_TARGETS_PER_JOB)
    if max_targets <= 0:
        max_targets = DEFAULT_MAX_TARGETS_PER_JOB

    selected_recruiters = _best_recruiters_for_company(
        job.company_ref,
        max_targets=max_targets,
        allow_fallback_contacts=allow_fallback_contacts,
    )
    recruiter_ids = [r.id for r in selected_recruiters]

    stale_targets_deleted = 0
    if recruiter_ids:
        deleted_count, _ = JobRecruiterTarget.objects.filter(
            job_posting=job,
            company_recruiter__company=job.company_ref,
        ).exclude(company_recruiter_id__in=recruiter_ids).delete()
        stale_targets_deleted += deleted_count
    else:
        deleted_count, _ = JobRecruiterTarget.objects.filter(
            job_posting=job,
            company_recruiter__company=job.company_ref,
        ).delete()
        stale_targets_deleted += deleted_count

    targets_upserted = 0
    for idx, recruiter in enumerate(selected_recruiters, start=1):
        if allow_fallback_contacts and not recruiter_contact_is_allowed(recruiter):
            recruiter.manually_targeted = True
            recruiter.save(update_fields=["manually_targeted", "updated_at"])

        defaults = {
            "recipient_email_snapshot": recruiter.email,
            "recipient_name_snapshot": recruiter.person_name,
            "selection_order": idx,
            "is_verified_for_job": _is_usable_apollo_recruiter(recruiter),
            "send_block_reason": "",
        }
        if auto_select:
            defaults["is_selected_for_job"] = True

        JobRecruiterTarget.objects.update_or_create(
            job_posting=job,
            company_recruiter=recruiter,
            defaults=defaults,
        )
        targets_upserted += 1

    return {"targets_upserted": targets_upserted, "stale_targets_deleted": stale_targets_deleted}


@transaction.atomic
def sync_job_targets_for_company_pending_jobs(
    *,
    company: Company,
    max_targets: int | None = None,
    auto_select: bool = False,
    allow_fallback_contacts: bool = False,
) -> dict:
    if max_targets is None:
        from core.services.app_settings_service import get_max_people_per_company
        max_targets = get_max_people_per_company()

    jobs = list(
        company.jobs.filter(is_manual_email_job=False).order_by("id")
    )
    totals = {"jobs_seen": 0, "targets_upserted": 0, "stale_targets_deleted": 0}

    for job in jobs:
        totals["jobs_seen"] += 1
        stats = sync_job_targets_for_job(
            job=job,
            max_targets=max_targets,
            auto_select=auto_select,
            allow_fallback_contacts=allow_fallback_contacts,
        )
        totals["targets_upserted"] += stats["targets_upserted"]
        totals["stale_targets_deleted"] += stats["stale_targets_deleted"]

    return totals
