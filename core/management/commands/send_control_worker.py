import os

from django.core.management.base import BaseCommand

from core.services.send_run_service import run_send_initial_for_batch


class Command(BaseCommand):
    help = "Run Send Control email sending in a separate worker process."

    def add_arguments(self, parser):
        parser.add_argument("--batch-date", required=True, help="Batch date in YYYY-MM-DD format.")

    def handle(self, *args, **options):
        batch_date = options["batch_date"]
        result = run_send_initial_for_batch(
            batch_date_str=batch_date,
            send_type="real",
            allow_recipient_discovery=False,
            skip_pending_recipients=True,
            source_label=f"Send control send-only worker_pid={os.getpid()}",
        )
        totals = result.get("totals", {})
        try:
            self.stdout.write(
                self.style.SUCCESS(
                    "Send worker finished. "
                    f"sent={totals.get('emails_sent', 0)} failed={totals.get('emails_failed', 0)} "
                    f"attempted={totals.get('emails_attempted', 0)}"
                )
            )
        except OSError:
            pass
