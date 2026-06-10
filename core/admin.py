from urllib.parse import urlencode
from datetime import date, datetime, time, timedelta

from django.contrib import admin
from django.contrib.admin.views.main import ChangeList
from django.db.models import Count, Max, Q
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils import timezone

from core.services.linkedin_people_url_service import build_company_people_base_url, build_people_search_url
from core.models import (
    ApprovalRecord,
    ApolloRejectedEmail,
    ApifyApiKey,
    BlacklistedCompany,
    Company,
    CompanyRecruiter,
    DailyBatch,
    DailyCompanyReplyStop,
    EmailVerification,
    GeneratedEmail,
    JobPosting,
    JobFilterReview,
    JobRecruiterTarget,
    RecruiterJsonUpload,
    SendRun,
    SenderAccount,
    SenderDailyUsage,
    SentEmailLog,
    SuppressedEmail,
    InboxScanEvent,
    SystemLog,
    TestEmailAccount,
    TargetedPeopleLookupRun,
    PromptTemplate,
)


admin.site.site_header = "Cold Email App Admin"
admin.site.site_title = "Cold Email App"
admin.site.index_title = "Cold Email Management"


@admin.register(DailyBatch)
class DailyBatchAdmin(admin.ModelAdmin):
    list_display = ("batch_date", "lookback_hours", "max_jobs_requested", "apify_run_status", "import_started_at", "import_finished_at")
    list_filter = ("apify_run_status", "batch_date")
    search_fields = ("notes",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-batch_date",)
    date_hierarchy = "batch_date"


@admin.register(ApifyApiKey)
class ApifyApiKeyAdmin(admin.ModelAdmin):
    list_display = ("key_name", "is_active", "is_exhausted", "rotation_order", "last_used_at", "last_success_at")
    list_filter = ("is_active", "is_exhausted")
    search_fields = ("key_name", "notes", "last_error")
    readonly_fields = ("created_at", "updated_at", "last_used_at", "last_success_at")
    ordering = ("rotation_order", "key_name")
    actions = ("reset_keys",)

    @admin.action(description="Reset selected keys (activate + clear exhausted/error)")
    def reset_keys(self, request, queryset):
        queryset.update(is_active=True, is_exhausted=False, last_error="")


@admin.register(BlacklistedCompany)
class BlacklistedCompanyAdmin(admin.ModelAdmin):
    list_display = ("normalized_name", "raw_name_latest", "company", "source", "updated_at")
    list_filter = ("source",)
    search_fields = ("normalized_name", "canonical_name", "raw_name_latest", "reason")
    readonly_fields = ("created_at", "updated_at", "canonical_name")
    list_select_related = ("company",)
    ordering = ("normalized_name",)


def _annotate_company_stats(qs):
    return qs.annotate(
        recruiters_total=Count("recruiters", filter=Q(recruiters__is_active=True), distinct=True),
        recruiters_with_email=Count(
            "recruiters",
            filter=Q(recruiters__is_active=True) & ~Q(recruiters__email__in=["", "none"]),
            distinct=True,
        ),
        recruiters_emailed=Count(
            "recruiters",
            filter=Q(recruiters__is_active=True, recruiters__email_sent=True),
            distinct=True,
        ),
        last_email_sent_date=Max("recruiters__email_sent_date"),
        jobs_total=Count("jobs", distinct=True),
        jobs_real_sent=Count("jobs", filter=Q(jobs__status=JobPosting.Status.REAL_SENT), distinct=True),
    )


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "normalized_name",
        "raw_name_latest",
        "legacy",
        "active_domain",
        "recruiters_total_col",
        "recruiters_with_email_col",
        "recruiters_emailed_col",
        "last_email_sent_date_col",
        "jobs_total_col",
        "jobs_real_sent_col",
        "is_blocked",
    )
    list_filter = ("legacy", "is_blocked", "domain_status", "pattern_status")
    search_fields = ("normalized_name", "raw_name_latest", "active_domain", "notes")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("normalized_name",)
    actions = ("mark_blocked", "mark_unblocked")

    class CompanyRecruiterInline(admin.TabularInline):
        model = CompanyRecruiter
        fields = (
            "person_name",
            "email",
            "legacy",
            "source",
            "email_status",
            "title_match",
            "location_match",
            "email_sent",
            "email_sent_date",
            "is_active",
            "updated_at",
        )
        readonly_fields = ("updated_at",)
        extra = 0
        show_change_link = True

    inlines = (CompanyRecruiterInline,)

    def get_queryset(self, request):
        return _annotate_company_stats(super().get_queryset(request))

    @admin.display(description="Recruiters")
    def recruiters_total_col(self, obj):
        return getattr(obj, "recruiters_total", 0)

    @admin.display(description="With email")
    def recruiters_with_email_col(self, obj):
        return getattr(obj, "recruiters_with_email", 0)

    @admin.display(description="Emailed")
    def recruiters_emailed_col(self, obj):
        return getattr(obj, "recruiters_emailed", 0)

    @admin.display(description="Last emailed")
    def last_email_sent_date_col(self, obj):
        return getattr(obj, "last_email_sent_date", None) or "-"

    @admin.display(description="Jobs")
    def jobs_total_col(self, obj):
        return getattr(obj, "jobs_total", 0)

    @admin.display(description="Jobs sent")
    def jobs_real_sent_col(self, obj):
        return getattr(obj, "jobs_real_sent", 0)

    @admin.action(description="Block selected companies")
    def mark_blocked(self, request, queryset):
        queryset.update(is_blocked=True)

    @admin.action(description="Unblock selected companies")
    def mark_unblocked(self, request, queryset):
        queryset.update(is_blocked=False)


@admin.register(RecruiterJsonUpload)
class RecruiterJsonUploadAdmin(admin.ModelAdmin):
    list_display = ("uploaded_at", "status", "normalized_company_count", "total_people_count")
    list_filter = ("status",)
    search_fields = ("raw_json_text", "notes")
    readonly_fields = ("uploaded_at",)
    ordering = ("-uploaded_at",)
    date_hierarchy = "uploaded_at"


@admin.register(TargetedPeopleLookupRun)
class TargetedPeopleLookupRunAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "company",
        "job_posting",
        "status",
        "emails_found",
        "credits_consumed",
        "allow_regular_fallback",
        "fallback_blocked_by_credit_guard",
    )
    list_filter = ("status", "allow_regular_fallback", "fallback_blocked_by_credit_guard", "created_at")
    search_fields = ("company__normalized_name", "raw_names", "error_message", "run_log_path")
    readonly_fields = (
        "created_at",
        "updated_at",
        "parsed_names",
        "source_counts",
        "result_rows",
        "totals",
        "run_log_path",
    )
    list_select_related = ("company", "job_posting")
    ordering = ("-created_at", "-id")


@admin.register(ApolloRejectedEmail)
class ApolloRejectedEmailAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "company",
        "person_name",
        "email",
        "email_status",
        "reason",
        "source_workflow",
        "apollo_person_id",
    )
    list_filter = ("email_status", "reason", "source_workflow", "created_at")
    search_fields = (
        "company__normalized_name",
        "person_name",
        "email",
        "apollo_person_id",
        "title",
        "reason",
    )
    readonly_fields = (
        "created_at",
        "company",
        "job_posting",
        "targeted_lookup_run",
        "person_name",
        "title",
        "email",
        "email_status",
        "apollo_person_id",
        "reason",
        "source_workflow",
        "run_log_path",
        "raw_payload",
    )
    list_select_related = ("company", "job_posting", "targeted_lookup_run")
    ordering = ("-created_at", "-id")


@admin.register(CompanyRecruiter)
class CompanyRecruiterAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "person_name",
        "email",
        "source",
        "email_status",
        "title_match",
        "location_match",
        "legacy",
        "is_active",
        "email_sent",
        "email_sent_date",
    )
    list_filter = ("source", "email_status", "title_match", "location_match", "legacy", "is_active", "email_sent")
    search_fields = ("company__normalized_name", "person_name", "normalized_person_name", "email")
    readonly_fields = ("created_at", "updated_at", "normalized_person_name")
    list_select_related = ("company",)
    ordering = ("company__normalized_name", "normalized_person_name")
    date_hierarchy = "email_sent_date"
    actions = ("mark_active", "mark_inactive")
    list_editable = ("is_active",)

    @admin.action(description="Mark selected recruiters active")
    def mark_active(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="Mark selected recruiters inactive")
    def mark_inactive(self, request, queryset):
        queryset.update(is_active=False)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            obj.job_targets.update(
                recipient_name_snapshot=obj.person_name,
                recipient_email_snapshot=obj.email,
            )


@admin.register(JobRecruiterTarget)
class JobRecruiterTargetAdmin(admin.ModelAdmin):
    list_display = ("job_posting", "recipient_name_snapshot", "recipient_email_snapshot", "selection_order", "is_selected_for_job", "is_sent_real")
    list_filter = ("is_selected_for_job", "is_sent_real", "job_posting__status")
    search_fields = ("job_posting__company", "job_posting__title", "recipient_name_snapshot", "recipient_email_snapshot")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("job_posting", "company_recruiter")
    ordering = ("job_posting", "selection_order", "recipient_name_snapshot")


@admin.register(JobPosting)
class JobPostingAdmin(admin.ModelAdmin):
    list_display = ("id", "company", "title", "location", "status", "source_platform", "is_manual_import", "job_link", "people_search_links")
    list_display_links = ("id", "title")
    # Use the date hierarchy (calendar-like navigation) instead of sidebar filters,
    # to avoid slow/long filter choice lists.
    list_filter = ("source_platform", "is_manual_import")
    change_list_template = "admin/core/jobposting/change_list.html"
    search_fields = (
        "external_job_id",
        "company",
        "title",
        "location",
        "normalized_company",
        "canonical_company",
        "linkedin_url",
        "normalized_linkedin_url",
        "apply_url",
        "company_linkedin",
        "dedupe_key",
    )
    readonly_fields = ("created_at", "updated_at", "job_link", "people_search_links")
    list_select_related = ("daily_batch", "company_ref")
    ordering = ("-created_at", "-id")
    date_hierarchy = None
    actions = ("generate_cold_emails", "auto_approve_safe_jobs", "clear_approvals", "prune_people_search_urls")

    class _JobPostingChangeList(ChangeList):
        """
        Allows custom query params (like our date picker) without Django admin trying
        to interpret them as ORM lookups (which triggers the "Database error" screen).
        """

        def get_filters_params(self, params=None):
            try:
                out = super().get_filters_params(params)  # Django 4.x
            except TypeError:
                out = super().get_filters_params()  # Django 3.x
            out.pop("created_on", None)
            return out

    def get_changelist(self, request, **kwargs):
        return self._JobPostingChangeList

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "by-date/",
                self.admin_site.admin_view(self.by_date_view),
                name="core_jobposting_by_date",
            ),
        ]
        return custom + urls

    def by_date_view(self, request):
        """
        Simple, robust date browser for jobs that avoids admin changelist filter lookups.
        Uses DailyBatch.batch_date (DateField) so SQLite date extraction is never needed.
        """
        raw = (request.GET.get("d") or "").strip()
        if raw:
            try:
                selected = date.fromisoformat(raw)
            except Exception:
                selected = timezone.localdate()
        else:
            selected = timezone.localdate()

        qs = (
            JobPosting.objects
            .select_related("daily_batch", "company_ref")
            .filter(daily_batch__batch_date=selected)
            .order_by("sort_company", "created_at", "id")
        )

        paginator = Paginator(qs, 200)
        page_number = request.GET.get("p") or "1"
        page_obj = paginator.get_page(page_number)

        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Job postings by date",
            "selected_date": selected.isoformat(),
            "prev_date": (selected - timedelta(days=1)).isoformat(),
            "next_date": (selected + timedelta(days=1)).isoformat(),
            "page_obj": page_obj,
            "jobs": list(page_obj.object_list),
        }
        return TemplateResponse(request, "admin/core/jobposting/by_date.html", ctx)

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        # Custom date picker filter (YYYY-MM-DD) for fast "show me jobs on X date" workflows.
        created_on = request.GET.get("created_on", "").strip()
        if created_on:
            try:
                selected = date.fromisoformat(created_on)
            except Exception:
                selected = None
            if selected:
                # Filter by batch_date (DateField) to avoid SQLite datetime cast/extract functions entirely.
                qs = qs.filter(daily_batch__batch_date=selected)
        return qs

    def get_ordering(self, request):
        # When a specific day is selected, group by company so it's easy to skim and spot duplicates.
        if (request.GET.get("created_on", "") or "").strip():
            return ("sort_company", "created_at", "id")
        return super().get_ordering(request)

    def changelist_view(self, request, extra_context=None):
        # If the user previously hit the admin "Invalid setup" screen, Django keeps `e=1` in the URL.
        # That forces the error template even after the underlying issue is fixed, so strip it.
        if "e" in request.GET:
            q = request.GET.copy()
            q.pop("e", None)
            q.pop("p", None)
            query = q.urlencode()
            return HttpResponseRedirect(f"{request.path}?{query}" if query else request.path)

        # Handle old date_hierarchy bookmarks by converting to our date picker when possible.
        year = (request.GET.get("created_at__year", "") or "").strip()
        month = (request.GET.get("created_at__month", "") or "").strip()
        day = (request.GET.get("created_at__day", "") or "").strip()

        if year and month and day:
            try:
                selected = date(int(year), int(month), int(day)).isoformat()
            except Exception:
                selected = ""

            q = request.GET.copy()
            for k in list(q.keys()):
                if k.startswith("created_at__"):
                    q.pop(k, None)
            q["created_on"] = selected
            q.pop("p", None)
            return HttpResponseRedirect(f"{request.path}?{q.urlencode()}")

        # Otherwise, just strip old date_hierarchy params.
        old_date_keys = [k for k in request.GET.keys() if k.startswith("created_at__")]
        if old_date_keys:
            q = request.GET.copy()
            for k in old_date_keys:
                q.pop(k, None)
            q.pop("p", None)
            return HttpResponseRedirect(f"{request.path}?{q.urlencode()}")

        extra_context = extra_context or {}
        created_on = (request.GET.get("created_on", "") or "").strip()
        extra_context["created_on"] = created_on

        # Build a "Clear" URL that preserves other filters/search but removes the date (and pagination).
        q = request.GET.copy()
        q.pop("created_on", None)
        q.pop("e", None)
        q.pop("p", None)
        query = q.urlencode()
        extra_context["created_on_clear_qs"] = query
        return super().changelist_view(request, extra_context=extra_context)

    class JobRecruiterTargetInline(admin.TabularInline):
        model = JobRecruiterTarget
        fields = (
            "selection_order",
            "recipient_name_snapshot",
            "recipient_email_snapshot",
            "is_selected_for_job",
            "is_sent_real",
            "send_block_reason",
            "updated_at",
        )
        readonly_fields = ("updated_at",)
        extra = 0

    class GeneratedEmailInline(admin.StackedInline):
        model = GeneratedEmail
        fields = ("subject", "body", "generation_status", "prompt_version", "edited_manually", "updated_at")
        readonly_fields = ("updated_at",)
        extra = 0
        max_num = 1

        def save_model(self, request, obj, form, change):
            if any(field in getattr(form, "changed_data", []) for field in ("subject", "body")):
                obj.edited_manually = True
            super().save_model(request, obj, form, change)

    inlines = (GeneratedEmailInline, JobRecruiterTargetInline)

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))

        # Insert our computed link widgets near the LinkedIn URL to reduce scrolling/confusion.
        if "linkedin_url" in fields:
            insert_at = fields.index("linkedin_url") + 1
        else:
            insert_at = 0

        for name in ("job_link", "people_search_links"):
            if name in fields:
                fields.remove(name)
            fields.insert(insert_at, name)
            insert_at += 1

        return fields

    @admin.display(description="Job URL")
    def job_link(self, obj: JobPosting):
        if not obj.linkedin_url:
            return "-"
        return format_html("<a class='ce-admin-btn ce-admin-btn-job' href='{}' target='_blank' rel='noopener noreferrer'>Open job</a>", obj.linkedin_url)

    @admin.display(description="People search")
    def people_search_links(self, obj: JobPosting):
        urls = getattr(obj, "linkedin_people_search_urls", None) or {}
        if not isinstance(urls, dict):
            urls = {}

        ds_director = urls.get("data_science_director") or urls.get("data science director") or urls.get("director") or ""
        ds_manager = urls.get("data_science_manager") or urls.get("data science manager") or ""
        ml_manager = urls.get("machine_learning_manager") or urls.get("machine learning manager") or ""

        # If these aren't stored yet but we have enough info, reconstruct a best-effort set.
        if (not ds_director or not ds_manager or not ml_manager) and getattr(obj, "company_linkedin", ""):
            base_people = build_company_people_base_url(getattr(obj, "company_linkedin", ""))
            geo = getattr(obj, "linkedin_geo_region_id", "") or ""
            if base_people and geo:
                ds_director = ds_director or build_people_search_url(base_people, geo, "data science director")
                ds_manager = ds_manager or build_people_search_url(base_people, geo, "data science manager")
                ml_manager = ml_manager or build_people_search_url(base_people, geo, "machine learning manager")

        links = []
        if ds_director:
            links.append(format_html("<a class='ce-admin-btn ce-admin-btn-people' href='{}' target='_blank' rel='noopener noreferrer'>Data Science Director</a>", ds_director))
        if ds_manager:
            links.append(format_html("<a class='ce-admin-btn ce-admin-btn-people' href='{}' target='_blank' rel='noopener noreferrer'>Data Science Manager</a>", ds_manager))
        if ml_manager:
            links.append(format_html("<a class='ce-admin-btn ce-admin-btn-people' href='{}' target='_blank' rel='noopener noreferrer'>ML Manager</a>", ml_manager))

        if not links:
            return "-"

        # Stack vertically to reduce misclicks.
        return format_html(
            "<div class='ce-admin-link-stack'>{}</div>",
            format_html_join(mark_safe("<br>"), "{}", ((link,) for link in links)),
        )

    @admin.action(description="Prune LinkedIn people search URLs (keep data/ML manager keys)")
    def prune_people_search_urls(self, request, queryset):
        kept_keys = {"data_science_director", "data_science_manager", "machine_learning_manager", "director"}
        updated = 0

        for job in queryset.iterator():
            urls = getattr(job, "linkedin_people_search_urls", None) or {}
            if not isinstance(urls, dict) or not urls:
                continue

            pruned = {k: v for k, v in urls.items() if k in kept_keys and safe_str(v).strip()}
            if pruned == urls:
                continue

            JobPosting.objects.filter(id=job.id).update(linkedin_people_search_urls=pruned)
            updated += 1

        self.message_user(request, f"Pruned people search URLs for {updated} job(s).")

    @admin.action(description="Generate cold emails (no sending)")
    def generate_cold_emails(self, request, queryset):
        from core.services.cold_email_generation_service import run_cold_email_generation_for_job
        from core.services.file_run_logger import create_run_log_path, append_exception

        ok = 0
        skipped = 0
        failed = 0

        for job in queryset:
            log_path = create_run_log_path("admin_generate_cold_email", f"{job.company}_{job.id}")
            try:
                stats = run_cold_email_generation_for_job(job, run_log_path=log_path)
                if stats.get("generated"):
                    ok += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                append_exception(log_path, f"ADMIN_GENERATE_ERROR job_id={job.id}", exc)

        self.message_user(request, f"Cold email generation done. generated={ok} skipped={skipped} failed={failed}")

    @admin.action(description="Clear approvals for selected jobs")
    def clear_approvals(self, request, queryset):
        ApprovalRecord.objects.filter(job_posting__in=queryset).update(is_approved=False, approved_at=None)
        self.message_user(request, f"Cleared approvals for {queryset.count()} jobs.")

    @admin.action(description="Auto-approve selected safe jobs")
    def auto_approve_safe_jobs(self, request, queryset):
        from core.services.auto_approval_service import auto_approve_batch

        batch_ids = list(queryset.values_list("daily_batch_id", flat=True).distinct())
        if len(batch_ids) != 1:
            self.message_user(request, "Select jobs from exactly one batch for auto-approval.", level="ERROR")
            return
        batch = queryset.first().daily_batch
        result = auto_approve_batch(batch)
        self.message_user(
            request,
            (
                "Auto-approval finished for batch "
                f"{result['batch_date']}. approved={result['approved']} "
                f"unapproved={result['unapproved']} safe_recipients={result['safe_recipient_count']}"
            ),
        )


@admin.register(GeneratedEmail)
class GeneratedEmailAdmin(admin.ModelAdmin):
    list_display = ("job_posting", "generation_status", "prompt_version", "edited_manually", "updated_at")
    list_filter = ("generation_status", "edited_manually")
    search_fields = ("job_posting__company", "job_posting__title", "subject", "prompt_version")
    list_select_related = ("job_posting",)
    ordering = ("-updated_at",)
    date_hierarchy = "updated_at"
    save_on_top = True

    def save_model(self, request, obj, form, change):
        if any(field in getattr(form, "changed_data", []) for field in ("subject", "body")):
            obj.edited_manually = True
        super().save_model(request, obj, form, change)


@admin.register(ApprovalRecord)
class ApprovalRecordAdmin(admin.ModelAdmin):
    list_display = ("job_posting", "is_approved", "approved_at", "updated_at")
    list_filter = ("is_approved",)
    search_fields = ("job_posting__company", "job_posting__title", "review_notes")
    list_select_related = ("job_posting",)
    ordering = ("-updated_at",)
    date_hierarchy = "approved_at"


@admin.register(SenderAccount)
class SenderAccountAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "display_name",
        "auth_method",
        "oauth_connected",
        "is_active",
        "is_paused",
        "paused_until",
        "daily_limit",
        "actual_sent_today_count",
        "round_robin_order",
        "last_used_at",
    )
    list_filter = ("auth_method", "is_active", "is_paused")
    search_fields = ("email", "display_name", "pause_reason", "notes")
    readonly_fields = (
        "created_at",
        "updated_at",
        "last_used_at",
        "oauth_connected",
        "oauth_scope",
        "oauth_token_expires_at",
        "oauth_token_updated_at",
    )
    fieldsets = (
        (None, {"fields": ("email", "display_name", "auth_method", "app_password")}),
        ("Sending controls", {"fields": ("is_active", "is_paused", "paused_until", "pause_reason", "daily_limit", "round_robin_order")}),
        ("Microsoft Graph OAuth", {"fields": ("oauth_connected", "oauth_scope", "oauth_token_expires_at", "oauth_token_updated_at")}),
        ("Notes", {"fields": ("notes",)}),
        ("Timestamps", {"fields": ("last_used_at", "created_at", "updated_at")}),
    )
    ordering = ("round_robin_order", "email")

    @admin.display(description="Sent today count")
    def actual_sent_today_count(self, obj):
        today = timezone.localdate()
        usage = obj.daily_usages.filter(usage_date=today).first()
        return int(getattr(usage, "sent_count", 0) or 0)

    @admin.display(boolean=True, description="OAuth connected")
    def oauth_connected(self, obj):
        return bool(getattr(obj, "oauth_refresh_token", ""))


@admin.register(SenderDailyUsage)
class SenderDailyUsageAdmin(admin.ModelAdmin):
    list_display = ("sender_account", "usage_date", "sent_count")
    list_filter = ("usage_date",)
    search_fields = ("sender_account__email",)
    list_select_related = ("sender_account",)
    ordering = ("-usage_date", "sender_account__email")
    date_hierarchy = "usage_date"


@admin.register(TestEmailAccount)
class TestEmailAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "is_active", "rotation_order")
    list_filter = ("is_active",)
    search_fields = ("email", "notes")
    ordering = ("rotation_order", "email")


@admin.register(SendRun)
class SendRunAdmin(admin.ModelAdmin):
    list_display = ("id", "run_type", "status", "started_at", "finished_at", "stopped_manually", "delay_seconds")
    list_filter = ("run_type", "status", "stopped_manually")
    search_fields = ("notes",)
    ordering = ("-id",)
    date_hierarchy = "started_at"


@admin.register(SentEmailLog)
class SentEmailLogAdmin(admin.ModelAdmin):
    list_display = ("id", "send_type", "message_type", "status", "to_email", "sender_account", "job_posting", "sent_at")
    list_filter = ("send_type", "message_type", "status")
    search_fields = ("to_email", "subject_snapshot", "job_posting__company", "job_posting__title", "error_message")
    list_select_related = ("send_run", "job_posting", "job_recruiter_target", "sender_account")
    ordering = ("-id",)
    date_hierarchy = "sent_at"


@admin.register(SuppressedEmail)
class SuppressedEmailAdmin(admin.ModelAdmin):
    list_display = ("email", "reason", "is_active", "created_at", "updated_at")
    list_filter = ("reason", "is_active")
    search_fields = ("email", "source_error")
    ordering = ("email",)
    date_hierarchy = "created_at"


@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    list_display = ("email", "decision", "provider_status", "is_catch_all", "verified_at", "expires_at")
    list_filter = ("decision", "provider_status", "is_catch_all", "provider")
    search_fields = ("email", "provider_status", "reason")
    readonly_fields = ("raw_response", "verified_at", "created_at", "updated_at")
    ordering = ("-verified_at", "email")
    date_hierarchy = "verified_at"


@admin.register(DailyCompanyReplyStop)
class DailyCompanyReplyStopAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "stop_date",
        "respondent_email",
        "reply_decision",
        "decision_source",
        "decision_confidence",
        "reply_at",
        "is_active",
    )
    list_filter = ("stop_date", "is_active", "reply_decision", "decision_source")
    search_fields = ("company__normalized_name", "respondent_email", "reason", "manual_note")
    list_select_related = ("company", "reply_event", "matched_sent_log")
    ordering = ("-stop_date", "company__normalized_name")
    date_hierarchy = "stop_date"


@admin.register(InboxScanEvent)
class InboxScanEventAdmin(admin.ModelAdmin):
    list_display = ("sender_account", "classification", "detected_email", "matched_sent_log", "message_date", "subject")
    list_filter = ("classification", "sender_account")
    search_fields = ("from_header", "subject", "detected_email", "snippet", "raw_detail")
    list_select_related = ("sender_account", "matched_sent_log")
    ordering = ("-message_date", "-id")
    date_hierarchy = "message_date"


@admin.register(SystemLog)
class SystemLogAdmin(admin.ModelAdmin):
    list_display = ("event_type", "job_posting", "created_at")
    list_filter = ("event_type",)
    search_fields = ("message", "job_posting__company", "job_posting__title")
    list_select_related = ("job_posting",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"


@admin.register(PromptTemplate)
class PromptTemplateAdmin(admin.ModelAdmin):
    list_display = ("purpose", "name", "is_active", "updated_at")
    list_filter = ("purpose", "is_active")
    search_fields = ("name", "content", "notes")
    ordering = ("purpose", "-is_active", "name", "-updated_at")
    readonly_fields = ("created_at", "updated_at")
    actions = ("activate_prompt", "deactivate_prompt")
    list_editable = ("is_active",)

    @admin.action(description="Activate selected prompt (per purpose)")
    def activate_prompt(self, request, queryset):
        updated = 0
        for obj in queryset:
            PromptTemplate.objects.filter(purpose=obj.purpose, is_active=True).exclude(id=obj.id).update(is_active=False)
            if not obj.is_active:
                obj.is_active = True
                obj.save(update_fields=["is_active", "updated_at"])
                updated += 1
        self.message_user(request, f"Activated {updated} prompt(s).")

    @admin.action(description="Deactivate selected prompts")
    def deactivate_prompt(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"Deactivated {count} prompt(s).")


@admin.register(JobFilterReview)
class JobFilterReviewAdmin(admin.ModelAdmin):
    list_display = ("job_posting", "daily_batch", "decision", "reason", "status", "updated_at")
    list_filter = ("decision", "status", "daily_batch")
    search_fields = ("job_posting__company", "job_posting__title", "reason", "raw_output")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("job_posting", "daily_batch")
    ordering = ("status", "-updated_at")
