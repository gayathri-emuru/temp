from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from core.services.linkedin_company_from_job_service import REQUEST_TIMEOUT_SECONDS
from core.utils import compact_spaces, normalize_generic_url, safe_str


@dataclass(frozen=True)
class LinkedInJobDetails:
    job_url: str
    final_url: str
    status_code: int
    page_html: str
    external_job_id: str
    title: str
    company: str
    location: str
    description_text: str
    apply_url: str
    recruiter_name: str = ""
    recruiter_title: str = ""
    recruiter_linkedin: str = ""


_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)", flags=re.IGNORECASE)


def _strip_tags(text: str) -> str:
    text = safe_str(text)
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ")
    return compact_spaces(text)


def _extract_tag_text_by_class(chunk: str, *, tag: str, class_name: str) -> str:
    pattern = re.compile(
        rf"<{re.escape(tag)}\b(?=[^>]*\bclass\s*=\s*[\"'][^\"']*{re.escape(class_name)}[^\"']*[\"'])[^>]*>(.*?)</{re.escape(tag)}>",
        flags=re.I | re.S,
    )
    match = pattern.search(safe_str(chunk))
    if not match:
        return ""
    return _strip_tags(match.group(1))


def extract_job_poster_from_html(page_html: str) -> dict:
    """
    Extract LinkedIn's public "Direct message the job poster" card when present.

    LinkedIn does not include this for every job, and the logged-in "Meet the hiring
    team" UI is not always in public HTML. This targets the public card shape.
    """
    page_html = safe_str(page_html)
    if not page_html:
        return {}

    idx = page_html.lower().find("message-the-recruiter")
    if idx < 0:
        return {}

    chunk = page_html[idx : idx + 8000]
    profile_url = ""
    href_match = re.search(
        r"href\s*=\s*[\"'](https?://(?:www\.)?linkedin\.com/in/[^\"'<>]+)[\"']",
        chunk,
        flags=re.I,
    )
    if href_match:
        profile_url = normalize_generic_url(href_match.group(1))

    name = _extract_tag_text_by_class(chunk, tag="h3", class_name="base-main-card__title")
    if not name:
        name = _extract_tag_text_by_class(chunk, tag="span", class_name="sr-only")

    title = _extract_tag_text_by_class(chunk, tag="h4", class_name="base-main-card__subtitle")

    if not name:
        return {}

    return {
        "name": name[:255],
        "title": title[:255],
        "linkedin": profile_url[:1000],
    }


def _extract_job_id_from_url(url: str) -> str:
    url = safe_str(url)
    if not url:
        return ""
    match = _JOB_ID_RE.search(url)
    if not match:
        return ""
    return safe_str(match.group(1))


def _is_linkedin_job_url(url: str) -> bool:
    url = safe_str(url)
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().strip(".")
        if not host:
            return False
        if host not in {"linkedin.com", "www.linkedin.com"} and not host.endswith(".linkedin.com"):
            return False
        return bool(_JOB_ID_RE.search(parsed.path or ""))
    except Exception:
        return False


def _extract_json_ld_blocks(page_html: str) -> list[dict]:
    page_html = safe_str(page_html)
    if not page_html:
        return []

    blocks: list[dict] = []

    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page_html,
        flags=re.I | re.S,
    ):
        raw = safe_str(match.group(1)).strip()
        if not raw:
            continue

        raw = raw.strip()
        # Some pages include HTML comments around JSON.
        raw = re.sub(r"^\s*<!--", "", raw)
        raw = re.sub(r"-->\s*$", "", raw)

        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data, dict):
            blocks.append(data)
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    blocks.append(item)

    return blocks


def _flatten_graph(obj: dict) -> list[dict]:
    if not isinstance(obj, dict):
        return []
    graph = obj.get("@graph")
    if isinstance(graph, list):
        return [x for x in graph if isinstance(x, dict)]
    return [obj]


def _find_job_posting_json_ld(page_html: str) -> dict:
    for block in _extract_json_ld_blocks(page_html):
        for obj in _flatten_graph(block):
            type_value = obj.get("@type")
            if isinstance(type_value, str) and type_value.lower() == "jobposting":
                return obj
            if isinstance(type_value, list) and any(str(t).lower() == "jobposting" for t in type_value):
                return obj
    return {}


def _extract_location_from_jobposting(obj: dict) -> str:
    job_location = obj.get("jobLocation")
    if isinstance(job_location, dict):
        job_location = [job_location]
    if not isinstance(job_location, list) or not job_location:
        return ""

    first = job_location[0] if isinstance(job_location[0], dict) else {}
    address = first.get("address") if isinstance(first.get("address"), dict) else {}
    region = safe_str(address.get("addressRegion"))
    locality = safe_str(address.get("addressLocality"))
    country = safe_str(address.get("addressCountry"))

    # Prefer state/region (matches our state-only normalization downstream).
    if region:
        return region

    if locality and country:
        return f"{locality}, {country}"
    if locality:
        return locality
    return ""


def _extract_description_from_html(page_html: str) -> str:
    """
    Extract the full job description from LinkedIn's HTML containers.
    LinkedIn embeds the full description in a div with class show-more-less-html__markup,
    which is inside description__text — both present on public job pages.
    """
    page_html = safe_str(page_html)
    if not page_html:
        return ""

    # Primary: div.show-more-less-html__markup contains the full formatted description
    for marker in ("show-more-less-html__markup", "description__text--rich"):
        idx = page_html.find(marker)
        if idx < 0:
            continue
        div_start = page_html.rfind("<div", 0, idx)
        if div_start < 0:
            continue
        # Find closing </div> — grab a generous chunk and strip tags
        chunk = page_html[div_start : div_start + 20000]
        # Skip past the opening tag to get the inner HTML
        tag_end = chunk.find(">")
        if tag_end < 0:
            continue
        inner = chunk[tag_end + 1 : tag_end + 15000]
        text = compact_spaces(_strip_tags(inner))
        if text and len(text) > 50:
            return text

    return ""


def _extract_apply_url_from_html(page_html: str) -> str:
    """
    Best-effort extraction. LinkedIn frequently hides apply URLs behind auth/JS, so this may be blank.
    """
    page_html = safe_str(page_html)
    if not page_html:
        return ""

    patterns = [
        r"\"applyUrl\"\s*:\s*\"(https?:\\/\\/[^\"\\]+)\"",
        r"\"companyApplyUrl\"\s*:\s*\"(https?:\\/\\/[^\"\\]+)\"",
        r"\"offsiteApplyUrl\"\s*:\s*\"(https?:\\/\\/[^\"\\]+)\"",
    ]
    for pat in patterns:
        match = re.search(pat, page_html, flags=re.I)
        if not match:
            continue
        raw = safe_str(match.group(1))
        raw = html.unescape(raw)
        raw = raw.replace("\\u002F", "/").replace("\\/", "/")
        raw = raw.replace("\\u003A", ":")
        return raw
    return ""


def _extract_meta_content(page_html: str, *, attr: str, name: str) -> str:
    page_html = safe_str(page_html)
    if not page_html:
        return ""
    # Matches: <meta property="og:title" content="..."> or <meta name="description" content="...">
    pattern = re.compile(
        rf"<meta[^>]+{attr}\s*=\s*[\"']{re.escape(name)}[\"'][^>]+content\s*=\s*[\"']([^\"']+)[\"']",
        flags=re.I,
    )
    match = pattern.search(page_html)
    if not match:
        return ""
    return compact_spaces(html.unescape(safe_str(match.group(1))))


def _extract_title_tag(page_html: str) -> str:
    page_html = safe_str(page_html)
    if not page_html:
        return ""
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.I | re.S)
    if not match:
        return ""
    return compact_spaces(_strip_tags(match.group(1)))


def _extract_json_string_value(page_html: str, key: str) -> str:
    """
    Best-effort: extracts a JSON string value for a given key from the HTML.
    """
    page_html = safe_str(page_html)
    key = safe_str(key)
    if not page_html or not key:
        return ""

    pattern = re.compile(rf"\"{re.escape(key)}\"\s*:\s*\"((?:\\.|[^\"\\])*)\"", flags=re.I)
    match = pattern.search(page_html)
    if not match:
        return ""

    raw = safe_str(match.group(1))
    try:
        decoded = json.loads(f"\"{raw}\"")
    except Exception:
        return ""

    return safe_str(decoded)


def _extract_company_from_inline_json(page_html: str) -> str:
    """Extract company name from LinkedIn's various inline JSON patterns."""
    page_html = safe_str(page_html)
    if not page_html:
        return ""

    for key in (
        "companyName",
        "hiringOrganizationName",
        "companyDisplayName",
        "jobCompanyName",
        "company_name",
    ):
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', flags=re.I)
        match = pattern.search(page_html)
        if not match:
            continue
        raw = safe_str(match.group(1))
        try:
            decoded = json.loads(f'"{raw}"')
        except Exception:
            decoded = raw
        decoded = compact_spaces(html.unescape(safe_str(decoded)))
        if decoded and decoded.lower() not in {"", "null", "none"}:
            return decoded

    return ""


def _extract_location_from_inline_json(page_html: str) -> str:
    """Extract job location from LinkedIn's inline JSON blobs.

    LinkedIn omits jobLocation from JSON-LD on unauthenticated pages but still
    embeds formattedLocation (and similar keys) in their inline JS data for rendering.
    """
    page_html = safe_str(page_html)
    if not page_html:
        return ""

    for key in (
        "formattedLocation",
        "jobLocation",
        "location",
        "geoRegion",
        "locationDescription",
        "jobLocationText",
    ):
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', flags=re.I)
        match = pattern.search(page_html)
        if not match:
            continue
        raw = safe_str(match.group(1))
        try:
            decoded = json.loads(f'"{raw}"')
        except Exception:
            decoded = raw
        decoded = compact_spaces(html.unescape(safe_str(decoded)))
        if decoded and decoded.lower() not in {"", "null", "none"}:
            return decoded

    return ""


def _parse_title_company_from_text(text: str) -> tuple[str, str, str]:
    """Given a cleaned title string, attempt to split into (job_title, company, location)."""
    text = re.sub(r"\s*\|\s*LinkedIn\s*$", "", text, flags=re.I).strip()
    text = re.sub(r"\s*-\s*LinkedIn\s*$", "", text, flags=re.I).strip()

    # "Company hiring Title in Location" — LinkedIn's <title> tag format
    m = re.match(r"^(.+?)\s+hiring\s+(.+?)\s+in\s+(.+)$", text, flags=re.I)
    if m and m.group(1).strip() and m.group(2).strip():
        return m.group(2).strip(), m.group(1).strip(), m.group(3).strip()

    # "Company hiring Title" (no location)
    m = re.match(r"^(.+?)\s+hiring\s+(.+)$", text, flags=re.I)
    if m and m.group(1).strip() and m.group(2).strip():
        return m.group(2).strip(), m.group(1).strip(), ""

    # "Job Title at Company"
    if " at " in text.lower():
        parts = re.split(r"\s+at\s+", text, maxsplit=1, flags=re.I)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip(), ""

    # "Job Title - Company"
    if " - " in text:
        parts = text.split(" - ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip(), ""

    # "Job Title | Company"
    if " | " in text:
        parts = text.split(" | ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip(), ""

    return text.strip(), "", ""


def _fallback_extract_title_company_description(page_html: str) -> tuple[str, str, str, str]:
    title = ""
    company = ""
    location = ""
    description_text = ""

    # 1. Try og:title first (most reliable when present)
    og_title = _extract_meta_content(page_html, attr="property", name="og:title")
    if og_title:
        title, company, location = _parse_title_company_from_text(og_title)

    # 2. Try <title> tag if we still need title or company or location
    if not title or not company:
        raw_title_tag = _extract_title_tag(page_html)
        t, c, loc = _parse_title_company_from_text(raw_title_tag)
        # Only use sign-in pages as a last resort — skip if it looks like a login wall
        if t and "sign" not in t.lower() and "log in" not in t.lower():
            title = title or t
            company = company or c
            location = location or loc

    # 3. Try inline JSON blobs LinkedIn embeds in the page
    if not company:
        company = _extract_company_from_inline_json(page_html)

    # 4. Inline JSON location (formattedLocation etc.) — most reliable non-JSON-LD source
    if not location:
        location = _extract_location_from_inline_json(page_html)

    # 5. Try og:site_name or twitter card as last-ditch company source
    if not company:
        site_name = _extract_meta_content(page_html, attr="property", name="og:site_name")
        if site_name and "linkedin" not in site_name.lower():
            company = site_name

    # Description priority:
    # 1. LinkedIn's HTML description container (full text, most reliable on public pages)
    # 2. JSON string keys embedded in inline scripts
    # 3. Meta description (truncated fallback)
    description_text = _extract_description_from_html(page_html)

    if not description_text:
        for key in ("descriptionText", "description", "jobDescription", "description_text", "jobpostingoperationtext"):
            raw = _extract_json_string_value(page_html, key)
            if raw:
                candidate = compact_spaces(_strip_tags(raw))
                if candidate and len(candidate) > 30:
                    description_text = candidate
                    break

    if not description_text:
        meta_desc = _extract_meta_content(page_html, attr="name", name="description") or _extract_meta_content(
            page_html, attr="property", name="og:description"
        )
        if meta_desc:
            description_text = meta_desc

    return title, company, location, description_text


def _fetch_job_page(job_url: str) -> tuple[int, str, str]:
    """
    Fetch a LinkedIn job page using curl_cffi to impersonate Chrome's TLS fingerprint,
    bypassing LinkedIn's bot detection (status 999). Falls back to plain requests if
    curl_cffi is unavailable.
    Returns (status_code, final_url, page_html).
    """
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore

        resp = cffi_requests.get(
            job_url,
            impersonate="chrome120",
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        return resp.status_code, safe_str(getattr(resp, "url", "")) or job_url, safe_str(resp.text)
    except ImportError:
        import requests as _requests

        resp = _requests.get(job_url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        return resp.status_code, safe_str(getattr(resp, "url", "")) or job_url, safe_str(resp.text)


def fetch_linkedin_job_details(job_url: str) -> LinkedInJobDetails:
    job_url = safe_str(job_url)
    if not job_url:
        raise RuntimeError("Empty job_url")

    if not _is_linkedin_job_url(job_url):
        raise RuntimeError(f"Not a LinkedIn jobs/view URL: {job_url}")

    status_code, final_url, page_html = _fetch_job_page(job_url)

    if not _is_linkedin_job_url(final_url):
        parsed = urlparse(final_url)
        raise RuntimeError(f"Redirected to non-job URL. status={status_code} final_host={parsed.netloc} final_path={parsed.path}")

    external_job_id = _extract_job_id_from_url(final_url) or _extract_job_id_from_url(job_url)

    if status_code != 200:
        if status_code in {429, 999, 403}:
            raise RuntimeError(
                f"LinkedIn blocked/limited the request. status={status_code} (try later or use Apify/import via other source)."
            )
        raise RuntimeError(f"LinkedIn returned non-200. status={status_code}")

    jobposting = _find_job_posting_json_ld(page_html)

    title = html.unescape(safe_str(jobposting.get("title")))

    hiring_org = jobposting.get("hiringOrganization")
    if isinstance(hiring_org, dict):
        company = html.unescape(safe_str(hiring_org.get("name")))
    else:
        company = html.unescape(safe_str(hiring_org))

    description_html = safe_str(jobposting.get("description"))
    description_text = _strip_tags(description_html)

    location = _extract_location_from_jobposting(jobposting)
    # Fallback: check inline JSON if JSON-LD jobLocation was absent
    if not location:
        location = _extract_location_from_inline_json(page_html)

    apply_url = safe_str(jobposting.get("url")) or ""
    if not apply_url:
        apply_url = _extract_apply_url_from_html(page_html)

    if not apply_url:
        apply_url = final_url

    if not title or not company or not description_text:
        fb_title, fb_company, fb_location, fb_desc = _fallback_extract_title_company_description(page_html)
        title = title or fb_title
        company = company or fb_company
        location = location or fb_location
        description_text = description_text or fb_desc

    if not title:
        raise RuntimeError("Could not extract job title from page (missing JSON-LD JobPosting.title)")
    if not company:
        raise RuntimeError("Could not extract company name from page (missing JSON-LD JobPosting.hiringOrganization.name)")
    if not description_text:
        # Use the title as a minimal description rather than hard-failing — the job can
        # still be processed and the AI email generator will have to work with less context.
        description_text = f"{title} at {company}"

    job_poster = extract_job_poster_from_html(page_html)

    return LinkedInJobDetails(
        job_url=job_url,
        final_url=final_url,
        status_code=status_code,
        page_html=page_html,
        external_job_id=external_job_id,
        title=title,
        company=company,
        location=location,
        description_text=description_text,
        apply_url=apply_url,
        recruiter_name=safe_str(job_poster.get("name")),
        recruiter_title=safe_str(job_poster.get("title")),
        recruiter_linkedin=safe_str(job_poster.get("linkedin")),
    )
