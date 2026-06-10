from django.core.management.base import BaseCommand

from core.services.job_filter_review_service import run_job_filter_review_for_batch


class Command(BaseCommand):
    help = "Run the OpenAI APPLY/REJECT filter for a batch and create review proposals."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="DailyBatch date in YYYY-MM-DD format.")
        parser.add_argument("--limit", type=int, default=0, help="Optional max jobs to classify.")

    def handle(self, *args, **options):
        result = run_job_filter_review_for_batch(
            batch_date=options["date"],
            limit=int(options.get("limit") or 0) or None,
        )
        self.stdout.write(str(result))
