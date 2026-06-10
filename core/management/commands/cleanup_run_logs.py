from pathlib import Path
from time import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Delete old file-based run logs from media/run_logs. "
        "This never deletes SentEmailLog database records."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Keep logs from the last N days. Default: 7.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without deleting files.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        if days < 1:
            raise CommandError("--days must be 1 or greater.")

        log_root = (Path(settings.MEDIA_ROOT) / "run_logs").resolve()
        if not log_root.exists():
            self.stdout.write(f"No run log directory found: {log_root}")
            return
        if not log_root.is_dir():
            raise CommandError(f"Run log path is not a directory: {log_root}")

        cutoff = time() - (days * 24 * 60 * 60)
        candidates = []

        for path in log_root.glob("*.log"):
            resolved = path.resolve()
            if log_root not in resolved.parents:
                raise CommandError(f"Refusing to touch path outside run_logs: {resolved}")
            if not resolved.is_file():
                continue
            if resolved.stat().st_mtime < cutoff:
                candidates.append(resolved)

        deleted_count = 0
        deleted_bytes = 0

        for path in sorted(candidates):
            size = path.stat().st_size
            if dry_run:
                self.stdout.write(f"Would delete: {path}")
                deleted_count += 1
                deleted_bytes += size
                continue
            path.unlink()
            deleted_count += 1
            deleted_bytes += size
            self.stdout.write(f"Deleted: {path}")

        action = "would delete" if dry_run else "deleted"
        self.stdout.write(
            self.style.SUCCESS(
                f"Run log cleanup complete: {action} {deleted_count} .log file(s), "
                f"{deleted_bytes} byte(s), keeping last {days} day(s). "
                "SentEmailLog database records were not touched."
            )
        )
