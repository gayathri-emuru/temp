import re

from django.core.management.base import BaseCommand, CommandError

from core.models import JobPosting
from core.services.manual_job_email_service import _jobs_for_token, run_manual_job_email_generation_for_token
from core.utils import safe_str


TOKEN_RE = re.compile(r"^manual-job-email-(?P<token>[a-f0-9]+)-\d+$", re.I)


def _latest_manual_job_token() -> str:
    latest_job = JobPosting.objects.filter(is_manual_email_job=True).order_by("-created_at", "-id").first()
    if not latest_job:
        return ""

    match = TOKEN_RE.match(safe_str(latest_job.external_job_id))
    if not match:
        return ""
    return match.group("token")


class Command(BaseCommand):
    help = "Regenerate generated subject/body drafts for the latest manual job email review batch."

    def add_arguments(self, parser):
        parser.add_argument("--token", default="", help="Manual job review token. Defaults to latest manual batch.")

    def handle(self, *args, **options):
        token = safe_str(options.get("token")).strip() or _latest_manual_job_token()
        if not token:
            raise CommandError("No manual job review token found.")

        jobs = list(_jobs_for_token(token))
        if not jobs:
            raise CommandError(f"No manual job review jobs found for token={token}.")

        self.stdout.write(f"Regenerating token={token} jobs={len(jobs)}")
        result = run_manual_job_email_generation_for_token(token=token, skip_existing=False)
        for row in result.get("rows") or []:
            self.stdout.write(
                "job_id={job_id} status={status} detail={detail}".format(
                    job_id=row.get("job_id"),
                    status=row.get("status"),
                    detail=safe_str(row.get("detail"))[:120],
                )
            )

        self.stdout.write(f"Summary: {result.get('totals')}")
