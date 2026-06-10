from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import JobPosting
from core.services.normalization_service import canonical_company_name, normalize_company_name
from core.utils import safe_str

# Reuse the proven merge/scoring logic used by the existing job deduper.
from core.management.commands.dedupe_job_postings import _merge_job_into, _pick_keep  # noqa: E402


def _expected_company_keys(job: JobPosting) -> tuple[str, str]:
    """
    Returns (expected_normalized_company, expected_canonical_company) for a job.

    Prefer Company.normalized_name when available so acronym/fuzzy merges resolve to a single key.
    """
    if getattr(job, "company_ref_id", None) and getattr(job, "company_ref", None):
        normalized = safe_str(getattr(job.company_ref, "normalized_name", "")).strip().lower()
    else:
        normalized = normalize_company_name(safe_str(getattr(job, "company", ""))).strip().lower()

    canonical = canonical_company_name(normalized or safe_str(getattr(job, "company", "")))
    return normalized, canonical


class Command(BaseCommand):
    help = (
        "Deduplicate recent JobPosting rows by canonical_company across a lookback window "
        "(default 10 days). Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply merges (otherwise dry-run).")
        parser.add_argument("--days", type=int, default=10, help="Lookback window in days (default 10).")
        parser.add_argument(
            "--fix-keys",
            action="store_true",
            help="Also backfill normalized_company/canonical_company from Company for recent jobs.",
        )
        parser.add_argument("--limit", type=int, default=2000, help="Max duplicate groups to process (default 2000).")

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        days = max(int(options.get("days") or 10), 1)
        limit = max(int(options.get("limit") or 2000), 1)
        fix_keys = bool(options.get("fix_keys"))

        today = timezone.localdate()
        start_date = today - timedelta(days=days)

        qs = (
            JobPosting.objects
            .select_related("daily_batch", "company_ref")
            .filter(daily_batch__batch_date__gte=start_date)
            .order_by("-id")
        )

        jobs = list(qs)
        if not jobs:
            self.stdout.write(self.style.SUCCESS("No recent jobs found."))
            return

        key_mismatches = 0
        key_map: dict[int, tuple[str, str]] = {}
        for job in jobs:
            expected_normalized, expected_canonical = _expected_company_keys(job)
            key_map[int(job.id)] = (expected_normalized, expected_canonical)
            if safe_str(getattr(job, "canonical_company", "")).strip() != expected_canonical:
                key_mismatches += 1

        if key_mismatches:
            self.stdout.write(
                self.style.WARNING(
                    f"Recent jobs with canonical_company mismatch vs Company-based key: {key_mismatches} "
                    f"(fix_keys={'on' if fix_keys else 'off'}, apply={apply})"
                )
            )

        # Group by expected canonical_company
        groups: dict[str, list[int]] = defaultdict(list)
        for job in jobs:
            _, expected_canonical = key_map[int(job.id)]
            if not expected_canonical:
                continue
            groups[expected_canonical].append(int(job.id))

        dup_groups = [(k, ids) for k, ids in groups.items() if len(ids) > 1]
        if not dup_groups:
            self.stdout.write(self.style.SUCCESS("No company duplicates found in the lookback window."))
            return

        dup_groups = dup_groups[:limit]
        self.stdout.write(
            self.style.WARNING(
                f"Duplicate company groups found: {len(dup_groups)} (days={days}, apply={apply}, fix_keys={fix_keys})"
            )
        )

        # Preview
        for canonical, ids in dup_groups[:50]:
            group_jobs = list(JobPosting.objects.filter(id__in=ids).select_related("daily_batch").order_by("id"))
            keep = _pick_keep(group_jobs)
            drops = [j for j in group_jobs if j.id != keep.id]
            self.stdout.write(
                f"- company={canonical!r}: keep #{keep.id} status={keep.status} date={keep.daily_batch.batch_date} "
                f"drop={[j.id for j in drops]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING("Dry-run only. Re-run with --apply to merge/delete duplicates."))
            return

        merged = 0
        updated_keys = 0

        with transaction.atomic():
            if fix_keys:
                for job in jobs:
                    expected_normalized, expected_canonical = key_map[int(job.id)]
                    updates = {}
                    if safe_str(getattr(job, "normalized_company", "")).strip().lower() != expected_normalized:
                        updates["normalized_company"] = expected_normalized
                    if safe_str(getattr(job, "canonical_company", "")).strip() != expected_canonical:
                        updates["canonical_company"] = expected_canonical
                    if updates:
                        JobPosting.objects.filter(id=job.id).update(**updates)
                        updated_keys += 1

            for canonical, ids in dup_groups:
                group_jobs = list(JobPosting.objects.filter(id__in=ids))
                if len(group_jobs) <= 1:
                    continue
                keep = _pick_keep(group_jobs)
                for src in group_jobs:
                    if src.id == keep.id:
                        continue
                    stats = _merge_job_into(keep, src)
                    merged += 1
                    self.stdout.write(f"MERGED company={canonical}: {stats}")

        self.stdout.write(self.style.SUCCESS(f"Done. jobs_merged={merged} jobs_keys_updated={updated_keys}"))

