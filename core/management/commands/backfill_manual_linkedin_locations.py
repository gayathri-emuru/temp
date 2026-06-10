"""
Management command: backfill_manual_linkedin_locations

Re-scrapes every manually imported LinkedIn job that has no location (or --all to
re-scrape everything), then updates the four location columns:
  location, normalized_location, canonical_location, sort_location
"""
import time

from django.core.management.base import BaseCommand

from core.models import JobPosting
from core.services.linkedin_job_scrape_service import fetch_linkedin_job_details
from core.services.normalization_service import (
    build_sort_location,
    canonical_location,
    normalize_location,
)
from core.utils import safe_str

DELAY_BETWEEN_REQUESTS = 2.5  # seconds — be gentle with LinkedIn


class Command(BaseCommand):
    help = "Backfill location fields for manually imported LinkedIn jobs by re-scraping each job URL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_jobs",
            help="Re-scrape all manual jobs, not just those with a blank location.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be updated without writing to DB.",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=DELAY_BETWEEN_REQUESTS,
            help=f"Seconds to wait between requests (default: {DELAY_BETWEEN_REQUESTS}).",
        )

    def handle(self, *args, **options):
        all_jobs = options["all_jobs"]
        dry_run = options["dry_run"]
        delay = options["delay"]

        qs = JobPosting.objects.filter(is_manual_import=True).exclude(linkedin_url="")
        if not all_jobs:
            qs = qs.filter(location="")

        total = qs.count()
        self.stdout.write(
            f"Found {total} manual LinkedIn job(s) to process "
            f"({'all' if all_jobs else 'blank-location only'}, dry_run={dry_run})."
        )

        updated = 0
        skipped = 0
        errored = 0

        for i, job in enumerate(qs.iterator(), start=1):
            self.stdout.write(f"[{i}/{total}] Job {job.id} — {job.company} | {job.title} | current_location={job.location!r}")

            try:
                details = fetch_linkedin_job_details(job.linkedin_url)
                new_location = safe_str(details.location).strip()
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  ERROR scraping {job.linkedin_url}: {exc}"))
                errored += 1
                time.sleep(delay)
                continue

            if not new_location:
                self.stdout.write(self.style.WARNING("  No location found on page — skipping."))
                skipped += 1
                time.sleep(delay)
                continue

            if new_location == job.location:
                self.stdout.write(f"  Location unchanged: {new_location!r} — skipping.")
                skipped += 1
                time.sleep(delay)
                continue

            self.stdout.write(self.style.SUCCESS(f"  Location: {job.location!r} -> {new_location!r}"))

            if not dry_run:
                job.location = new_location
                job.normalized_location = normalize_location(new_location)
                job.canonical_location = canonical_location(new_location)
                job.sort_location = build_sort_location(new_location)
                job.save(update_fields=[
                    "location",
                    "normalized_location",
                    "canonical_location",
                    "sort_location",
                    "updated_at",
                ])

            updated += 1
            time.sleep(delay)

        self.stdout.write(
            f"\nDone. updated={updated}  skipped={skipped}  errored={errored}  total={total}"
            + (" (dry run — no DB writes)" if dry_run else "")
        )
