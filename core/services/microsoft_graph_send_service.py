from __future__ import annotations

import base64
import os
import time
from datetime import timedelta
from email.mime.multipart import MIMEMultipart

import requests
from django.utils import timezone

from core.services.smtp_send_service import ensure_email_sending_allowed
from core.utils import safe_str


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_GRAPH_SCOPES = "offline_access User.Read Mail.Send"
TOKEN_REFRESH_SKEW_SECONDS = 300


class MicrosoftGraphAuthError(RuntimeError):
    pass


class MicrosoftGraphSendError(RuntimeError):
    pass


def microsoft_client_id() -> str:
    return safe_str(os.getenv("MICROSOFT_GRAPH_CLIENT_ID") or os.getenv("MICROSOFT_CLIENT_ID")).strip()


def microsoft_tenant(default: str = "consumers") -> str:
    return safe_str(os.getenv("MICROSOFT_GRAPH_TENANT") or os.getenv("MICROSOFT_TENANT") or default).strip() or default


def microsoft_scopes() -> str:
    return safe_str(os.getenv("MICROSOFT_GRAPH_SCOPES") or DEFAULT_GRAPH_SCOPES).strip() or DEFAULT_GRAPH_SCOPES


def _oauth_url(path: str, *, tenant: str | None = None) -> str:
    tenant_value = safe_str(tenant).strip() or microsoft_tenant()
    return f"https://login.microsoftonline.com/{tenant_value}/oauth2/v2.0/{path}"


def _request_timeout_seconds() -> int:
    try:
        return max(5, int(os.getenv("MICROSOFT_GRAPH_TIMEOUT_SECONDS", "30") or "30"))
    except Exception:
        return 30


def _require_client_id(client_id: str | None = None) -> str:
    value = safe_str(client_id).strip() or microsoft_client_id()
    if not value:
        raise MicrosoftGraphAuthError(
            "MICROSOFT_GRAPH_CLIENT_ID is required. Create a public-client Microsoft app registration "
            "with Mail.Send, User.Read, and offline_access delegated permissions, then put its client ID in .env."
        )
    return value


def start_device_authorization(*, client_id: str | None = None, tenant: str | None = None) -> dict:
    client_id = _require_client_id(client_id)
    response = requests.post(
        _oauth_url("devicecode", tenant=tenant),
        data={"client_id": client_id, "scope": microsoft_scopes()},
        timeout=_request_timeout_seconds(),
    )
    if response.status_code >= 400:
        raise MicrosoftGraphAuthError(f"Microsoft device-code start failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def poll_device_authorization(
    *,
    device_code: str,
    interval_seconds: int,
    expires_in_seconds: int,
    client_id: str | None = None,
    tenant: str | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    client_id = _require_client_id(client_id)
    deadline = time.monotonic() + min(int(timeout_seconds or expires_in_seconds or 900), int(expires_in_seconds or 900))
    interval = max(1, int(interval_seconds or 5))

    while time.monotonic() < deadline:
        response = requests.post(
            _oauth_url("token", tenant=tenant),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
            timeout=_request_timeout_seconds(),
        )
        payload = response.json() if response.content else {}
        if response.status_code < 400 and payload.get("access_token"):
            return payload

        error = safe_str(payload.get("error")).strip()
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if error in {"authorization_declined", "expired_token", "bad_verification_code"}:
            raise MicrosoftGraphAuthError(f"Microsoft device-code login stopped: {error}")

        detail = payload.get("error_description") or response.text[:1000]
        raise MicrosoftGraphAuthError(f"Microsoft device-code login failed: {error or response.status_code} {detail}")

    raise MicrosoftGraphAuthError("Microsoft device-code login timed out before authorization completed.")


def store_token_payload(sender, payload: dict) -> None:
    expires_in = int(payload.get("expires_in") or 3600)
    sender.oauth_access_token = safe_str(payload.get("access_token"))
    refresh_token = safe_str(payload.get("refresh_token"))
    if refresh_token:
        sender.oauth_refresh_token = refresh_token
    sender.oauth_token_expires_at = timezone.now() + timedelta(seconds=max(60, expires_in))
    sender.oauth_scope = safe_str(payload.get("scope") or microsoft_scopes())[:1000]
    sender.oauth_token_updated_at = timezone.now()
    sender.auth_method = sender.AuthMethod.MICROSOFT_GRAPH
    sender.save(
        update_fields=[
            "oauth_access_token",
            "oauth_refresh_token",
            "oauth_token_expires_at",
            "oauth_scope",
            "oauth_token_updated_at",
            "auth_method",
            "updated_at",
        ]
    )


def refresh_microsoft_access_token(sender, *, client_id: str | None = None, tenant: str | None = None) -> str:
    client_id = _require_client_id(client_id)
    refresh_token = safe_str(getattr(sender, "oauth_refresh_token", "")).strip()
    if not refresh_token:
        raise MicrosoftGraphAuthError(f"{sender.email} is not connected to Microsoft Graph yet.")

    response = requests.post(
        _oauth_url("token", tenant=tenant),
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": microsoft_scopes(),
        },
        timeout=_request_timeout_seconds(),
    )
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        detail = payload.get("error_description") or response.text[:1000]
        raise MicrosoftGraphAuthError(f"Microsoft token refresh failed for {sender.email}: {detail}")

    store_token_payload(sender, payload)
    return safe_str(sender.oauth_access_token)


def get_microsoft_access_token(sender) -> str:
    token = safe_str(getattr(sender, "oauth_access_token", "")).strip()
    expires_at = getattr(sender, "oauth_token_expires_at", None)
    if token and expires_at and timezone.now() < expires_at - timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS):
        return token
    return refresh_microsoft_access_token(sender)


def fetch_microsoft_profile(access_token: str) -> dict:
    response = requests.get(
        f"{GRAPH_BASE_URL}/me?$select=mail,userPrincipalName,displayName",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_request_timeout_seconds(),
    )
    if response.status_code >= 400:
        raise MicrosoftGraphAuthError(f"Microsoft profile check failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def send_via_microsoft_graph(*, sender, message: MIMEMultipart) -> None:
    ensure_email_sending_allowed()
    access_token = get_microsoft_access_token(sender)
    mime_payload = base64.b64encode(message.as_bytes()).decode("ascii")
    response = requests.post(
        f"{GRAPH_BASE_URL}/me/sendMail",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "text/plain",
        },
        data=mime_payload,
        timeout=_request_timeout_seconds(),
    )
    if response.status_code not in {202}:
        raise MicrosoftGraphSendError(
            f"Microsoft Graph sendMail failed for {sender.email}: {response.status_code} {response.text[:1000]}"
        )
