from datetime import timedelta

from core.models import JobPosting
from core.utils import safe_str
from urllib.parse import urlparse


def find_duplicate_reason(
    normalized_linkedin_url: str,
    normalized_apply_url: str,
    canonical_company: str,
    canonical_title: str,
    canonical_location: str,
):
    if normalized_linkedin_url:
        # Backward-compat: older rows might have stored different host, but same /jobs/view/{id} path.
        try:
            parsed = urlparse(safe_str(normalized_linkedin_url))
            path = safe_str(parsed.path).rstrip("/")
            if path and JobPosting.objects.filter(normalized_linkedin_url__endswith=path).exists():
                return "normalized_linkedin_url_path"
        except Exception:
            pass

        if JobPosting.objects.filter(normalized_linkedin_url=normalized_linkedin_url).exists():
            return "normalized_linkedin_url"

    if normalized_apply_url:
        if JobPosting.objects.filter(normalized_apply_url=normalized_apply_url).exists():
            return "normalized_apply_url"

    # Allow dedupe even when location is missing (some sources omit it).
    if canonical_company and canonical_title:
        qs = JobPosting.objects.filter(
            canonical_company=canonical_company,
            canonical_title=canonical_title,
        )
        if canonical_location:
            qs = qs.filter(canonical_location=canonical_location)
            if qs.exists():
                return "canonical_company_title_state"
        else:
            if qs.exists():
                return "canonical_company_title"

    return ""


def job_exists_by_dedupe_signals(
    normalized_linkedin_url: str,
    normalized_apply_url: str,
    canonical_company: str,
    canonical_title: str,
    canonical_location: str,
) -> bool:
    return bool(
        find_duplicate_reason(
            normalized_linkedin_url=normalized_linkedin_url,
            normalized_apply_url=normalized_apply_url,
            canonical_company=canonical_company,
            canonical_title=canonical_title,
            canonical_location=canonical_location,
        )
    )


def get_prior_7d_company_roles(normalized_company: str, current_batch_date):
    start_date = current_batch_date - timedelta(days=7)

    jobs = (
        JobPosting.objects
        .filter(
            normalized_company=normalized_company,
            daily_batch__batch_date__gte=start_date,
            daily_batch__batch_date__lt=current_batch_date,
        )
        .select_related("daily_batch")
        .order_by("-daily_batch__batch_date", "sort_title")
    )

    output = []
    seen = set()

    for job in jobs:
        key = (job.title, job.location, job.daily_batch.batch_date.isoformat())
        if key in seen:
            continue
        seen.add(key)

        output.append(
            {
                "batch_date": job.daily_batch.batch_date.isoformat(),
                "title": job.title,
                "location": job.location,
            }
        )

    return output
