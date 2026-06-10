from __future__ import annotations

import ast
import json
import re
import time
from urllib.parse import quote_plus

from django.db import transaction

from core.models import ApolloRejectedEmail, Company, CompanyRecruiter, JobPosting, JobRecruiterTarget, TargetedPeopleLookupRun
from core.services.apollo_recruiter_fetch_service import (
    APOLLO_BACKOFF_SECS,
    _email_domain,
    _email_has_prior_real_initial_send,
    _email_matches_domain,
    _extract_match_email,
    _has_real_email,
    _increment_counter,
    _person_location_string,
    _split_name,
    match_person_email_from_apollo,
    upsert_company_recruiters_from_apollo,
)
from core.services.app_settings_service import get_max_people_per_company
from core.services.company_domain_service import is_usable_company_domain, normalize_domain_value
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.job_target_sync_service import sync_job_targets_for_company_pending_jobs
from core.services.normalization_service import normalize_company_name, normalize_person_name
from core.services.recruiter_title_guard_service import (
    is_data_science_manager_contact_title,
    recruiter_contact_is_allowed,
)
from core.utils import safe_str


SEARCH_KEYWORDS = [
    ("Data Science Director", "data science director"),
    ("Data Science Manager", "data science manager"),
    ("ML Manager", "machine learning manager"),
    ("Analytics Director", "analytics director"),
]

PERSON_NICKNAME_GROUPS = [
    {"alex", "alexander", "alexandra"},
    {"andy", "andrew"},
    {"ben", "benjamin"},
    {"beth", "elizabeth", "liz", "lizzy"},
    {"bill", "billy", "will", "william"},
    {"bob", "bobby", "rob", "robert"},
    {"chris", "christopher", "christina", "christine"},
    {"dan", "daniel"},
    {"dave", "david"},
    {"jen", "jennifer"},
    {"joe", "joseph"},
    {"john", "jon", "jonathan"},
    {"kate", "katherine", "kathryn", "katie"},
    {"mike", "michael"},
    {"nick", "nicholas"},
    {"pat", "patrick", "patricia"},
    {"sam", "samantha", "samuel"},
    {"steve", "stephen", "steven"},
    {"tom", "thomas"},
]
PERSON_NICKNAMES = {
    name: group
    for group in PERSON_NICKNAME_GROUPS
    for name in group
}
PERSON_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "mba"}


def parse_target_person_names(raw_text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;\n]+", safe_str(raw_text)):
        text = re.sub(r"\s+", " ", part).strip()
        if not text:
            continue
        # Allow "Jane Doe - Data Science Manager" while keeping the matchable name.
        text = re.split(r"\s+[-–—]\s+", text, maxsplit=1)[0].strip()
        norm = normalize_person_name(text)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        names.append(text)
    return names


def build_people_search_links(*, company_name: str, company_url: str = "", stored_urls: dict | None = None) -> list[dict]:
    if isinstance(stored_urls, dict):
        labels = {
            "data_science_director": "Data Science Director",
            "data_science_manager": "Data Science Manager",
            "machine_learning_manager": "ML Manager",
            "data science director": "Data Science Director",
            "data science manager": "Data Science Manager",
            "machine learning manager": "ML Manager",
            "director": "Data Science Director",
        }
        stored_links = [
            {
                "label": labels.get(safe_str(key).strip().lower(), safe_str(key).strip().title()),
                "url": safe_str(url).strip(),
                "location_scoped": True,
            }
            for key, url in stored_urls.items()
            if safe_str(url).strip() and safe_str(key).strip().lower() in labels
        ]
        if stored_links:
            return stored_links

    clean_url = safe_str(company_url).strip()
    base = ""
    if "linkedin.com/company/" in clean_url.lower():
        base = clean_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if base.endswith("/about"):
            base = base[: -len("/about")]
        base = f"{base}/people/"

    links = []
    for label, keyword in SEARCH_KEYWORDS:
        if base:
            url = f"{base}?keywords={quote_plus(keyword)}"
        else:
            url = (
                "https://www.linkedin.com/search/results/people/"
                f"?keywords={quote_plus(f'{company_name} {keyword}')}"
            )
        links.append({"label": label, "url": url, "location_scoped": False})
    return links


def _load_object_payload(raw_text: str, label: str) -> dict:
    text = safe_str(raw_text).strip()
    if not text:
        raise ValueError(f"{label} cannot be empty.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            payload = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"{label} must be a JSON object or Python dict.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object/dict.")
    return payload


def _parse_people_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            for name in parse_target_person_names(safe_str(item)):
                norm = normalize_person_name(name)
                if norm and norm not in seen:
                    seen.add(norm)
                    names.append(name)
        return names
    return parse_target_person_names(safe_str(value))


def _latest_lookup_job(company: Company) -> JobPosting | None:
    pending = (
        company.jobs.filter(status=JobPosting.Status.RECRUITERS_PENDING, is_manual_email_job=False)
        .order_by("-daily_batch__batch_date", "id")
        .first()
    )
    if pending:
        return pending
    return (
        company.jobs.filter(is_manual_email_job=False)
        .order_by("-daily_batch__batch_date", "id")
        .first()
    )


def run_bulk_targeted_people_lookup(
    *,
    company_domain_map_text: str,
    domain_people_map_text: str,
    allow_regular_fallback: bool = True,
    dry_run: bool = True,
    max_people: int | None = None,
) -> dict:
    max_people = int(max_people or get_max_people_per_company())
    max_people = max(1, max_people)
    company_domain_map = _load_object_payload(company_domain_map_text, "Company/domain map")
    domain_people_map = _load_object_payload(domain_people_map_text, "Domain/people map")

    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "allow_regular_fallback": bool(allow_regular_fallback),
        "max_people": max_people,
        "companies_seen": 0,
        "domains_updated": 0,
        "unchanged_domains": 0,
        "invalid_domains": 0,
        "company_not_found": 0,
        "ambiguous_domains": 0,
        "people_domain_not_found": 0,
        "lookups_planned": 0,
        "lookups_run": 0,
        "names_seen": 0,
        "names_submitted": 0,
        "names_skipped_over_slot_limit": 0,
        "emails_found": 0,
        "unverified_emails": 0,
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "errors": 0,
        "domain_details": [],
        "lookup_details": [],
    }

    company_by_domain: dict[str, Company] = {}
    duplicate_domains: set[str] = set()

    for company_name, domain_raw in company_domain_map.items():
        result["companies_seen"] += 1
        normalized_company = normalize_company_name(company_name)
        normalized_domain = normalize_domain_value(safe_str(domain_raw))
        detail = {
            "company_name": safe_str(company_name),
            "normalized_company": normalized_company,
            "domain": normalized_domain,
        }

        if not normalized_company:
            result["errors"] += 1
            detail["status"] = "error"
            detail["message"] = "company name is blank after normalization"
            result["domain_details"].append(detail)
            continue

        raw_company_key = safe_str(company_name).strip().lower()
        company = Company.objects.filter(normalized_name=raw_company_key).first()
        if not company:
            company = Company.objects.filter(normalized_name=normalized_company).first()
        if not company:
            result["company_not_found"] += 1
            detail["status"] = "company_not_found"
            result["domain_details"].append(detail)
            continue

        if not normalized_domain:
            detail["status"] = "skipped_blank_domain"
            result["domain_details"].append(detail)
            continue

        if not is_usable_company_domain(normalized_domain):
            result["invalid_domains"] += 1
            detail["status"] = "invalid_domain"
            result["domain_details"].append(detail)
            continue

        existing = company_by_domain.get(normalized_domain)
        if existing and existing.id != company.id:
            duplicate_domains.add(normalized_domain)
            result["ambiguous_domains"] += 1
            detail["status"] = "ambiguous_domain"
            detail["message"] = f"also mapped to {existing.normalized_name}"
            result["domain_details"].append(detail)
            continue
        company_by_domain[normalized_domain] = company

        current_domain = normalize_domain_value(company.active_domain)
        if current_domain == normalized_domain:
            result["unchanged_domains"] += 1
            detail["status"] = "unchanged"
        elif dry_run:
            detail["status"] = "would_update"
            detail["current_domain"] = current_domain
        else:
            company.active_domain = normalized_domain
            company.domain_status = Company.DomainStatus.SET
            company.save(update_fields=["active_domain", "domain_status", "updated_at"])
            result["domains_updated"] += 1
            detail["status"] = "updated"
            detail["previous_domain"] = current_domain
        result["domain_details"].append(detail)

    db_domain_rows = (
        Company.objects.filter(is_blocked=False)
        .exclude(active_domain="")
        .values("id", "active_domain")
    )
    db_company_by_domain = {}
    for row in db_domain_rows:
        domain = normalize_domain_value(row.get("active_domain"))
        if domain and domain not in db_company_by_domain:
            db_company_by_domain[domain] = int(row["id"])

    for domain_key, people_value in domain_people_map.items():
        normalized_domain = normalize_domain_value(domain_key)
        names = _parse_people_value(people_value)
        result["names_seen"] += len(names)
        detail = {
            "domain": normalized_domain,
            "raw_domain": safe_str(domain_key),
            "names_seen": len(names),
            "names": names,
        }

        if not normalized_domain or normalized_domain in duplicate_domains:
            result["errors"] += 1
            detail["status"] = "invalid_or_ambiguous_domain"
            result["lookup_details"].append(detail)
            continue

        company = company_by_domain.get(normalized_domain)
        if not company:
            company_id = db_company_by_domain.get(normalized_domain)
            company = Company.objects.filter(id=company_id).first() if company_id else None
        if not company:
            result["people_domain_not_found"] += 1
            detail["status"] = "domain_not_found"
            result["lookup_details"].append(detail)
            continue

        open_slots = max(0, max_people - len(_active_unsent_email_recruiters(company)))
        names_to_submit = names[:open_slots]
        skipped_names = names[open_slots:]
        result["names_skipped_over_slot_limit"] += len(skipped_names)
        detail.update(
            {
                "company": company.normalized_name,
                "open_slots": open_slots,
                "names_submitted": len(names_to_submit),
                "names_skipped_over_slot_limit": len(skipped_names),
                "skipped_names": skipped_names,
            }
        )

        if not names_to_submit:
            detail["status"] = "skipped_no_open_slots_or_names"
            result["lookup_details"].append(detail)
            continue

        result["lookups_planned"] += 1
        result["names_submitted"] += len(names_to_submit)
        if dry_run:
            detail["status"] = "would_run"
            result["lookup_details"].append(detail)
            continue

        lookup_result = run_targeted_people_lookup(
            company=company,
            job=_latest_lookup_job(company),
            raw_names=", ".join(names_to_submit),
            allow_regular_fallback=allow_regular_fallback,
            max_people=max_people,
        )
        totals = lookup_result.get("totals") or {}
        result["lookups_run"] += 1
        result["emails_found"] += int(totals.get("emails_found") or 0)
        result["unverified_emails"] += int(totals.get("unverified_emails") or 0)
        result["credits_consumed"] += int(totals.get("credits_consumed") or 0)
        result["credits_not_converted_to_email"] += int(totals.get("credits_not_converted_to_email") or 0)
        detail["status"] = "completed"
        detail["run_id"] = lookup_result.get("run_id")
        detail["totals"] = totals
        result["lookup_details"].append(detail)

    return result


def _target_jobs(company: Company) -> list[JobPosting]:
    return list(
        company.jobs.filter(is_manual_email_job=False)
        .select_related("company_ref")
        .order_by("-daily_batch__batch_date", "id")
    )


def _active_unsent_email_recruiters(company: Company) -> list[CompanyRecruiter]:
    recruiters = list(
        CompanyRecruiter.objects.filter(company=company, is_active=True, email_sent=False)
        .exclude(email__in=["", "none"])
        .order_by("id")
    )
    return [recruiter for recruiter in recruiters if _recruiter_allows_targeting(recruiter)]


def _recruiter_allows_targeting(recruiter: CompanyRecruiter) -> bool:
    source = safe_str(getattr(recruiter, "source", "")).strip().lower()
    apollo_id = safe_str(getattr(recruiter, "apollo_person_id", "")).strip()
    if source == CompanyRecruiter.Source.APOLLO or apollo_id:
        return safe_str(getattr(recruiter, "email_status", "")).strip().lower() == "verified"
    return True


def _store_rejected_apollo_email(
    *,
    company: Company,
    job: JobPosting | None,
    run: TargetedPeopleLookupRun | None,
    person_name: str,
    title: str = "",
    email: str,
    email_status: str,
    apollo_person_id: str = "",
    reason: str = "",
    run_log_path: str = "",
    raw_payload: dict | None = None,
) -> None:
    email = safe_str(email).strip().lower()
    if not company or not company.id or not email or email == "none" or "@" not in email:
        return
    ApolloRejectedEmail.objects.create(
        company=company,
        job_posting=job if job and getattr(job, "id", None) else None,
        targeted_lookup_run=run if run and getattr(run, "id", None) else None,
        person_name=safe_str(person_name).strip(),
        title=safe_str(title).strip(),
        email=email,
        email_status=safe_str(email_status).strip().lower() or "unknown",
        apollo_person_id=safe_str(apollo_person_id).strip(),
        reason=safe_str(reason).strip(),
        source_workflow="targeted_people_lookup",
        run_log_path=safe_str(run_log_path).strip(),
        raw_payload=raw_payload if isinstance(raw_payload, dict) else {},
    )


def _select_recruiter_for_company_jobs(*, company: Company, recruiters: list[CompanyRecruiter], max_people: int) -> int:
    recruiters = [
        r
        for r in recruiters
        if r
        and r.id
        and not r.email_sent
        and _has_real_email(r.email)
        and _recruiter_allows_targeting(r)
    ]
    if not recruiters:
        return 0

    jobs = _target_jobs(company)
    selected = 0
    for job in jobs:
        for idx, recruiter in enumerate(recruiters, start=1):
            JobRecruiterTarget.objects.update_or_create(
                job_posting=job,
                company_recruiter=recruiter,
                defaults={
                    "recipient_email_snapshot": recruiter.email,
                    "recipient_name_snapshot": recruiter.person_name,
                    "selection_order": idx,
                    "is_selected_for_job": True,
                    "is_verified_for_job": True,
                },
            )
            selected += 1
        if job.status == JobPosting.Status.RECRUITERS_PENDING:
            job.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
            job.save(update_fields=["status", "updated_at"])

    _prioritize_manual_targets(company=company, manual_recruiters=recruiters, max_people=max_people)
    return selected


def _prioritize_manual_targets(*, company: Company, manual_recruiters: list[CompanyRecruiter], max_people: int) -> None:
    manual_ids = {r.id for r in manual_recruiters if r and r.id}
    if not manual_ids:
        return

    for job in _target_jobs(company):
        targets = list(
            JobRecruiterTarget.objects.filter(
                job_posting=job,
                is_selected_for_job=True,
                company_recruiter__email_sent=False,
            )
            .exclude(recipient_email_snapshot__in=["", "none"])
            .select_related("company_recruiter")
            .order_by("selection_order", "id")
        )
        targets.sort(key=lambda t: (0 if t.company_recruiter_id in manual_ids else 1, t.selection_order, t.id))
        for idx, target in enumerate(targets, start=1):
            updates = []
            if idx <= max_people:
                if not target.is_selected_for_job:
                    target.is_selected_for_job = True
                    updates.append("is_selected_for_job")
                if target.selection_order != idx:
                    target.selection_order = idx
                    updates.append("selection_order")
            else:
                if target.is_selected_for_job:
                    target.is_selected_for_job = False
                    updates.append("is_selected_for_job")
            if updates:
                target.save(update_fields=updates + ["updated_at"])


def _existing_recruiter_for_name(company: Company, name: str) -> CompanyRecruiter | None:
    norm = normalize_person_name(name)
    if not norm:
        return None
    exact = CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm).order_by("id").first()
    if exact:
        return exact

    candidates = [
        recruiter
        for recruiter in CompanyRecruiter.objects.filter(company=company).exclude(person_name="").order_by("id")
        if _person_names_are_safe_variants(name, recruiter.person_name)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _person_first_last(name: str) -> tuple[str, str]:
    tokens = normalize_person_name(name).split()
    tokens = [token for token in tokens if token and len(token) > 1]
    while tokens and tokens[-1] in PERSON_NAME_SUFFIXES:
        tokens.pop()
    if len(tokens) < 2:
        return "", ""
    return tokens[0], tokens[-1]


def _first_names_are_safe_variants(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return right in PERSON_NICKNAMES.get(left, set()) or left in PERSON_NICKNAMES.get(right, set())


def _person_names_are_safe_variants(left: str, right: str) -> bool:
    left_first, left_last = _person_first_last(left)
    right_first, right_last = _person_first_last(right)
    if not left_first or not right_first or not left_last or not right_last:
        return False
    if left_last != right_last:
        return False
    return _first_names_are_safe_variants(left_first, right_first)


def _upsert_targeted_apollo_recruiter(
    *,
    company: Company,
    name: str,
    payload: dict,
    email: str,
    email_status: str,
    manually_targeted: bool = False,
) -> tuple[CompanyRecruiter, bool]:
    person_payload = payload.get("person") if isinstance(payload.get("person"), dict) else payload
    apollo_id = safe_str((person_payload or {}).get("id")).strip() if isinstance(person_payload, dict) else ""
    norm_person = normalize_person_name(name)
    apollo_name = safe_str((person_payload or {}).get("name")).strip() if isinstance(person_payload, dict) else ""
    norm_apollo_person = normalize_person_name(apollo_name)
    clean_email = safe_str(email).strip().lower()

    recruiter = None
    if apollo_id:
        recruiter = CompanyRecruiter.objects.filter(company=company, apollo_person_id=apollo_id).order_by("id").first()
    if recruiter is None and norm_person:
        recruiter = CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_person).order_by("id").first()
    if recruiter is None and norm_apollo_person and norm_apollo_person != norm_person:
        recruiter = CompanyRecruiter.objects.filter(company=company, normalized_person_name=norm_apollo_person).order_by("id").first()
    if recruiter is None and clean_email:
        recruiter = CompanyRecruiter.objects.filter(company=company, email__iexact=clean_email).order_by("id").first()

    created = False
    if recruiter is None:
        recruiter = CompanyRecruiter(company=company, person_name=name, email="none", is_active=True)
        created = True

    title = safe_str((person_payload or {}).get("title")).strip() if isinstance(person_payload, dict) else ""
    linkedin_url = safe_str((person_payload or {}).get("linkedin_url")).strip() if isinstance(person_payload, dict) else ""
    location = _person_location_string(person_payload if isinstance(person_payload, dict) else {})

    recruiter.person_name = apollo_name or name
    if apollo_id:
        recruiter.apollo_person_id = apollo_id
    recruiter.apollo_title = title or recruiter.apollo_title
    recruiter.apollo_location = location or recruiter.apollo_location
    recruiter.apollo_linkedin_url = linkedin_url or recruiter.apollo_linkedin_url
    recruiter.email = clean_email or email
    recruiter.source = CompanyRecruiter.Source.APOLLO
    recruiter.email_status = email_status
    recruiter.title_match = is_data_science_manager_contact_title(title)
    recruiter.location_match = False
    recruiter.is_active = True
    if manually_targeted:
        recruiter.manually_targeted = True
    recruiter.save()
    return recruiter, created


def _person_detail_payload(person_payload: dict | None) -> dict:
    person = person_payload if isinstance(person_payload, dict) else {}
    return {
        "apollo_person_id": safe_str(person.get("id")).strip(),
        "apollo_name": safe_str(person.get("name")).strip(),
        "title": safe_str(person.get("title")).strip() or safe_str(person.get("headline")).strip(),
        "location": _person_location_string(person),
        "linkedin_url": safe_str(person.get("linkedin_url")).strip(),
        "organization_name": safe_str((person.get("organization") or {}).get("name")).strip()
        if isinstance(person.get("organization"), dict)
        else "",
    }


def _current_target_count(company: Company) -> int:
    targets = (
        JobRecruiterTarget.objects.filter(
            job_posting__company_ref=company,
            job_posting__is_manual_email_job=False,
            is_selected_for_job=True,
            company_recruiter__email_sent=False,
        )
        .exclude(recipient_email_snapshot__in=["", "none"])
        .select_related("company_recruiter")
    )
    recruiter_ids = {
        target.company_recruiter_id
        for target in targets
    }
    return len(recruiter_ids)


def run_targeted_people_lookup(
    *,
    company: Company,
    raw_names: str,
    job: JobPosting | None = None,
    allow_regular_fallback: bool = True,
    max_people: int | None = None,
) -> dict:
    max_people = int(max_people or get_max_people_per_company())
    max_people = max(1, max_people)
    names = parse_target_person_names(raw_names)
    run_log_path = create_run_log_path("targeted_people_lookup", company.normalized_name)
    run = TargetedPeopleLookupRun.objects.create(
        company=company,
        job_posting=job,
        raw_names=safe_str(raw_names),
        parsed_names=names,
        allow_regular_fallback=bool(allow_regular_fallback),
        max_people=max_people,
        run_log_path=run_log_path,
    )

    stats = {
        "run_id": run.id,
        "company": company.normalized_name,
        "domain": normalize_domain_value(company.active_domain),
        "names_submitted": len(names),
        "targeted_local": 0,
        "targeted_apollo": 0,
        "regular_fallback": 0,
        "emails_found": 0,
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "fallback_used": 0,
        "fallback_blocked_by_credit_guard": 0,
        "targets_selected": 0,
        "targeted_apollo_attempt_limit": 0,
        "targeted_apollo_attempts": 0,
        "unverified_emails": 0,
        "apollo_email_status_counts": {},
        "skip_reasons": {},
        "run_log_path": run_log_path,
    }
    rows = []
    manual_recruiters: list[CompanyRecruiter] = []
    known_recruiter_ids = {r.id for r in _active_unsent_email_recruiters(company)}

    append_and_print(
        run_log_path,
        (
            f"START company={company.normalized_name} names={names} "
            f"fallback={bool(allow_regular_fallback)} max_people={max_people}"
        ),
    )

    domain = normalize_domain_value(company.active_domain)
    if not is_usable_company_domain(domain):
        stats["status"] = "missing_domain"
        _increment_counter(stats["skip_reasons"], "missing_domain")
        run.status = TargetedPeopleLookupRun.Status.FAILED
        run.error_message = "Company has no usable domain."
        run.totals = stats
        run.result_rows = rows
        run.source_counts = {"targeted_local": 0, "targeted_apollo": 0, "regular_fallback": 0}
        run.save(update_fields=["status", "error_message", "totals", "result_rows", "source_counts", "updated_at"])
        append_and_print(run_log_path, f"END stats={stats}")
        return {"totals": stats, "rows": rows, "run_id": run.id}

    targeted_apollo_attempt_limit = max(0, max_people - len(_active_unsent_email_recruiters(company)))
    targeted_apollo_attempts = 0
    stats["targeted_apollo_attempt_limit"] = targeted_apollo_attempt_limit

    for name in names:
        current_allowed_count = len(_active_unsent_email_recruiters(company))
        existing = _existing_recruiter_for_name(company, name)
        if existing and existing.email_sent:
            _increment_counter(stats["skip_reasons"], "person_already_contacted")
            rows.append(
                {
                    "name": name,
                    "source": "local",
                    "status": "skipped",
                    "reason": "person_already_contacted",
                    "email": existing.email,
                    "title": safe_str(existing.apollo_title).strip(),
                    "location": safe_str(existing.apollo_location).strip(),
                    "linkedin_url": safe_str(existing.apollo_linkedin_url).strip(),
                    "email_status": safe_str(existing.email_status).strip(),
                    "credits": 0,
                }
            )
            continue
        if existing and _has_real_email(existing.email):
            if not _recruiter_allows_targeting(existing):
                reason = f"apollo_non_verified_email:{safe_str(existing.email_status).strip().lower() or 'unknown'}"
                _increment_counter(stats["skip_reasons"], reason)
                stats["unverified_emails"] += 1
                _increment_counter(stats["apollo_email_status_counts"], safe_str(existing.email_status).strip().lower() or "unknown")
                rows.append(
                    {
                        "name": name,
                        "source": "local",
                        "status": "skipped",
                        "reason": reason,
                        "email": existing.email,
                        "apollo_person_id": safe_str(existing.apollo_person_id).strip(),
                        "apollo_name": existing.person_name,
                        "title": safe_str(existing.apollo_title).strip(),
                        "email_status": safe_str(existing.email_status).strip(),
                    }
                )
                continue
            if _email_has_prior_real_initial_send(existing.email):
                _increment_counter(stats["skip_reasons"], "email_already_contacted")
                rows.append(
                    {
                        "name": name,
                        "source": "local",
                        "status": "skipped",
                        "reason": "email_already_contacted",
                        "email": existing.email,
                        "title": safe_str(existing.apollo_title).strip(),
                        "location": safe_str(existing.apollo_location).strip(),
                        "linkedin_url": safe_str(existing.apollo_linkedin_url).strip(),
                        "email_status": safe_str(existing.email_status).strip(),
                        "credits": 0,
                    }
                )
                continue
            if not recruiter_contact_is_allowed(existing):
                existing.manually_targeted = True
                existing.save(update_fields=["manually_targeted", "updated_at"])
            manual_recruiters.append(existing)
            stats["targeted_local"] += 1
            rows.append(
                {
                    "name": name,
                    "source": "targeted_local",
                    "status": "selected",
                    "email": existing.email,
                    "title": safe_str(existing.apollo_title).strip(),
                    "location": safe_str(existing.apollo_location).strip(),
                    "linkedin_url": safe_str(existing.apollo_linkedin_url).strip(),
                    "email_status": safe_str(existing.email_status).strip(),
                    "credits": 0,
                    "reason": "already_in_local_db",
                }
            )
            continue
        if existing:
            _increment_counter(stats["skip_reasons"], "person_already_known_no_email")
            rows.append(
                {
                    "name": name,
                    "source": "local",
                    "status": "skipped",
                    "reason": "person_already_known_no_email",
                    "email": existing.email,
                    "title": safe_str(existing.apollo_title).strip(),
                    "location": safe_str(existing.apollo_location).strip(),
                    "linkedin_url": safe_str(existing.apollo_linkedin_url).strip(),
                    "email_status": safe_str(existing.email_status).strip(),
                    "credits": 0,
                }
            )
            continue

        if current_allowed_count >= max_people:
            _increment_counter(stats["skip_reasons"], "company_already_full")
            rows.append({"name": name, "source": "targeted_apollo", "status": "skipped", "reason": "company_already_full", "email": ""})
            continue

        if targeted_apollo_attempts >= targeted_apollo_attempt_limit:
            _increment_counter(stats["skip_reasons"], "targeted_attempt_limit_reached")
            rows.append({"name": name, "source": "targeted_apollo", "status": "skipped", "reason": "targeted_attempt_limit_reached", "email": ""})
            continue

        first_name, last_name = _split_name(name)
        if not first_name or not last_name:
            _increment_counter(stats["skip_reasons"], "person_name_not_matchable")
            rows.append({"name": name, "source": "targeted_apollo", "status": "skipped", "reason": "person_name_not_matchable", "email": ""})
            continue

        targeted_apollo_attempts += 1
        stats["targeted_apollo_attempts"] = targeted_apollo_attempts
        try:
            payload = match_person_email_from_apollo(
                first_name=first_name,
                last_name=last_name,
                organization_name=company.raw_name_latest or company.normalized_name,
            )
        except Exception as exc:
            _increment_counter(stats["skip_reasons"], "apollo_match_error")
            rows.append({"name": name, "source": "targeted_apollo", "status": "error", "reason": str(exc)[:500], "email": ""})
            append_exception(run_log_path, f"TARGETED_MATCH_ERROR person={name}", exc)
            time.sleep(APOLLO_BACKOFF_SECS)
            continue

        credits = int((payload or {}).get("credits_consumed") or 0)
        stats["credits_consumed"] += credits
        email, email_status = _extract_match_email(payload if isinstance(payload, dict) else {})
        if email:
            _increment_counter(stats["apollo_email_status_counts"], email_status or "unknown")
        person_payload = payload.get("person") if isinstance(payload, dict) and isinstance(payload.get("person"), dict) else payload
        details = _person_detail_payload(person_payload if isinstance(person_payload, dict) else {})
        apollo_id = details["apollo_person_id"]
        append_and_print(run_log_path, f"TARGETED_MATCH_DONE person={name} credits={credits} email={email or '[NONE]'} status={email_status or '[NONE]'}")

        if not email:
            _increment_counter(stats["skip_reasons"], "no_work_email_returned")
            stats["credits_not_converted_to_email"] += credits
            rows.append(
                {
                    "name": name,
                    "source": "targeted_apollo",
                    "status": "skipped",
                    "reason": "no_work_email_returned",
                    "email": "",
                    "credits": credits,
                    **details,
                }
            )
            if credits:
                stats["fallback_blocked_by_credit_guard"] = 1
                break
            continue

        if email_status != "verified":
            reason = f"apollo_non_verified_email:{email_status or 'unknown'}"
            _increment_counter(stats["skip_reasons"], reason)
            stats["unverified_emails"] += 1
            stats["credits_not_converted_to_email"] += credits
            _store_rejected_apollo_email(
                company=company,
                job=job,
                run=run,
                person_name=name,
                title=safe_str(details.get("title")).strip(),
                email=email,
                email_status=email_status,
                apollo_person_id=safe_str(details.get("apollo_person_id")).strip(),
                reason=reason,
                run_log_path=run_log_path,
                raw_payload=person_payload if isinstance(person_payload, dict) else {},
            )
            rows.append(
                {
                    "name": name,
                    "source": "targeted_apollo",
                    "status": "skipped",
                    "reason": reason,
                    "email": email,
                    "credits": credits,
                    "email_status": email_status,
                    **details,
                }
            )
            if credits:
                stats["fallback_blocked_by_credit_guard"] = 1
                break
            continue

        if _email_has_prior_real_initial_send(email):
            _increment_counter(stats["skip_reasons"], "email_already_contacted")
            stats["credits_not_converted_to_email"] += credits
            rows.append(
                {
                    "name": name,
                    "source": "targeted_apollo",
                    "status": "skipped",
                    "reason": "email_already_contacted",
                    "email": email,
                    "credits": credits,
                    "email_status": email_status,
                    **details,
                }
            )
            if credits:
                stats["fallback_blocked_by_credit_guard"] = 1
                break
            continue

        # NOTE: title filter is intentionally skipped here. These names were
        # explicitly pasted by the user into the Target Names box, so we trust
        # the user's intent and email whoever they asked us to email (CEO, CTO,
        # founder, etc.). The title filter still applies to Apollo's automatic
        # fallback discovery flow, just not to user-provided names.

        reason = ""
        if not _email_matches_domain(email, domain):
            reason = f"accepted_alternate_domain:{_email_domain(email)}"

        recruiter, created = _upsert_targeted_apollo_recruiter(
            company=company,
            name=name,
            payload=payload if isinstance(payload, dict) else {},
            email=email,
            email_status=email_status,
            manually_targeted=True,
        )
        known_recruiter_ids.add(recruiter.id)
        manual_recruiters.append(recruiter)
        stats["targeted_apollo"] += 1
        rows.append(
            {
                "name": name,
                "source": "targeted_apollo",
                "status": "selected",
                "email": email,
                "credits": credits,
                "email_status": email_status,
                "created": created,
                "reason": reason,
                **details,
            }
        )

        if len(_active_unsent_email_recruiters(company)) >= max_people:
            break

    if manual_recruiters:
        stats["targets_selected"] += _select_recruiter_for_company_jobs(
            company=company,
            recruiters=manual_recruiters,
            max_people=max_people,
        )

    should_fallback = bool(allow_regular_fallback) and not stats["fallback_blocked_by_credit_guard"]
    should_fallback = should_fallback and len(_active_unsent_email_recruiters(company)) < max_people
    if should_fallback:
        stats["fallback_used"] = 1
        try:
            fallback_stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint=safe_str(getattr(job, "location", "")) if job else "",
                max_people=max_people,
                run_log_path=create_run_log_path("targeted_lookup_regular_fallback", company.normalized_name),
                exclude_person_names=names,
            )
            stats["regular_fallback"] = int(fallback_stats.get("emails_found") or 0)
            stats["unverified_emails"] += int(fallback_stats.get("unverified_emails") or 0)
            stats["credits_consumed"] += int(fallback_stats.get("credits_consumed") or 0)
            stats["credits_not_converted_to_email"] += int(fallback_stats.get("credits_not_converted_to_email") or 0)
            for key, value in (fallback_stats.get("apollo_email_status_counts") or {}).items():
                _increment_counter(stats["apollo_email_status_counts"], key, int(value or 0))
            for key, value in (fallback_stats.get("skip_reasons") or {}).items():
                _increment_counter(stats["skip_reasons"], f"fallback:{key}", int(value or 0))
            rows.append(
                {
                    "name": "[regular fallback]",
                    "source": "regular_apollo_fallback",
                    "status": "completed",
                    "email": "",
                    "credits": int(fallback_stats.get("credits_consumed") or 0),
                    "reason": f"emails_found={fallback_stats.get('emails_found') or 0}",
                    "title": "",
                    "location": "",
                    "linkedin_url": "",
                    "email_status": "",
                }
            )
        except Exception as exc:
            _increment_counter(stats["skip_reasons"], "fallback_error")
            rows.append({"name": "[regular fallback]", "source": "regular_apollo_fallback", "status": "error", "reason": str(exc)[:500], "email": ""})
            append_exception(run_log_path, "REGULAR_FALLBACK_ERROR", exc)
    elif stats["fallback_blocked_by_credit_guard"]:
        rows.append(
            {
                "name": "[regular fallback]",
                "source": "regular_apollo_fallback",
                "status": "skipped",
                "email": "",
                "reason": "blocked_after_targeted_credit_waste",
                "credits": 0,
            }
        )

    if manual_recruiters:
        _prioritize_manual_targets(company=company, manual_recruiters=manual_recruiters, max_people=max_people)

    new_recruiters = [
        r for r in _active_unsent_email_recruiters(company)
        if r.id not in known_recruiter_ids and not r.legacy
    ]
    stats["emails_found"] = int(stats["targeted_local"] + stats["targeted_apollo"] + stats["regular_fallback"])
    stats["source_counts"] = {
        "targeted_local": int(stats["targeted_local"]),
        "targeted_apollo": int(stats["targeted_apollo"]),
        "regular_apollo_fallback": int(stats["regular_fallback"]),
        "stored_after_run": len(_active_unsent_email_recruiters(company)),
    }
    stats["current_selected_recipients"] = _current_target_count(company)
    if new_recruiters:
        stats["new_regular_recruiter_ids"] = [r.id for r in new_recruiters]

    if stats["emails_found"] > 0 or stats["current_selected_recipients"] > 0:
        status = TargetedPeopleLookupRun.Status.SUCCESS
    elif rows:
        status = TargetedPeopleLookupRun.Status.PARTIAL
    else:
        status = TargetedPeopleLookupRun.Status.SKIPPED

    run.status = status
    run.emails_found = stats["emails_found"]
    run.credits_consumed = stats["credits_consumed"]
    run.credits_not_converted_to_email = stats["credits_not_converted_to_email"]
    run.fallback_blocked_by_credit_guard = bool(stats["fallback_blocked_by_credit_guard"])
    run.source_counts = stats["source_counts"]
    run.result_rows = rows
    run.totals = stats
    run.save(
        update_fields=[
            "status",
            "emails_found",
            "credits_consumed",
            "credits_not_converted_to_email",
            "fallback_blocked_by_credit_guard",
            "source_counts",
            "result_rows",
            "totals",
            "updated_at",
        ]
    )
    append_and_print(run_log_path, f"END stats={stats}")
    return {"totals": stats, "rows": rows, "run_id": run.id}
