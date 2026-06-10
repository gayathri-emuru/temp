"""
Management command: normalize_manual_linkedin_locations

Takes existing location values on manually imported LinkedIn jobs,
normalizes them to US state (full form) using extract_us_state_from_location,
updates the four location columns, then prints a summary table.
"""
from django.core.management.base import BaseCommand

from core.models import JobPosting
from core.services.normalization_service import (
    build_sort_location,
    canonical_location,
    normalize_location,
)
from core.services.openai_location_service import extract_us_state_from_location
from core.utils import safe_str


class Command(BaseCommand):
    help = "Normalize location fields on manual LinkedIn jobs to US state (full form)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing to DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        qs = (
            JobPosting.objects
            .filter(is_manual_import=True)
            .exclude(linkedin_url="")
            .exclude(location="")
            .order_by("company")
        )

        total = qs.count()
        self.stdout.write(f"Processing {total} manual LinkedIn job(s). dry_run={dry_run}\n")

        updated = 0
        unchanged = 0
        failed = 0
        rows = []

        for job in qs.iterator():
            raw = safe_str(job.location).strip()
            try:
                state = extract_us_state_from_location(raw)
            except Exception:
                state = ""

            if not state:
                rows.append((job.company, job.title, raw, raw, "FAILED"))
                failed += 1
                continue

            if state == raw:
                rows.append((job.company, job.title, raw, state, "unchanged"))
                unchanged += 1
                continue

            rows.append((job.company, job.title, raw, state, "updated"))
            if not dry_run:
                job.location = state
                job.normalized_location = normalize_location(state)
                job.canonical_location = canonical_location(state)
                job.sort_location = build_sort_location(state)
                job.save(update_fields=[
                    "location",
                    "normalized_location",
                    "canonical_location",
                    "sort_location",
                    "updated_at",
                ])
            updated += 1

        self.stdout.write(
            f"\n{'Company':<40} {'Title':<45} {'Before':<30} {'After':<25} Status"
        )
        self.stdout.write("-" * 155)
        for company, title, before, after, status in rows:
            self.stdout.write(
                f"{company[:39]:<40} {title[:44]:<45} {before[:29]:<30} {after[:24]:<25} {status}"
            )

        self.stdout.write(
            f"\nDone. updated={updated}  unchanged={unchanged}  failed={failed}  total={total}"
            + (" (dry run)" if dry_run else "")
        )
