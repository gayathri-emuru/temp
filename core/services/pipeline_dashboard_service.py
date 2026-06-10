from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from core.models import ApprovalRecord, BlacklistedCompany, Company, CompanyRecruiter, DailyBatch, GeneratedEmail, JobPosting, JobRecruiterTarget, SentEmailLog, TargetedPeopleLookupRun
from core.services.apollo_recruiter_fetch_service import fetch_apollo_credits_info, get_apify_person_lead, job_has_apify_person_lead
from core.services.app_settings_service import get_apollo_dashboard_credits_used, get_company_cooldown_days, get_max_people_per_company
from core.services.company_domain_service import is_usable_company_domain, normalize_domain_value
from core.services.email_sending_control_service import get_email_sending_state
from core.services.normalization_service import normalize_person_name
from core.services.targeted_people_lookup_service import build_people_search_links
from core.utils import safe_str


_CREDIT_SUMMARY_RE = re.compile(
    r"CREDIT_SUMMARY company=(?P<company>.*?) credits=(?P<credits>\d+) "
    r"emails_found=(?P<emails>\d+) not_converted=(?P<not_converted>\d+) "
    r"(?:verified=(?P<verified>\d+) unverified=(?P<unverified>\d+) "
    r"email_status_counts=(?P<email_status_counts>.*?) )?"
    r"accepted_alternate_domain=(?P<alternate>\d+) accepted_non_us=(?P<non_us>\d+) "
    r"(?:accepted_paid_nonmatching_title=\d+ accepted_last_resort_title=\d+ )?"
    r"skip_reasons=(?P<skip_reasons>.*)$"
)


def company_needs_apollo_topup(company: Company, max_people: int) -> bool:
    if not company or company.is_blocked or not is_usable_company_domain(company.active_domain):
        return False

    try:
        cap = max(1, int(max_people or 1))
    except Exception:
        cap = 1

    cooldown_days = get_company_cooldown_days()
    prior_real_initial_sends = 0
    normalized = safe_str(getattr(company, "normalized_name", "")).strip()
    if cooldown_days > 0 and normalized:
        prior_real_initial_sends = SentEmailLog.objects.filter(
            job_posting__company_ref__normalized_name=normalized,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
            sent_at__gte=timezone.now() - timedelta(days=cooldown_days),
        ).count()

    remaining_send_capacity = max(0, cap - prior_real_initial_sends)
    if remaining_send_capacity <= 0:
        return False

    stored_people_count = (
        CompanyRecruiter.objects
        .filter(company=company, is_active=True, email_sent=False)
        .exclude(email__in=["", "none"])
        .filter(
            Q(legacy=True)
            | Q(source=CompanyRecruiter.Source.LEGACY)
            | Q(source=CompanyRecruiter.Source.APOLLO, email_status__iexact="verified")
            | (Q(apollo_person_id__isnull=False) & ~Q(apollo_person_id="") & Q(email_status__iexact="verified"))
        )
        .count()
    )
    existing_allowed_count = min(cap, stored_people_count)
    return existing_allowed_count < remaining_send_capacity


def _parse_credit_waste_line(line: str) -> dict:
    if "CREDIT_WASTE " not in line:
        return {}
    _, payload = line.split("CREDIT_WASTE ", 1)
    values = {}
    keys = ["company", "reason", "person", "email", "status", "apollo_id", "title", "note"]
    for index, key in enumerate(keys):
        marker = f"{key}="
        start = payload.find(marker)
        if start < 0:
            continue
        start += len(marker)
        next_positions = [payload.find(f" {next_key}=", start) for next_key in keys[index + 1 :]]
        next_positions = [pos for pos in next_positions if pos >= 0]
        end = min(next_positions) if next_positions else len(payload)
        value = payload[start:end].strip()
        values[key] = "" if value == "[NONE]" else value
    return {
        "company": values.get("company", ""),
        "reason": values.get("reason", ""),
        "person": values.get("person", ""),
        "email": values.get("email", ""),
        "email_status": values.get("status", ""),
        "apollo_person_id": values.get("apollo_id", ""),
        "title": values.get("title", ""),
        "note": values.get("note", ""),
    }


def _today_apollo_log_report() -> dict:
    today_token = timezone.localdate().strftime("%Y%m%d")
    log_dir = Path(settings.BASE_DIR) / "media" / "run_logs"
    rows = []
    wasted_people = []
    totals = {
        "companies": 0,
        "credits": 0,
        "emails": 0,
        "verified": 0,
        "unverified": 0,
        "not_converted": 0,
        "alternate_domain": 0,
        "non_us": 0,
        "email_status_counts": {},
    }
    if not log_dir.exists():
        return {"date": timezone.localdate(), "totals": totals, "rows": rows, "wasted_credit_people": wasted_people}

    for path in sorted(log_dir.glob(f"pipeline_recruiter_topup_*_{today_token}_*.log")):
        summary = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            waste = _parse_credit_waste_line(line)
            if waste:
                waste["run_log_path"] = str(path)
                wasted_people.append(waste)
            match = _CREDIT_SUMMARY_RE.search(line)
            if match:
                try:
                    skip_reasons = ast.literal_eval(match.group("skip_reasons"))
                except Exception:
                    skip_reasons = match.group("skip_reasons")
                try:
                    email_status_counts = ast.literal_eval(match.group("email_status_counts") or "{}")
                except Exception:
                    email_status_counts = {}
                summary = {
                    "company": match.group("company"),
                    "credits": int(match.group("credits")),
                    "emails": int(match.group("emails")),
                    "verified": int(match.group("verified") or match.group("emails") or 0),
                    "unverified": int(match.group("unverified") or 0),
                    "not_converted": int(match.group("not_converted")),
                    "email_status_counts": email_status_counts if isinstance(email_status_counts, dict) else {},
                    "alternate_domain": int(match.group("alternate")),
                    "non_us": int(match.group("non_us")),
                    "skip_reasons": skip_reasons,
                    "run_log_path": str(path),
                }
        if summary:
            rows.append(summary)
            totals["companies"] += 1
            totals["credits"] += summary["credits"]
            totals["emails"] += summary["emails"]
            totals["verified"] += summary["verified"]
            totals["unverified"] += summary["unverified"]
            totals["not_converted"] += summary["not_converted"]
            totals["alternate_domain"] += summary["alternate_domain"]
            totals["non_us"] += summary["non_us"]
            if isinstance(summary.get("email_status_counts"), dict):
                for key, value in summary["email_status_counts"].items():
                    totals["email_status_counts"][key] = int(totals["email_status_counts"].get(key, 0) or 0) + int(value or 0)
    return {
        "date": timezone.localdate(),
        "totals": totals,
        "rows": rows,
        "waste_summary_rows": [row for row in rows if int(row.get("not_converted") or 0) > 0],
        "wasted_credit_people": wasted_people,
    }


def _available_batch_queryset():
    return (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0)
        .order_by("-batch_date", "-id")
    )


def _latest_populated_batch() -> DailyBatch | None:
    return _available_batch_queryset().first()


def _populated_batch_for_date(batch_date: str = "") -> DailyBatch | None:
    text = safe_str(batch_date).strip()
    if not text:
        return _latest_populated_batch()
    try:
        selected = datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return _latest_populated_batch()
    return (
        DailyBatch.objects.annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False)))
        .filter(job_count__gt=0, batch_date=selected)
        .order_by("-id")
        .first()
        or _latest_populated_batch()
    )


def _available_batch_rows(limit: int | None = None) -> list[dict]:
    qs = _available_batch_queryset()
    if limit:
        qs = qs[:limit]
    return [{"batch": batch, "job_count": int(getattr(batch, "job_count", 0) or 0)} for batch in qs]


def _manual_job_id_rows(batch: DailyBatch) -> list[dict]:
    jobs = (
        JobPosting.objects.filter(
            daily_batch=batch,
            is_manual_import=True,
            is_manual_email_job=False,
            source_platform__in=[JobPosting.SourcePlatform.LINKEDIN, JobPosting.SourcePlatform.DICE],
        )
        .select_related("company_ref")
        .order_by("source_platform", "company_ref__normalized_name", "id")
    )
    reference_counts: dict[str, int] = {}
    for value in jobs.exclude(manual_job_reference_id="").values_list("manual_job_reference_id", flat=True):
        key = safe_str(value).strip()
        if key:
            reference_counts[key] = int(reference_counts.get(key) or 0) + 1

    rows = []
    for job in jobs:
        manual_reference = safe_str(getattr(job, "manual_job_reference_id", "")).strip()
        rows.append(
            {
                "job": job,
                "job_id": int(job.id),
                "source_platform": safe_str(job.source_platform).strip(),
                "company": safe_str(job.company).strip(),
                "normalized_company": safe_str(getattr(job.company_ref, "normalized_name", "")).strip()
                or safe_str(job.normalized_company).strip(),
                "title": safe_str(job.title).strip(),
                "location": safe_str(job.location).strip(),
                "linkedin_url": safe_str(job.normalized_linkedin_url).strip() or safe_str(job.linkedin_url).strip(),
                "apply_url": safe_str(job.normalized_apply_url).strip() or safe_str(job.apply_url).strip(),
                "manual_job_reference_id": manual_reference,
                "has_manual_job_reference_id": bool(manual_reference),
                "manual_job_reference_duplicate": bool(manual_reference and int(reference_counts.get(manual_reference) or 0) > 1),
            }
        )
    return rows


def _has_real_email(value: str) -> bool:
    value = safe_str(value).strip().lower()
    return bool(value and value != "none")


def _is_legacy_recruiter(recruiter: CompanyRecruiter) -> bool:
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    return bool(getattr(recruiter, "legacy", False) or source == CompanyRecruiter.Source.LEGACY)


def _is_verified_apollo_recruiter(recruiter: CompanyRecruiter) -> bool:
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    apollo_id = safe_str(getattr(recruiter, "apollo_person_id", "")).strip()
    email_status = safe_str(getattr(recruiter, "email_status", "")).strip().lower()
    return bool((source == CompanyRecruiter.Source.APOLLO or apollo_id) and email_status == "verified")


def _target_allows_real_send(target: JobRecruiterTarget) -> bool:
    recruiter = getattr(target, "company_recruiter", None)
    if not recruiter:
        return False
    return _is_legacy_recruiter(recruiter) or _is_verified_apollo_recruiter(recruiter)


def _linkedin_company_about_url(job: JobPosting) -> str:
    slug = safe_str(getattr(job, "apify_linkedin_org_slug", "")).strip().strip("/")
    if slug:
        return f"https://www.linkedin.com/company/{slug}/about/"

    for value in [getattr(job, "apify_linkedin_org_url", ""), getattr(job, "company_linkedin", "")]:
        url = safe_str(value).strip()
        if "linkedin.com/company/" not in url.lower():
            continue
        base = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if not base.endswith("/about"):
            base = f"{base}/about"
        return f"{base}/"

    return ""


def _company_rows_for_latest_batch(batch: DailyBatch, max_per_company: int = 20) -> list[dict]:
    jobs = (
        JobPosting.objects.filter(daily_batch=batch, company_ref__isnull=False, is_manual_email_job=False)
        .select_related("company_ref")
        .order_by("company_ref__normalized_name", "id")
    )

    by_company: dict[int, dict] = {}
    for job in jobs:
        company = job.company_ref
        if company.id not in by_company:
            by_company[company.id] = {
                "company": company,
                "normalized_name": company.normalized_name,
                "raw_name_latest": company.raw_name_latest,
                "domain": normalize_domain_value(company.active_domain),
                "domain_usable": is_usable_company_domain(company.active_domain),
                "is_blocked": bool(company.is_blocked),
                "job_count": 0,
                "pending_job_count": 0,
                "sample_job_id": job.id,
                "sample_job_title": job.title,
                "sample_job_url": job.linkedin_url,
                "sample_company_url": _linkedin_company_about_url(job),
                "sample_location": job.location,
                "sample_people_search_urls": job.linkedin_people_search_urls if isinstance(job.linkedin_people_search_urls, dict) else {},
                "apify_person_lead_count": 0,
                "pending_apify_person_lead_count": 0,
                "apify_person_name": "",
                "apify_person_title": "",
                "apify_person_email": "",
            }

        row = by_company[company.id]
        row["job_count"] += 1
        if job_has_apify_person_lead(job):
            row["apify_person_lead_count"] += 1
            if not row["apify_person_name"]:
                lead = get_apify_person_lead(job)
                lead_name = safe_str(lead.get("name")).strip()
                lead_norm = normalize_person_name(lead_name)
                row["apify_person_name"] = lead_name
                row["apify_person_title"] = safe_str(lead.get("title")).strip()
                if lead_norm:
                    target = (
                        job.targets.select_related("company_recruiter")
                        .filter(company_recruiter__normalized_person_name=lead_norm)
                        .exclude(recipient_email_snapshot__in=["", "none"])
                        .order_by("selection_order", "id")
                        .first()
                    )
                    if target:
                        row["apify_person_email"] = safe_str(target.recipient_email_snapshot).strip()
        if job.status == JobPosting.Status.RECRUITERS_PENDING:
            row["pending_job_count"] += 1
            if job_has_apify_person_lead(job):
                row["pending_apify_person_lead_count"] += 1

    company_ids = list(by_company.keys())
    if not company_ids:
        return []

    blacklisted_company_ids = set(
        BlacklistedCompany.objects.filter(company_id__in=company_ids).values_list("company_id", flat=True)
    )
    blacklisted_names = set(
        BlacklistedCompany.objects.filter(
            Q(company_id__in=company_ids)
            | Q(normalized_name__in=[row["normalized_name"] for row in by_company.values()])
        ).values_list("normalized_name", flat=True)
    )
    allowed_contacts_by_company: dict[int, list[CompanyRecruiter]] = {company_id: [] for company_id in company_ids}
    recruiters_by_company: dict[int, list[CompanyRecruiter]] = {company_id: [] for company_id in company_ids}
    for recruiter in (
        CompanyRecruiter.objects.filter(company_id__in=company_ids, is_active=True, email_sent=False)
        .exclude(email__in=["", "none"])
        .order_by("company_id", "-title_match", "-location_match", "normalized_person_name", "id")
    ):
        if not (_is_legacy_recruiter(recruiter) or _is_verified_apollo_recruiter(recruiter)):
            continue
        recruiters_by_company.setdefault(recruiter.company_id, []).append(recruiter)

    for company_id, recruiters in recruiters_by_company.items():
        allowed_contacts_by_company[company_id] = recruiters[:max_per_company]

    legacy_counts: dict[int, int] = {}
    apollo_email_counts: dict[int, int] = {}
    for company_id, contacts in allowed_contacts_by_company.items():
        legacy_counts[company_id] = sum(
            1
            for recruiter in contacts
            if _is_legacy_recruiter(recruiter)
        )
        apollo_email_counts[company_id] = sum(
            1
            for recruiter in contacts
            if _is_verified_apollo_recruiter(recruiter)
        )

    target_counts: dict[int, int] = {}
    for target in (
        JobRecruiterTarget.objects.filter(
            job_posting__daily_batch=batch,
            job_posting__company_ref_id__in=company_ids,
            job_posting__is_manual_email_job=False,
            company_recruiter__email_sent=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter", "job_posting")
        .order_by("job_posting__company_ref_id", "id")
    ):
        if not _target_allows_real_send(target):
            continue
        company_id = target.job_posting.company_ref_id
        target_counts[company_id] = int(target_counts.get(company_id, 0) or 0) + 1

    cooldown_days = get_company_cooldown_days()
    sent_counts = {}
    if cooldown_days > 0:
        sent_qs = SentEmailLog.objects.filter(
            job_posting__company_ref_id__in=company_ids,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
            sent_at__gte=timezone.now() - timedelta(days=cooldown_days),
        )
        sent_counts = dict(
            sent_qs
            .values("job_posting__company_ref_id")
            .annotate(count=Count("id"))
            .values_list("job_posting__company_ref_id", "count")
        )

    rows = []
    for company_id, row in by_company.items():
        is_blacklisted = bool(company_id in blacklisted_company_ids or row["normalized_name"] in blacklisted_names)
        legacy_count = int(legacy_counts.get(company_id, 0) or 0)
        apollo_email_count = int(apollo_email_counts.get(company_id, 0) or 0)
        allowed_count = legacy_count + apollo_email_count
        sent_count = int(sent_counts.get(company_id, 0) or 0)
        remaining_send_capacity = max(0, max_per_company - sent_count)
        target_count = int(target_counts.get(company_id, 0) or 0)
        apollo_slots = max(0, min(max_per_company - allowed_count, remaining_send_capacity - allowed_count))
        company_obj = row["company"]
        has_batch_job = bool(row["job_count"])
        can_run_apollo_topup = bool(
            has_batch_job
            and row["domain_usable"]
            and apollo_slots > 0
            and not row["is_blocked"]
            and not is_blacklisted
        )
        row.update(
            {
                "legacy_count": legacy_count,
                "verified_apollo_count": apollo_email_count,
                "apollo_email_count": apollo_email_count,
                "allowed_recruiter_count": allowed_count,
                "target_count": target_count,
                "sent_count": sent_count,
                "remaining_send_capacity": remaining_send_capacity,
                "is_blacklisted": is_blacklisted,
                "apollo_slots_needed": apollo_slots if can_run_apollo_topup else 0,
                "apollo_blocked_reason": "" if row["domain_usable"] else "missing_domain",
                "ready_for_recruiter_fill": can_run_apollo_topup,
                "has_enough_recruiters": allowed_count >= remaining_send_capacity,
                "last_apollo_emails_found": int(getattr(company_obj, "last_apollo_emails_found", 0) or 0),
                "last_apollo_verified_emails_found": int(getattr(company_obj, "last_apollo_verified_emails_found", 0) or 0),
                "last_apollo_unverified_emails_found": int(getattr(company_obj, "last_apollo_unverified_emails_found", 0) or 0),
                "last_apollo_email_status_counts": getattr(company_obj, "last_apollo_email_status_counts", {}) or {},
                "last_apollo_credits_consumed": int(getattr(company_obj, "last_apollo_credits_consumed", 0) or 0),
                "last_apollo_run_at": getattr(company_obj, "last_apollo_run_at", None),
            }
        )
        if is_blacklisted or row["is_blocked"]:
            source_plan = "Blacklisted"
            next_action = "Rejected from future imports"
            gap_reason = "Company is blocked to avoid future Apify work."
        elif not row["domain_usable"]:
            source_plan = "Needs domain"
            next_action = "Add domain"
            gap_reason = "No Apollo lookup until domain is filled."
        elif row["pending_apify_person_lead_count"]:
            source_plan = "Exact job poster"
            if target_count and apollo_slots > 0 and can_run_apollo_topup:
                next_action = "Run Apollo top-up"
                gap_reason = f"Job poster is selected; needs up to {apollo_slots} more usable email(s)."
            elif target_count:
                next_action = "Ready for email draft"
                gap_reason = ""
            else:
                next_action = "Run exact Apollo lookup, then top-up"
                gap_reason = f"Job poster is tried first; if no email, use normal {max_per_company}-person top-up."
        elif target_count >= max_per_company:
            source_plan = "Legacy/Apollo"
            next_action = "Ready for email draft"
            gap_reason = ""
        elif target_count > 0 and apollo_slots > 0 and can_run_apollo_topup:
            source_plan = "Partial filled"
            next_action = "Run Apollo top-up"
            gap_reason = f"Only {target_count} usable recipient(s) found; needs up to {apollo_slots} more."
        elif target_count > 0:
            source_plan = "Partial filled"
            next_action = "Ready for email draft"
            gap_reason = f"Only {target_count} usable recipient(s) found."
        elif row["pending_job_count"] and apollo_slots > 0:
            source_plan = "Apollo top-up"
            next_action = "Run Apollo top-up"
            gap_reason = f"Needs up to {apollo_slots} usable email(s)."
        else:
            source_plan = "No action queued"
            next_action = "Review only if needed"
            gap_reason = "No pending recruiter-fill job is queued."

        row.update(
            {
                "source_plan": source_plan,
                "usable_recipient_count": target_count,
                "next_action": next_action,
                "gap_reason": gap_reason,
            }
        )
        rows.append(row)

    action_priority = {
        "Run Apollo top-up": 0,
        "Run exact Apollo lookup, then top-up": 1,
        "Add domain": 2,
        "Ready for email draft": 3,
        "Review only if needed": 4,
        "Rejected from future imports": 5,
    }
    rows.sort(
        key=lambda x: (
            int(x.get("usable_recipient_count") or 0),
            action_priority.get(x.get("next_action"), 9),
            x["normalized_name"],
        )
    )
    return rows


def _batch_job_metrics(batch: DailyBatch) -> dict:
    jobs = JobPosting.objects.filter(daily_batch=batch, is_manual_email_job=False)
    total = jobs.count()
    status_counts = dict(jobs.values("status").annotate(count=Count("id")).values_list("status", "count"))

    generated = GeneratedEmail.objects.filter(job_posting__daily_batch=batch, job_posting__is_manual_email_job=False).exclude(subject="").exclude(body="").count()
    approved = ApprovalRecord.objects.filter(job_posting__daily_batch=batch, job_posting__is_manual_email_job=False, is_approved=True).count()
    job_ids = list(jobs.values_list("id", flat=True))
    allowed_target_job_ids: set[int] = set()
    selected_allowed_target_job_ids: set[int] = set()
    for target in (
        JobRecruiterTarget.objects.filter(
            job_posting_id__in=job_ids,
            company_recruiter__email_sent=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter")
    ):
        if not _target_allows_real_send(target):
            continue
        allowed_target_job_ids.add(target.job_posting_id)
        if target.is_selected_for_job:
            selected_allowed_target_job_ids.add(target.job_posting_id)

    with_targets = len(allowed_target_job_ids)
    ready_to_generate = jobs.filter(id__in=allowed_target_job_ids).exclude(generated_email__isnull=False).distinct().count()
    ready_to_review = jobs.filter(generated_email__isnull=False).exclude(generated_email__subject="").exclude(
        generated_email__body=""
    ).count()
    ready_to_send = (
        jobs.filter(
            id__in=selected_allowed_target_job_ids,
            approval_record__is_approved=True,
            generated_email__isnull=False,
        )
        .exclude(generated_email__subject="")
        .exclude(generated_email__body="")
        .distinct()
        .count()
    )

    return {
        "total_jobs": total,
        "status_counts": status_counts,
        "generated_emails": generated,
        "approved_jobs": approved,
        "jobs_with_targets": with_targets,
        "ready_to_generate": ready_to_generate,
        "ready_to_review": ready_to_review,
        "ready_to_send": ready_to_send,
    }


def _targeted_lookup_rows(company_rows: list[dict], *, batch: DailyBatch | None = None) -> list[dict]:
    company_ids = [row["company"].id for row in company_rows if row.get("company")]
    latest_runs = {}
    fetched_people_by_company: dict[int, list[dict]] = {company_id: [] for company_id in company_ids}
    if company_ids:
        lookup_qs = TargetedPeopleLookupRun.objects.filter(company_id__in=company_ids)
        if batch:
            lookup_qs = lookup_qs.filter(job_posting__daily_batch=batch)
        for run in lookup_qs.select_related("company", "job_posting").order_by("company_id", "-created_at", "-id"):
            latest_runs.setdefault(run.company_id, run)
        for recruiter in (
            CompanyRecruiter.objects.filter(company_id__in=company_ids, is_active=True, email_sent=False)
            .exclude(email__in=["", "none"])
            .order_by("company_id", "source", "person_name", "id")
        ):
            fetched_people_by_company.setdefault(recruiter.company_id, []).append(
                {
                    "name": safe_str(recruiter.person_name).strip(),
                    "email": safe_str(recruiter.email).strip(),
                    "source": safe_str(recruiter.source).strip() or "stored",
                    "email_status": safe_str(recruiter.email_status).strip(),
                    "title": safe_str(recruiter.apollo_title).strip(),
                    "location": safe_str(recruiter.apollo_location).strip(),
                    "linkedin_url": safe_str(recruiter.apollo_linkedin_url).strip(),
                    "apollo_person_id": safe_str(recruiter.apollo_person_id).strip(),
                }
            )

    rows = []
    for row in company_rows:
        if row.get("is_blacklisted") or row.get("is_blocked"):
            continue
        if not row.get("domain_usable"):
            continue
        company = row["company"]
        open_slots = max(0, int(row.get("apollo_slots_needed") or 0))
        needs_attention = bool(open_slots or int(row.get("usable_recipient_count") or 0) == 0)
        rows.append(
            {
                "company": company,
                "company_id": company.id,
                "job_id": row.get("sample_job_id"),
                "normalized_name": row.get("normalized_name"),
                "domain": row.get("domain"),
                "sample_job_title": row.get("sample_job_title"),
                "sample_job_url": row.get("sample_job_url"),
                "sample_location": row.get("sample_location"),
                "usable_recipient_count": int(row.get("usable_recipient_count") or 0),
                "open_slots": open_slots,
                "needs_attention": needs_attention,
                "search_links": build_people_search_links(
                    company_name=row.get("normalized_name") or company.normalized_name,
                    company_url=row.get("sample_company_url") or "",
                    stored_urls=row.get("sample_people_search_urls") or {},
                ),
                "fetched_people": fetched_people_by_company.get(company.id, [])[:20],
                "latest_lookup_run": latest_runs.get(company.id),
            }
        )

    rows.sort(key=lambda item: (0 if item["needs_attention"] else 1, item["normalized_name"]))
    return rows


def build_company_regex_search_context(pattern: str = "", *, limit: int = 200) -> dict:
    pattern = safe_str(pattern).strip()
    context = {
        "pattern": pattern,
        "rows": [],
        "total_matches": 0,
        "displayed_matches": 0,
        "sent_matches": 0,
        "approved_matches": 0,
        "generated_matches": 0,
        "limit": int(limit or 200),
        "error": "",
    }
    if not pattern:
        return context

    try:
        regex = re.compile(pattern, flags=re.I)
    except re.error as exc:
        context["error"] = f"Invalid regex: {exc}"
        return context

    def _chunks(values: list[int], size: int = 800):
        for start in range(0, len(values), size):
            yield values[start : start + size]

    jobs = (
        JobPosting.objects.filter(is_manual_email_job=False)
        .select_related("daily_batch", "company_ref")
        .order_by("-daily_batch__batch_date", "-id")
    )

    matched_ids: list[int] = []
    displayed_ids: list[int] = []
    for job in jobs.iterator(chunk_size=500):
        company_values = [
            safe_str(job.company),
            safe_str(job.normalized_company),
            safe_str(getattr(job.company_ref, "raw_name_latest", "")),
            safe_str(getattr(job.company_ref, "normalized_name", "")),
        ]
        if not any(regex.search(value) for value in company_values if value):
            continue
        matched_ids.append(int(job.id))
        if len(displayed_ids) < context["limit"]:
            displayed_ids.append(int(job.id))

    sent_job_ids: set[int] = set()
    approved_job_ids: set[int] = set()
    generated_job_ids: set[int] = set()
    for id_chunk in _chunks(matched_ids):
        sent_job_ids.update(
            SentEmailLog.objects.filter(
                job_posting_id__in=id_chunk,
                send_type=SentEmailLog.SendType.REAL,
                status=SentEmailLog.SendStatus.SENT,
                message_type=SentEmailLog.MessageType.INITIAL,
            )
            .values_list("job_posting_id", flat=True)
            .distinct()
        )
        approved_job_ids.update(
            ApprovalRecord.objects.filter(job_posting_id__in=id_chunk, is_approved=True)
            .values_list("job_posting_id", flat=True)
        )
        generated_job_ids.update(
            GeneratedEmail.objects.filter(job_posting_id__in=id_chunk)
            .exclude(subject="")
            .values_list("job_posting_id", flat=True)
        )

    display_jobs_by_id = {
        int(job.id): job
        for job in (
            JobPosting.objects.filter(id__in=displayed_ids)
            .select_related("daily_batch", "company_ref", "generated_email", "approval_record")
            .prefetch_related("targets__company_recruiter", "sent_logs")
        )
    }

    rows = []
    for job_id in displayed_ids:
        job = display_jobs_by_id.get(job_id)
        if not job:
            continue

        generated_email = getattr(job, "generated_email", None)
        approval = getattr(job, "approval_record", None)
        sent_logs = list(getattr(job, "sent_logs").all()) if hasattr(job, "sent_logs") else []
        real_initial_sent = [
            log
            for log in sent_logs
            if log.send_type == SentEmailLog.SendType.REAL
            and log.status == SentEmailLog.SendStatus.SENT
            and log.message_type == SentEmailLog.MessageType.INITIAL
        ]
        selected_targets = [
            target
            for target in getattr(job, "targets").all()
            if bool(getattr(target, "is_selected_for_job", False))
            and safe_str(getattr(target, "recipient_email_snapshot", "")).strip().lower() not in {"", "none"}
        ]

        is_generated = bool(generated_email and safe_str(generated_email.subject).strip())
        is_approved = bool(approval and approval.is_approved)
        is_sent = bool(real_initial_sent)
        latest_sent_at = None
        for log in real_initial_sent:
            if log.sent_at and (latest_sent_at is None or log.sent_at > latest_sent_at):
                latest_sent_at = log.sent_at

        rows.append(
            {
                "job": job,
                "job_id": job.id,
                "batch_date": getattr(job.daily_batch, "batch_date", None),
                "company": safe_str(job.company).strip(),
                "normalized_company": safe_str(getattr(job.company_ref, "normalized_name", "")).strip()
                or safe_str(job.normalized_company).strip(),
                "title": safe_str(job.title).strip(),
                "location": safe_str(job.location).strip(),
                "status": safe_str(job.status).strip(),
                "linkedin_url": safe_str(job.normalized_linkedin_url).strip() or safe_str(job.linkedin_url).strip(),
                "apply_url": safe_str(job.normalized_apply_url).strip() or safe_str(job.apply_url).strip(),
                "generated": is_generated,
                "subject": safe_str(getattr(generated_email, "subject", "")).strip() if generated_email else "",
                "approved": is_approved,
                "approved_at": getattr(approval, "approved_at", None) if approval else None,
                "real_sent": is_sent,
                "sent_count": len(real_initial_sent),
                "latest_sent_at": latest_sent_at,
                "sent_recipients": sorted({safe_str(log.to_email).strip() for log in real_initial_sent if safe_str(log.to_email).strip()}),
                "selected_targets": [
                    {
                        "name": safe_str(target.recipient_name_snapshot).strip(),
                        "email": safe_str(target.recipient_email_snapshot).strip(),
                        "recruiter_title": safe_str(getattr(target.company_recruiter, "apollo_title", "")).strip()
                        if getattr(target, "company_recruiter", None)
                        else "",
                    }
                    for target in selected_targets[:8]
                ],
                "recruiter_lead_name": safe_str(job.recruiter_name).strip() or safe_str(job.ai_hiring_mgr_name).strip(),
                "recruiter_lead_title": safe_str(job.recruiter_title).strip(),
                "description_preview": safe_str(job.description).strip()[:420],
            }
        )

    context["total_matches"] = len(matched_ids)
    context["displayed_matches"] = len(rows)
    context["sent_matches"] = len(sent_job_ids)
    context["approved_matches"] = len(approved_job_ids)
    context["generated_matches"] = len(generated_job_ids)
    context["rows"] = rows
    return context


def build_pipeline_dashboard_context(batch_date: str = "") -> dict:
    requested_batch_date = safe_str(batch_date).strip()
    batch = _populated_batch_for_date(requested_batch_date)
    selected_batch_date = batch.batch_date.isoformat() if batch else requested_batch_date
    available_batches = _available_batch_rows()
    recent_available_batches = available_batches[:5]
    email_sending_state = get_email_sending_state()
    max_per_company = get_max_people_per_company()

    if not batch:
        return {
            "batch": None,
            "selected_batch_date": selected_batch_date,
            "requested_batch_date": requested_batch_date,
            "available_batches": available_batches,
            "recent_available_batches": recent_available_batches,
            "max_targets_per_job": max_per_company,
            "email_sending_state": email_sending_state,
            "email_sending_enabled": bool(email_sending_state["effective_enabled"]),
        }

    company_rows = _company_rows_for_latest_batch(batch, max_per_company=max_per_company)
    targeted_lookup_rows = _targeted_lookup_rows(company_rows, batch=batch)
    missing_domain_rows = [r for r in company_rows if not r["domain_usable"] and not r["is_blocked"]]
    missing_domain_job_count = sum(int(r["job_count"] or 0) for r in missing_domain_rows)
    latest_jobs = list(
        JobPosting.objects.filter(daily_batch=batch, company_ref__isnull=False, is_manual_email_job=False)
        .select_related("company_ref")
        .order_by("company_ref__normalized_name", "id")
    )
    manual_job_id_rows = _manual_job_id_rows(batch)
    safe_missing_domain_job_rows = []
    protected_missing_domain_job_rows = []
    apify_person_lead_job_rows = []
    for job in latest_jobs:
        domain_usable = is_usable_company_domain(getattr(job.company_ref, "active_domain", ""))
        has_person_lead = job_has_apify_person_lead(job)
        if has_person_lead:
            apify_person_lead_job_rows.append({"job": job, "company": job.company_ref})
        if domain_usable:
            continue
        row = {"job": job, "company": job.company_ref}
        if has_person_lead:
            protected_missing_domain_job_rows.append(row)
        else:
            safe_missing_domain_job_rows.append(row)
    recruiter_blocked_rows = [r for r in company_rows if r["pending_job_count"] and not r["domain_usable"]]
    recruiter_ready_rows = [r for r in company_rows if r["ready_for_recruiter_fill"]]
    zero_usable_recipient_rows = [r for r in company_rows if int(r.get("usable_recipient_count") or 0) == 0]
    ready_legacy_only_rows = [
        r
        for r in company_rows
        if r["pending_job_count"] and r["allowed_recruiter_count"] >= max_per_company
    ]

    domain_payload = {row["normalized_name"]: "" for row in missing_domain_rows}
    domain_template_text = json.dumps(domain_payload, indent=2, ensure_ascii=False)
    company_names_text = "\n".join(row["normalized_name"] for row in missing_domain_rows)

    apify_person_ready_job_rows = [
        row
        for row in apify_person_lead_job_rows
        if row["job"].status == JobPosting.Status.RECRUITERS_PENDING
        and is_usable_company_domain(getattr(row["company"], "active_domain", ""))
    ]
    apollo_credit_ceiling = sum(int(row["apollo_slots_needed"] or 0) for row in recruiter_ready_rows) + len(
        apify_person_ready_job_rows
    )

    batch_topup_all_company_count = sum(
        1
        for row in company_rows
        if not row.get("is_blocked") and not row.get("is_blacklisted")
    )
    batch_topup_all_needs_apollo_company_count = sum(
        1
        for row in company_rows
        if not row.get("is_blocked")
        and not row.get("is_blacklisted")
        and company_needs_apollo_topup(row.get("company"), max_per_company)
    )
    blacklisted_company_ids = [
        row["company"].id
        for row in company_rows
        if row.get("is_blacklisted") and row.get("company")
    ]
    approved_topup_company_ids = list(
        JobPosting.objects
        .filter(
            daily_batch=batch,
            company_ref__isnull=False,
            company_ref__is_blocked=False,
            is_manual_email_job=False,
            approval_record__is_approved=True,
            generated_email__isnull=False,
        )
        .exclude(generated_email__subject="")
        .exclude(generated_email__body="")
        .exclude(company_ref_id__in=blacklisted_company_ids)
        .values_list("company_ref_id", flat=True)
        .distinct()
    )
    batch_topup_approved_company_count = (
        len(approved_topup_company_ids)
    )
    approved_topup_companies_by_id = {
        row["company"].id: row["company"]
        for row in company_rows
        if row.get("company") and row["company"].id in approved_topup_company_ids
    }
    batch_topup_approved_needs_apollo_company_count = (
        sum(
            1
            for company in approved_topup_companies_by_id.values()
            if company_needs_apollo_topup(company, max_per_company)
        )
    )

    metrics = _batch_job_metrics(batch)
    blockers = {
        "companies_missing_domain": len(missing_domain_rows),
        "companies_ready_for_apollo_topup": len(recruiter_ready_rows),
        "companies_blocked_recruiters_missing_domain": len(recruiter_blocked_rows),
        "companies_zero_usable_recipients": len(zero_usable_recipient_rows),
        "blacklisted_companies_latest_batch": sum(1 for r in company_rows if r.get("is_blacklisted") or r.get("is_blocked")),
        "missing_domain_jobs_safe_to_delete": len(safe_missing_domain_job_rows),
        "missing_domain_jobs_protected_by_person_lead": len(protected_missing_domain_job_rows),
        "apify_person_lead_jobs": len(apify_person_lead_job_rows),
        "apify_person_ready_jobs": len(apify_person_ready_job_rows),
        "companies_legacy_ready": len(ready_legacy_only_rows),
        "jobs_missing_recipients": max(0, metrics["total_jobs"] - metrics["jobs_with_targets"]),
        "jobs_missing_generated_email": max(0, metrics["total_jobs"] - metrics["generated_emails"]),
        "jobs_needing_approval": max(0, metrics["ready_to_review"] - metrics["approved_jobs"]),
    }
    active_company_rows = [
        row
        for row in company_rows
        if row.get("company") and not row.get("is_blocked") and not row.get("is_blacklisted")
    ]
    domain_ready_company_rows = [row for row in active_company_rows if row.get("domain_usable")]
    company_people_summary = {
        "total_batch_companies": len(active_company_rows),
        "domain_ready_companies": len(domain_ready_company_rows),
        "companies_needing_topup": len(recruiter_ready_rows),
        "companies_with_zero_people": sum(
            1
            for row in domain_ready_company_rows
            if int(row.get("usable_recipient_count") or 0) == 0
            and int(row.get("apollo_slots_needed") or 0) > 0
        ),
        "companies_partial_needing_topup": sum(
            1
            for row in recruiter_ready_rows
            if int(row.get("usable_recipient_count") or 0) > 0
        ),
        "companies_full_or_capped": sum(
            1
            for row in domain_ready_company_rows
            if int(row.get("usable_recipient_count") or 0) > 0
            and not row.get("ready_for_recruiter_fill")
        ),
        "selected_people_now": sum(int(row.get("usable_recipient_count") or 0) for row in domain_ready_company_rows),
        "company_apollo_slots_needed": sum(int(row.get("apollo_slots_needed") or 0) for row in recruiter_ready_rows),
        "exact_person_jobs_ready": len(apify_person_ready_job_rows),
        "max_people_per_company": max_per_company,
    }

    apollo_batch_totals = {
        "total_emails_found": sum(int(r.get("last_apollo_emails_found") or 0) for r in company_rows),
        "total_verified_emails_found": sum(int(r.get("last_apollo_verified_emails_found") or 0) for r in company_rows),
        "total_unverified_emails_found": sum(int(r.get("last_apollo_unverified_emails_found") or 0) for r in company_rows),
        "total_credits_consumed": sum(int(r.get("last_apollo_credits_consumed") or 0) for r in company_rows),
        "companies_with_apollo_run": sum(1 for r in company_rows if r.get("last_apollo_run_at")),
    }
    apollo_dashboard_credits_used = get_apollo_dashboard_credits_used()
    from core.models import AppSetting
    app_setting = AppSetting.get_solo()
    local_apollo_email_count = (
        CompanyRecruiter.objects
        .filter(source=CompanyRecruiter.Source.APOLLO)
        .filter(email_status__iexact="verified")
        .exclude(email="")
        .exclude(email="none")
        .values("email")
        .distinct()
        .count()
    )
    apollo_credit_audit = {
        "dashboard_credits_used": apollo_dashboard_credits_used,
        "local_unique_apollo_emails": local_apollo_email_count,
        "dashboard_minus_local_apollo_emails": apollo_dashboard_credits_used - local_apollo_email_count,
        "latest_batch_logged_credits": apollo_batch_totals["total_credits_consumed"],
        "latest_batch_logged_emails": apollo_batch_totals["total_emails_found"],
        "latest_batch_not_converted": max(
            0,
            apollo_batch_totals["total_credits_consumed"] - apollo_batch_totals["total_emails_found"],
        ),
        "next_plan_max_credits": apollo_credit_ceiling,
        "dashboard_after_plan_ceiling": apollo_dashboard_credits_used + apollo_credit_ceiling,
        "checkpoint_date": app_setting.apollo_checkpoint_date,
        "checkpoint_local_unique_emails": app_setting.apollo_checkpoint_local_unique_emails,
        "checkpoint_today_logged_credits": app_setting.apollo_checkpoint_today_logged_credits,
        "checkpoint_today_logged_emails": app_setting.apollo_checkpoint_today_logged_emails,
        "checkpoint_today_not_converted": app_setting.apollo_checkpoint_today_not_converted,
    }
    today_apollo_log_report = _today_apollo_log_report()
    apollo_credit_audit["since_checkpoint_logged_credits"] = max(
        0,
        int(today_apollo_log_report["totals"]["credits"] or 0)
        - int(app_setting.apollo_checkpoint_today_logged_credits or 0),
    )
    apollo_credit_audit["since_checkpoint_logged_emails"] = max(
        0,
        int(today_apollo_log_report["totals"]["emails"] or 0)
        - int(app_setting.apollo_checkpoint_today_logged_emails or 0),
    )
    apollo_credit_audit["since_checkpoint_logged_waste"] = max(
        0,
        int(today_apollo_log_report["totals"]["not_converted"] or 0)
        - int(app_setting.apollo_checkpoint_today_not_converted or 0),
    )
    apollo_credit_audit["since_checkpoint_local_email_delta"] = max(
        0,
        local_apollo_email_count - int(app_setting.apollo_checkpoint_local_unique_emails or 0),
    )
    apollo_credit_audit["expected_dashboard_now"] = (
        apollo_dashboard_credits_used + apollo_credit_audit["since_checkpoint_logged_credits"]
    )
    bulk_targeted_company_domain_map = {
        row["normalized_name"]: row.get("domain") or ""
        for row in targeted_lookup_rows
    }
    bulk_targeted_domain_people_map = {
        row["domain"]: ""
        for row in targeted_lookup_rows
        if row.get("domain")
    }

    return {
        "batch": batch,
        "selected_batch_date": selected_batch_date,
        "requested_batch_date": requested_batch_date,
        "available_batches": available_batches,
        "recent_available_batches": recent_available_batches,
        "metrics": metrics,
        "blockers": blockers,
        "company_people_summary": company_people_summary,
        "company_rows": company_rows,
        "manual_job_id_rows": manual_job_id_rows,
        "manual_job_id_missing_count": sum(1 for row in manual_job_id_rows if not row["has_manual_job_reference_id"]),
        "manual_job_id_duplicate_count": sum(1 for row in manual_job_id_rows if row.get("manual_job_reference_duplicate")),
        "targeted_lookup_rows": targeted_lookup_rows,
        "missing_domain_rows": missing_domain_rows,
        "missing_domain_job_count": missing_domain_job_count,
        "safe_missing_domain_job_rows": safe_missing_domain_job_rows,
        "protected_missing_domain_job_rows": protected_missing_domain_job_rows,
        "apify_person_lead_job_rows": apify_person_lead_job_rows,
        "apify_person_ready_job_rows": apify_person_ready_job_rows,
        "recruiter_ready_rows": recruiter_ready_rows,
        "recruiter_blocked_rows": recruiter_blocked_rows,
        "zero_usable_recipient_rows": zero_usable_recipient_rows,
        "domain_template_text": domain_template_text,
        "company_names_text": company_names_text,
        "bulk_targeted_company_domain_text": json.dumps(bulk_targeted_company_domain_map, indent=2, ensure_ascii=False),
        "bulk_targeted_domain_people_text": json.dumps(bulk_targeted_domain_people_map, indent=2, ensure_ascii=False),
        "apollo_credit_ceiling": apollo_credit_ceiling,
        "batch_topup_approved_company_count": batch_topup_approved_company_count,
        "batch_topup_all_company_count": batch_topup_all_company_count,
        "batch_topup_approved_needs_apollo_company_count": batch_topup_approved_needs_apollo_company_count,
        "batch_topup_all_needs_apollo_company_count": batch_topup_all_needs_apollo_company_count,
        "apollo_batch_totals": apollo_batch_totals,
        "apollo_credit_audit": apollo_credit_audit,
        "today_apollo_log_report": today_apollo_log_report,
        "max_targets_per_job": max_per_company,
        "email_sending_state": email_sending_state,
        "email_sending_enabled": bool(email_sending_state["effective_enabled"]),
    }
