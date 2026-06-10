from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.services.external_job_import_service import run_external_job_url_import


class Command(BaseCommand):
    help = "Scrape and optionally import external job URLs such as iCIMS job pages. Defaults to dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--url", action="append", default=[], help="External job URL. Can be repeated.")
        parser.add_argument("--file", default="", help="Text file containing external job URLs.")
        parser.add_argument("--commit", action="store_true", help="Create/update JobPosting rows. Without this, runs as dry-run.")
        parser.add_argument("--use-openai-filter", action="store_true", help="Run the APPLY/REJECT job filter before import.")
        parser.add_argument("--force-refetch", action="store_true", help="Update existing jobs that match by URL or external ID.")
        parser.add_argument("--skip-cooldown", action="store_true", help="Ignore company cooldown checks.")
        parser.add_argument("--include-blocked-companies", action="store_true", help="Do not skip companies marked blocked.")
        parser.add_argument("--cooldown-days", type=int, default=None, help="Override configured company cooldown days.")

    def handle(self, *args, **options):
        chunks = list(options.get("url") or [])
        file_path = options.get("file") or ""
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    chunks.append(handle.read())
            except OSError as exc:
                raise CommandError(f"Could not read --file: {exc}") from exc

        raw_urls_text = "\n".join(chunks)
        if not raw_urls_text.strip():
            raise CommandError("Provide at least one --url or --file.")

        result = run_external_job_url_import(
            raw_urls_text=raw_urls_text,
            cooldown_days=options.get("cooldown_days"),
            apply_cooldown_filters=not bool(options.get("skip_cooldown")),
            skip_blocked_companies=not bool(options.get("include_blocked_companies")),
            use_openai_filter=bool(options.get("use_openai_filter")),
            dry_run=not bool(options.get("commit")),
            force_refetch=bool(options.get("force_refetch")),
        )
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
