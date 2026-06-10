from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import List, Optional

from core.utils import safe_str


DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_SMTP_TIMEOUT_SECONDS = int(os.getenv("SMTP_TIMEOUT_SECONDS", "30") or "30")
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
OUTLOOK_PERSONAL_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    provider: str


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _email_domain(email: str) -> str:
    value = safe_str(email).strip().lower()
    return value.rsplit("@", 1)[-1] if "@" in value else ""


def _configured_domain_set(env_names: tuple[str, ...]) -> set[str]:
    domains: set[str] = set()
    for name in env_names:
        raw = os.getenv(name, "")
        for item in raw.replace(";", ",").split(","):
            domain = item.strip().lower().lstrip("@")
            if domain:
                domains.add(domain)
    return domains


def _is_configured_microsoft365_domain(domain: str) -> bool:
    if not domain:
        return False
    if domain.endswith(".onmicrosoft.com") or domain == "onmicrosoft.com":
        return True
    return domain in _configured_domain_set(("MICROSOFT365_SMTP_DOMAINS", "OUTLOOK_SMTP_DOMAINS"))


def smtp_settings_for_email(email: str, *, smtp_host: str | None = None, smtp_port: int | None = None) -> SmtpSettings:
    explicit_host = safe_str(smtp_host).strip()
    if explicit_host:
        return SmtpSettings(
            host=explicit_host,
            port=int(smtp_port or _int_env("SMTP_PORT", DEFAULT_SMTP_PORT)),
            provider="custom",
        )

    env_host = safe_str(os.getenv("SMTP_HOST", "")).strip()
    if env_host:
        return SmtpSettings(
            host=env_host,
            port=int(smtp_port or _int_env("SMTP_PORT", DEFAULT_SMTP_PORT)),
            provider="env",
        )

    port = int(smtp_port or _int_env("SMTP_PORT", DEFAULT_SMTP_PORT))
    domain = _email_domain(email)
    if domain in GMAIL_DOMAINS or domain.endswith(".gmail.com"):
        return SmtpSettings(host="smtp.gmail.com", port=port, provider="gmail")
    if domain in OUTLOOK_PERSONAL_DOMAINS:
        return SmtpSettings(host="smtp-mail.outlook.com", port=port, provider="outlook")
    if _is_configured_microsoft365_domain(domain):
        return SmtpSettings(host="smtp.office365.com", port=port, provider="microsoft365")
    return SmtpSettings(host=DEFAULT_SMTP_HOST, port=port, provider="default")


def imap_host_for_email(email: str) -> str:
    domain = _email_domain(email)
    if domain in GMAIL_DOMAINS or domain.endswith(".gmail.com"):
        return "imap.gmail.com"
    if domain in OUTLOOK_PERSONAL_DOMAINS or _is_configured_microsoft365_domain(domain):
        return "outlook.office365.com"
    return f"imap.{domain}" if domain else ""


def _outlook_auth_error_message(error: Exception) -> str:
    return (
        "Microsoft Outlook SMTP rejected the login. Outlook.com requires Modern Auth/OAuth2; "
        "if this account still allows password SMTP, use a Microsoft app password and enable "
        f"POP/IMAP access for the mailbox. Original SMTP error: {safe_str(error)[:500]}"
    )


def _email_sending_enabled() -> bool:
    enabled = os.getenv("EMAIL_SENDING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    paused = os.getenv("EMAIL_SENDING_PAUSED", "").strip().lower() in {"1", "true", "yes", "on"}
    return bool(enabled and not paused)


def ensure_email_sending_allowed() -> None:
    if _email_sending_enabled():
        return

    paused = os.getenv("EMAIL_SENDING_PAUSED", "").strip()
    enabled = os.getenv("EMAIL_SENDING_ENABLED", "").strip()
    if str(paused).strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "Email sending is paused (EMAIL_SENDING_PAUSED=1). "
            "Resume it from the dashboard or set EMAIL_SENDING_PAUSED=0. "
            f"(current EMAIL_SENDING_ENABLED={enabled!r})"
        )
    raise RuntimeError(
        "Email sending is disabled. "
        "Set EMAIL_SENDING_ENABLED=1 to allow sends."
    )


def build_mime_message(
    *,
    from_name: str,
    from_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    attachment_paths: Optional[List[str]] = None,
) -> MIMEMultipart:
    msg = MIMEMultipart()
    display_name = safe_str(from_name).strip()
    sender_email = safe_str(from_email).strip()
    msg["From"] = formataddr((display_name or sender_email, sender_email))
    msg["To"] = safe_str(to_email)
    msg["Subject"] = safe_str(subject)

    msg.attach(MIMEText(safe_str(body_text), "plain", "utf-8"))

    for path in attachment_paths or []:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            continue

        with open(file_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=file_path.name)
        part["Content-Disposition"] = f'attachment; filename="{file_path.name}"'
        msg.attach(part)

    return msg


def send_via_smtp(
    *,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    username: str,
    app_password: str,
    message: MIMEMultipart,
) -> None:
    ensure_email_sending_allowed()

    settings = smtp_settings_for_email(username, smtp_host=smtp_host, smtp_port=smtp_port)
    server = smtplib.SMTP(settings.host, settings.port, timeout=DEFAULT_SMTP_TIMEOUT_SECONDS)
    try:
        server.ehlo()
        server.starttls()
        server.ehlo()
        try:
            server.login(username, app_password)
        except smtplib.SMTPAuthenticationError as exc:
            if settings.provider in {"outlook", "microsoft365"}:
                raise RuntimeError(_outlook_auth_error_message(exc)) from exc
            raise
        server.sendmail(username, safe_str(message["To"]), message.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
