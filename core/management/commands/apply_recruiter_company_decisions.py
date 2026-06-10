from pathlib import Path

from django.core.management.base import BaseCommand

from core.services.recruiter_decision_apply_service import apply_recruiter_decisions


class Command(BaseCommand):
    help = "Apply decisions from unresolved recruiter-company review JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--decision-file",
            type=str,
            required=True,
            help="Path to the decision JSON file.",
        )

    def handle(self, *args, **options):
        decision_file = Path(options["decision_file"])
        if not decision_file.exists():
            raise FileNotFoundError(f"Decision file not found: {decision_file}")

        result = apply_recruiter_decisions(str(decision_file))

        self.stdout.write(self.style.SUCCESS("Recruiter decisions applied."))
        self.stdout.write("")

        self.stdout.write("Stats:")
        for key, value in result["stats"].items():
            self.stdout.write(f"  {key}: {value}")

        if result["errors"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Errors:"))
            for item in result["errors"]:
                self.stdout.write(f"  {item['error']}")
