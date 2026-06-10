import os
import json
from pathlib import Path
from urllib.parse import urlencode

from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.forms import (
    ApolloRecruiterFetchForm,
    CompanyDomainMappingApplyForm,
    CompanyDomainMappingTemplateForm,
    ConsultancyOutreachForm,
    DiceManualImportForm,
    GenerateColdEmailsForm,
    ImportPipelineForm,
    LegacyCompanyDomainMappingApplyForm,
    LegacyCompanyDomainRangeTemplateForm,
    LinkedInManualImportForm,
    LinkedInPostOutreachForm,
    LinkedInPostPreviewForm,
    ManualBulkEmailForm,
    RecruiterDecisionApplyForm,
    RecruiterJsonProcessForm,
    PromptExperimentForm,
    TestEmailDeliveryForm,
)
from core.services.apollo_recruiter_fetch_service import (
    fetch_apollo_credits_info,
    job_has_apify_person_lead,
    run_apollo_recruiter_fetch_for_pending_companies,
    upsert_apify_person_recruiter_from_apollo,
    upsert_company_recruiters_from_apollo,
)
from core.services.cold_email_generation_service import (
    run_cold_email_generation_for_eligible_jobs,
    run_cold_email_generation_for_job,
)
from core.models import BlacklistedCompany, Company, DailyBatch, JobPosting
from core.services.file_run_logger import append_and_print, append_exception, create_run_log_path
from core.services.import_pipeline_service import run_high_volume_unique_company_import, run_import_pipeline
from core.services.manual_linkedin_import_service import (
    prepare_manual_linkedin_result_for_display,
    run_manual_linkedin_import,
    sync_manual_jobs_with_existing_recruiters,
)
from core.services.external_job_import_service import (
    prepare_external_job_import_result_for_display,
    run_dice_job_import,
)
from core.services.linkedin_post_preview_service import preview_linkedin_post
from core.services.linkedin_post_outreach_service import (
    build_linkedin_post_review_history,
    create_linkedin_post_review_batch_from_rows,
    parse_linkedin_post_review_rows_from_post,
    run_linkedin_post_outreach,
    summarize_linkedin_post_rows,
)
from core.services.manual_bulk_email_service import send_manual_bulk_email
from core.services.consultancy_outreach_service import (
    parse_consultancy_outreach_json,
    send_consultancy_outreach,
)
from core.services.manual_job_email_service import (
    build_manual_job_email_review_context,
    create_manual_job_email_batch,
    run_manual_job_email_generation_for_token,
    send_manual_job_email_batch,
    update_manual_job_email_recipient,
)
from core.services.recruiter_decision_apply_service import apply_recruiter_decisions
from core.services.recruiter_import_service import process_recruiter_json_text
from core.services.read_only_review_dashboard_service import build_read_only_review_dashboard_context
from core.services.job_filter_review_service import (
    accept_job_filter_reviews,
    dismiss_job_filter_reviews,
    run_job_filter_review_for_batch,
)
from core.services.test_email_delivery_service import run_test_email_delivery_for_job
from core.services.test_email_delivery_service import parse_email_list as parse_test_email_list
from core.services.send_control_dashboard_service import (
    build_send_control_context,
    clear_stuck_runs,
    populated_batch_for_date as send_control_populated_batch_for_date,
    sender_daily_limit_summary,
    set_all_sender_daily_limits,
    start_batch_send,
    stop_sending,
)
from core.services.company_send_block_service import set_company_send_block
from core.services.live_company_reply_service import (
    build_live_company_reply_dashboard_context,
    manually_set_company_reply_stop,
)
from core.services.inbox_monitor_service import (
    DEFAULT_INBOX_MONITOR_MAX_MESSAGES,
    build_inbox_monitor_context,
    scan_and_store_inbox_events,
    scan_inbox_monitor,
)
from core.services.followup_dashboard_service import (
    build_followup_dashboard_context,
    run_company_followups_from_dashboard,
)
from core.services.send_timing_service import configured_send_delay_seconds
from core.services.job_target_sync_service import sync_job_targets_for_job
from core.services.app_settings_service import (
    get_company_cooldown_days,
    get_max_people_per_company,
    save_apollo_credit_checkpoint,
    save_pipeline_control_settings,
    set_company_cooldown_days,
    set_max_people_per_company,
)
from core.services.email_ai_settings_service import (
    get_email_ai_generation_settings,
    save_email_ai_generation_settings,
)
from core.services.openai_location_service import extract_us_state_from_location
from core.services.auto_approval_service import auto_approve_latest_batch
from core.services.openai_cold_email_service import get_cold_email_prompt_info
from core.services.email_sending_control_service import (
    get_email_sending_state,
    get_resume_attachment_state,
    set_email_sending_paused,
    set_resume_attachment_enabled,
)
from core.services.prompt_experiment_service import generate_email_for_job_with_custom_prompt
from core.services.company_domain_service import (
    apply_company_domain_mapping,
    apply_legacy_company_domain_mapping,
    get_company_domain_mapping_template_text,
    get_legacy_company_domain_mapping_template_text,
    is_usable_company_domain,
    normalize_domain_value,
)
from core.services.company_blacklist_service import blacklist_zero_usable_recipient_companies
from core.services.pipeline_dashboard_service import (
    _populated_batch_for_date,
    build_company_regex_search_context,
    build_pipeline_dashboard_context,
    company_needs_apollo_topup,
)
from core.services.targeted_people_lookup_service import (
    parse_target_person_names,
    run_bulk_targeted_people_lookup,
    run_targeted_people_lookup,
)
from core.utils import safe_str


def _pop_session_value(request, key: str):
    try:
        return request.session.pop(key, None)
    except Exception:
        return None


def _remember_test_email_delivery_form(request, form: TestEmailDeliveryForm) -> None:
    if form.is_valid():
        request.session["test_email_delivery_form_initial"] = {
            "job_id": form.cleaned_data.get("job_id"),
            "send_mode": safe_str(form.cleaned_data.get("send_mode")),
            "sender_email": safe_str(form.cleaned_data.get("sender_email")),
            "recipient_emails": safe_str(form.cleaned_data.get("recipient_emails")),
            "delay_seconds": form.cleaned_data.get("delay_seconds"),
            "use_openai_email": bool(form.cleaned_data.get("use_openai_email")),
            "regenerate_openai_each_run": bool(form.cleaned_data.get("regenerate_openai_each_run")),
            "prefix_subject_with_test_tag": bool(form.cleaned_data.get("prefix_subject_with_test_tag")),
        }
        return

    request.session["test_email_delivery_form_initial"] = {
        "job_id": safe_str(request.POST.get("job_id")),
        "send_mode": safe_str(request.POST.get("send_mode")),
        "sender_email": safe_str(request.POST.get("sender_email")),
        "recipient_emails": safe_str(request.POST.get("recipient_emails")),
        "delay_seconds": safe_str(request.POST.get("delay_seconds")),
        "use_openai_email": bool(request.POST.get("use_openai_email")),
        "regenerate_openai_each_run": bool(request.POST.get("regenerate_openai_each_run")),
        "prefix_subject_with_test_tag": bool(request.POST.get("prefix_subject_with_test_tag")),
    }


def _default_context(request):
    email_sending_state = get_email_sending_state()
    return {
        "import_form": ImportPipelineForm(),
        "recruiter_json_form": RecruiterJsonProcessForm(),
        "decision_form": RecruiterDecisionApplyForm(),
        "apollo_recruiter_fetch_form": ApolloRecruiterFetchForm(),
        "generate_cold_emails_form": GenerateColdEmailsForm(),
        "linkedin_manual_import_form": LinkedInManualImportForm(),
        "test_email_delivery_form": TestEmailDeliveryForm(
            initial=request.session.get("test_email_delivery_form_initial") or {}
        ),
        "prompt_experiment_form": PromptExperimentForm(
            initial={
                "job_id": request.session.get("prompt_experiment_job_id", 209),
                "prompt_text": safe_str(request.session.get("prompt_experiment_prompt_text", "")).strip(),
            }
        ),
        "company_domain_mapping_template_form": CompanyDomainMappingTemplateForm(),
        "company_domain_mapping_apply_form": CompanyDomainMappingApplyForm(),
        "legacy_company_domain_template_form": LegacyCompanyDomainRangeTemplateForm(),
        "legacy_company_domain_apply_form": LegacyCompanyDomainMappingApplyForm(),
        # Flash results (Post/Redirect/Get): set by POST views, displayed once.
        "import_result": _pop_session_value(request, "import_result"),
        "recruiter_json_result": _pop_session_value(request, "recruiter_json_result"),
        "decision_result": _pop_session_value(request, "decision_result"),
        "apollo_recruiter_fetch_result": _pop_session_value(request, "apollo_recruiter_fetch_result"),
        "generate_cold_emails_result": _pop_session_value(request, "generate_cold_emails_result"),
        "linkedin_manual_import_result": _pop_session_value(request, "linkedin_manual_import_result"),
        "test_email_delivery_result": _pop_session_value(request, "test_email_delivery_result"),
        "prompt_experiment_result": _pop_session_value(request, "prompt_experiment_result"),
        "company_domain_mapping_template_result": _pop_session_value(request, "company_domain_mapping_template_result"),
        "company_domain_mapping_apply_result": _pop_session_value(request, "company_domain_mapping_apply_result"),
        "legacy_company_domain_template_result": _pop_session_value(request, "legacy_company_domain_template_result"),
        "legacy_company_domain_apply_result": _pop_session_value(request, "legacy_company_domain_apply_result"),
        "email_sending_state": email_sending_state,
        "email_sending_enabled": bool(email_sending_state["effective_enabled"]),
        "cold_email_prompt_info": get_cold_email_prompt_info(),
    }


def operations_dashboard(request):
    return render(request, "core/operations_dashboard.html", _default_context(request))


@require_http_methods(["GET"])
def live_company_reply_dashboard_view(request):
    return render(
        request,
        "core/live_company_reply_dashboard.html",
        build_live_company_reply_dashboard_context(batch_date=safe_str(request.GET.get("batch_date", "")).strip()),
    )


@require_http_methods(["POST"])
def live_company_reply_manual_decision_view(request):
    company = get_object_or_404(Company, pk=request.POST.get("company_id"))
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    selected_date = build_live_company_reply_dashboard_context(batch_date=batch_date)["selected_date"]
    action = safe_str(request.POST.get("action", "")).strip().lower()
    should_stop = action != "resume"
    manually_set_company_reply_stop(
        company=company,
        stop_date=timezone.localdate(),
        should_stop=should_stop,
        note=safe_str(request.POST.get("note", "")).strip(),
    )
    return redirect(f"{request.path.rsplit('/manual-decision/', 1)[0]}/?{urlencode({'batch_date': selected_date.isoformat()})}")


@require_http_methods(["GET"])
def linkedin_job_id_mapper_view(request):
    batch_date = safe_str(request.GET.get("batch_date", "")).strip()
    batch = _populated_batch_for_date(batch_date)
    latest_urls = []

    if batch:
        rows = (
            JobPosting.objects
            .filter(daily_batch=batch, is_manual_email_job=False)
            .order_by("id")
            .values_list("normalized_linkedin_url", "linkedin_url")
        )
        seen = set()
        for normalized_url, raw_url in rows:
            url = safe_str(normalized_url).strip() or safe_str(raw_url).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            latest_urls.append(url)

    return render(
        request,
        "core/linkedin_job_id_mapper.html",
        {
            "latest_linkedin_urls_text": "\n".join(latest_urls),
            "latest_linkedin_urls_count": len(latest_urls),
            "latest_batch_date": batch.batch_date.isoformat() if batch else "",
        },
    )


@require_http_methods(["GET", "POST"])
def linkedin_post_preview_view(request):
    result = None
    initial_url = safe_str(request.GET.get("url", "")).strip()
    history_date = safe_str(request.GET.get("history_date", "")).strip()
    if request.method == "POST":
        action = safe_str(request.POST.get("action", "extract")).strip().lower()
        if action == "create_review_from_rows":
            rows = parse_linkedin_post_review_rows_from_post(request.POST)
            source_urls = []
            for row in rows:
                source_url = safe_str(row.get("url") or row.get("canonical_url")).strip()
                if source_url:
                    source_urls.append(source_url)
            form = LinkedInPostOutreachForm(initial={"linkedin_post_urls": "\n".join(source_urls)})
            try:
                batch_result = create_linkedin_post_review_batch_from_rows(rows)
            except Exception as exc:
                batch_result = {"ok": False, "error": str(exc)[:4000]}
            totals = summarize_linkedin_post_rows(rows)
            totals["review_rows"] = int((batch_result.get("totals") or {}).get("valid_unique_rows") or 0)
            result = {
                "ok": batch_result.get("ok") is not False,
                "totals": totals,
                "rows": rows,
                "invalid_rows": [],
                "review_batch": batch_result,
            }
            if batch_result.get("ok") is False:
                messages.error(request, f"Review batch was not created: {batch_result.get('error', 'check missing fields')}")
            else:
                messages.success(
                    request,
                    (
                        "LinkedIn post review batch created. "
                        f"rows={totals.get('review_rows', 0)}. "
                        "Drafts were not generated yet."
                    ),
                )
                return redirect(safe_str(batch_result.get("review_url")).strip() or "linkedin_post_preview")
        else:
            form = LinkedInPostOutreachForm(request.POST)
            if form.is_valid():
                try:
                    result = run_linkedin_post_outreach(
                        raw_urls_text=safe_str(form.cleaned_data["linkedin_post_urls"]),
                        find_emails=bool(form.cleaned_data.get("find_emails")),
                        ai_extract_details=bool(form.cleaned_data.get("use_chatgpt_extraction")),
                        create_review_batch=False,
                    )
                    messages.success(
                        request,
                        (
                            "LinkedIn post outreach completed. "
                            f"posts={result.get('totals', {}).get('extracted_posts', 0)} "
                            f"emails={result.get('totals', {}).get('emails_found', 0)} "
                            f"credits={result.get('totals', {}).get('apollo_credits', 0)}"
                        ),
                    )
                except Exception as exc:
                    messages.error(request, f"LinkedIn post outreach failed: {exc}")
            else:
                messages.error(request, "Paste one or more valid LinkedIn post URLs.")
    else:
        form = LinkedInPostOutreachForm(initial={"linkedin_post_urls": initial_url})

    return render(
        request,
        "core/linkedin_post_preview.html",
        {
            "form": form,
            "result": result,
            "history": build_linkedin_post_review_history(selected_date=history_date),
        },
    )


@require_http_methods(["GET", "POST"])
def manual_linkedin_flow_view(request):
    result = prepare_manual_linkedin_result_for_display(request.session.get("manual_linkedin_flow_result"))
    sync_legacy_result = request.session.get("manual_linkedin_sync_legacy_result")
    company_cooldown_days = get_company_cooldown_days()
    max_people_per_company = get_max_people_per_company()
    latest_job_filter_batch = (
        DailyBatch.objects
        .filter(jobs__is_manual_email_job=False)
        .distinct()
        .order_by("-batch_date")
        .first()
    )
    latest_job_filter_batch_job_count = (
        latest_job_filter_batch.jobs.filter(is_manual_email_job=False).count()
        if latest_job_filter_batch
        else 0
    )
    base_context = {
        "result": result,
        "sync_legacy_result": sync_legacy_result,
        "company_cooldown_days": company_cooldown_days,
        "max_people_per_company": max_people_per_company,
        "sender_limit_summary": sender_daily_limit_summary(),
        "latest_job_filter_batch": latest_job_filter_batch,
        "latest_job_filter_batch_job_count": latest_job_filter_batch_job_count,
    }
    if request.method == "GET":
        form = LinkedInManualImportForm(initial={"cooldown_days": company_cooldown_days})
        context = {**base_context, "form": form}
        return render(
            request,
            "core/manual_linkedin_flow.html",
            context,
        )

    form = LinkedInManualImportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Manual LinkedIn flow form is invalid.")
        context = {**base_context, "form": form}
        return render(
            request,
            "core/manual_linkedin_flow.html",
            context,
        )

    view_log_path = create_run_log_path("manual_linkedin_flow", "all")
    append_and_print(view_log_path, "VIEW_START action=manual_linkedin_flow_view")
    try:
        result = run_manual_linkedin_import(
            raw_urls_text=safe_str(form.cleaned_data["linkedin_job_urls"]),
            cooldown_days=int(form.cleaned_data["cooldown_days"] or 0),
            apply_cooldown_filters=True,
            skip_blocked_companies=bool(form.cleaned_data.get("skip_blocked_companies")),
            use_openai_filter=bool(form.cleaned_data.get("use_openai_filter")),
            dry_run=bool(form.cleaned_data.get("dry_run")),
            force_refetch=bool(form.cleaned_data.get("force_refetch")),
            hiring_team_text=safe_str(form.cleaned_data.get("hiring_team_text")),
            log_path=view_log_path,
        )
        result["run_log_path"] = view_log_path
        request.session["manual_linkedin_flow_result"] = result
        messages.success(
            request,
            (
                "Manual LinkedIn flow completed. "
                f"created={result.get('created_jobs')} scrape_ok={result.get('scrape_ok')} "
                f"posters={result.get('hiring_team_leads_stored', 0)} "
                f"errors={result.get('job_errors')}"
            ),
        )
    except Exception as exc:
        append_exception(view_log_path, "VIEW_EXCEPTION action=manual_linkedin_flow_view", exc)
        request.session["manual_linkedin_flow_result"] = {"ok": False, "error": str(exc)[:4000], "run_log_path": view_log_path}
        messages.error(request, f"Manual LinkedIn flow failed: {exc}")

    return redirect("manual_linkedin_flow")


@require_http_methods(["GET", "POST"])
def manual_dice_flow_view(request):
    result = prepare_external_job_import_result_for_display(request.session.get("manual_dice_flow_result"))
    company_cooldown_days = get_company_cooldown_days()
    max_people_per_company = get_max_people_per_company()
    latest_dice_jobs = (
        JobPosting.objects
        .filter(source_platform=JobPosting.SourcePlatform.DICE)
        .select_related("daily_batch", "company_ref")
        .order_by("-created_at", "-id")[:12]
    )
    dice_total_jobs = JobPosting.objects.filter(source_platform=JobPosting.SourcePlatform.DICE).count()
    dice_today_jobs = JobPosting.objects.filter(
        source_platform=JobPosting.SourcePlatform.DICE,
        daily_batch__batch_date=timezone.localdate(),
    ).count()
    base_context = {
        "result": result,
        "company_cooldown_days": company_cooldown_days,
        "max_people_per_company": max_people_per_company,
        "sender_limit_summary": sender_daily_limit_summary(),
        "latest_dice_jobs": latest_dice_jobs,
        "dice_total_jobs": dice_total_jobs,
        "dice_today_jobs": dice_today_jobs,
    }
    if request.method == "GET":
        form = DiceManualImportForm(initial={"cooldown_days": company_cooldown_days})
        return render(request, "core/manual_dice_flow.html", {**base_context, "form": form})

    form = DiceManualImportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Manual Dice flow form is invalid.")
        return render(request, "core/manual_dice_flow.html", {**base_context, "form": form})

    view_log_path = create_run_log_path("manual_dice_flow", "all")
    append_and_print(view_log_path, "VIEW_START action=manual_dice_flow_view")
    try:
        result = run_dice_job_import(
            raw_urls_text=safe_str(form.cleaned_data["dice_job_urls"]),
            cooldown_days=int(form.cleaned_data["cooldown_days"] or 0),
            apply_cooldown_filters=True,
            skip_blocked_companies=bool(form.cleaned_data.get("skip_blocked_companies")),
            use_openai_filter=bool(form.cleaned_data.get("use_openai_filter")),
            dry_run=bool(form.cleaned_data.get("dry_run")),
            force_refetch=bool(form.cleaned_data.get("force_refetch")),
            log_path=view_log_path,
        )
        result["run_log_path"] = view_log_path
        request.session["manual_dice_flow_result"] = result
        messages.success(
            request,
            (
                "Manual Dice flow completed. "
                f"created={result.get('created_jobs')} updated={result.get('updated_jobs')} "
                f"scrape_ok={result.get('scrape_ok')} errors={result.get('job_errors')}"
            ),
        )
    except Exception as exc:
        append_exception(view_log_path, "VIEW_EXCEPTION action=manual_dice_flow_view", exc)
        request.session["manual_dice_flow_result"] = {"ok": False, "error": str(exc)[:4000], "run_log_path": view_log_path}
        messages.error(request, f"Manual Dice flow failed: {exc}")

    return redirect("manual_dice_flow")


@require_http_methods(["POST"])
def manual_linkedin_sync_legacy_view(request):
    try:
        result = sync_manual_jobs_with_existing_recruiters()
        messages.success(
            request,
            (
                "Legacy sync complete. "
                f"synced={result.get('synced')} no_recruiters={result.get('no_recruiters')} "
                f"errors={result.get('errors')} total_checked={result.get('total_jobs')}"
            ),
        )
        request.session["manual_linkedin_sync_legacy_result"] = result
    except Exception as exc:
        messages.error(request, f"Legacy sync failed: {exc}")

    return redirect("manual_linkedin_flow")


@require_http_methods(["POST"])
def manual_linkedin_apply_rejected_view(request):
    previous_result = prepare_manual_linkedin_result_for_display(request.session.get("manual_linkedin_flow_result"))
    if not isinstance(previous_result, dict):
        messages.error(request, "No rejected jobs are available from the latest manual LinkedIn run.")
        return redirect("manual_linkedin_flow")

    rejected_rows = list(previous_result.get("reject_decision_rows") or [])
    selected_urls = [safe_str(value).strip() for value in request.POST.getlist("reject_url") if safe_str(value).strip()]
    action = safe_str(request.POST.get("action")).strip()
    if action == "apply_all_rejected":
        selected_urls = [safe_str(row.get("linkedin_url")).strip() for row in rejected_rows if safe_str(row.get("linkedin_url")).strip()]
    elif action == "apply_all_skipped_rejected":
        selected_urls = [
            safe_str(row.get("linkedin_url")).strip()
            for row in previous_result.get("not_imported_rows") or []
            if safe_str(row.get("status")).strip().lower() in {"rejected", "openai_reject"}
            and safe_str(row.get("linkedin_url")).strip()
        ]

    unique_urls = []
    seen = set()
    for url in selected_urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)

    if not unique_urls:
        messages.error(request, "Select at least one rejected job with a LinkedIn URL to apply again.")
        return redirect("manual_linkedin_flow")

    view_log_path = create_run_log_path("manual_linkedin_apply_rejected", "selected")
    append_and_print(view_log_path, f"VIEW_START action=manual_linkedin_apply_rejected count={len(unique_urls)}")
    try:
        result = run_manual_linkedin_import(
            raw_urls_text="\n".join(unique_urls),
            cooldown_days=0,
            apply_cooldown_filters=False,
            skip_blocked_companies=True,
            use_openai_filter=False,
            dry_run=False,
            force_refetch=False,
            hiring_team_text="",
            log_path=view_log_path,
        )
        result["run_log_path"] = view_log_path
        result["reapplied_from_rejected"] = True
        result["reapplied_input_urls"] = unique_urls
        request.session["manual_linkedin_flow_result"] = result
        messages.success(
            request,
            (
                "Rejected jobs applied again with OpenAI filter bypassed. "
                f"selected={len(unique_urls)} created={result.get('created_jobs')} "
                f"skipped={result.get('manual_summary', {}).get('skipped_jobs', 0)} errors={result.get('job_errors')}"
            ),
        )
    except Exception as exc:
        append_exception(view_log_path, "VIEW_EXCEPTION action=manual_linkedin_apply_rejected", exc)
        messages.error(request, f"Applying rejected jobs again failed: {exc}")

    return redirect("manual_linkedin_flow")


@require_http_methods(["GET", "POST"])
def manual_bulk_email_view(request):
    result = _pop_session_value(request, "manual_bulk_email_result")
    job_email_result = _pop_session_value(request, "manual_job_email_result")
    if request.method == "GET":
        form = ManualBulkEmailForm()
        return render(
            request,
            "core/manual_bulk_email.html",
            {"form": form, "result": result, "job_email_result": job_email_result, "email_sending_state": get_email_sending_state()},
        )

    action = safe_str(request.POST.get("action")).strip()
    if action == "generate_manual_job_emails":
        try:
            job_email_result = create_manual_job_email_batch(
                names=request.POST.getlist("manual_job_person_name"),
                emails=request.POST.getlist("manual_job_person_email"),
                job_texts=request.POST.getlist("manual_job_text"),
            )
            request.session["manual_job_email_result"] = job_email_result
            totals = job_email_result.get("totals") or {}
            messages.success(
                request,
                (
                    "Manual job-tailored emails generated. "
                    f"jobs={totals.get('created_jobs')} generated={totals.get('generated')} "
                    f"skipped={totals.get('skipped_already_sent_or_pending')}"
                ),
            )
        except Exception as exc:
            request.session["manual_job_email_result"] = {"ok": False, "error": str(exc)[:4000]}
            messages.error(request, f"Manual job-tailored generation failed: {exc}")
        return redirect("manual_bulk_email")

    form = ManualBulkEmailForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Manual bulk email form is invalid. Confirm the send and check required fields.")
        return render(
            request,
            "core/manual_bulk_email.html",
            {"form": form, "result": result, "job_email_result": job_email_result, "email_sending_state": get_email_sending_state()},
        )

    try:
        result = send_manual_bulk_email(
            raw_named_recipients=form.cleaned_data.get("named_recipient_map") or "",
            raw_recipient_emails=form.cleaned_data["recipient_emails"],
            subject=form.cleaned_data["subject"],
            body=form.cleaned_data["body"],
            delay_seconds=int(form.cleaned_data["delay_seconds"] or 0),
        )
        request.session["manual_bulk_email_result"] = result
        totals = result.get("totals") or {}
        messages.success(
            request,
            (
                "Manual bulk email finished. "
                f"sent={totals.get('emails_sent')} failed={totals.get('emails_failed')} "
                f"skipped={totals.get('skipped_already_sent_or_pending')}"
            ),
        )
    except Exception as exc:
        request.session["manual_bulk_email_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Manual bulk email failed: {exc}")

    return redirect("manual_bulk_email")


@require_http_methods(["GET", "POST"])
def consultancy_outreach_view(request):
    result = _pop_session_value(request, "consultancy_outreach_result")
    preview = None
    if request.method == "GET":
        form = ConsultancyOutreachForm()
        return render(
            request,
            "core/consultancy_outreach.html",
            {
                "form": form,
                "preview": preview,
                "result": result,
                "email_sending_state": get_email_sending_state(),
            },
        )

    form = ConsultancyOutreachForm(request.POST)
    action = safe_str(request.POST.get("action", "preview")).strip().lower()
    if not form.is_valid():
        messages.error(request, "Consultancy outreach form is invalid. Check the JSON, subject, and message.")
        return render(
            request,
            "core/consultancy_outreach.html",
            {
                "form": form,
                "preview": preview,
                "result": result,
                "email_sending_state": get_email_sending_state(),
            },
        )

    try:
        preview = parse_consultancy_outreach_json(form.cleaned_data["consultancy_json"])
    except Exception as exc:
        messages.error(request, f"Could not parse consultancy JSON: {exc}")
        return render(
            request,
            "core/consultancy_outreach.html",
            {
                "form": form,
                "preview": preview,
                "result": result,
                "email_sending_state": get_email_sending_state(),
            },
        )

    if action != "send":
        messages.success(
            request,
            f"Preview ready: {preview['totals']['valid_recipients']} valid recipient(s), {preview['totals']['invalid_rows']} invalid/duplicate row(s).",
        )
        return render(
            request,
            "core/consultancy_outreach.html",
            {
                "form": form,
                "preview": preview,
                "result": result,
                "email_sending_state": get_email_sending_state(),
            },
        )

    if safe_str(request.POST.get("confirm_send")).strip().lower() not in {"1", "true", "on", "yes"}:
        messages.error(request, "Check the confirmation box before sending from the UI.")
        return render(
            request,
            "core/consultancy_outreach.html",
            {
                "form": form,
                "preview": preview,
                "result": result,
                "email_sending_state": get_email_sending_state(),
            },
        )

    try:
        result = send_consultancy_outreach(
            raw_json=form.cleaned_data["consultancy_json"],
            subject=form.cleaned_data["subject"],
            body=form.cleaned_data["body"],
            delay_seconds=int(form.cleaned_data["delay_seconds"] or 0),
        )
        request.session["consultancy_outreach_result"] = result
        totals = result.get("totals") or {}
        messages.success(
            request,
            (
                "Consultancy outreach finished. "
                f"sent={totals.get('emails_sent')} failed={totals.get('emails_failed')} "
                f"skipped={totals.get('skipped_already_sent_or_pending')}"
            ),
        )
    except Exception as exc:
        request.session["consultancy_outreach_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Consultancy outreach failed: {exc}")

    return redirect("consultancy_outreach")


@require_http_methods(["GET"])
def manual_job_email_review_view(request, token: str):
    context = build_manual_job_email_review_context(token=safe_str(token).strip())
    context["email_sending_state"] = get_email_sending_state()
    context["generate_result"] = _pop_session_value(request, "manual_job_email_generate_result")
    context["send_result"] = _pop_session_value(request, "manual_job_email_send_result")
    return render(request, "core/manual_job_email_review.html", context)


@require_http_methods(["POST"])
def manual_job_email_generate_view(request, token: str):
    token = safe_str(token).strip()
    generation_scope = safe_str(request.POST.get("generation_scope", "missing")).strip().lower()
    skip_existing = generation_scope != "all"
    try:
        result = run_manual_job_email_generation_for_token(token=token, skip_existing=skip_existing)
        request.session["manual_job_email_generate_result"] = result
        totals = result.get("totals") or {}
        action_label = "missing drafts" if skip_existing else "all drafts"
        messages.success(
            request,
            (
                f"Generated {action_label} for this manual review batch. "
                f"seen={totals.get('jobs_seen')} generated={totals.get('generated')} "
                f"skipped_existing={totals.get('skipped_existing')} errors={totals.get('generation_errors')}"
            ),
        )
    except Exception as exc:
        request.session["manual_job_email_generate_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Manual draft generation failed: {exc}")
    return redirect("manual_job_email_review", token=token)


@require_http_methods(["POST"])
def manual_job_email_recipient_update_view(request, token: str):
    token = safe_str(token).strip()
    try:
        result = update_manual_job_email_recipient(
            token=token,
            job_id=int(request.POST.get("job_id") or 0),
            name=safe_str(request.POST.get("recipient_name")).strip(),
            email=safe_str(request.POST.get("recipient_email")).strip(),
        )
        if result.get("already_real_sent_or_pending"):
            messages.warning(
                request,
                f"Updated recipient to {result.get('email')}, but this email already has a real initial or pending email.",
            )
        else:
            messages.success(request, f"Updated recipient for job {result.get('job_id')}.")
    except Exception as exc:
        messages.error(request, f"Recipient update failed: {exc}")
    return redirect("manual_job_email_review", token=token)


@require_http_methods(["POST"])
def manual_job_email_send_view(request, token: str):
    token = safe_str(token).strip()
    try:
        result = send_manual_job_email_batch(
            token=token,
            job_ids=[int(x) for x in request.POST.getlist("job_id") if safe_str(x).strip().isdigit()],
            delay_seconds=int(request.POST.get("delay_seconds", 15) or 0),
        )
        request.session["manual_job_email_send_result"] = result
        totals = result.get("totals") or {}
        messages.success(
            request,
            (
                "Manual job-tailored send finished. "
                f"sent={totals.get('emails_sent')} failed={totals.get('emails_failed')} "
                f"skipped={totals.get('skipped_already_sent_or_pending')}"
            ),
        )
    except Exception as exc:
        request.session["manual_job_email_send_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Manual job-tailored send failed: {exc}")
    return redirect("manual_job_email_review", token=token)


def _pipeline_redirect(batch_date: str = ""):
    batch_date = safe_str(batch_date).strip()
    url = "/pipeline-dashboard/"
    if batch_date:
        url = f"{url}?{urlencode({'batch_date': batch_date})}"
    return redirect(url)


def pipeline_dashboard_view(request):
    batch_date = safe_str(request.GET.get("batch_date", "")).strip()
    company_search_pattern = safe_str(request.GET.get("company_search", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    audit = context.get("apollo_credit_audit") or {}
    default_compare_result = {
        "checkpoint_dashboard": audit.get("dashboard_credits_used", 0),
        "current_dashboard": audit.get("expected_dashboard_now", 0),
        "dashboard_delta": audit.get("since_checkpoint_logged_credits", 0),
        "logged_credit_delta": audit.get("since_checkpoint_logged_credits", 0),
        "logged_email_delta": audit.get("since_checkpoint_logged_emails", 0),
        "logged_waste_delta": audit.get("since_checkpoint_logged_waste", 0),
        "local_unique_email_delta": audit.get("since_checkpoint_local_email_delta", 0),
        "unexplained_delta": 0,
        "auto_expected": True,
    }
    context.update(
        {
            "import_form": ImportPipelineForm(),
            "max_people_per_company": get_max_people_per_company(),
            "company_cooldown_days": get_company_cooldown_days(),
            "email_ai_settings": get_email_ai_generation_settings(),
            "apollo_credits_info": _pop_session_value(request, "apollo_credits_info"),
            "pipeline_domain_apply_result": _pop_session_value(request, "pipeline_domain_apply_result"),
            "pipeline_recruiter_result": _prepare_pipeline_recruiter_result_for_display(
                request.session.get("pipeline_recruiter_result")
            ),
            "apollo_compare_result": request.session.get("apollo_compare_result") or default_compare_result,
            "pipeline_targeted_lookup_result": _pop_session_value(request, "pipeline_targeted_lookup_result"),
            "pipeline_bulk_targeted_lookup_result": _pop_session_value(request, "pipeline_bulk_targeted_lookup_result"),
            "pipeline_batch_topup_result": _pop_session_value(request, "pipeline_batch_topup_result"),
            "pipeline_generate_result": _pop_session_value(request, "pipeline_generate_result"),
            "pipeline_import_result": _pop_session_value(request, "pipeline_import_result"),
            "pipeline_delete_missing_domain_jobs_result": _pop_session_value(request, "pipeline_delete_missing_domain_jobs_result"),
            "pipeline_delete_single_job_result": _pop_session_value(request, "pipeline_delete_single_job_result"),
            "pipeline_inline_domain_result": _pop_session_value(request, "pipeline_inline_domain_result"),
            "pipeline_manual_job_id_result": _pop_session_value(request, "pipeline_manual_job_id_result"),
            "pipeline_blacklist_result": _pop_session_value(request, "pipeline_blacklist_result"),
            "company_search": build_company_regex_search_context(company_search_pattern),
        }
    )
    return render(request, "core/pipeline_dashboard.html", context)


@require_http_methods(["POST"])
def set_email_generation_model_view(request):
    next_url = safe_str(request.POST.get("next")).strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = ""

    try:
        values = save_email_ai_generation_settings(
            provider=request.POST.get("email_ai_provider", "openai"),
            openai_model=request.POST.get("openai_email_model", ""),
            anthropic_model=request.POST.get("anthropic_email_model", ""),
        )
        messages.success(
            request,
            f"Email generation model updated: {values['provider_label']} using {values['model']}.",
        )
    except Exception as exc:
        messages.error(request, f"Failed to update email generation model: {exc}")

    return redirect(next_url or "pipeline_dashboard")


@require_http_methods(["POST"])
def pipeline_check_apollo_credits_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        info = fetch_apollo_credits_info()
        if info:
            request.session["apollo_credits_info"] = info
            messages.success(request, f"Apollo API response: {info}")
        else:
            messages.warning(request, "Apollo did not return credit balance info in the response. Check your Apollo dashboard directly.")
    except Exception as exc:
        messages.error(request, f"Apollo credits check failed: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_set_max_people_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        value = set_max_people_per_company(int(request.POST.get("max_people", 20)))
        messages.success(request, f"Max people per company updated to {value}.")
    except Exception as exc:
        messages.error(request, f"Failed to update setting: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_set_controls_view(request):
    next_url = safe_str(request.POST.get("next")).strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = ""
    try:
        values = save_pipeline_control_settings(
            max_people_per_company=int(request.POST.get("max_people_per_company", request.POST.get("max_people", 10)) or 10),
            company_cooldown_days=int(request.POST.get("company_cooldown_days", 0) or 0),
        )
        messages.success(
            request,
            (
                "Pipeline controls updated: "
                f"cooldown={values['company_cooldown_days']} days, "
                f"max people per company={values['max_people_per_company']}."
            ),
        )
    except Exception as exc:
        messages.error(request, f"Failed to update pipeline controls: {exc}")
    return redirect(next_url or "pipeline_dashboard")


@require_http_methods(["POST"])
def pipeline_set_company_cooldown_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        value = set_company_cooldown_days(int(request.POST.get("company_cooldown_days", 0) or 0))
        messages.success(request, f"Company cooldown updated to {value} days.")
    except Exception as exc:
        messages.error(request, f"Failed to update cooldown: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_set_apollo_dashboard_credits_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        value = int(request.POST.get("apollo_dashboard_credits_used", 0))
        context = build_pipeline_dashboard_context(batch_date=batch_date)
        audit = context["apollo_credit_audit"]
        today = context["today_apollo_log_report"]["totals"]
        checkpoint = save_apollo_credit_checkpoint(
            dashboard_credits_used=value,
            local_unique_emails=audit["local_unique_apollo_emails"],
            today_logged_credits=today["credits"],
            today_logged_emails=today["emails"],
            today_not_converted=today["not_converted"],
        )
        messages.success(
            request,
            (
                f"Apollo checkpoint saved at {checkpoint.apollo_dashboard_credits_used}. "
                f"Local baseline: {checkpoint.apollo_checkpoint_local_unique_emails} Apollo emails, "
                f"{checkpoint.apollo_checkpoint_today_logged_credits} credits logged today."
            ),
        )
    except Exception as exc:
        messages.error(request, f"Failed to update Apollo dashboard checkpoint: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_compare_apollo_dashboard_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        current_dashboard = max(0, int(request.POST.get("apollo_dashboard_current", 0)))
        context = build_pipeline_dashboard_context(batch_date=batch_date)
        audit = context["apollo_credit_audit"]
        today = context["today_apollo_log_report"]["totals"]
        dashboard_delta = current_dashboard - int(audit["dashboard_credits_used"] or 0)
        local_email_delta = int(audit["local_unique_apollo_emails"] or 0) - int(audit["checkpoint_local_unique_emails"] or 0)
        logged_credit_delta = int(today["credits"] or 0) - int(audit["checkpoint_today_logged_credits"] or 0)
        logged_email_delta = int(today["emails"] or 0) - int(audit["checkpoint_today_logged_emails"] or 0)
        logged_waste_delta = int(today["not_converted"] or 0) - int(audit["checkpoint_today_not_converted"] or 0)
        unexplained_delta = dashboard_delta - logged_credit_delta
        result = {
            "checkpoint_dashboard": int(audit["dashboard_credits_used"] or 0),
            "current_dashboard": current_dashboard,
            "dashboard_delta": dashboard_delta,
            "local_unique_email_delta": local_email_delta,
            "logged_credit_delta": logged_credit_delta,
            "logged_email_delta": logged_email_delta,
            "logged_waste_delta": logged_waste_delta,
            "unexplained_delta": unexplained_delta,
            "checkpoint_date": str(audit.get("checkpoint_date") or ""),
            "auto_expected": False,
        }
        request.session["apollo_compare_result"] = result
        if unexplained_delta > 0:
            messages.warning(request, f"Apollo increased by {dashboard_delta}, but our app logged {logged_credit_delta}. Unexplained gap: {unexplained_delta}.")
        else:
            messages.success(request, f"Apollo delta {dashboard_delta}; app logged {logged_credit_delta}; unexplained gap {unexplained_delta}.")
    except Exception as exc:
        messages.error(request, f"Failed to compare Apollo dashboard number: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_run_import_view(request):
    form = ImportPipelineForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Import form is invalid.")
        return redirect("pipeline_dashboard")

    try:
        result = run_import_pipeline(
            lookback_hours=form.cleaned_data["lookback_hours"],
            max_jobs=form.cleaned_data["limit"],
            actor_id=form.cleaned_data["actor_id"],
        )
        request.session["pipeline_import_result"] = result
        if result.get("ok"):
            messages.success(request, "Import pipeline completed.")
        else:
            messages.error(request, f"Import pipeline failed: {result.get('error')}")
    except Exception as exc:
        request.session["pipeline_import_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Import pipeline failed: {exc}")

    return redirect("pipeline_dashboard")


@require_http_methods(["POST"])
def pipeline_run_high_volume_import_view(request):
    form = ImportPipelineForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Import form is invalid.")
        return redirect("pipeline_dashboard")

    try:
        result = run_high_volume_unique_company_import(
            lookback_hours=form.cleaned_data["lookback_hours"],
            target_created_jobs=120,
            actor_id=form.cleaned_data["actor_id"],
            batch_size=10,
            max_runs=40,
        )
        request.session["pipeline_import_result"] = result
        if result.get("ok"):
            messages.success(
                request,
                (
                    "High-volume unique-company import completed: "
                    f"runs={result.get('runs_attempted')} raw={result.get('raw_jobs')} "
                    f"created={result.get('created_jobs')}."
                ),
            )
        else:
            messages.error(request, f"High-volume import failed: {result.get('error')}")
    except Exception as exc:
        request.session["pipeline_import_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"High-volume import failed: {exc}")

    return redirect("pipeline_dashboard")


@require_http_methods(["POST"])
def pipeline_run_today_exclusion_import_view(request):
    form = ImportPipelineForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Import form is invalid.")
        return redirect("pipeline_dashboard")

    try:
        result = run_high_volume_unique_company_import(
            lookback_hours=form.cleaned_data["lookback_hours"],
            target_created_jobs=120,
            actor_id=form.cleaned_data["actor_id"],
            batch_size=10,
            max_runs=40,
            exclusion_mode="today",
        )
        request.session["pipeline_import_result"] = result
        if result.get("ok"):
            messages.success(
                request,
                (
                    "Today-exclusion high-volume import completed: "
                    f"runs={result.get('runs_attempted')} raw={result.get('raw_jobs')} "
                    f"created={result.get('created_jobs')}."
                ),
            )
        else:
            messages.error(request, f"Today-exclusion import failed: {result.get('error')}")
    except Exception as exc:
        request.session["pipeline_import_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Today-exclusion import failed: {exc}")

    return redirect("pipeline_dashboard")


@require_http_methods(["POST"])
def pipeline_apply_latest_batch_domains_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    mapping_json = safe_str(request.POST.get("mapping_json"))
    try:
        result = apply_company_domain_mapping(mapping_json)
        request.session["pipeline_domain_apply_result"] = {"ok": True, "result": result}
        messages.success(
            request,
            (
                "Applied latest-batch domains. "
                f"updated={result.get('updated')} removed={result.get('removed')} "
                f"skipped={result.get('skipped_blank')} errors={result.get('errors')}"
            ),
        )
    except Exception as exc:
        request.session["pipeline_domain_apply_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Domain apply failed: {exc}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_update_inline_domains_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    company_ids = [
        int(row["company"].id)
        for row in (context.get("company_rows") or [])
        if row.get("company") and row["company"].id
    ]
    companies = {company.id: company for company in Company.objects.filter(id__in=company_ids)}
    result = {
        "rows_seen": len(company_ids),
        "updated": 0,
        "unchanged": 0,
        "skipped_blank": 0,
        "invalid_domain": 0,
        "errors": 0,
        "details": [],
    }

    for company_id in company_ids:
        company = companies.get(company_id)
        if not company:
            result["errors"] += 1
            continue

        raw_domain = safe_str(request.POST.get(f"domain_{company_id}", "")).strip()
        normalized_domain = normalize_domain_value(raw_domain)
        current_domain = normalize_domain_value(company.active_domain)

        if not raw_domain:
            result["skipped_blank"] += 1
            continue

        if not is_usable_company_domain(normalized_domain):
            result["invalid_domain"] += 1
            result["details"].append(
                {
                    "company": company.normalized_name,
                    "domain": raw_domain,
                    "normalized_domain": normalized_domain,
                    "status": "invalid_domain",
                }
            )
            continue

        if normalized_domain == current_domain:
            result["unchanged"] += 1
            continue

        company.active_domain = normalized_domain
        company.domain_status = Company.DomainStatus.SET
        company.save(update_fields=["active_domain", "domain_status", "updated_at"])
        result["updated"] += 1
        result["details"].append(
            {
                "company": company.normalized_name,
                "old_domain": current_domain,
                "new_domain": normalized_domain,
                "status": "updated",
            }
        )

    request.session["pipeline_inline_domain_result"] = result
    if result["invalid_domain"] or result["errors"]:
        messages.error(
            request,
            f"Saved domain edits with issues. updated={result['updated']} invalid={result['invalid_domain']} errors={result['errors']}",
        )
    else:
        messages.success(request, f"Saved domain edits. updated={result['updated']} unchanged={result['unchanged']}")
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_update_manual_job_ids_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    batch = context.get("batch")
    if not batch:
        messages.error(request, "No selected batch found.")
        return _pipeline_redirect(batch_date)

    job_ids = [
        int(row["job_id"])
        for row in (context.get("manual_job_id_rows") or [])
        if safe_str(row.get("job_id")).strip().isdigit()
    ]
    jobs = {job.id: job for job in JobPosting.objects.filter(id__in=job_ids, daily_batch=batch)}
    result = {
        "batch_date": batch.batch_date.isoformat(),
        "rows_seen": len(job_ids),
        "updated": 0,
        "unchanged": 0,
        "cleared": 0,
        "skipped_duplicate": 0,
        "errors": 0,
        "details": [],
    }
    posted_values_by_job_id = {
        job_id: safe_str(request.POST.get(f"manual_job_reference_id_{job_id}", "")).strip()[:255]
        for job_id in job_ids
    }
    posted_job_ids_by_value: dict[str, list[int]] = {}
    for job_id, value in posted_values_by_job_id.items():
        if not value:
            continue
        posted_job_ids_by_value.setdefault(value.lower(), []).append(job_id)
    duplicate_posted_values = {
        value_key
        for value_key, value_job_ids in posted_job_ids_by_value.items()
        if len(value_job_ids) > 1
    }

    for job_id in job_ids:
        job = jobs.get(job_id)
        if not job:
            result["errors"] += 1
            continue

        value = posted_values_by_job_id.get(job_id, "")
        current = safe_str(getattr(job, "manual_job_reference_id", "")).strip()
        value_key = value.lower()
        if value and value_key in duplicate_posted_values and value != current:
            result["skipped_duplicate"] += 1
            result["details"].append(
                {
                    "job_id": int(job.id),
                    "company": safe_str(job.company).strip(),
                    "title": safe_str(job.title).strip(),
                    "manual_job_reference_id": value,
                    "status": "skipped_duplicate_in_form",
                }
            )
            continue

        if value and value != current:
            existing = (
                JobPosting.objects
                .filter(daily_batch=batch, manual_job_reference_id__iexact=value)
                .exclude(id=job.id)
                .only("id", "company", "title")
                .first()
            )
            if existing:
                result["skipped_duplicate"] += 1
                result["details"].append(
                    {
                        "job_id": int(job.id),
                        "company": safe_str(job.company).strip(),
                        "title": safe_str(job.title).strip(),
                        "manual_job_reference_id": value,
                        "status": f"skipped_duplicate_existing_job_{existing.id}",
                    }
                )
                continue

        if value == current:
            result["unchanged"] += 1
            continue

        job.manual_job_reference_id = value
        job.save(update_fields=["manual_job_reference_id", "updated_at"])
        if value:
            result["updated"] += 1
            status = "updated"
        else:
            result["cleared"] += 1
            status = "cleared"
        result["details"].append(
            {
                "job_id": int(job.id),
                "company": safe_str(job.company).strip(),
                "title": safe_str(job.title).strip(),
                "manual_job_reference_id": value,
                "status": status,
            }
        )

    request.session["pipeline_manual_job_id_result"] = result
    message = (
        "Saved manual job IDs. "
        f"updated={result['updated']} cleared={result['cleared']} unchanged={result['unchanged']} "
        f"duplicates_skipped={result['skipped_duplicate']}."
    )
    if result["skipped_duplicate"]:
        messages.warning(request, message)
    else:
        messages.success(request, message)
    return _pipeline_redirect(batch_date or batch.batch_date.isoformat())


@require_http_methods(["POST"])
def pipeline_delete_latest_batch_missing_domain_jobs_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    batch = context.get("batch")
    if not batch:
        messages.error(request, "No selected batch found.")
        return _pipeline_redirect(batch_date)

    safe_job_ids = [
        row["job"].id
        for row in (context.get("safe_missing_domain_job_rows") or [])
        if row.get("job")
    ]
    qs = JobPosting.objects.filter(daily_batch=batch, id__in=safe_job_ids)
    deleted_jobs = qs.count()
    deleted_objects, deleted_by_model = qs.delete()

    result = {
        "batch_date": batch.batch_date.isoformat(),
        "safe_jobs_seen": len(safe_job_ids),
        "protected_jobs_with_apify_person_lead": len(context.get("protected_missing_domain_job_rows") or []),
        "jobs_deleted": deleted_jobs,
        "deleted_objects": deleted_objects,
        "deleted_by_model": deleted_by_model,
    }
    request.session["pipeline_delete_missing_domain_jobs_result"] = result
    messages.success(
        request,
        f"Deleted {deleted_jobs} safe jobs from {batch.batch_date} with no usable domain and no Apify person lead.",
    )
    return _pipeline_redirect(batch_date or batch.batch_date.isoformat())


@require_http_methods(["POST"])
def pipeline_delete_single_job_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    job_id = safe_str(request.POST.get("job_id", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    batch = context.get("batch")
    if not batch:
        messages.error(request, "No selected batch found.")
        return _pipeline_redirect(batch_date)

    job = (
        JobPosting.objects
        .filter(id=job_id, daily_batch=batch, is_manual_email_job=False)
        .select_related("company_ref")
        .first()
    )
    if not job:
        messages.error(request, "Could not find that job in the selected batch.")
        return _pipeline_redirect(batch_date or batch.batch_date.isoformat())

    result = {
        "batch_date": batch.batch_date.isoformat(),
        "job_id": int(job.id),
        "company": safe_str(job.company).strip(),
        "title": safe_str(job.title).strip(),
        "linkedin_url": safe_str(job.normalized_linkedin_url).strip() or safe_str(job.linkedin_url).strip(),
    }
    deleted_objects, deleted_by_model = job.delete()
    result["deleted_objects"] = deleted_objects
    result["deleted_by_model"] = deleted_by_model
    request.session["pipeline_delete_single_job_result"] = result
    messages.success(
        request,
        f"Deleted job {result['job_id']}: {result['company']} - {result['title']}",
    )
    return _pipeline_redirect(batch_date or batch.batch_date.isoformat())


def _merge_count_dict(target: dict, source: dict) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        key = safe_str(key).strip() or "unknown"
        try:
            amount = int(value or 0)
        except Exception:
            amount = 0
        target[key] = int(target.get(key) or 0) + amount


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


_APOLLO_SKIP_REASON_LABELS = {
    "non_data_science_manager_title": "Title was not data/ML/director enough",
    "missing_contact_title": "Profile had no usable title",
    "recruiting_or_talent_title_blocked": "Recruiting, talent, or HR title",
    "low_value_contact_title": "Low-value title for this outreach",
    "missing_or_placeholder_email": "Apollo did not return a usable email",
    "apollo_no_email": "Apollo did not return an email",
    "apollo_unavailable_email": "Apollo said email was unavailable",
    "apollo_invalid_email": "Apollo said email was invalid",
    "apollo_bounced_email": "Apollo marked the email as bounced",
    "apollo_spammy_email": "Apollo marked the email as spammy",
    "apollo_search_no_email_available": "Apollo search said no email was available",
    "duplicate_email_in_batch": "Duplicate email in this run",
    "email_already_contacted": "Email was already contacted",
    "person_already_contacted": "Person was already contacted",
    "person_already_contacted_fallback": "Person was already contacted",
    "email_already_contacted_fallback": "Email was already contacted",
    "credit_guard_stopped_enrichment": "Credit safety guard stopped enrichment",
    "credit_guard_stopped_run": "Credit safety guard stopped the run",
}


_APOLLO_STOP_REASON_LABELS = {
    "completed_selected_scope": "Finished the selected companies.",
    "completed_selected_scope_continue_after_waste": "Finished the selected companies. Continue mode was on.",
    "credit_guard_stopped_after_exact_person_lookup": "Stopped early because a named-person lookup spent a credit without getting a usable email.",
    "credit_guard_stopped_after_company_topup": "Stopped early because a company lookup spent a credit without getting a usable email.",
}


def _plain_apollo_skip_reason(raw_key: str) -> str:
    key = safe_str(raw_key).strip()
    if key.startswith("apollo_non_verified_email:"):
        status = key.rsplit(":", 1)[-1]
        return f"Apollo email not verified ({status or 'unknown'})"
    if ":" in key:
        key = key.rsplit(":", 1)[-1]
    return _APOLLO_SKIP_REASON_LABELS.get(key, key.replace("_", " ").title() if key else "Other")


def _top_apollo_skip_reason_rows(skip_reasons: dict, *, limit: int = 5) -> list:
    rows = []
    if not isinstance(skip_reasons, dict):
        return rows
    merged = {}
    for key, value in skip_reasons.items():
        label = _plain_apollo_skip_reason(key)
        merged[label] = merged.get(label, 0) + _as_int(value)
    for label, count in sorted(merged.items(), key=lambda item: item[1], reverse=True):
        if count <= 0:
            continue
        rows.append({"label": label, "count": count})
        if len(rows) >= limit:
            break
    return rows


def _apollo_company_result_reason(stats: dict, *, requested_slots: int, found: int, still_missing: int) -> str:
    if stats.get("error"):
        return f"Error: {safe_str(stats.get('error'))[:220]}"
    if _as_int(stats.get("errors")):
        return "Apollo had an error for this company. Open the run log for details."
    if requested_slots <= 0:
        return "No open person slots needed for this company."
    if still_missing <= 0:
        return "Filled the open slots Apollo was asked to fill."
    skip_rows = _top_apollo_skip_reason_rows(stats.get("skip_reasons") or {}, limit=2)
    skip_text = "; ".join(f"{row['count']} {row['label'].lower()}" for row in skip_rows)
    if found > 0 and skip_text:
        return f"Found {found} usable email(s), but not enough to fill every slot. Main reason: {skip_text}."
    if found > 0:
        return f"Found {found} usable email(s), but Apollo did not find enough extra matching people."
    if _as_int(stats.get("search_returned_people")) <= 0:
        return "Apollo returned 0 people for this company/domain."
    if skip_text:
        return f"Apollo found profiles, but skipped them. Main reason: {skip_text}."
    if _as_int(stats.get("credits_not_converted_to_email")) > 0:
        return "Apollo spent a credit but did not return a usable email."
    return "Apollo did not find a usable email for the open slots."


def _apollo_skipped_title_samples_from_log(raw_log_path: str, *, limit: int = 8) -> list:
    path_text = safe_str(raw_log_path).strip()
    if not path_text:
        return []

    try:
        log_root = (Path(settings.MEDIA_ROOT) / "run_logs").resolve()
        log_path = Path(path_text)
        if not log_path.is_absolute():
            log_path = log_root / log_path.name
        resolved = log_path.resolve()
        try:
            resolved.relative_to(log_root)
        except ValueError:
            return []
        if not resolved.exists() or not resolved.is_file():
            return []

        counts = {}
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "SEARCH_" not in line or "_SKIP" not in line or " title=" not in line:
                    continue
                title = line.rsplit(" title=", 1)[-1].strip()
                if not title or title == "[NONE]":
                    continue
                if len(title) > 160:
                    title = f"{title[:157]}..."
                counts[title] = counts.get(title, 0) + 1
    except Exception:
        return []

    rows = []
    for title, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())):
        rows.append({"title": title, "count": count})
        if len(rows) >= limit:
            break
    return rows


def _apollo_title_count_rows(title_counts: dict, *, limit: int = 8) -> list:
    if not isinstance(title_counts, dict):
        return []
    rows = []
    for title, count in sorted(
        title_counts.items(),
        key=lambda item: (-_as_int(item[1]), safe_str(item[0]).lower()),
    ):
        title = safe_str(title).strip()
        count = _as_int(count)
        if not title or count <= 0:
            continue
        rows.append({"title": title, "count": count})
        if len(rows) >= limit:
            break
    return rows


def _apollo_title_samples_for_company(stats: dict, *, limit: int = 8) -> list:
    rows = _apollo_title_count_rows((stats or {}).get("seen_title_counts"), limit=limit)
    if rows:
        return rows
    return _apollo_skipped_title_samples_from_log((stats or {}).get("run_log_path"), limit=limit)


def _prepare_pipeline_recruiter_result_for_display(result):
    if not isinstance(result, dict):
        return result

    totals = result.setdefault("totals", {})
    companies = result.get("companies") or []
    requested_slots = _as_int(totals.get("apollo_slots_requested"))
    if requested_slots <= 0:
        requested_slots = sum(
            max(
                0,
                _as_int(stats.get("requested_apollo_slots_at_click"))
                or _as_int(stats.get("remaining_send_capacity"))
                or _as_int(stats.get("max_people")),
            )
            for stats in companies
            if isinstance(stats, dict)
        )
        totals["apollo_slots_requested"] = requested_slots

    found = _as_int(totals.get("apollo_emails"))
    unverified = _as_int(totals.get("unverified_emails"))
    credits = _as_int(totals.get("credits_consumed"))
    not_converted = _as_int(totals.get("credits_not_converted_to_email"))
    eligible = _as_int(totals.get("eligible_company_count_at_start"))
    selected = _as_int(totals.get("selected_company_count"))
    ran = _as_int(totals.get("companies_seen"))
    exact_selected = _as_int(totals.get("selected_exact_person_job_count"))
    exact_ran = _as_int(totals.get("apify_person_jobs_seen"))
    exact_emails = _as_int(totals.get("apify_person_emails"))
    skipped_exact_success = _as_int(totals.get("companies_skipped_after_exact_person_success"))
    selected_not_run = max(0, selected - ran - skipped_exact_success)
    still_missing = max(0, requested_slots - found)
    eligible_not_selected = max(0, eligible - selected)
    stop_reason = safe_str(totals.get("stop_reason")).strip()

    company_rows = []
    for stats in companies:
        if not isinstance(stats, dict):
            continue
        company_requested = (
            _as_int(stats.get("requested_apollo_slots_at_click"))
            or _as_int(stats.get("remaining_send_capacity"))
            or _as_int(stats.get("max_people"))
        )
        company_found = _as_int(stats.get("emails_found"))
        company_missing = max(0, company_requested - company_found)
        company_rows.append(
            {
                "company": safe_str(stats.get("company")).strip() or "-",
                "needed": company_requested,
                "found": company_found,
                "unverified": _as_int(stats.get("unverified_emails")),
                "missing": company_missing,
                "credits": _as_int(stats.get("credits_consumed")),
                "searched_profiles": _as_int(stats.get("search_returned_people")),
                "reason": _apollo_company_result_reason(
                    stats,
                    requested_slots=company_requested,
                    found=company_found,
                    still_missing=company_missing,
                ),
                "run_log_path": safe_str(stats.get("run_log_path")).strip(),
                "title_samples": _apollo_title_samples_for_company(stats),
            }
        )

    display = {
        "plain_stop_reason": _APOLLO_STOP_REASON_LABELS.get(stop_reason, stop_reason.replace("_", " ").capitalize() if stop_reason else "-"),
        "eligible_not_selected": eligible_not_selected,
        "selected_not_run": selected_not_run,
        "exact_person_requested": exact_selected,
        "exact_person_ran": exact_ran,
        "exact_person_emails": exact_emails,
        "companies_skipped_after_exact_person_success": skipped_exact_success,
        "people_still_needed_after_run": still_missing,
        "credits_used_for_emails": max(0, min(credits, found)),
        "skip_reason_rows": _top_apollo_skip_reason_rows(totals.get("skip_reasons") or {}, limit=6),
        "company_rows": company_rows,
        "summary_sentence": (
            f"At the click, {eligible} companies needed people. "
            f"You selected {selected} compan{'y' if selected == 1 else 'ies'} and Apollo ran {ran}. "
            f"Those selected companies needed up to {requested_slots} more person email(s); Apollo accepted {found} verified email(s) "
            f"and rejected {unverified} non-verified email(s). "
            f"{not_converted} credit(s) did not turn into an email."
        ),
    }
    result["display"] = display
    return result


@require_http_methods(["POST"])
def pipeline_targeted_people_lookup_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    company_id = safe_str(request.POST.get("company_id")).strip()
    job_id = safe_str(request.POST.get("job_id")).strip()
    raw_names = safe_str(request.POST.get("target_names"))
    only_targeted_lookup = safe_str(request.POST.get("only_targeted_lookup")).strip().lower() in {"1", "true", "yes", "on"}
    allow_fallback = (
        not only_targeted_lookup
        and safe_str(request.POST.get("allow_regular_fallback", "1")).strip().lower() in {"1", "true", "yes", "on"}
    )
    global_max_people = get_max_people_per_company()
    try:
        requested_max_people = int(request.POST.get("max_people_for_company") or global_max_people)
    except Exception:
        requested_max_people = global_max_people
    max_people_for_company = min(global_max_people, max(1, requested_max_people))

    company = Company.objects.filter(id=company_id).first()
    if not company:
        messages.error(request, "Could not find that company for targeted lookup.")
        return _pipeline_redirect(batch_date)

    job = None
    if job_id:
        job = JobPosting.objects.filter(id=job_id, company_ref=company).first()

    parsed_names = parse_target_person_names(raw_names)
    if not parsed_names and not allow_fallback:
        messages.error(request, "Add at least one person name, or leave regular fallback enabled.")
        return _pipeline_redirect(batch_date)

    try:
        result = run_targeted_people_lookup(
            company=company,
            job=job,
            raw_names=raw_names,
            allow_regular_fallback=allow_fallback,
            max_people=max_people_for_company,
        )
        request.session["pipeline_targeted_lookup_result"] = result
        totals = result.get("totals") or {}
        messages.success(
            request,
            (
                f"Targeted lookup for {company.normalized_name} finished. "
                f"targeted_local={totals.get('targeted_local', 0)} "
                f"targeted_apollo={totals.get('targeted_apollo', 0)} "
                f"fallback={totals.get('regular_fallback', 0)} "
                f"credits={totals.get('credits_consumed', 0)} "
                f"selected={totals.get('current_selected_recipients', 0)}"
            ),
        )
    except Exception as exc:
        request.session["pipeline_targeted_lookup_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Targeted lookup failed for {company.normalized_name}: {exc}")

    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_bulk_targeted_people_lookup_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    company_domain_map = safe_str(request.POST.get("company_domain_map"))
    domain_people_map = safe_str(request.POST.get("domain_people_map"))
    only_targeted_lookup = safe_str(request.POST.get("only_targeted_lookup")).strip().lower() in {"1", "true", "yes", "on"}
    allow_fallback = (
        not only_targeted_lookup
        and safe_str(request.POST.get("allow_regular_fallback", "1")).strip().lower() in {"1", "true", "yes", "on"}
    )
    action = safe_str(request.POST.get("action", "preview")).strip().lower()
    dry_run = action != "run"

    try:
        result = run_bulk_targeted_people_lookup(
            company_domain_map_text=company_domain_map,
            domain_people_map_text=domain_people_map,
            allow_regular_fallback=allow_fallback,
            dry_run=dry_run,
            max_people=get_max_people_per_company(),
        )
        request.session["pipeline_bulk_targeted_lookup_result"] = result
        if dry_run:
            messages.success(
                request,
                (
                    "Bulk targeted lookup preview ready. "
                    f"planned={result.get('lookups_planned', 0)} "
                    f"names={result.get('names_submitted', 0)} "
                    f"over_slot={result.get('names_skipped_over_slot_limit', 0)}"
                ),
            )
        else:
            messages.success(
                request,
                (
                    "Bulk targeted lookup finished. "
                    f"runs={result.get('lookups_run', 0)} "
                    f"emails={result.get('emails_found', 0)} "
                    f"unverified={result.get('unverified_emails', 0)} "
                    f"credits={result.get('credits_consumed', 0)} "
                    f"not_converted={result.get('credits_not_converted_to_email', 0)}"
                ),
            )
    except Exception as exc:
        request.session["pipeline_bulk_targeted_lookup_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Bulk targeted lookup failed: {exc}")

    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_run_recruiter_topup_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    apify_person_rows = [row for row in (context.get("apify_person_ready_job_rows") or []) if row.get("job")]
    rows = [row for row in (context.get("recruiter_ready_rows") or []) if row.get("company")]
    limit_text = safe_str(request.POST.get("limit", "5")).strip().lower()
    test_mode = limit_text == "test1"
    continue_after_waste = limit_text == "all_continue"
    if test_mode:
        limit = 1
    elif limit_text in {"all", "all_continue"}:
        limit = len(rows)
    else:
        try:
            limit = max(1, int(limit_text or "5"))
        except Exception:
            limit = 5

    company_topup_cap = 1 if test_mode else get_max_people_per_company()
    selected_apify_person_rows = apify_person_rows[:limit]
    selected = rows[:limit]
    totals = {
        "requested_limit": limit_text,
        "continue_after_waste": bool(continue_after_waste),
        "eligible_company_count_at_start": len(rows),
        "eligible_exact_person_job_count_at_start": len(apify_person_rows),
        "selected_company_count": len(selected),
        "selected_exact_person_job_count": len(selected_apify_person_rows),
        "apollo_slots_requested": sum(_as_int(row.get("apollo_slots_needed")) for row in selected),
        "exact_person_slots_requested": len(selected_apify_person_rows),
        "companies_seen": 0,
        "companies_errors": 0,
        "companies_skipped_after_exact_person_success": 0,
        "apify_person_jobs_seen": 0,
        "apify_person_emails": 0,
        "legacy_reused": 0,
        "apollo_emails": 0,
        "unverified_emails": 0,
        "apollo_email_status_counts": {},
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "accepted_alternate_domain_emails": 0,
        "accepted_non_us_emails": 0,
        "apollo_search_calls": 0,
        "apollo_search_api_per_page_total": 0,
        "apollo_search_returned_people": 0,
        "skip_reasons": {},
        "wasted_credit_people": [],
        "stopped_credit_guard": 0,
        "stop_reason": "",
    }
    companies = []
    person_jobs = []
    fallback_company_ids = set()

    try:
        max_credit_waste = max(0, int(os.getenv("APOLLO_MAX_CREDITS_NOT_CONVERTED_PER_RUN", "0") or "0"))
    except Exception:
        max_credit_waste = 0

    def _credit_guard_hit() -> bool:
        if continue_after_waste:
            return False
        credits = int(totals.get("credits_consumed") or 0)
        emails = int(totals.get("apollo_emails") or 0)
        return credits > emails + max_credit_waste

    for row in selected_apify_person_rows:
        job = row["job"]
        totals["apify_person_jobs_seen"] += 1
        try:
            stats = upsert_apify_person_recruiter_from_apollo(
                job=job,
                run_log_path=create_run_log_path("pipeline_apify_person_match", f"job_{job.id}"),
            )
        except Exception as exc:
            totals["companies_errors"] += 1
            stats = {"job_id": job.id, "company": job.company, "error": str(exc)[:4000]}

        totals["apify_person_emails"] += int(stats.get("emails_found") or 0)
        totals["apollo_emails"] += int(stats.get("emails_found") or 0)
        totals["unverified_emails"] += int(stats.get("unverified_emails") or 0)
        totals["credits_consumed"] += int(stats.get("credits_consumed") or 0)
        totals["credits_not_converted_to_email"] += int(stats.get("credits_not_converted_to_email") or 0)
        totals["accepted_alternate_domain_emails"] += int(stats.get("accepted_alternate_domain_emails") or 0)
        totals["accepted_non_us_emails"] += int(stats.get("accepted_non_us_emails") or 0)
        totals["apollo_search_calls"] += int(stats.get("search_calls") or 0)
        totals["apollo_search_api_per_page_total"] += int(stats.get("search_api_per_page_total") or 0)
        totals["apollo_search_returned_people"] += int(stats.get("search_returned_people") or 0)
        _merge_count_dict(totals["apollo_email_status_counts"], stats.get("apollo_email_status_counts") or {})
        _merge_count_dict(totals["skip_reasons"], stats.get("skip_reasons") or {})
        totals["wasted_credit_people"].extend(stats.get("wasted_credit_people") or [])
        if stats.get("errors") or stats.get("error"):
            totals["companies_errors"] += 1
        if not int(stats.get("emails_found") or 0) and row.get("company"):
            fallback_company_ids.add(row["company"].id)
        person_jobs.append(stats)
        if _credit_guard_hit():
            totals["stopped_credit_guard"] = 1
            totals["stop_reason"] = "credit_guard_stopped_after_exact_person_lookup"
            _merge_count_dict(totals["skip_reasons"], {"credit_guard_stopped_run": 1})
            break

    for row in ([] if totals["stopped_credit_guard"] else selected):
        company = row["company"]
        exact_person_succeeded = (
            apify_person_rows
            and company.id not in fallback_company_ids
            and row.get("pending_apify_person_lead_count")
        )
        if exact_person_succeeded and not company_needs_apollo_topup(company, company_topup_cap):
            totals["companies_skipped_after_exact_person_success"] += 1
            continue
        totals["companies_seen"] += 1
        try:
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint=row.get("sample_location", ""),
                max_people=company_topup_cap,
                run_log_path=create_run_log_path("pipeline_recruiter_topup", company.normalized_name),
            )
        except Exception as exc:
            totals["companies_errors"] += 1
            stats = {"company": company.normalized_name, "error": str(exc)[:4000]}

        stats["requested_apollo_slots_at_click"] = _as_int(row.get("apollo_slots_needed"))
        stats["usable_recipients_before_run"] = _as_int(row.get("usable_recipient_count"))
        stats["pending_jobs_at_click"] = _as_int(row.get("pending_job_count"))
        stats["domain_at_click"] = safe_str(row.get("domain")).strip()
        totals["legacy_reused"] += int(stats.get("legacy_reused") or 0)
        totals["apollo_emails"] += int(stats.get("emails_found") or 0)
        totals["unverified_emails"] += int(stats.get("unverified_emails") or 0)
        totals["credits_consumed"] += int(stats.get("credits_consumed") or 0)
        totals["credits_not_converted_to_email"] += int(stats.get("credits_not_converted_to_email") or 0)
        totals["accepted_alternate_domain_emails"] += int(stats.get("accepted_alternate_domain_emails") or 0)
        totals["accepted_non_us_emails"] += int(stats.get("accepted_non_us_emails") or 0)
        totals["apollo_search_calls"] += int(stats.get("search_calls") or 0)
        totals["apollo_search_api_per_page_total"] += int(stats.get("search_api_per_page_total") or 0)
        totals["apollo_search_returned_people"] += int(stats.get("search_returned_people") or 0)
        _merge_count_dict(totals["apollo_email_status_counts"], stats.get("apollo_email_status_counts") or {})
        _merge_count_dict(totals["skip_reasons"], stats.get("skip_reasons") or {})
        totals["wasted_credit_people"].extend(stats.get("wasted_credit_people") or [])
        if stats.get("errors") or stats.get("error"):
            totals["companies_errors"] += 1
        companies.append(stats)
        if _credit_guard_hit():
            totals["stopped_credit_guard"] = 1
            totals["stop_reason"] = "credit_guard_stopped_after_company_topup"
            _merge_count_dict(totals["skip_reasons"], {"credit_guard_stopped_run": 1})
            break
    if not totals["stop_reason"]:
        totals["stop_reason"] = "completed_selected_scope_continue_after_waste" if continue_after_waste else "completed_selected_scope"

    result = _prepare_pipeline_recruiter_result_for_display(
        {"totals": totals, "apify_person_jobs": person_jobs, "companies": companies}
    )
    request.session["pipeline_recruiter_result"] = result
    messages.success(
        request,
        (
            "Recruiter top-up finished. "
            f"exact_person_jobs={totals['apify_person_jobs_seen']} companies={totals['companies_seen']} "
            f"apollo_emails={totals['apollo_emails']} "
            f"unverified={totals['unverified_emails']} "
            f"credits={totals['credits_consumed']} "
            f"not_converted={totals['credits_not_converted_to_email']} "
            f"alternate_domain={totals['accepted_alternate_domain_emails']} non_us={totals['accepted_non_us_emails']} "
            f"search_calls={totals['apollo_search_calls']} search_returned={totals['apollo_search_returned_people']} "
            f"credit_guard_stopped={totals['stopped_credit_guard']} "
            f"errors={totals['companies_errors']}"
        ),
    )
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_blacklist_zero_recipient_companies_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    batch = context.get("batch")
    if not batch:
        messages.error(request, "No selected batch found.")
        return _pipeline_redirect(batch_date)

    try:
        result = blacklist_zero_usable_recipient_companies(
            batch=batch,
            company_rows=context.get("company_rows") or [],
        )
        request.session["pipeline_blacklist_result"] = result
        messages.success(
            request,
            (
                "Blacklisted zero-recipient companies. "
                f"created={result['created']} updated={result['updated']} companies={result['companies_seen']}"
            ),
        )
    except Exception as exc:
        request.session["pipeline_blacklist_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Blacklist update failed: {exc}")
    return _pipeline_redirect(batch_date or batch.batch_date.isoformat())


@require_http_methods(["POST"])
def pipeline_unblacklist_company_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    company_id = safe_str(request.POST.get("company_id", "")).strip()
    company = Company.objects.filter(id=company_id).first()
    if not company:
        messages.error(request, "Could not find that company to unblacklist.")
        return _pipeline_redirect(batch_date)

    normalized_name = safe_str(company.normalized_name).strip().lower()
    deleted_count = 0
    deleted_count += BlacklistedCompany.objects.filter(company=company).delete()[0]
    if normalized_name:
        deleted_count += BlacklistedCompany.objects.filter(normalized_name=normalized_name).delete()[0]

    was_blocked = bool(company.is_blocked)
    if company.is_blocked:
        company.is_blocked = False
        company.save(update_fields=["is_blocked", "updated_at"])

    request.session["pipeline_blacklist_result"] = {
        "ok": True,
        "action": "unblacklist_company",
        "company": normalized_name or company.raw_name_latest,
        "deleted_blacklist_rows": deleted_count,
        "unblocked_company": was_blocked,
    }
    messages.success(
        request,
        f"Removed blacklist for {normalized_name or company.raw_name_latest}. "
        f"deleted_rows={deleted_count} unblocked={was_blocked}",
    )
    return _pipeline_redirect(batch_date)


@require_http_methods(["POST"])
def pipeline_generate_emails_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    context = build_pipeline_dashboard_context(batch_date=batch_date)
    batch = context.get("batch")
    if not batch:
        messages.error(request, "No selected batch found.")
        return _pipeline_redirect(batch_date)

    max_jobs = None
    raw_max_jobs = safe_str(request.POST.get("max_jobs", "")).strip()
    if raw_max_jobs:
        try:
            max_jobs = max(1, int(raw_max_jobs))
        except Exception:
            max_jobs = 100

    generation_scope = safe_str(request.POST.get("generation_scope", "missing")).strip().lower()
    skip_existing = generation_scope != "all"
    try:
        result = run_cold_email_generation_for_eligible_jobs(
            max_jobs=max_jobs,
            batch_date=batch.batch_date.isoformat(),
            skip_existing=skip_existing,
        )
        request.session["pipeline_generate_result"] = result
        totals = result.get("totals") or {}
        action_label = "missing drafts" if skip_existing else "all drafts"
        messages.success(
            request,
            (
                f"Email generation finished for {batch.batch_date} ({action_label}). "
                f"generated={totals.get('generated')} errors={totals.get('job_errors')}"
            ),
        )
    except Exception as exc:
        request.session["pipeline_generate_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Email generation failed: {exc}")
    return _pipeline_redirect(batch_date or batch.batch_date.isoformat())


def read_only_review_dashboard_view(request):
    context = build_read_only_review_dashboard_context(batch_date=safe_str(request.GET.get("batch_date")))
    context["email_ai_settings"] = get_email_ai_generation_settings()
    context["job_filter_review_result"] = _pop_session_value(request, "job_filter_review_result")
    context["job_filter_accept_result"] = _pop_session_value(request, "job_filter_accept_result")
    context["read_only_generate_email_result"] = _pop_session_value(request, "read_only_generate_email_result")
    context["read_only_bulk_generate_email_result"] = _pop_session_value(request, "read_only_bulk_generate_email_result")
    return render(request, "core/read_only_review_dashboard.html", context)


def _read_only_review_redirect(batch_date: str = "", job_id: str = ""):
    batch_date = safe_str(batch_date).strip()
    job_id = safe_str(job_id).strip()
    url = "/review-readonly/"
    if batch_date:
        url = f"{url}?batch_date={batch_date}"
    if job_id:
        url = f"{url}#job-{job_id}"
    return redirect(url)


@require_http_methods(["POST"])
def read_only_generate_job_email_view(request):
    job_id = safe_str(request.POST.get("job_id", "")).strip()
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    if not job_id.isdigit():
        messages.error(request, "Job ID is required to generate an email.")
        return _read_only_review_redirect(batch_date=batch_date)

    job = JobPosting.objects.filter(id=int(job_id), is_manual_email_job=False).select_related("daily_batch").first()
    if not job:
        messages.error(request, f"Could not find job {job_id} for email generation.")
        return _read_only_review_redirect(batch_date=batch_date, job_id=job_id)

    try:
        result = run_cold_email_generation_for_job(
            job=job,
            run_log_path=create_run_log_path("review_cold_email_job", f"{job.company}_{job.id}"),
        )
        request.session["read_only_generate_email_result"] = result
        if result.get("generated"):
            messages.success(request, f"Generated email for Job {job.id} - {job.title}.")
        elif result.get("skipped_no_recipient"):
            messages.warning(request, f"Job {job.id} was skipped because it has no selected recipient email.")
        elif result.get("skipped_no_description"):
            messages.warning(request, f"Job {job.id} was skipped because it has no job description.")
        elif result.get("error"):
            messages.error(request, f"Email generation failed for Job {job.id}: {result.get('error')}")
        else:
            messages.warning(request, f"No email was generated for Job {job.id}. Check the run log for details.")
    except Exception as exc:
        result = {"ok": False, "job_id": job.id, "company": job.company, "title": job.title, "error": str(exc)[:4000]}
        request.session["read_only_generate_email_result"] = result
        messages.error(request, f"Email generation failed for Job {job.id}: {exc}")

    return _read_only_review_redirect(batch_date=batch_date or job.daily_batch.batch_date.isoformat(), job_id=job_id)


@require_http_methods(["POST"])
def read_only_regenerate_batch_emails_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    if not batch_date:
        messages.error(request, "Batch date is required to regenerate all emails.")
        return redirect("read_only_review_dashboard")

    generation_scope = safe_str(request.POST.get("generation_scope", "all")).strip().lower()
    skip_existing = generation_scope != "all"
    try:
        result = run_cold_email_generation_for_eligible_jobs(
            max_jobs=None,
            batch_date=batch_date,
            skip_existing=skip_existing,
        )
        request.session["read_only_bulk_generate_email_result"] = result
        totals = result.get("totals") or {}
        action_label = "missing drafts" if skip_existing else "all drafts"
        messages.success(
            request,
            (
                f"Batch email generation finished ({action_label}). "
                f"jobs_seen={totals.get('jobs_seen')} generated={totals.get('generated')} "
                f"errors={totals.get('job_errors')}"
            ),
        )
    except Exception as exc:
        request.session["read_only_bulk_generate_email_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Batch email regeneration failed: {exc}")

    return _read_only_review_redirect(batch_date=batch_date)


@require_http_methods(["POST"])
def run_job_filter_review_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    if not batch_date:
        messages.error(request, "Batch date is required for job filter review.")
        return redirect("read_only_review_dashboard")

    try:
        result = run_job_filter_review_for_batch(batch_date=batch_date)
        request.session["job_filter_review_result"] = result
        messages.success(
            request,
            (
                "Job filter review completed. "
                f"seen={result.get('jobs_seen')} apply={result.get('apply')} "
                f"reject={result.get('reject')} errors={result.get('errors')}"
            ),
        )
    except Exception as exc:
        request.session["job_filter_review_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Job filter review failed: {exc}")

    return redirect(f"/review-readonly/?batch_date={batch_date}")


def _selected_review_ids(request):
    ids = []
    for raw_id in request.POST.getlist("review_id"):
        try:
            ids.append(int(raw_id))
        except Exception:
            continue
    return ids


@require_http_methods(["POST"])
def accept_job_filter_reviews_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    ids = _selected_review_ids(request)
    if not ids:
        messages.error(request, "Select at least one proposed reject to accept.")
        return redirect(f"/review-readonly/?batch_date={batch_date}" if batch_date else "read_only_review_dashboard")

    result = accept_job_filter_reviews(ids)
    request.session["job_filter_accept_result"] = result
    messages.success(
        request,
        f"Accepted {result.get('accepted')} proposed reject(s); blocked {result.get('jobs_blocked')} job(s).",
    )
    return redirect(f"/review-readonly/?batch_date={batch_date}" if batch_date else "read_only_review_dashboard")


@require_http_methods(["POST"])
def dismiss_job_filter_reviews_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    ids = _selected_review_ids(request)
    if not ids:
        messages.error(request, "Select at least one proposed reject to dismiss.")
        return redirect(f"/review-readonly/?batch_date={batch_date}" if batch_date else "read_only_review_dashboard")

    result = dismiss_job_filter_reviews(ids)
    request.session["job_filter_accept_result"] = result
    messages.success(request, f"Dismissed {result.get('dismissed')} proposed reject(s).")
    return redirect(f"/review-readonly/?batch_date={batch_date}" if batch_date else "read_only_review_dashboard")


def send_control_dashboard_view(request):
    batch_date = safe_str(request.GET.get("batch_date", "")).strip()
    context = build_send_control_context(batch_date=batch_date)
    context["auto_approve_result"] = _pop_session_value(request, "send_control_auto_approve_result")
    context["send_control_topup_result"] = _pop_session_value(request, "send_control_topup_result")
    return render(request, "core/send_control_dashboard.html", context)


@require_http_methods(["POST"])
def send_control_start_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        delay_min_seconds = max(0, int(request.POST.get("delay_min_seconds", "") or 0))
        delay_max_seconds = max(delay_min_seconds, int(request.POST.get("delay_max_seconds", "") or delay_min_seconds))
    except Exception:
        messages.error(request, "Delay range must be valid whole seconds.")
        suffix = f"?batch_date={batch_date}" if batch_date else ""
        return redirect(f"/send-control/{suffix}")

    ok, message = start_batch_send(
        batch_date,
        delay_min_seconds=delay_min_seconds,
        delay_max_seconds=delay_max_seconds,
    )
    if ok:
        messages.success(request, message)
    else:
        messages.error(request, message)
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(f"/send-control/{suffix}")


@require_http_methods(["POST"])
def send_control_stop_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    messages.success(request, stop_sending())
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(f"/send-control/{suffix}")


@require_http_methods(["POST"])
def send_control_clear_stuck_run_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    cleared = clear_stuck_runs(batch_date)
    messages.success(request, f"Cleared {cleared} stuck run(s) and unpaused sending.")
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(f"/send-control/{suffix}")


@require_http_methods(["POST"])
def send_control_set_sender_limit_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    next_url = safe_str(request.POST.get("next", "")).strip()
    redirect_url = next_url if next_url.startswith("/") and not next_url.startswith("//") else ""
    try:
        limit = int(request.POST.get("daily_limit", "") or 0)
        if limit < 1 or limit > 500:
            raise ValueError("limit_out_of_range")
    except Exception:
        messages.error(request, "Sender daily limit must be a whole number from 1 to 500.")
        suffix = f"?batch_date={batch_date}" if batch_date else ""
        return redirect(redirect_url or f"/send-control/{suffix}")

    result = set_all_sender_daily_limits(limit)
    messages.success(
        request,
        f"Updated {result['updated']} sender account(s) to a daily limit of {result['limit']}.",
    )
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(redirect_url or f"/send-control/{suffix}")


@require_http_methods(["POST"])
def send_control_set_resume_attachment_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    action = safe_str(request.POST.get("action", "toggle")).strip().lower() or "toggle"
    current = get_resume_attachment_state()

    if action in {"include", "enable", "on", "attach"}:
        enabled = True
    elif action in {"exclude", "disable", "off", "no_attach"}:
        enabled = False
    elif action == "toggle":
        enabled = not bool(current["enabled"])
    else:
        messages.error(request, f"Unknown resume attachment action: {action!r}")
        suffix = f"?batch_date={batch_date}" if batch_date else ""
        return redirect(f"/send-control/{suffix}")

    set_resume_attachment_enabled(enabled=enabled, persist_to_dotenv=True)
    if enabled:
        messages.success(request, "Resume attachment enabled for future sends.")
    else:
        messages.success(request, "Resume attachment disabled for future sends.")

    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(f"/send-control/{suffix}")


@require_http_methods(["POST"])
def send_control_batch_apollo_topup_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    raw_scope = safe_str(request.POST.get("scope", "approved")).strip().lower()
    needs_only = raw_scope in {"approved_needs_apollo", "all_batch_needs_apollo"}
    scope = "all_batch" if raw_scope == "all_batch_needs_apollo" else "approved"
    if raw_scope in {"all_batch", "approved"}:
        scope = raw_scope
    next_url = safe_str(request.POST.get("next", "")).strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = ""
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    redirect_url = next_url or f"/send-control/{suffix}"
    result_session_key = "pipeline_batch_topup_result" if next_url.startswith("/pipeline-dashboard/") else "send_control_topup_result"
    batch = send_control_populated_batch_for_date(batch_date)
    if not batch:
        messages.error(request, "No populated batch found for Apollo top-up.")
        return redirect(redirect_url)

    cap = get_max_people_per_company()
    jobs = JobPosting.objects.filter(
        daily_batch=batch,
        company_ref__isnull=False,
        company_ref__is_blocked=False,
        is_manual_email_job=False,
    ).select_related("company_ref")
    if scope != "all_batch":
        jobs = (
            jobs
            .filter(approval_record__is_approved=True, generated_email__isnull=False)
            .exclude(generated_email__subject="")
            .exclude(generated_email__body="")
        )

    blacklisted_company_ids = set(
        BlacklistedCompany.objects.filter(company_id__isnull=False).values_list("company_id", flat=True)
    )
    company_ids = list(
        jobs.exclude(company_ref_id__in=blacklisted_company_ids)
        .values_list("company_ref_id", flat=True)
        .distinct()
    )
    companies = list(Company.objects.filter(id__in=company_ids, is_blocked=False).order_by("normalized_name", "id"))
    if needs_only:
        companies = [
            company
            for company in companies
            if company_needs_apollo_topup(company, cap)
        ]
    jobs_by_company = {}
    for job in jobs:
        jobs_by_company.setdefault(job.company_ref_id, []).append(job)

    totals = {
        "scope": scope,
        "needs_only": needs_only,
        "batch_date": batch.batch_date.isoformat(),
        "max_people": cap,
        "companies_seen": 0,
        "companies_skipped_missing_domain": 0,
        "companies_errors": 0,
        "emails_found": 0,
        "unverified_emails": 0,
        "credits_consumed": 0,
        "credits_not_converted_to_email": 0,
        "jobs_synced": 0,
        "details": [],
    }

    for company in companies:
        totals["companies_seen"] += 1
        if not is_usable_company_domain(company.active_domain):
            totals["companies_skipped_missing_domain"] += 1
            totals["details"].append({"company": company.normalized_name, "status": "skipped_missing_domain"})
            continue

        company_jobs = jobs_by_company.get(company.id) or []
        raw_loc = safe_str(company_jobs[0].location if company_jobs else "").strip()
        location_hint = extract_us_state_from_location(raw_loc)
        try:
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint=location_hint,
                max_people=cap,
                run_log_path=create_run_log_path("send_control_batch_apollo_topup", company.normalized_name),
                allow_last_resort_titles=False,
                allow_paid_nonmatching_titles=False,
                allow_alternate_domain_emails=True,
                allow_broad_fallback_titles=True,
            )
            synced = 0
            for job in company_jobs:
                sync_job_targets_for_job(job=job, max_targets=cap, auto_select=True, allow_fallback_contacts=True)
                synced += 1
            totals["jobs_synced"] += synced
            totals["emails_found"] += int(stats.get("emails_found") or 0)
            totals["unverified_emails"] += int(stats.get("unverified_emails") or 0)
            totals["credits_consumed"] += int(stats.get("credits_consumed") or 0)
            totals["credits_not_converted_to_email"] += int(stats.get("credits_not_converted_to_email") or 0)
            totals["details"].append(
                {
                    "company": company.normalized_name,
                    "status": "completed",
                    "emails_found": int(stats.get("emails_found") or 0),
                    "unverified_emails": int(stats.get("unverified_emails") or 0),
                    "credits": int(stats.get("credits_consumed") or 0),
                    "jobs_synced": synced,
                }
            )
        except Exception as exc:
            totals["companies_errors"] += 1
            totals["details"].append({"company": company.normalized_name, "status": "error", "error": str(exc)[:500]})

    request.session[result_session_key] = totals
    messages.success(
        request,
        (
            f"Apollo top-up finished for {totals['companies_seen']} compan"
            f"{'y' if totals['companies_seen'] == 1 else 'ies'}; "
            f"verified_emails={totals['emails_found']} unverified={totals['unverified_emails']} credits={totals['credits_consumed']} "
            f"jobs_synced={totals['jobs_synced']} errors={totals['companies_errors']}."
        ),
    )
    return redirect(redirect_url)


@require_http_methods(["POST"])
def send_control_company_block_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    company_id = safe_str(request.POST.get("company_id", "")).strip()
    action = safe_str(request.POST.get("action", "block")).strip().lower()
    reason = safe_str(request.POST.get("reason", "")).strip()
    company = Company.objects.filter(id=company_id).first()
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    if not company:
        messages.error(request, "Could not find that company.")
        return redirect(f"/send-control/{suffix}")

    if action == "unblock":
        set_company_send_block(company=company, blocked=False)
        messages.success(request, f"Resumed sends for {company.normalized_name or company.raw_name_latest}.")
    else:
        set_company_send_block(company=company, blocked=True, reason=reason or "manual reply / no more sends")
        messages.success(
            request,
            (
                f"Stopped future sends to {company.normalized_name or company.raw_name_latest}. "
                "The active sender will skip this company and continue with other companies."
            ),
        )
    return redirect(f"/send-control/{suffix}")


def inbox_monitor_dashboard_view(request):
    return render(request, "core/inbox_monitor_dashboard.html", build_inbox_monitor_context())


def followup_dashboard_view(request):
    context = build_followup_dashboard_context()
    context["send_result"] = _pop_session_value(request, "followup_send_result")
    return render(request, "core/followup_dashboard.html", context)


@require_http_methods(["POST"])
def followup_send_selected_view(request):
    try:
        delay_seconds = max(0, int(request.POST.get("delay_seconds", "") or configured_send_delay_seconds()))
    except Exception:
        delay_seconds = configured_send_delay_seconds()
    try:
        result = run_company_followups_from_dashboard(post_data=request.POST, delay_seconds=delay_seconds)
        totals = result.get("totals", {})
        request.session["followup_send_result"] = result
        messages.success(
            request,
            (
                "Follow-up run finished. "
                f"selected={totals.get('selected', 0)} sent={totals.get('emails_sent', 0)} "
                f"failed={totals.get('emails_failed', 0)} skipped={totals.get('emails_skipped', 0)}"
            ),
        )
    except Exception as exc:
        request.session["followup_send_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Follow-up send failed: {exc}")
    return redirect("followup_dashboard")


def inbox_monitor_data_view(request):
    try:
        max_messages = int(request.GET.get("max_messages", DEFAULT_INBOX_MONITOR_MAX_MESSAGES))
    except Exception:
        max_messages = DEFAULT_INBOX_MONITOR_MAX_MESSAGES
    max_messages = max(1, min(500, max_messages))
    return JsonResponse(scan_inbox_monitor(max_messages=max_messages))


@require_http_methods(["POST"])
def inbox_monitor_scan_now_view(request):
    try:
        result = scan_and_store_inbox_events(max_messages=DEFAULT_INBOX_MONITOR_MAX_MESSAGES)
        stored = result.get("stored", {})
        totals = result.get("totals", {})
        messages.success(
            request,
            (
                "Inbox scan finished. "
                f"accounts={totals.get('ok', 0)}/{totals.get('accounts', 0)} "
                f"replies={totals.get('reply', 0)} bounces={totals.get('bounce', 0)} "
                f"blocks={totals.get('blocked', 0)} "
                f"new_events={stored.get('created', 0)} suppressed={stored.get('suppressed', 0)} "
                f"company_reply_stops={stored.get('reply_stops', 0)}"
            ),
        )
    except Exception as exc:
        messages.error(request, f"Inbox scan failed: {exc}")
    next_url = safe_str(request.POST.get("next", "")).strip()
    if not next_url or not next_url.startswith("/"):
        next_url = "/inbox-monitor/"
    return redirect(next_url)


@require_http_methods(["POST"])
def send_control_auto_approve_view(request):
    batch_date = safe_str(request.POST.get("batch_date", "")).strip()
    try:
        if batch_date:
            from core.models import DailyBatch
            from core.services.auto_approval_service import auto_approve_batch

            batch = DailyBatch.objects.filter(batch_date=batch_date).order_by("-id").first()
            if not batch:
                raise RuntimeError(f"No batch found for {batch_date}.")
            result = auto_approve_batch(batch)
        else:
            result = auto_approve_latest_batch()
        request.session["send_control_auto_approve_result"] = result
        messages.success(
            request,
            (
                "Auto-approval finished. "
                f"approved={result['approved']} unapproved={result['unapproved']} "
                f"safe_recipients={result['safe_recipient_count']}"
            ),
        )
    except Exception as exc:
        request.session["send_control_auto_approve_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Auto-approval failed: {exc}")
    suffix = f"?batch_date={batch_date}" if batch_date else ""
    return redirect(f"/send-control/{suffix}")


@require_http_methods(["POST"])
def toggle_email_sending_view(request):
    """
    Dashboard kill-switch for email sending.

    This toggles EMAIL_SENDING_PAUSED at runtime (and persists it to `.env` by default),
    blocking *all* sends including test deliveries.
    """
    action = safe_str(request.POST.get("action", "toggle")).strip().lower() or "toggle"
    current = get_email_sending_state()

    if action in {"pause", "disable"}:
        paused = True
    elif action in {"resume", "unpause", "enable"}:
        paused = False
    elif action == "toggle":
        paused = not bool(current["paused"])
    else:
        messages.error(request, f"Unknown action: {action!r}")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    set_email_sending_paused(paused=paused, persist_to_dotenv=True)
    state = get_email_sending_state()

    if state["paused"]:
        messages.success(request, "Email sending paused (kill-switch ON).")
    else:
        messages.success(request, "Email sending resumed (kill-switch OFF).")

    if not state["env_enabled"]:
        messages.warning(request, "Note: EMAIL_SENDING_ENABLED is not enabled; sending will remain blocked.")

    next_url = safe_str(request.POST.get("next", "")).strip()
    if next_url and not next_url.startswith("/"):
        next_url = ""
    return redirect(next_url or request.META.get("HTTP_REFERER", "/"))


@require_http_methods(["POST"])
def run_import_pipeline_view(request):
    form = ImportPipelineForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Import form is invalid.")
        return redirect("operations_dashboard")

    try:
        result = run_import_pipeline(
            lookback_hours=form.cleaned_data["lookback_hours"],
            max_jobs=form.cleaned_data["limit"],
            actor_id=form.cleaned_data["actor_id"],
        )
        request.session["import_result"] = result
        if result.get("ok"):
            messages.success(request, "Import pipeline completed successfully.")
        else:
            messages.error(request, f"Import pipeline failed: {result.get('error')}")
    except Exception as exc:
        # Defensive: run_import_pipeline should return stats even on error.
        request.session["import_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Import pipeline failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def run_test_email_delivery_view(request):
    form = TestEmailDeliveryForm(request.POST)
    _remember_test_email_delivery_form(request, form)

    if not form.is_valid():
        messages.error(request, "Test email delivery form is invalid.")
        return redirect("operations_dashboard")

    try:
        job_id = int(form.cleaned_data["job_id"])
        send_mode = safe_str(form.cleaned_data.get("send_mode"))
        sender_email = safe_str(form.cleaned_data.get("sender_email"))
        recipient_emails = parse_test_email_list(form.cleaned_data.get("recipient_emails") or "")
        delay_seconds = int(form.cleaned_data.get("delay_seconds") or 0)
        use_openai_email = bool(form.cleaned_data.get("use_openai_email"))
        regen_openai = bool(form.cleaned_data.get("regenerate_openai_each_run"))
        prefix_subject = bool(form.cleaned_data.get("prefix_subject_with_test_tag"))

        sender_emails = []
        if send_mode == "one_sender":
            if sender_email:
                sender_emails = [sender_email.strip().lower()]
            else:
                from core.models import SenderAccount

                first = (
                    SenderAccount.objects
                    .filter(is_active=True, is_paused=False)
                    .order_by("round_robin_order", "email", "id")
                    .values_list("email", flat=True)
                    .first()
                )
                if first:
                    sender_emails = [safe_str(first).strip().lower()]

        result = run_test_email_delivery_for_job(
            job_id=job_id,
            delay_seconds=delay_seconds,
            sender_emails=sender_emails or None,
            test_recipient_emails=recipient_emails or None,
            use_openai_email=use_openai_email,
            regenerate_if_generated_email_exists=regen_openai,
            prefix_subject_with_test_tag=prefix_subject,
        )

        request.session["test_email_delivery_result"] = result
        totals = (result or {}).get("totals") or {}
        messages.success(
            request,
            (
                "Delivery test finished. "
                f"sent={totals.get('emails_sent')} failed={totals.get('emails_failed')} "
                f"attempted={totals.get('emails_attempted')}"
            ),
        )
    except Exception as exc:
        request.session["test_email_delivery_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Delivery test failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def prompt_experiment_generate_view(request):
    """
    Temporary: generate subject/body for one selected job id with a custom prompt.
    This does NOT send emails.
    """
    form = PromptExperimentForm(request.POST)
    if not form.is_valid():
        request.session["prompt_experiment_result"] = {"ok": False, "error": "Prompt form is invalid."}
        messages.error(request, "Prompt form is invalid.")
        return redirect("operations_dashboard")

    prompt_text = safe_str(form.cleaned_data.get("prompt_text")).strip()
    job_id = int(form.cleaned_data.get("job_id") or 209)
    # Persist the last prompt so the dashboard textarea stays filled after POST/redirect.
    request.session["prompt_experiment_job_id"] = job_id
    request.session["prompt_experiment_prompt_text"] = prompt_text
    run_log_path = create_run_log_path("prompt_experiment", f"job_{job_id}")

    try:
        result = generate_email_for_job_with_custom_prompt(
            job_id=job_id,
            prompt_text=prompt_text,
            run_log_path=run_log_path,
        )
        request.session["prompt_experiment_result"] = result
        messages.success(request, f"Generated email for job {job_id}.")
    except Exception as exc:
        request.session["prompt_experiment_result"] = {"ok": False, "error": str(exc)[:4000], "run_log_path": run_log_path}
        messages.error(request, f"Prompt experiment failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def company_domain_mapping_template_view(request):
    form = CompanyDomainMappingTemplateForm(request.POST)
    if not form.is_valid():
        request.session["company_domain_mapping_template_result"] = {"ok": False, "error": "Template form is invalid."}
        messages.error(request, "Template form is invalid.")
        return redirect("operations_dashboard")

    only_missing = bool(form.cleaned_data.get("only_missing"))

    try:
        if only_missing:
            text = get_company_domain_mapping_template_text()
        else:
            # All companies with jobs (non-blocked), as { "company": "" } JSON.
            from django.db.models import Count, Q
            from core.models import Company

            qs = (
                Company.objects
                .filter(is_blocked=False, jobs__isnull=False, jobs__is_manual_email_job=False)
                .annotate(job_count=Count("jobs", filter=Q(jobs__is_manual_email_job=False), distinct=True))
                .order_by("normalized_name")
                .distinct()
            )
            payload = {safe_str(c.normalized_name): "" for c in qs}
            import json

            text = json.dumps(payload, indent=2, ensure_ascii=False)

        request.session["company_domain_mapping_template_result"] = {"ok": True, "text": text}
        messages.success(request, "Generated company-domain mapping template.")
    except Exception as exc:
        request.session["company_domain_mapping_template_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Template generation failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def company_domain_mapping_apply_view(request):
    form = CompanyDomainMappingApplyForm(request.POST)
    if not form.is_valid():
        request.session["company_domain_mapping_apply_result"] = {"ok": False, "error": "Apply form is invalid."}
        messages.error(request, "Apply form is invalid.")
        return redirect("operations_dashboard")

    mapping_json = safe_str(form.cleaned_data.get("mapping_json"))

    try:
        result = apply_company_domain_mapping(mapping_json)
        request.session["company_domain_mapping_apply_result"] = {"ok": True, "result": result}
        messages.success(
            request,
            (
                "Applied company-domain mapping. "
                f"updated={result.get('updated')} removed={result.get('removed')} skipped_blank={result.get('skipped_blank')} "
                f"not_found={result.get('not_found')} invalid_domain={result.get('invalid_domain')} errors={result.get('errors')}"
            ),
        )
    except Exception as exc:
        request.session["company_domain_mapping_apply_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Apply mapping failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def legacy_company_domain_template_view(request):
    form = LegacyCompanyDomainRangeTemplateForm(request.POST)
    if not form.is_valid():
        request.session["legacy_company_domain_template_result"] = {
            "ok": False,
            "error": "Legacy domain template form is invalid.",
        }
        messages.error(request, "Legacy domain template form is invalid.")
        return redirect("operations_dashboard")

    try:
        result = get_legacy_company_domain_mapping_template_text(
            start_range=form.cleaned_data["start_range"],
            end_range=form.cleaned_data["end_range"],
            only_missing=bool(form.cleaned_data.get("only_missing")),
        )
        request.session["legacy_company_domain_template_result"] = {"ok": True, **result}
        messages.success(
            request,
            (
                "Generated legacy company-domain template. "
                f"returned={result.get('returned')} total_in_scope={result.get('total_legacy_companies_in_scope')}"
            ),
        )
    except Exception as exc:
        request.session["legacy_company_domain_template_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Legacy template generation failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def legacy_company_domain_apply_view(request):
    form = LegacyCompanyDomainMappingApplyForm(request.POST)
    if not form.is_valid():
        request.session["legacy_company_domain_apply_result"] = {
            "ok": False,
            "error": "Legacy domain apply form is invalid.",
        }
        messages.error(request, "Legacy domain apply form is invalid.")
        return redirect("operations_dashboard")

    mapping_json = safe_str(form.cleaned_data.get("mapping_json"))

    try:
        result = apply_legacy_company_domain_mapping(mapping_json)
        request.session["legacy_company_domain_apply_result"] = {"ok": True, "result": result}
        messages.success(
            request,
            (
                "Applied legacy company-domain mapping. "
                f"updated={result.get('updated')} removed={result.get('removed')} skipped_blank={result.get('skipped_blank')} "
                f"not_found={result.get('not_found')} not_legacy={result.get('not_legacy')} "
                f"invalid_domain={result.get('invalid_domain')} errors={result.get('errors')}"
            ),
        )
    except Exception as exc:
        request.session["legacy_company_domain_apply_result"] = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Apply legacy mapping failed: {exc}")

    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def manual_linkedin_import_view(request):
    form = LinkedInManualImportForm(request.POST)
    result = None

    view_log_path = create_run_log_path("manual_linkedin_import_view", "all")
    append_and_print(view_log_path, "VIEW_START action=manual_linkedin_import_view")

    if not form.is_valid():
        append_and_print(view_log_path, f"VIEW_INVALID_FORM errors={form.errors}")
        messages.error(request, "Manual LinkedIn import form is invalid.")
        return redirect("operations_dashboard")

    try:
        urls_text = safe_str(form.cleaned_data["linkedin_job_urls"])
        cooldown_days = int(form.cleaned_data["cooldown_days"] or 0)
        apply_cooldown_filters = bool(form.cleaned_data.get("apply_cooldown_filters"))
        skip_blocked_companies = bool(form.cleaned_data.get("skip_blocked_companies"))
        use_openai_filter = bool(form.cleaned_data.get("use_openai_filter"))
        dry_run = bool(form.cleaned_data.get("dry_run"))

        append_and_print(
            view_log_path,
            "VIEW_FORM_DATA "
            f"cooldown_days={cooldown_days} apply_cooldown_filters={apply_cooldown_filters} "
            f"skip_blocked_companies={skip_blocked_companies} use_openai_filter={use_openai_filter} dry_run={dry_run}",
        )

        result = run_manual_linkedin_import(
            raw_urls_text=urls_text,
            cooldown_days=cooldown_days,
            apply_cooldown_filters=apply_cooldown_filters,
            skip_blocked_companies=skip_blocked_companies,
            use_openai_filter=use_openai_filter,
            dry_run=dry_run,
            hiring_team_text=safe_str(form.cleaned_data.get("hiring_team_text")),
            log_path=view_log_path,
        )
        try:
            result["run_log_path"] = view_log_path
        except Exception:
            pass

        messages.success(request, "Manual LinkedIn import completed.")
        append_and_print(
            view_log_path,
            f"VIEW_DONE created_jobs={result.get('created_jobs')} skipped_existing_url={result.get('skipped_existing_url')} "
            f"skipped_existing_external_job_id={result.get('skipped_existing_external_job_id')} "
            f"skipped_duplicate={result.get('skipped_duplicate')} skipped_company_cooldown={result.get('skipped_company_cooldown')} "
            f"skipped_blocked_company={result.get('skipped_blocked_company')} "
            f"errors={result.get('job_errors')}",
        )
    except Exception as exc:
        append_exception(view_log_path, "VIEW_EXCEPTION action=manual_linkedin_import_view", exc)
        result = {"ok": False, "error": str(exc)[:4000], "run_log_path": view_log_path}
        messages.error(request, f"Manual LinkedIn import failed: {exc}")

    request.session["linkedin_manual_import_result"] = result
    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def process_recruiter_json_view(request):
    form = RecruiterJsonProcessForm(request.POST)
    result = None

    if not form.is_valid():
        messages.error(request, "Recruiter JSON form is invalid.")
        return redirect("operations_dashboard")

    try:
        result = process_recruiter_json_text(raw_text=form.cleaned_data["recruiter_json_text"])
        messages.success(request, "Recruiter JSON processed successfully.")
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Recruiter JSON processing failed: {exc}")

    request.session["recruiter_json_result"] = result
    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def apply_recruiter_decisions_view(request):
    form = RecruiterDecisionApplyForm(request.POST)
    result = None

    if not form.is_valid():
        messages.error(request, "Decision form is invalid.")
        return redirect("operations_dashboard")

    try:
        result = apply_recruiter_decisions(decision_file_path=form.cleaned_data["decision_file_path"])
        messages.success(request, "Recruiter decisions applied successfully.")
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Applying recruiter decisions failed: {exc}")

    request.session["decision_result"] = result
    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def run_apollo_recruiter_fetch_view(request):
    form = ApolloRecruiterFetchForm(request.POST)
    result = None

    if not form.is_valid():
        messages.error(request, "Apollo recruiter fetch form is invalid.")
        return redirect("operations_dashboard")

    try:
        result = run_apollo_recruiter_fetch_for_pending_companies(
            company_name=form.cleaned_data["company"],
            max_people=get_max_people_per_company(),
        )
        messages.success(request, "Apollo recruiter fetch completed successfully.")
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:4000]}
        messages.error(request, f"Apollo recruiter fetch failed: {exc}")

    request.session["apollo_recruiter_fetch_result"] = result
    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def run_generate_cold_emails_view(request):
    form = GenerateColdEmailsForm(request.POST)
    result = None

    view_log_path = create_run_log_path("generate_cold_emails_view", "all")
    append_and_print(view_log_path, "VIEW_START action=run_generate_cold_emails_view")

    if not form.is_valid():
        append_and_print(view_log_path, f"VIEW_INVALID_FORM errors={form.errors}")
        messages.error(request, "Generate cold emails form is invalid.")
        return redirect("operations_dashboard")

    try:
        append_and_print(
            view_log_path,
            f"VIEW_FORM_DATA company={form.cleaned_data['company'] or '[ALL]'} max_jobs={form.cleaned_data['max_jobs']} "
            f"generation_scope={safe_str(request.POST.get('generation_scope', 'missing')).strip().lower()}",
        )

        generation_scope = safe_str(request.POST.get("generation_scope", "missing")).strip().lower()
        result = run_cold_email_generation_for_eligible_jobs(
            company_name=form.cleaned_data["company"],
            max_jobs=form.cleaned_data["max_jobs"],
            skip_existing=generation_scope != "all",
        )

        messages.success(request, "Cold email generation completed.")
        append_and_print(view_log_path, f"VIEW_DONE totals={result.get('totals')}")
    except Exception as exc:
        append_exception(view_log_path, "VIEW_EXCEPTION action=run_generate_cold_emails_view", exc)
        result = {"ok": False, "error": str(exc)[:4000], "run_log_path": view_log_path}
        messages.error(request, f"Cold email generation failed: {exc}")

    request.session["generate_cold_emails_result"] = result
    return redirect("operations_dashboard")


@require_http_methods(["POST"])
def force_apollo_company_topup_view(request):
    company_id_raw = safe_str(request.POST.get("company_id", "")).strip()
    next_url = safe_str(request.POST.get("next", "/send-control/")).strip() or "/send-control/"

    try:
        company_id = int(company_id_raw)
        company = Company.objects.get(id=company_id)
    except (ValueError, TypeError, Company.DoesNotExist):
        messages.error(request, f"Company not found (id={company_id_raw}).")
        return redirect(next_url)

    try:
        log_path = create_run_log_path("force_apollo_topup", company.normalized_name)
        raw_loc = safe_str(
            company.jobs.filter(is_manual_email_job=False).order_by("-updated_at", "-id").values_list("location", flat=True).first()
        ).strip()
        location_hint = extract_us_state_from_location(raw_loc)
        cap = get_max_people_per_company()
        stats = upsert_company_recruiters_from_apollo(
            company=company,
            location_hint=location_hint,
            max_people=cap,
            run_log_path=log_path,
        )
        # Sync targets for ALL non-blacklisted company jobs so new recruiter records are wired up.
        synced = 0
        for job in company.jobs.filter(company_ref__is_blocked=False, is_manual_email_job=False).select_related("company_ref"):
            sync_job_targets_for_job(job=job, max_targets=cap, auto_select=True, allow_fallback_contacts=True)
            synced += 1

        emails_found = int(stats.get("emails_found") or 0)
        credits = int(stats.get("credits_consumed") or 0)
        messages.success(
            request,
            f"Force top-up for {company.normalized_name}: {emails_found} new email(s) found, "
            f"{credits} credit(s) used, {synced} job(s) synced.",
        )
    except Exception as exc:
        messages.error(request, f"Force top-up failed for company {company_id_raw}: {exc}")

    return redirect(next_url)
