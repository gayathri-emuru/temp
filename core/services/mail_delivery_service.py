from __future__ import annotations

from email.mime.multipart import MIMEMultipart

from core.services.microsoft_graph_send_service import send_via_microsoft_graph
from core.services.smtp_send_service import send_via_smtp
from core.services.email_verification_service import enforce_email_verification


def send_via_sender_account(*, sender, message: MIMEMultipart, enforce_recipient_verification: bool = False) -> None:
    if enforce_recipient_verification:
        enforce_email_verification(str(message.get("To", "")))

    if getattr(sender, "auth_method", "") == sender.AuthMethod.MICROSOFT_GRAPH:
        send_via_microsoft_graph(sender=sender, message=message)
        return

    send_via_smtp(username=sender.email, app_password=sender.app_password, message=message)
