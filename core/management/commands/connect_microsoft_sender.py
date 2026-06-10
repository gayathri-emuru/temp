from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import SenderAccount
from core.services.microsoft_graph_send_service import (
    fetch_microsoft_profile,
    poll_device_authorization,
    start_device_authorization,
    store_token_payload,
)
from core.services.normalization_service import normalize_email_address
from core.utils import safe_str


class Command(BaseCommand):
    help = "Connect a SenderAccount to Microsoft Graph OAuth using device-code login."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Existing SenderAccount email to connect.")
        parser.add_argument("--create", action="store_true", help="Create the sender if it does not exist.")
        parser.add_argument("--tenant", default="", help="Optional Microsoft OAuth tenant: consumers, common, organizations, or tenant ID.")
        parser.add_argument("--timeout-seconds", type=int, default=900, help="How long to wait for browser authorization.")
        parser.add_argument(
            "--allow-email-mismatch",
            action="store_true",
            help="Store the token even if Microsoft reports a different signed-in mailbox.",
        )

    def handle(self, *args, **options):
        email = normalize_email_address(options["email"])
        if not email:
            raise CommandError("A valid sender email is required.")

        sender = SenderAccount.objects.filter(email__iexact=email).first()
        if not sender:
            if not options["create"]:
                raise CommandError(f"SenderAccount {email!r} does not exist. Re-run with --create to add it.")
            sender = SenderAccount.objects.create(email=email, app_password="", auth_method=SenderAccount.AuthMethod.MICROSOFT_GRAPH)

        tenant = safe_str(options.get("tenant")).strip() or None
        flow = start_device_authorization(tenant=tenant)
        verification_uri = flow.get("verification_uri") or flow.get("verification_url") or "https://microsoft.com/devicelogin"
        user_code = flow.get("user_code", "")
        message = flow.get("message", "")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"Connect Microsoft sender: {sender.email}"))
        self.stdout.write(f"Open: {verification_uri}")
        if user_code:
            self.stdout.write(f"Code: {user_code}")
        if message:
            self.stdout.write(message)
        self.stdout.write("")
        self.stdout.write("Waiting for Microsoft authorization...")

        payload = poll_device_authorization(
            device_code=flow["device_code"],
            interval_seconds=int(flow.get("interval") or 5),
            expires_in_seconds=int(flow.get("expires_in") or options["timeout_seconds"]),
            tenant=tenant,
            timeout_seconds=int(options["timeout_seconds"]),
        )

        profile = fetch_microsoft_profile(payload["access_token"])
        reported_emails = {
            safe_str(profile.get("mail")).strip().lower(),
            safe_str(profile.get("userPrincipalName")).strip().lower(),
        }
        reported_emails.discard("")
        expected_email = sender.email.strip().lower()
        if reported_emails and expected_email not in reported_emails and not options["allow_email_mismatch"]:
            raise CommandError(
                "Microsoft login completed, but the signed-in mailbox did not match the SenderAccount. "
                f"Expected {expected_email}; Microsoft reported {', '.join(sorted(reported_emails))}. "
                "Re-run with the correct account or pass --allow-email-mismatch if this is an alias."
            )

        store_token_payload(sender, payload)
        self.stdout.write(self.style.SUCCESS(f"Connected {sender.email} to Microsoft Graph OAuth."))
