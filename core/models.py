from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.services.normalization_service import (
    canonical_company_name,
    normalize_company_name,
    normalize_email_address,
    normalize_person_name,
)


class DailyBatch(models.Model):
    class RunStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    batch_date = models.DateField(unique=True)
    lookback_hours = models.PositiveIntegerField(default=24)
    max_jobs_requested = models.PositiveIntegerField(default=2)
    apify_run_status = models.CharField(
        max_length=20,
        choices=RunStatus.choices,
        default=RunStatus.PENDING,
    )
    import_started_at = models.DateTimeField(null=True, blank=True)
    import_finished_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-batch_date"]

    def __str__(self):
        return f"Batch {self.batch_date}"


class ApifyApiKey(models.Model):
    key_name = models.CharField(max_length=100, unique=True)
    api_key = models.TextField()

    is_active = models.BooleanField(default=True)
    is_exhausted = models.BooleanField(default=False)
    rotation_order = models.PositiveIntegerField(default=0, db_index=True)

    last_used_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rotation_order", "key_name"]

    def save(self, *args, **kwargs):
        # If the API key value changes in admin, automatically re-enable and un-exhaust it
        # so rotation starts using the new key immediately.
        if self.pk:
            try:
                old = ApifyApiKey.objects.filter(pk=self.pk).values("api_key").first()
            except Exception:
                old = None
            old_api_key = (old or {}).get("api_key") if isinstance(old, dict) else None
            if old_api_key is not None and old_api_key != self.api_key:
                self.is_active = True
                self.is_exhausted = False
                self.last_error = ""

        return super().save(*args, **kwargs)

    def __str__(self):
        return self.key_name


class Company(models.Model):
    class DomainStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        SET = "set", "Set"
        DOUBT = "doubt", "Doubt"

    class PatternStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        SET = "set", "Set"
        DOUBT = "doubt", "Doubt"

    raw_name_latest = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, unique=True)
    legacy = models.BooleanField(default=False, db_index=True)

    active_domain = models.CharField(max_length=255, blank=True, default="")
    linkedin_company_slug = models.CharField(max_length=200, blank=True, default="", db_index=True)
    domain_status = models.CharField(
        max_length=20,
        choices=DomainStatus.choices,
        default=DomainStatus.UNKNOWN,
    )
    pattern_status = models.CharField(
        max_length=20,
        choices=PatternStatus.choices,
        default=PatternStatus.UNKNOWN,
    )

    daily_send_limit = models.PositiveIntegerField(default=10)
    rolling_7d_send_limit = models.PositiveIntegerField(default=25)

    is_blocked = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")

    last_apollo_run_at = models.DateTimeField(null=True, blank=True)
    last_apollo_emails_found = models.PositiveIntegerField(default=0)
    last_apollo_verified_emails_found = models.PositiveIntegerField(default=0)
    last_apollo_unverified_emails_found = models.PositiveIntegerField(default=0)
    last_apollo_email_status_counts = models.JSONField(blank=True, default=dict)
    last_apollo_credits_consumed = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_name"]

    def __str__(self):
        return self.normalized_name


class BlacklistedCompany(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        related_name="blacklist_entries",
        null=True,
        blank=True,
    )
    raw_name_latest = models.CharField(max_length=255, blank=True, default="")
    normalized_name = models.CharField(max_length=255, unique=True, db_index=True)
    canonical_name = models.CharField(max_length=255, blank=True, default="", db_index=True)
    reason = models.TextField(blank=True, default="")
    source = models.CharField(max_length=80, blank=True, default="zero_usable_recipients")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "blacklisted_companies"
        ordering = ["normalized_name"]

    def save(self, *args, **kwargs):
        raw_name = self.raw_name_latest or getattr(self.company, "raw_name_latest", "") or self.normalized_name
        normalized_name = normalize_company_name(self.normalized_name or raw_name)
        self.normalized_name = normalized_name or self.normalized_name
        self.canonical_name = canonical_company_name(self.normalized_name or raw_name)
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.normalized_name


class JobPosting(models.Model):
    class SourcePlatform(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        DICE = "dice", "Dice"
        EXTERNAL = "external", "External"

    class Status(models.TextChoices):
        IMPORTED = "imported", "Imported"
        RECRUITERS_PENDING = "recruiters_pending", "Recruiters Pending"
        EMAIL_DISCOVERY_READY = "email_discovery_ready", "Email Discovery Ready"
        EMAIL_DISCOVERY_DONE = "email_discovery_done", "Email Discovery Done"
        EMAIL_GENERATED = "email_generated", "Email Generated"
        APPROVED = "approved", "Approved"
        TEST_SENT = "test_sent", "Test Sent"
        REAL_SENT = "real_sent", "Real Sent"
        FAILED = "failed", "Failed"
        BLOCKED = "blocked", "Blocked"

    daily_batch = models.ForeignKey(
        DailyBatch,
        on_delete=models.CASCADE,
        related_name="jobs",
    )
    is_manual_import = models.BooleanField(default=False, db_index=True)
    is_manual_email_job = models.BooleanField(default=False, db_index=True)
    source_platform = models.CharField(
        max_length=30,
        choices=SourcePlatform.choices,
        default=SourcePlatform.LINKEDIN,
        db_index=True,
    )
    company_ref = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="jobs",
        null=True,
        blank=True,
    )

    external_job_id = models.CharField(max_length=255, blank=True, default="")
    manual_job_reference_id = models.CharField(max_length=255, blank=True, default="")
    linkedin_url = models.URLField(max_length=1000)
    apply_url = models.URLField(max_length=1000, blank=True, default="")
    normalized_linkedin_url = models.URLField(max_length=1000, blank=True, default="", db_index=True)
    normalized_apply_url = models.URLField(max_length=1000, blank=True, default="", db_index=True)

    title = models.CharField(max_length=500)
    company = models.CharField(max_length=255)
    location = models.CharField(max_length=500, blank=True, default="")
    salary = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField()
    description_fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)

    apify_linkedin_org_url = models.URLField(max_length=1000, blank=True, default="", db_index=True)
    apify_linkedin_org_slug = models.CharField(max_length=200, blank=True, default="", db_index=True)

    company_linkedin = models.URLField(max_length=1000, blank=True, default="")
    linkedin_geo_region_id = models.CharField(max_length=50, blank=True, default="", db_index=True)
    linkedin_people_search_urls = models.JSONField(default=dict, blank=True)

    recruiter_name = models.CharField(max_length=255, blank=True, default="")
    recruiter_title = models.CharField(max_length=255, blank=True, default="")
    recruiter_linkedin = models.URLField(max_length=1000, blank=True, default="")
    ai_hiring_mgr_name = models.CharField(max_length=255, blank=True, default="")
    ai_hiring_mgr_email = models.CharField(max_length=320, blank=True, default="")

    normalized_company = models.CharField(max_length=255, db_index=True)
    normalized_title = models.CharField(max_length=500, db_index=True)
    normalized_location = models.CharField(max_length=500, db_index=True)

    canonical_company = models.CharField(max_length=255, blank=True, default="", db_index=True)
    canonical_title = models.CharField(max_length=500, blank=True, default="", db_index=True)
    canonical_location = models.CharField(max_length=500, blank=True, default="", db_index=True)

    dedupe_key = models.CharField(max_length=1500, db_index=True)

    sort_company = models.CharField(max_length=255, db_index=True)
    sort_title = models.CharField(max_length=500, db_index=True)
    sort_location = models.CharField(max_length=500, db_index=True)

    prior_7d_company_roles = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.IMPORTED,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_company", "sort_title", "sort_location", "-created_at"]
        indexes = [
            models.Index(fields=["daily_batch", "status"]),
            models.Index(fields=["canonical_company", "canonical_title", "canonical_location"]),
            models.Index(fields=["normalized_company", "normalized_title", "normalized_location"]),
        ]

    def __str__(self):
        return f"{self.company} | {self.title} | {self.location}"


class RecruiterJsonUpload(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    uploaded_at = models.DateTimeField(auto_now_add=True)
    raw_json_text = models.TextField()
    normalized_company_count = models.PositiveIntegerField(default=0)
    total_people_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Recruiter JSON Upload {self.uploaded_at:%Y-%m-%d %H:%M:%S}"


class CompanyRecruiter(models.Model):
    class Source(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        LEGACY = "legacy", "Legacy"
        APOLLO = "apollo", "Apollo"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="recruiters",
    )
    person_name = models.CharField(max_length=255)
    normalized_person_name = models.CharField(max_length=255, db_index=True)
    legacy = models.BooleanField(default=False, db_index=True)

    apollo_person_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    apollo_title = models.CharField(max_length=255, null=True, blank=True)
    apollo_location = models.CharField(max_length=255, null=True, blank=True)
    apollo_linkedin_url = models.URLField(max_length=500, blank=True, default="")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.UNKNOWN, db_index=True)
    email_status = models.CharField(max_length=30, blank=True, default="unknown", db_index=True)
    title_match = models.BooleanField(default=False, db_index=True)
    location_match = models.BooleanField(default=False, db_index=True)
    manually_targeted = models.BooleanField(default=False, db_index=True)

    email = models.CharField(max_length=320, default="none")
    email_sent = models.BooleanField(default=False, db_index=True)
    email_sent_date = models.DateField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company__normalized_name", "normalized_person_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "normalized_person_name"],
                name="uniq_company_normalized_person",
            ),
            models.UniqueConstraint(
                fields=["company", "apollo_person_id"],
                condition=Q(apollo_person_id__isnull=False) & ~Q(apollo_person_id=""),
                name="uniq_company_apollo_person_id",
            ),
        ]

    def __str__(self):
        return f"{self.company.normalized_name} | {self.person_name}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)

        self.normalized_person_name = normalize_person_name(self.person_name or "")

        email_value = "none" if self.email is None else str(self.email).strip().lower()
        self.email = email_value if email_value else "none"

        if self.legacy:
            self.source = self.Source.LEGACY
            if not self.email_status or self.email_status == "unknown":
                self.email_status = "legacy"
        elif self.apollo_person_id and self.source == self.Source.UNKNOWN:
            self.source = self.Source.APOLLO

        if update_fields is not None:
            # Ensure normalized/derived fields are persisted when callers use update_fields.
            update_fields.update({"normalized_person_name", "email", "email_sent", "email_sent_date", "source", "email_status"})
            kwargs["update_fields"] = list(update_fields)

        super().save(*args, **kwargs)


class JobRecruiterTarget(models.Model):
    job_posting = models.ForeignKey(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="targets",
    )
    company_recruiter = models.ForeignKey(
        CompanyRecruiter,
        on_delete=models.CASCADE,
        related_name="job_targets",
    )

    recipient_email_snapshot = models.CharField(max_length=320, default="none")
    recipient_name_snapshot = models.CharField(max_length=255)

    selection_order = models.PositiveIntegerField(default=0)
    is_selected_for_job = models.BooleanField(default=False)
    is_verified_for_job = models.BooleanField(default=False)
    is_test_target_used = models.BooleanField(default=False)
    is_sent_real = models.BooleanField(default=False)

    send_block_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["job_posting", "selection_order", "recipient_name_snapshot"]
        constraints = [
            models.UniqueConstraint(
                fields=["job_posting", "company_recruiter"],
                name="uniq_job_company_recruiter_target",
            )
        ]

    def __str__(self):
        return f"{self.job_posting} -> {self.recipient_name_snapshot}"


class TargetedPeopleLookupRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="targeted_people_lookup_runs",
    )
    job_posting = models.ForeignKey(
        JobPosting,
        on_delete=models.SET_NULL,
        related_name="targeted_people_lookup_runs",
        null=True,
        blank=True,
    )
    raw_names = models.TextField(blank=True, default="")
    parsed_names = models.JSONField(default=list, blank=True)
    allow_regular_fallback = models.BooleanField(default=True)
    fallback_blocked_by_credit_guard = models.BooleanField(default=False)
    max_people = models.PositiveIntegerField(default=10)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING, db_index=True)
    emails_found = models.PositiveIntegerField(default=0)
    credits_consumed = models.PositiveIntegerField(default=0)
    credits_not_converted_to_email = models.PositiveIntegerField(default=0)
    source_counts = models.JSONField(default=dict, blank=True)
    result_rows = models.JSONField(default=list, blank=True)
    totals = models.JSONField(default=dict, blank=True)
    run_log_path = models.CharField(max_length=1000, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.company.normalized_name} targeted lookup #{self.id}"


class ApolloRejectedEmail(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="apollo_rejected_emails",
    )
    job_posting = models.ForeignKey(
        JobPosting,
        on_delete=models.SET_NULL,
        related_name="apollo_rejected_emails",
        null=True,
        blank=True,
    )
    targeted_lookup_run = models.ForeignKey(
        TargetedPeopleLookupRun,
        on_delete=models.SET_NULL,
        related_name="apollo_rejected_emails",
        null=True,
        blank=True,
    )
    person_name = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=320, db_index=True)
    email_status = models.CharField(max_length=30, blank=True, default="unknown", db_index=True)
    apollo_person_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    reason = models.CharField(max_length=120, blank=True, default="")
    source_workflow = models.CharField(max_length=80, blank=True, default="")
    run_log_path = models.CharField(max_length=1000, blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "email_status"]),
            models.Index(fields=["email", "email_status"]),
        ]

    def __str__(self):
        return f"{self.company.normalized_name} rejected {self.email} ({self.email_status})"


class GeneratedEmail(models.Model):
    class GenerationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATED = "generated", "Generated"
        FAILED = "failed", "Failed"

    job_posting = models.OneToOneField(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="generated_email",
    )
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    generation_status = models.CharField(
        max_length=20,
        choices=GenerationStatus.choices,
        default=GenerationStatus.PENDING,
    )
    prompt_version = models.CharField(max_length=100, blank=True, default="")
    edited_manually = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"GeneratedEmail for Job {self.job_posting_id}"


class ApprovalRecord(models.Model):
    job_posting = models.OneToOneField(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="approval_record",
    )
    is_approved = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Approval for Job {self.job_posting_id}: {self.is_approved}"


class SenderAccount(models.Model):
    class AuthMethod(models.TextChoices):
        SMTP_PASSWORD = "smtp_password", "SMTP password / app password"
        MICROSOFT_GRAPH = "microsoft_graph", "Microsoft Graph OAuth"

    email = models.EmailField(unique=True)
    app_password = models.CharField(max_length=255, blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    auth_method = models.CharField(
        max_length=30,
        choices=AuthMethod.choices,
        default=AuthMethod.SMTP_PASSWORD,
        db_index=True,
    )
    oauth_refresh_token = models.TextField(blank=True, default="")
    oauth_access_token = models.TextField(blank=True, default="")
    oauth_token_expires_at = models.DateTimeField(null=True, blank=True)
    oauth_scope = models.CharField(max_length=1000, blank=True, default="")
    oauth_token_updated_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_paused = models.BooleanField(default=False)
    paused_until = models.DateTimeField(null=True, blank=True, db_index=True)
    pause_reason = models.CharField(max_length=1000, blank=True, default="")

    daily_limit = models.PositiveIntegerField(default=50)
    sent_today_count = models.PositiveIntegerField(default=0)

    round_robin_order = models.PositiveIntegerField(default=0, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["round_robin_order", "email"]
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="uniq_sender_email_ci",
                violation_error_message="Sender account with this email already exists.",
            )
        ]

    def clean(self):
        super().clean()
        self.email = normalize_email_address(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email_address(self.email)
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class SenderDailyUsage(models.Model):
    sender_account = models.ForeignKey(
        SenderAccount,
        on_delete=models.CASCADE,
        related_name="daily_usages",
    )
    usage_date = models.DateField(default=timezone.localdate)
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-usage_date", "sender_account__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["sender_account", "usage_date"],
                name="uniq_sender_usage_per_day",
            )
        ]

    def __str__(self):
        return f"{self.sender_account.email} | {self.usage_date} | {self.sent_count}"


class TestEmailAccount(models.Model):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    rotation_order = models.PositiveIntegerField(default=0, db_index=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["rotation_order", "email"]
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="uniq_test_email_ci",
                violation_error_message="Test email account with this email already exists.",
            )
        ]

    def clean(self):
        super().clean()
        self.email = normalize_email_address(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email_address(self.email)
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class SendRun(models.Model):
    class RunType(models.TextChoices):
        TEST = "test", "Test"
        REAL = "real", "Real"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        STOPPED = "stopped", "Stopped"

    run_type = models.CharField(max_length=10, choices=RunType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    stopped_manually = models.BooleanField(default=False)
    delay_seconds = models.PositiveIntegerField(default=15)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.run_type} run #{self.id}"


class SentEmailLog(models.Model):
    class MessageType(models.TextChoices):
        INITIAL = "initial", "Initial"
        FOLLOW_UP = "follow_up", "Follow Up"

    class SendType(models.TextChoices):
        TEST = "test", "Test"
        REAL = "real", "Real"

    class SendStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    send_run = models.ForeignKey(
        SendRun,
        on_delete=models.CASCADE,
        related_name="sent_logs",
    )
    job_posting = models.ForeignKey(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="sent_logs",
    )
    job_recruiter_target = models.ForeignKey(
        JobRecruiterTarget,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_logs",
    )
    sender_account = models.ForeignKey(
        SenderAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_logs",
    )

    to_email = models.EmailField()
    subject_snapshot = models.CharField(max_length=500)
    body_snapshot = models.TextField()
    attachment_path = models.CharField(max_length=1000, blank=True, default="")

    send_type = models.CharField(max_length=10, choices=SendType.choices)
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.INITIAL,
        db_index=True,
    )
    status = models.CharField(max_length=20, choices=SendStatus.choices, default=SendStatus.PENDING)
    bypass_global_dedupe = models.BooleanField(default=False)

    error_message = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["sender_account", "send_type", "status", "sent_at"], name="core_sentem_sender__79de49_idx"),
            models.Index(fields=["send_type", "status", "sent_at"], name="core_sentem_send_ty_3b3c14_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["to_email"],
                condition=Q(send_type="real", status="sent", message_type="initial", bypass_global_dedupe=False),
                name="uniq_real_sent_initial_once",
            ),
            models.UniqueConstraint(
                fields=["to_email"],
                condition=Q(
                    send_type="real",
                    status__in=["pending", "sent"],
                    message_type="initial",
                    bypass_global_dedupe=False,
                ),
                name="uniq_real_active_initial_once",
            )
        ]

    def __str__(self):
        return f"{self.send_type} | {self.to_email} | {self.status}"


class SuppressedEmail(models.Model):
    class Reason(models.TextChoices):
        HARD_BOUNCE = "hard_bounce", "Hard Bounce"
        BAD_ADDRESS = "bad_address", "Bad Address"
        OPT_OUT = "opt_out", "Opt Out"
        COMPLAINT = "complaint", "Complaint"
        MANUAL = "manual", "Manual"

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=30, choices=Reason.choices, default=Reason.MANUAL)
    source_error = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["email"]

    def clean(self):
        super().clean()
        self.email = normalize_email_address(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email_address(self.email)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.email} | {self.reason}"


class EmailVerification(models.Model):
    class Decision(models.TextChoices):
        ALLOW = "allow", "Allow"
        BLOCK = "block", "Block"
        DEFER = "defer", "Defer"

    email = models.EmailField(unique=True)
    provider = models.CharField(max_length=50, default="emailverifier.io")
    decision = models.CharField(max_length=20, choices=Decision.choices, db_index=True)
    provider_status = models.CharField(max_length=100, blank=True, default="", db_index=True)
    reason = models.CharField(max_length=500, blank=True, default="")
    is_catch_all = models.BooleanField(default=False, db_index=True)
    raw_response = models.JSONField(blank=True, default=dict)
    verified_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-verified_at", "email"]

    def save(self, *args, **kwargs):
        self.email = normalize_email_address(self.email)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.email} | {self.decision} | {self.provider_status}"


class InboxScanEvent(models.Model):
    class Classification(models.TextChoices):
        REPLY = "reply", "Reply"
        BOUNCE = "bounce", "Hard Bounce"
        BLOCKED = "blocked", "Block Warning"
        NOTICE = "notice", "Notice"

    sender_account = models.ForeignKey(
        SenderAccount,
        on_delete=models.CASCADE,
        related_name="inbox_scan_events",
    )
    message_key = models.CharField(max_length=500)
    from_header = models.CharField(max_length=1000, blank=True, default="")
    subject = models.CharField(max_length=1000, blank=True, default="")
    message_date = models.DateTimeField(null=True, blank=True)
    classification = models.CharField(max_length=30, choices=Classification.choices, db_index=True)
    detected_email = models.EmailField(blank=True, default="", db_index=True)
    matched_sent_log = models.ForeignKey(
        SentEmailLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbox_events",
    )
    snippet = models.TextField(blank=True, default="")
    raw_detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-message_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["sender_account", "message_key"], name="uniq_inbox_event_per_sender_msg"),
        ]

    def __str__(self):
        return f"{self.sender_account.email} | {self.classification} | {self.subject[:80]}"


class DailyCompanyReplyStop(models.Model):
    class ReplyDecision(models.TextChoices):
        STOP = "stop", "Stop"
        CONTINUE = "continue", "Continue"
        REVIEW = "review", "Needs Review"

    class DecisionSource(models.TextChoices):
        RULE = "rule", "Rule"
        OPENAI = "openai", "OpenAI"
        MANUAL = "manual", "Manual"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="daily_reply_stops",
    )
    stop_date = models.DateField(default=timezone.localdate, db_index=True)
    reply_event = models.ForeignKey(
        InboxScanEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_company_reply_stops",
    )
    matched_sent_log = models.ForeignKey(
        SentEmailLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_company_reply_stops",
    )
    respondent_email = models.EmailField(blank=True, default="")
    reply_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    reply_decision = models.CharField(
        max_length=20,
        choices=ReplyDecision.choices,
        default=ReplyDecision.STOP,
        db_index=True,
    )
    decision_source = models.CharField(
        max_length=20,
        choices=DecisionSource.choices,
        default=DecisionSource.RULE,
        db_index=True,
    )
    decision_confidence = models.FloatField(default=0.0)
    reason = models.CharField(max_length=500, blank=True, default="")
    reply_excerpt = models.TextField(blank=True, default="")
    manual_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-stop_date", "company__normalized_name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "stop_date"], name="uniq_daily_company_reply_stop"),
        ]

    def __str__(self):
        return f"{self.company.normalized_name} | {self.stop_date} | reply stop"


class SystemLog(models.Model):
    class EventType(models.TextChoices):
        IMPORTED = "imported", "Imported"
        FILTERED_APPLY = "filtered_apply", "Filtered Apply"
        FILTERED_REJECT = "filtered_reject", "Filtered Reject"
        DEDUPED = "deduped", "Deduped"
        RECRUITER_JSON_UPLOADED = "recruiter_json_uploaded", "Recruiter JSON Uploaded"
        APOLLO_DONE = "apollo_done", "Apollo Done"
        VERIFIER_DONE = "verifier_done", "Verifier Done"
        EMAIL_GENERATED = "email_generated", "Email Generated"
        APPROVED = "approved", "Approved"
        TEST_SENT = "test_sent", "Test Sent"
        REAL_SENT = "real_sent", "Real Sent"
        FAILED = "failed", "Failed"

    job_posting = models.ForeignKey(
        JobPosting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="system_logs",
    )
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class PromptTemplate(models.Model):
    """
    DB-stored prompt templates so prompt iteration is fast via Admin.
    Cold-email generation prefers the active prompt (if present) and falls back to file-based prompt.
    """

    class Purpose(models.TextChoices):
        COLD_EMAIL = "cold_email", "Cold Email"
        COMPANY_NORMALIZATION = "company_normalization", "Company Normalization"
        JOB_FILTER = "job_filter", "Job Filter"

    purpose = models.CharField(max_length=50, choices=Purpose.choices, db_index=True)
    name = models.CharField(max_length=120)
    content = models.TextField()
    is_active = models.BooleanField(default=False, db_index=True)

    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["purpose", "-is_active", "name", "-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["purpose", "name"], name="uniq_prompt_purpose_name"),
        ]

    def __str__(self):
        active = " (active)" if self.is_active else ""
        return f"{self.purpose}:{self.name}{active}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            PromptTemplate.objects.filter(purpose=self.purpose, is_active=True).exclude(id=self.id).update(is_active=False)


class JobFilterReview(models.Model):
    class Decision(models.TextChoices):
        APPLY = "APPLY", "Apply"
        REJECT = "REJECT", "Reject"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        ACCEPTED = "accepted", "Accepted"
        DISMISSED = "dismissed", "Dismissed"
        AUTO_KEEP = "auto_keep", "Auto Keep"

    job_posting = models.OneToOneField(
        JobPosting,
        on_delete=models.CASCADE,
        related_name="filter_review",
    )
    daily_batch = models.ForeignKey(
        DailyBatch,
        on_delete=models.CASCADE,
        related_name="filter_reviews",
    )
    decision = models.CharField(max_length=10, choices=Decision.choices, db_index=True)
    reason = models.CharField(max_length=120, blank=True, default="")
    raw_output = models.TextField(blank=True, default="")
    prompt_name = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)

    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "daily_batch__batch_date", "job_posting__company", "job_posting__title"]
        indexes = [
            models.Index(fields=["daily_batch", "status"], name="core_jobfil_daily_b_d8ac48_idx"),
            models.Index(fields=["decision", "status"], name="core_jobfil_decisio_565abe_idx"),
        ]

    def __str__(self):
        return f"{self.decision} review for Job {self.job_posting_id}: {self.status}"


class AppSetting(models.Model):
    """Singleton settings row (always id=1). Use AppSetting.get_solo() to read."""

    id = models.AutoField(primary_key=True)
    max_people_per_company = models.PositiveIntegerField(
        default=10,
        help_text="Global cap: max recruiters fetched and emails sent per company.",
    )
    company_cooldown_days = models.PositiveIntegerField(
        default=10,
        help_text="Global company cooldown in days for imports and manual LinkedIn flow.",
    )
    apollo_dashboard_credits_used = models.PositiveIntegerField(
        default=0,
        help_text="Manual lifetime Apollo credits-used number copied from Apollo's dashboard for reconciliation.",
    )
    apollo_checkpoint_date = models.DateField(null=True, blank=True)
    apollo_checkpoint_local_unique_emails = models.PositiveIntegerField(default=0)
    apollo_checkpoint_today_logged_credits = models.PositiveIntegerField(default=0)
    apollo_checkpoint_today_logged_emails = models.PositiveIntegerField(default=0)
    apollo_checkpoint_today_not_converted = models.PositiveIntegerField(default=0)
    email_generation_provider = models.CharField(max_length=20, default="openai")
    openai_email_model = models.CharField(max_length=120, blank=True, default="")
    anthropic_email_model = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        verbose_name = "App Setting"

    @classmethod
    def get_solo(cls) -> "AppSetting":
        obj, _ = cls.objects.get_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 10})
        return obj
