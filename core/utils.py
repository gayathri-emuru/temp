import re
from urllib.parse import urlparse, urlunparse
from urllib.parse import parse_qs


def safe_str(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    if text.lower() in {"", "none", "null", "nan", "[]"}:
        return default
    return text


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", safe_str(value)).strip()


def _ensure_scheme(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""

    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        return url

    if "." in url and " " not in url:
        return f"https://{url}"

    return url


def normalize_linkedin_job_url(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""

    try:
        url = _ensure_scheme(url)
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url.rstrip("/")

        host = (parsed.netloc or "").lower().strip(".")
        is_linkedin_host = host in {"linkedin.com", "www.linkedin.com"} or host.endswith(".linkedin.com")

        # LinkedIn URLs often come from search/collections pages with the job id in query params.
        # Convert anything with an identifiable job id into the canonical jobs/view/{id} form.
        if is_linkedin_host:
            path = safe_str(parsed.path)
            match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", path, flags=re.I)
            if match:
                job_id = safe_str(match.group(1))
                if job_id.isdigit():
                    return f"https://www.linkedin.com/jobs/view/{job_id}/"

            qs = parse_qs(parsed.query or "")
            for key in ("currentJobId", "jobId", "currentjobid", "jobid"):
                values = qs.get(key) or []
                if not values:
                    continue
                job_id = safe_str(values[0])
                if job_id.isdigit():
                    return f"https://www.linkedin.com/jobs/view/{job_id}/"

        cleaned = parsed._replace(query="", fragment="")
        normalized = urlunparse(cleaned).rstrip("/")
        reparsed = urlparse(normalized)

        return f"{reparsed.scheme.lower()}://{reparsed.netloc.lower()}{reparsed.path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def normalize_generic_url(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""

    try:
        url = _ensure_scheme(url)
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url.rstrip("/")

        cleaned = parsed._replace(query="", fragment="")
        normalized = urlunparse(cleaned).rstrip("/")
        reparsed = urlparse(normalized)

        return f"{reparsed.scheme.lower()}://{reparsed.netloc.lower()}{reparsed.path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def normalize_job_or_generic_url(url: str) -> str:
    """
    Normalizes job URL fields.

    LinkedIn jobs/view URLs are always stored in canonical numeric form:
      https://www.linkedin.com/jobs/view/<job_id>/

    Non-LinkedIn URLs keep the generic cleaned form.
    """
    text = safe_str(url)
    if not text:
        return ""

    try:
        ensured = _ensure_scheme(text)
        parsed = urlparse(ensured)
        host = (parsed.netloc or "").lower().strip(".")
        is_linkedin_host = host in {"linkedin.com", "www.linkedin.com"} or host.endswith(".linkedin.com")
        if is_linkedin_host:
            path = safe_str(parsed.path).lower()
            query = safe_str(parsed.query).lower()
            if "/jobs/view/" in path or "currentjobid=" in query or "jobid=" in query:
                return normalize_linkedin_job_url(ensured)
    except Exception:
        pass

    return normalize_generic_url(text)


def extract_domain_from_url_or_host(value: str) -> str:
    value = safe_str(value).lower()
    if not value:
        return ""

    try:
        value = _ensure_scheme(value)
        parsed = urlparse(value)

        host = parsed.netloc or parsed.path
        host = host.strip().lower()

        if "@" in host:
            host = host.split("@", 1)[1]

        if "/" in host:
            host = host.split("/", 1)[0]

        if ":" in host:
            host = host.split(":", 1)[0]

        host = re.sub(r"^www\.", "", host).strip().rstrip(".")

        if not host or "." not in host:
            return ""

        return host
    except Exception:
        return ""
