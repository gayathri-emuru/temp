from django.core.management.base import BaseCommand

from core.constants import DEFAULT_APIFY_ACTOR_ID, DEFAULT_FETCH_LIMIT, DEFAULT_LOOKBACK_HOURS
from core.services.import_pipeline_service import run_import_pipeline


class Command(BaseCommand):
    help = "Run Apify -> OpenAI filter -> normalization -> dedupe -> store APPLY jobs pipeline"

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=DEFAULT_LOOKBACK_HOURS,
            help="How many hours back to fetch jobs from Apify.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_FETCH_LIMIT,
            help="Max jobs to request from Apify.",
        )
        parser.add_argument(
            "--actor-id",
            type=str,
            default=DEFAULT_APIFY_ACTOR_ID,
            help="Apify actor ID.",
        )

    def handle(self, *args, **options):
        stats = run_import_pipeline(
            lookback_hours=options["lookback_hours"],
            max_jobs=options["limit"],
            actor_id=options["actor_id"],
        )

        if stats.get("ok"):
            self.stdout.write(self.style.SUCCESS("Import pipeline completed successfully."))
        else:
            self.stderr.write(self.style.ERROR(f"Import pipeline failed: {stats.get('error')}"))
            raise SystemExit(1)

        for key, value in stats.items():
            self.stdout.write(f"{key}: {value}")
