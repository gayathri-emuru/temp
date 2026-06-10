from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import ApprovalRecord, GeneratedEmail, JobPosting, JobRecruiterTarget, SentEmailLog, SystemLog
from core.utils import safe_str


STATUS_RANK = {
    JobPosting.Status.REAL_SENT: 70,
    JobPosting.Status.TEST_SENT: 60,
    JobPosting.Status.APPROVED: 50,
    JobPosting.Status.EMAIL_GENERATED: 40,
    JobPosting.Status.EMAIL_DISCOVERY_DONE: 35,
    JobPosting.Status.EMAIL_DISCOVERY_READY: 30,
    JobPosting.Status.RECRUITERS_PENDING: 20,
    JobPosting.Status.IMPORTED: 10,
    JobPosting.Status.FAILED: 0,
    JobPosting.Status.BLOCKED: 0,
}


def _job_score(job: JobPosting) -> tuple:
    return (
        STATUS_RANK.get(job.status, 0),
        1 if hasattr(job, "generated_email") else 0,
        1 if hasattr(job, "approval_record") else 0,
        SentEmailLog.objects.filter(job_posting=job).count(),
        JobRecruiterTarget.objects.filter(job_posting=job).count(),
        -int(job.id),
    )


def _pick_keep(jobs: list[JobPosting]) -> JobPosting:
    jobs = list(jobs)
    jobs.sort(key=_job_score, reverse=True)
    return jobs[0]


@transaction.atomic
def _merge_job_into(target: JobPosting, source: JobPosting) -> dict:
    stats = {
        "source_id": source.id,
        "target_id": target.id,
        "targets_moved": 0,
        "targets_merged": 0,
        "targets_dropped_conflict": 0,
        "sent_logs_moved": 0,
        "system_logs_moved": 0,
        "generated_email_moved": 0,
        "approval_record_moved": 0,
    }

    # Move targets (avoid unique(job_posting, company_recruiter) conflicts)
    for t in JobRecruiterTarget.objects.filter(job_posting=source).order_by("id"):
        conflict = JobRecruiterTarget.objects.filter(job_posting=target, company_recruiter=t.company_recruiter).first()
        if not conflict:
            t.job_posting = target
            t.save(update_fields=["job_posting", "updated_at"])
            stats["targets_moved"] += 1
            continue

        # Merge flags into existing target row.
        conflict.is_selected_for_job = bool(conflict.is_selected_for_job or t.is_selected_for_job)
        conflict.is_verified_for_job = bool(conflict.is_verified_for_job or t.is_verified_for_job)
        conflict.is_test_target_used = bool(conflict.is_test_target_used or t.is_test_target_used)
        conflict.is_sent_real = bool(conflict.is_sent_real or t.is_sent_real)
        if safe_str(t.send_block_reason) and not safe_str(conflict.send_block_reason):
            conflict.send_block_reason = t.send_block_reason
        conflict.save()
        t.delete()
        stats["targets_merged"] += 1

    stats["sent_logs_moved"] = SentEmailLog.objects.filter(job_posting=source).update(job_posting=target)
    stats["system_logs_moved"] = SystemLog.objects.filter(job_posting=source).update(job_posting=target)

    # Move GeneratedEmail/ApprovalRecord only if target doesn't have one.
    if not GeneratedEmail.objects.filter(job_posting=target).exists():
        stats["generated_email_moved"] = GeneratedEmail.objects.filter(job_posting=source).update(job_posting=target)
    else:
        GeneratedEmail.objects.filter(job_posting=source).delete()

    if not ApprovalRecord.objects.filter(job_posting=target).exists():
        stats["approval_record_moved"] = ApprovalRecord.objects.filter(job_posting=source).update(job_posting=target)
    else:
        ApprovalRecord.objects.filter(job_posting=source).delete()

    # Prefer filling missing fields on target.
    changed_fields = []
    for field in [
        "company_ref_id",
        "external_job_id",
        "linkedin_url",
        "apply_url",
        "normalized_linkedin_url",
        "normalized_apply_url",
        "location",
        "salary",
        "apify_linkedin_org_url",
        "apify_linkedin_org_slug",
        "company_linkedin",
        "linkedin_geo_region_id",
    ]:
        if not safe_str(getattr(target, field, "")) and safe_str(getattr(source, field, "")):
            setattr(target, field, getattr(source, field))
            changed_fields.append(field)

    if changed_fields:
        target.save(update_fields=list(set(changed_fields + ["updated_at"])))

    source.delete()
    return stats


class Command(BaseCommand):
    help = "Deduplicate JobPosting rows by external_job_id and normalized_linkedin_url. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply deletes/merges (otherwise dry-run).")
        parser.add_argument("--limit", type=int, default=2000, help="Max duplicate groups to process (default 2000).")

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        limit = int(options.get("limit") or 2000)

        groups = []

        # Group by external_job_id
        ext_map = defaultdict(list)
        for row in JobPosting.objects.exclude(external_job_id="").values("id", "external_job_id"):
            ext = safe_str(row.get("external_job_id")).strip()
            if not ext:
                continue
            ext_map[ext].append(int(row["id"]))
        for ids in ext_map.values():
            if len(ids) > 1:
                groups.append(("external_job_id", ids))

        # Group by normalized_linkedin_url (case-insensitive)
        url_map = defaultdict(list)
        for row in JobPosting.objects.exclude(normalized_linkedin_url="").values("id", "normalized_linkedin_url"):
            url = safe_str(row.get("normalized_linkedin_url")).strip().lower()
            if not url:
                continue
            url_map[url].append(int(row["id"]))
        for ids in url_map.values():
            if len(ids) > 1:
                groups.append(("normalized_linkedin_url", ids))

        # Deduplicate overlapping groups by using a canonical sorted id tuple.
        seen = set()
        unique_groups = []
        for reason, ids in groups:
            key = tuple(sorted(ids))
            if key in seen:
                continue
            seen.add(key)
            unique_groups.append((reason, list(key)))

        if not unique_groups:
            self.stdout.write(self.style.SUCCESS("No JobPosting duplicates found."))
            return

        unique_groups = unique_groups[: max(1, limit)]
        self.stdout.write(self.style.WARNING(f"Duplicate groups found: {len(unique_groups)} (apply={apply})"))

        for reason, ids in unique_groups[:50]:
            jobs = list(JobPosting.objects.filter(id__in=ids).order_by("id"))
            keep = _pick_keep(jobs)
            drops = [j for j in jobs if j.id != keep.id]
            self.stdout.write(
                f"- {reason}: keep #{keep.id} ext={keep.external_job_id!r} url={safe_str(keep.normalized_linkedin_url)[:80]!r} "
                f"drop={[j.id for j in drops]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING("Dry-run only. Re-run with --apply to merge/delete duplicates."))
            return

        merged = 0
        for reason, ids in unique_groups:
            jobs = list(JobPosting.objects.filter(id__in=ids))
            if len(jobs) <= 1:
                continue
            keep = _pick_keep(jobs)
            for src in jobs:
                if src.id == keep.id:
                    continue
                stats = _merge_job_into(keep, src)
                merged += 1
                self.stdout.write(f"MERGED {reason}: {stats}")

        self.stdout.write(self.style.SUCCESS(f"Done. duplicates_merged={merged}"))

