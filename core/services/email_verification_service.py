from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import requests
from django.utils import timezone

from core.models import EmailVerification
from core.services.normalization_service import normalize_email_address
from core.utils import safe_str


DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_CACHE_DAYS = 30
DEFAULT_DEFER_CACHE_MINUTES = 15

ALLOW_STATUSES = {
    "deliverable",
    "safe",
    "valid",
    "verified",
    "catch_all",
    "catchall",
    "accept_all",
    "acceptall",
}
BLOCK_STATUSES = {
    "bad",
    "blocked",
    "disposable",
    "do_not_send",
    "invalid",
    "mailbox_not_found",
    "spam_trap",
    "spamtrap",
    "undeliverable",
}
DEFER_STATUSES = {
    "error",
    "pending",
    "rate_limited",
    "retry",
    "unknown",
    "unverifiable",
}
BLOCK_MARKERS = (
    "disposable",
    "do not send",
    "does not exist",
    "invalid",
    "mailbox not found",
    "no such user",
    "spam trap",
    "spamtrap",
    "undeliverable",
)
CATCH_ALL_MARKERS = ("accept all", "accept-all", "accept_all", "catch all", "catch-all", "catch_all")


class EmailVerificationError(RuntimeError):
    pass


class EmailVerificationBlockedError(EmailVerificationError):
    pass


class EmailVerificationUnavailableError(EmailVerificationError):
    pass


@dataclass(frozen=True)
class VerificationDecision:
    email: str
    decision: str
    provider_status: str
    reason: str
    is_catch_all: bool
    cached: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == EmailVerification.Decision.ALLOW


def email_verification_enforced() -> bool:
    return _env_bool("EMAIL_VERIFIER_ENFORCE", "0")


def verify_email_for_real_send(email: str, *, force_refresh: bool = False) -> VerificationDecision:
    email = normalize_email_address(email)
    if not email:
        raise EmailVerificationBlockedError("EmailVerifier.io blocked blank or invalid recipient email.")

    if not force_refresh:
        cached = EmailVerification.objects.filter(email=email, expires_at__gt=timezone.now()).first()
        if cached:
            return _decision_from_model(cached, cached=True)

    api_key = os.getenv("EMAILVERIFIER_ACTIVE_KEY", "").strip()
    api_url = os.getenv("EMAIL_VERIFIER_API_URL", "").strip()
    if not api_key:
        return _store_defer(email, "missing_api_key", "EMAILVERIFIER_ACTIVE_KEY is not configured.")
    if not api_url:
        return _store_defer(email, "missing_api_url", "EMAIL_VERIFIER_API_URL is not configured.")

    try:
        payload = _call_provider(api_url=api_url, api_key=api_key, email=email)
        decision, status, reason, is_catch_all = _classify_response(payload, email=email)
        return _store_decision(
            email=email,
            decision=decision,
            provider_status=status,
            reason=reason,
            is_catch_all=is_catch_all,
            raw_response=payload,
        )
    except requests.RequestException as exc:
        return _store_defer(email, "provider_request_error", safe_str(exc))
    except Exception as exc:
        return _store_defer(email, "provider_response_error", safe_str(exc))


def enforce_email_verification(email: str) -> VerificationDecision:
    if not email_verification_enforced():
        return VerificationDecision(
            email=normalize_email_address(email),
            decision=EmailVerification.Decision.ALLOW,
            provider_status="enforcement_disabled",
            reason="EmailVerifier.io enforcement is disabled.",
            is_catch_all=False,
        )

    result = verify_email_for_real_send(email)
    if result.decision == EmailVerification.Decision.BLOCK:
        raise EmailVerificationBlockedError(
            f"EmailVerifier.io rejected {result.email}: {result.provider_status or result.reason}"
        )
    return result


def _call_provider(*, api_url: str, api_key: str, email: str) -> dict:
    method = os.getenv("EMAIL_VERIFIER_API_METHOD", "POST").strip().upper()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
    }
    if method == "GET":
        response = requests.get(api_url, headers=headers, params={"email": email, "api_key": api_key}, timeout=_timeout())
    else:
        response = requests.post(
            api_url,
            headers=headers,
            json={"email": email, "api_key": api_key},
            timeout=_timeout(),
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("EmailVerifier.io returned a non-object JSON response.")
    return payload


def _classify_response(payload: dict, *, email: str = "") -> tuple[str, str, str, bool]:
    values = _flatten_values(payload)
    status = _first_status(payload, values)
    combined = " ".join(values).lower().replace("-", "_").replace(" ", "_")
    provider_unavailable = (
        status in DEFER_STATUSES
        or any(marker in combined for marker in ("temporarily_unavailable", "temporary_unavailable", "provider_unavailable"))
    )
    is_catch_all = (
        _find_boolean(payload, ("is_catch_all", "catch_all", "catchall", "accept_all")) is True
        or any(marker.replace("-", "_").replace(" ", "_") in combined for marker in CATCH_ALL_MARKERS)
    )
    explicit_invalid = _find_boolean(payload, ("is_valid", "valid", "is_deliverable", "deliverable")) is False
    explicitly_unsafe = _find_boolean(payload, ("is_safe_to_send", "safe_to_send", "safe")) is False
    disposable = _find_boolean(payload, ("is_disposable", "disposable")) is True
    spam_trap = _find_boolean(payload, ("is_spam_trap", "spam_trap", "spamtrap")) is True

    if (
        disposable
        or spam_trap
        or (explicitly_unsafe and not is_catch_all)
        or status in BLOCK_STATUSES
        or any(marker.replace(" ", "_") in combined for marker in BLOCK_MARKERS)
        or (explicit_invalid and not is_catch_all and not provider_unavailable)
    ):
        return EmailVerification.Decision.BLOCK, status or "invalid", "Provider explicitly rejected the address.", is_catch_all
    if is_catch_all:
        return EmailVerification.Decision.ALLOW, status or "catch_all", "Catch-all accepted by policy.", True
    if provider_unavailable:
        return EmailVerification.Decision.DEFER, status, "Provider could not determine deliverability.", False
    if status in ALLOW_STATUSES:
        return EmailVerification.Decision.ALLOW, status, "Provider approved the address.", False
    return EmailVerification.Decision.DEFER, status or "unrecognized_response", "Unrecognized provider response; send deferred.", False


def _find_boolean(value: Any, keys: tuple[str, ...]) -> bool | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if safe_str(key).strip().lower() in keys and isinstance(item, bool):
                return item
            nested = _find_boolean(item, keys)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _find_boolean(item, keys)
            if nested is not None:
                return nested
    return None


def _first_status(payload: dict, values: list[str]) -> str:
    status_keys = ("status", "result", "verdict", "state", "deliverability", "recommendation")
    for key in status_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_status(value)
    for container_key in ("data", "result", "verification"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in status_keys:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return _normalize_status(value)
    for value in values:
        normalized = _normalize_status(value)
        if normalized in ALLOW_STATUSES | BLOCK_STATUSES | DEFER_STATUSES:
            return normalized
    return ""


def _flatten_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            values.extend(_flatten_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.extend(_flatten_values(item))
    elif value is not None:
        values.append(safe_str(value))
    return [item.strip() for item in values if item and item.strip()]


def _normalize_status(value: str) -> str:
    return safe_str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _store_decision(*, email: str, decision: str, provider_status: str, reason: str, is_catch_all: bool, raw_response: dict) -> VerificationDecision:
    now = timezone.now()
    cache_days = _positive_int("EMAIL_VERIFIER_CACHE_DAYS", DEFAULT_CACHE_DAYS)
    obj, _ = EmailVerification.objects.update_or_create(
        email=email,
        defaults={
            "provider": "emailverifier.io",
            "decision": decision,
            "provider_status": provider_status[:100],
            "reason": reason[:500],
            "is_catch_all": is_catch_all,
            "raw_response": raw_response,
            "verified_at": now,
            "expires_at": now + timedelta(days=cache_days),
        },
    )
    return _decision_from_model(obj)


def _store_defer(email: str, status: str, reason: str) -> VerificationDecision:
    now = timezone.now()
    obj, _ = EmailVerification.objects.update_or_create(
        email=email,
        defaults={
            "provider": "emailverifier.io",
            "decision": EmailVerification.Decision.DEFER,
            "provider_status": status[:100],
            "reason": reason[:500],
            "is_catch_all": False,
            "raw_response": {},
            "verified_at": now,
            "expires_at": now + timedelta(minutes=_positive_int("EMAIL_VERIFIER_DEFER_CACHE_MINUTES", DEFAULT_DEFER_CACHE_MINUTES)),
        },
    )
    return _decision_from_model(obj)


def _decision_from_model(obj: EmailVerification, *, cached: bool = False) -> VerificationDecision:
    return VerificationDecision(
        email=obj.email,
        decision=obj.decision,
        provider_status=obj.provider_status,
        reason=obj.reason,
        is_catch_all=obj.is_catch_all,
        cached=cached,
    )


def _timeout() -> int:
    return _positive_int("EMAIL_VERIFIER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except Exception:
        return default


def _env_bool(name: str, default: str = "0") -> bool:
    return safe_str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}
