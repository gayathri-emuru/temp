from django.db import transaction
from typing import Optional

from core.models import SuppressedEmail
from core.models import EmailVerification
from core.services.normalization_service import normalize_email_address
from core.utils import safe_str


HARD_BOUNCE_PATTERNS = (
    "user unknown",
    "unknown user",
    "no such user",
    "no such recipient",
    "recipient address rejected",
    "address not found",
    "mailbox unavailable",
    "mailbox not found",
    "invalid recipient",
    "invalid address",
    "does not exist",
    "550 5.1.1",
    "550-5.1.1",
    "5.1.1",
)


def is_suppressed_email(email: str) -> bool:
    email = normalize_email_address(email)
    if not email:
        return False
    return SuppressedEmail.objects.filter(email=email, is_active=True).exists()


def is_verifier_blocked_email(email: str) -> bool:
    email = normalize_email_address(email)
    if not email:
        return False
    verification = EmailVerification.objects.filter(email=email).first()
    if not verification:
        return False
    if verification.decision == EmailVerification.Decision.BLOCK:
        return True
    return False


def is_blocked_or_suppressed_email(email: str) -> bool:
    return is_suppressed_email(email) or is_verifier_blocked_email(email)


def suppression_reason_from_error(error_message: str) -> str:
    text = safe_str(error_message).strip().lower()
    if not text:
        return ""
    if any(pattern in text for pattern in HARD_BOUNCE_PATTERNS):
        return SuppressedEmail.Reason.BAD_ADDRESS
    return ""


@transaction.atomic
def suppress_email(*, email: str, reason: str, source_error: str = "") -> Optional[SuppressedEmail]:
    email = normalize_email_address(email)
    if not email:
        return None
    reason = safe_str(reason).strip() or SuppressedEmail.Reason.MANUAL
    obj, _ = SuppressedEmail.objects.update_or_create(
        email=email,
        defaults={
            "reason": reason,
            "source_error": safe_str(source_error).strip()[:4000],
            "is_active": True,
        },
    )
    return obj


def suppress_if_hard_bounce(*, email: str, error_message: str) -> bool:
    reason = suppression_reason_from_error(error_message)
    if not reason:
        return False
    suppress_email(email=email, reason=reason, source_error=error_message)
    return True
