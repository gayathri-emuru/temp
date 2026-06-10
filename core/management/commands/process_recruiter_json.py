from pathlib import Path

from django.core.management.base import BaseCommand

from core.services.recruiter_import_service import process_recruiter_json_text


class Command(BaseCommand):
    help = "Process pasted recruiter JSON blocks, auto-match safe companies, and generate review files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--input-file",
            type=str,
            required=True,
            help="Path to text file containing one or more JSON company blocks.",
        )

    def handle(self, *args, **options):
        input_file = Path(options["input_file"])
        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        raw_text = input_file.read_text(encoding="utf-8")
        result = process_recruiter_json_text(raw_text)

        self.stdout.write(self.style.SUCCESS("Recruiter JSON processed successfully."))
        self.stdout.write("")

        self.stdout.write("Summary:")
        for key, value in result["summary"].items():
            self.stdout.write(f"  {key}: {value}")

        self.stdout.write("")
        self.stdout.write("Generated files:")
        for key, value in result["files"].items():
            self.stdout.write(f"  {key}: {value}")
