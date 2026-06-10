from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from core.utils import safe_str


LINKEDIN_POST_TIMEOUT_SECONDS = 25
LINKEDIN_PROFILE_TIMEOUT_SECONDS = 12


def _clean_linkedin_url(url: str) -> str:
    value = safe_str(url).strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{value}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _absolute_clean_url(href: str) -> str:
    href = safe_str(href).strip()
    if not href:
        return ""
    return _clean_linkedin_url(urljoin("https://www.linkedin.com", href))


def _attrs(raw: str) -> dict:
    out = {}
    for match in re.finditer(r"""([:\w-]+)\s*=\s*(['"])(.*?)\2""", safe_str(raw), flags=re.DOTALL):
        out[match.group(1).lower()] = unescape(match.group(3)).strip()
    return out


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", "\n", safe_str(value))
    value = re.sub(r"(?s)<!--.*?-->", "\n", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?is)</(?:p|div|li|h\d|section|article|tr)>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = unescape(value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _page_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return _strip_html(match.group(1)) if match else ""


def _meta_content(html: str, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for match in re.finditer(r"(?is)<meta\b([^>]*)>", html):
        attrs = _attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = safe_str(attrs.get("content")).strip()
        if key in wanted and content:
            return content
    return ""


def _first_anchor(html: str, predicate) -> tuple[str, str]:
    for match in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", html):
        attrs = _attrs(match.group(1))
        label = _strip_html(match.group(2))
        href = safe_str(attrs.get("href")).strip()
        if predicate(label, href, attrs):
            return label, _absolute_clean_url(href)
    return "", ""


def _looks_like_person_label(label: str) -> bool:
    label = re.sub(r"\s+", " ", safe_str(label)).strip()
    if not label or len(label) > 120:
        return False
    lowered = label.lower()
    return not any(
        token in lowered
        for token in (
            "linkedin",
            "sign in",
            "join now",
            "view profile",
            "followers",
            "connections",
            "like",
            "comment",
            "share",
        )
    )


def _first_person_anchor(html: str) -> tuple[str, str]:
    actor_name, actor_url = _first_anchor(
        html,
        lambda label, href, attrs: bool(
            _looks_like_person_label(label)
            and "/in/" in href
            and (
                "public_post_feed-actor-name" in " ".join(str(value).lower() for value in attrs.values())
                or "feed-actor" in " ".join(str(value).lower() for value in attrs.values())
                or "main-feed-card__actor" in " ".join(str(value).lower() for value in attrs.values())
            )
        ),
    )
    if actor_name:
        return actor_name, actor_url

    return _first_anchor(
        html,
        lambda label, href, attrs: bool(_looks_like_person_label(label) and "/in/" in href),
    )


def _company_from_headline(text: str) -> str:
    text = re.sub(r"\s+", " ", safe_str(text)).strip()
    if not text:
        return ""
    match = re.search(r"\b(?:at|with)\s+([A-Z][A-Za-z0-9&.,' -]{2,80})", text)
    if not match:
        return ""
    return safe_str(match.group(1)).strip(" .,-")


def _preview_linkedin_profile(profile_url: str) -> dict:
    profile_url = safe_str(profile_url).strip()
    if not profile_url:
        return {}
    try:
        response = requests.get(
            profile_url,
            timeout=LINKEDIN_PROFILE_TIMEOUT_SECONDS,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        response.raise_for_status()
    except Exception as exc:
        return {"profile_error": str(exc)[:500]}

    html = response.text
    title = _meta_content(html, "og:title", "twitter:title") or _page_title(html)
    description = _meta_content(html, "og:description", "twitter:description", "description")
    canonical_url = _meta_content(html, "og:url", "twitter:url", "lnkd:url") or response.url
    profile_name = ""
    if title:
        profile_name = re.split(r"\s+[|\-]\s+LinkedIn\b", title, maxsplit=1, flags=re.I)[0].strip()
        if " - " in profile_name:
            profile_name = profile_name.split(" - ", 1)[0].strip()

    company_name, company_url = _first_anchor(
        html,
        lambda label, href, attrs: bool(label and "/company/" in href),
    )
    headline = description
    if title and " - " in title:
        headline = title.split(" - ", 1)[1].strip()
    return {
        "profile_name": profile_name,
        "profile_headline": headline,
        "profile_company": company_name or _company_from_headline(headline) or _company_from_headline(description),
        "profile_company_linkedin_url": company_url,
        "profile_canonical_url": _absolute_clean_url(canonical_url),
        "profile_error": "",
    }


def _parse_job_label(label: str, company_hint: str = "") -> dict:
    text = re.sub(r"\s+", " ", safe_str(label)).strip()
    company_hint = safe_str(company_hint).strip()
    out = {"job_title": text, "job_company": company_hint, "job_location": ""}
    if not text:
        return out

    if company_hint:
        marker = f" {company_hint}, "
        if marker.lower() in text.lower():
            idx = text.lower().find(marker.lower())
            out["job_title"] = text[:idx].strip()
            out["job_company"] = company_hint
            out["job_location"] = text[idx + len(marker) :].strip()
            return out

    parts = [part.strip() for part in text.rsplit(",", 2)]
    if len(parts) >= 3:
        title_company = parts[0]
        location = ", ".join(parts[1:]).strip()
        if company_hint and title_company.lower().endswith(company_hint.lower()):
            out["job_title"] = title_company[: -len(company_hint)].strip()
            out["job_company"] = company_hint
        else:
            out["job_title"] = title_company
        out["job_location"] = location
    return out


def preview_linkedin_post(url: str) -> dict:
    source_url = safe_str(url).strip()
    if "linkedin.com/" not in source_url.lower():
        raise ValueError("Paste a LinkedIn post URL.")

    response = requests.get(
        source_url,
        timeout=LINKEDIN_POST_TIMEOUT_SECONDS,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    response.raise_for_status()

    html = response.text
    page_title = _meta_content(html, "og:title", "twitter:title") or _page_title(html)
    post_text = _meta_content(html, "og:description", "twitter:description", "description")
    canonical_url = _meta_content(html, "og:url", "twitter:url", "lnkd:url") or response.url

    poster_name, poster_url = _first_person_anchor(html)
    if not poster_name and page_title and " | " in page_title:
        poster_name = page_title.rsplit(" | ", 1)[-1].strip()

    company_name, company_url = _first_anchor(
        html,
        lambda label, href, attrs: bool(
            label
            and "/company/" in href
            and "public_post-text" in " ".join(str(value).lower() for value in attrs.values())
        ),
    )
    shared_company_name, shared_company_url = _first_anchor(
        html,
        lambda label, href, attrs: bool(
            label
            and "/company/" in href
            and "public_post_reshare_feed-actor-name" in " ".join(str(value).lower() for value in attrs.values())
        ),
    )
    if not company_name:
        company_name, company_url = shared_company_name, shared_company_url
    if not company_name:
        company_name, company_url = _first_anchor(
            html,
            lambda label, href, attrs: bool(label and "/company/" in href),
        )

    profile = {}
    if poster_url and (not poster_name or not company_name):
        profile = _preview_linkedin_profile(poster_url)
        poster_name = poster_name or safe_str(profile.get("profile_name")).strip()
        company_name = company_name or safe_str(profile.get("profile_company")).strip()
        company_url = company_url or safe_str(profile.get("profile_company_linkedin_url")).strip()

    job_label, job_url = _first_anchor(
        html,
        lambda label, href, attrs: bool(label and "/jobs/view/" in href),
    )
    job = _parse_job_label(job_label, company_hint=shared_company_name or company_name)

    text = _strip_html(html)
    shared_post_text = ""
    if shared_company_name:
        marker = f"{shared_company_name}\n"
        idx = text.find(marker)
        if idx >= 0:
            after = text[idx + len(marker) :]
            stop_markers = ["\nLike\n", "\nComment\n", "\nShare\n", "\nTo view or add a comment"]
            stop_positions = [after.find(marker) for marker in stop_markers if after.find(marker) >= 0]
            end = min(stop_positions) if stop_positions else min(len(after), 1200)
            shared_post_text = after[:end].strip()

    return {
        "source_url": source_url,
        "canonical_url": _absolute_clean_url(canonical_url),
        "http_status": response.status_code,
        "page_title": page_title,
        "poster_name": poster_name,
        "poster_linkedin_url": poster_url,
        "company_name": company_name,
        "company_linkedin_url": company_url,
        "profile_headline": safe_str(profile.get("profile_headline")).strip(),
        "profile_company": safe_str(profile.get("profile_company")).strip(),
        "profile_company_linkedin_url": safe_str(profile.get("profile_company_linkedin_url")).strip(),
        "profile_error": safe_str(profile.get("profile_error")).strip(),
        "post_text": post_text,
        "shared_company_name": shared_company_name,
        "shared_company_linkedin_url": shared_company_url,
        "shared_post_text": shared_post_text,
        "job_title": job["job_title"],
        "job_company": job["job_company"] or shared_company_name or company_name,
        "job_location": job["job_location"],
        "job_url": job_url,
        "raw_text_preview": text[:4000],
    }
