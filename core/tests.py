import base64
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    ApprovalRecord,
    ApolloRejectedEmail,
    AppSetting,
    Company,
    CompanyRecruiter,
    DailyBatch,
    DailyCompanyReplyStop,
    EmailVerification,
    GeneratedEmail,
    InboxScanEvent,
    JobPosting,
    JobRecruiterTarget,
    PromptTemplate,
    SendRun,
    SenderAccount,
    SentEmailLog,
    TargetedPeopleLookupRun,
    TestEmailAccount,
)
from core.services.company_domain_service import (
    apply_company_domain_mapping,
    apply_legacy_company_domain_mapping,
    get_legacy_company_domain_mapping_template_text,
)
from core.services.company_cooldown_service import should_skip_new_job_for_company
from core.services.apollo_recruiter_fetch_service import (
    run_apollo_recruiter_fetch_for_pending_companies,
    upsert_apify_person_recruiter_from_apollo,
    upsert_company_recruiters_from_apollo,
)
from core.services.email_composition_service import build_full_email_body
from core.services.email_verification_service import (
    EmailVerificationBlockedError,
    enforce_email_verification,
    verify_email_for_real_send,
)
from core.services.external_job_import_service import (
    ExternalJobDetails,
    parse_external_job_details_from_html,
    run_external_job_url_import,
)
from core.services.import_pipeline_service import hard_reject_experience_requirement, run_import_pipeline
from core.services.inbox_monitor_service import scan_and_store_inbox_events
from core.services.job_target_sync_service import sync_job_targets_for_company_pending_jobs, sync_job_targets_for_job
from core.services.linkedin_job_scrape_service import LinkedInJobDetails, extract_job_poster_from_html
from core.services.linkedin_post_outreach_service import (
    create_linkedin_post_review_batch_from_rows,
    prepare_linkedin_post_rows_for_review,
    run_linkedin_post_outreach,
)
from core.services.live_company_reply_service import (
    build_live_company_reply_dashboard_context,
    company_has_reply_stop_today,
    record_reply_stop_for_event,
)
from core.services.manual_linkedin_import_service import parse_hiring_team_leads_from_text, run_manual_linkedin_import
from core.services.manual_bulk_email_service import parse_manual_bulk_emails, parse_manual_named_recipients
from core.services.manual_job_email_service import (
    build_manual_job_email_review_context,
    create_manual_job_email_batch,
    run_manual_job_email_generation_for_token,
    send_manual_job_email_batch,
    update_manual_job_email_recipient,
)
from core.services.recruiter_title_guard_service import is_domain_business_owner_contact_title
from core.services.cold_email_generation_service import RESUME_TEXT
from core.services.openai_cold_email_service import (
    _finalize_model_cold_email,
    _send_anthropic_cold_email_request,
    apply_cold_email_subject_prefix,
    build_compact_cold_email_subject,
    forbidden_model_body_phrases,
    humanize_company_name,
    invalid_resume_source_phrases,
    model_body_style_issues,
)
from core.services.openai_filter_service import DEFAULT_JOB_FILTER_SYSTEM_PROMPT, classify_job_apply_or_reject
from core.services.sender_account_service import is_smtp_daily_limit_error, pause_sender_for_daily_limit
from core.services.send_control_dashboard_service import build_send_plan_for_batch
from core.services.followup_dashboard_service import (
    build_followup_dashboard_context,
    run_company_followups_from_dashboard,
)
from core.services.mail_delivery_service import send_via_sender_account
from core.services.microsoft_graph_send_service import send_via_microsoft_graph
from core.services.send_run_service import run_send_initial_for_batch
from core.services.smtp_send_service import (
    build_mime_message,
    imap_host_for_email,
    send_via_smtp,
    smtp_settings_for_email,
)
from core.services.send_timing_service import (
    configured_send_delay_range_seconds,
    configured_send_delay_seconds,
    randomized_send_delay_seconds,
    set_send_delay_range_seconds,
)
from core.services.targeted_people_lookup_service import (
    parse_target_person_names,
    run_bulk_targeted_people_lookup,
    run_targeted_people_lookup,
)
from core.utils import normalize_job_or_generic_url, normalize_linkedin_job_url


class EmailVerifierLayerTests(TestCase):
    def _response(self, payload):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_valid_result_is_allowed_and_cached(self):
        env = {
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "valid", "is_disposable": False}),
        ) as post_mock:
            first = verify_email_for_real_send("Person@Example.com")
            second = verify_email_for_real_send("person@example.com")

        self.assertTrue(first.allowed)
        self.assertFalse(first.cached)
        self.assertTrue(second.allowed)
        self.assertTrue(second.cached)
        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(EmailVerification.objects.get().email, "person@example.com")

    def test_safe_catch_all_result_is_allowed(self):
        env = {
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "risky", "is_catch_all": True, "is_safe_to_send": True, "is_valid": False}),
        ):
            result = verify_email_for_real_send("person@amazon.example")

        self.assertTrue(result.allowed)
        self.assertTrue(result.is_catch_all)

    def test_enforcement_allows_safe_catch_all_with_apollo_verified_source(self):
        company = Company.objects.create(raw_name_latest="Amazon Example", normalized_name="amazon example")
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Apollo Verified",
            email="person@amazon.example",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-person",
            email_status="verified",
        )
        env = {
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "catch-all", "is_catch_all": True, "is_safe_to_send": True}),
        ):
            result = enforce_email_verification("person@amazon.example")

        self.assertTrue(result.allowed)
        self.assertTrue(result.is_catch_all)

    def test_enforcement_allows_safe_catch_all_without_apollo_verified_source(self):
        env = {
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "catch-all", "is_catch_all": True, "is_safe_to_send": True}),
        ):
            result = enforce_email_verification("person@amazon.example")

        self.assertTrue(result.allowed)
        self.assertTrue(result.is_catch_all)

    def test_unsafe_catch_all_result_is_allowed_as_risky_catch_all(self):
        env = {
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "catch-all", "is_catch_all": True, "is_safe_to_send": False}),
        ):
            result = verify_email_for_real_send("person@amazon.example")

        self.assertEqual(result.decision, EmailVerification.Decision.ALLOW)
        self.assertTrue(result.is_catch_all)

    def test_enforcement_allows_unsafe_catch_all_as_risky_catch_all(self):
        company = Company.objects.create(raw_name_latest="Amazon Example", normalized_name="amazon example")
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Apollo Verified",
            email="person@amazon.example",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-person",
            email_status="verified",
        )
        env = {
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "catch-all", "is_catch_all": True, "is_safe_to_send": False}),
        ):
            result = enforce_email_verification("person@amazon.example")

        self.assertTrue(result.allowed)
        self.assertTrue(result.is_catch_all)

    def test_explicit_invalid_result_is_blocked(self):
        env = {
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "invalid", "is_valid": False}),
        ):
            result = verify_email_for_real_send("missing@example.com")

        self.assertEqual(result.decision, EmailVerification.Decision.BLOCK)

    def test_enforcement_blocks_when_provider_says_invalid(self):
        env = {
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response({"status": "invalid", "is_valid": False}),
        ):
            with self.assertRaises(EmailVerificationBlockedError):
                enforce_email_verification("missing@example.com")

    def test_enforcement_allows_send_when_provider_is_unavailable(self):
        env = {
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "",
            "EMAIL_VERIFIER_API_URL": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            result = enforce_email_verification("person@example.com")

        self.assertEqual(result.decision, EmailVerification.Decision.DEFER)
        self.assertEqual(result.provider_status, "missing_api_key")

    def test_temporary_unknown_result_is_deferred_not_blocked(self):
        env = {
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
        }
        payload = {
            "status": "unknown",
            "subresult": "temporarily_unavailable",
            "is_deliverable": False,
            "is_valid_syntax": True,
            "mx_accepts_mail": True,
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=self._response(payload),
        ):
            result = verify_email_for_real_send("person@example.com")

        self.assertEqual(result.decision, EmailVerification.Decision.DEFER)
        self.assertEqual(result.provider_status, "unknown")

    def test_delivery_router_blocks_before_smtp(self):
        sender = SenderAccount(
            email="sender@gmail.com",
            app_password="pw",
            auth_method=SenderAccount.AuthMethod.SMTP_PASSWORD,
        )
        message = build_mime_message(
            from_name="Sender",
            from_email=sender.email,
            to_email="missing@example.com",
            subject="Hello",
            body_text="Body",
            attachment_paths=[],
        )
        blocked = mock.Mock(side_effect=EmailVerificationBlockedError("invalid"))
        with mock.patch("core.services.mail_delivery_service.enforce_email_verification", blocked), mock.patch(
            "core.services.mail_delivery_service.send_via_smtp"
        ) as smtp_mock:
            with self.assertRaises(EmailVerificationBlockedError):
                send_via_sender_account(sender=sender, message=message, enforce_recipient_verification=True)

        smtp_mock.assert_not_called()


class LinkedinJobUrlNormalizationTests(SimpleTestCase):
    def test_slugged_job_url_is_stored_as_numeric_canonical_url(self):
        self.assertEqual(
            normalize_linkedin_job_url(
                "https://www.linkedin.com/jobs/view/associate-data-scientist-ii-at-post-holdings-4401966729"
            ),
            "https://www.linkedin.com/jobs/view/4401966729/",
        )

    def test_query_job_id_is_stored_as_numeric_canonical_url(self):
        self.assertEqual(
            normalize_linkedin_job_url("https://www.linkedin.com/jobs/search/?currentJobId=4401966729"),
            "https://www.linkedin.com/jobs/view/4401966729/",
        )

    def test_apply_url_uses_linkedin_canonical_url_when_it_is_a_job_url(self):
        self.assertEqual(
            normalize_job_or_generic_url(
                "https://www.linkedin.com/jobs/view/associate-data-scientist-ii-at-post-holdings-4401966729"
            ),
            "https://www.linkedin.com/jobs/view/4401966729/",
        )

    def test_apply_url_keeps_non_linkedin_urls_generic(self):
        self.assertEqual(
            normalize_job_or_generic_url("https://example.com/apply?tracking=abc#section"),
            "https://example.com/apply",
        )


class HiringTeamLeadParsingTests(SimpleTestCase):
    def test_parses_single_visible_hiring_team_card_without_job_url(self):
        leads = parse_hiring_team_leads_from_text(
            "Meet the hiring team\nSarah Ellis 3rd\nVice President, Human Resources at Cavallo\nJob poster",
            job_urls=["https://www.linkedin.com/jobs/view/4417407332/"],
        )

        lead = leads["https://www.linkedin.com/jobs/view/4417407332"]
        self.assertEqual(lead["name"], "Sarah Ellis")
        self.assertEqual(lead["title"], "Vice President, Human Resources at Cavallo")

    def test_parses_job_url_keyed_hiring_team_block(self):
        leads = parse_hiring_team_leads_from_text(
            "\n".join(
                [
                    "https://www.linkedin.com/jobs/view/4417407332/",
                    "Sarah Ellis",
                    "Vice President, Human Resources at Cavallo",
                    "https://www.linkedin.com/in/sarah-ellis-example/",
                ]
            ),
            job_urls=["https://www.linkedin.com/jobs/view/4417407332/"],
        )

        lead = leads["https://www.linkedin.com/jobs/view/4417407332"]
        self.assertEqual(lead["name"], "Sarah Ellis")
        self.assertEqual(lead["linkedin"], "https://www.linkedin.com/in/sarah-ellis-example")


class LinkedInJobPosterScrapingTests(SimpleTestCase):
    def test_extracts_public_message_recruiter_card(self):
        lead = extract_job_poster_from_html(
            """
            <div class="message-the-recruiter">
                <p>Direct message the job poster from CGI</p>
                <a href="https://www.linkedin.com/in/michellepodinker" data-tracking-control-name="public_jobs">
                    <span class="sr-only"> Michelle Podinker <!----> </span>
                </a>
                <h3 class="base-main-card__title"> Michelle Podinker </h3>
                <h4 class="base-main-card__subtitle"> Senior Talent Acquistion Specialist at CGI </h4>
            </div>
            """
        )

        self.assertEqual(lead["name"], "Michelle Podinker")
        self.assertEqual(lead["title"], "Senior Talent Acquistion Specialist at CGI")
        self.assertEqual(lead["linkedin"], "https://www.linkedin.com/in/michellepodinker")


class ExternalJobImportTests(SimpleTestCase):
    def test_parses_icims_json_ld_details(self):
        html = """
        <html><head><title>Data Engineer - AI in Remote</title></head><body>
        <script type="application/ld+json">
        {
          "@context": "http://schema.org",
          "@type": "JobPosting",
          "title": "Data Engineer - AI",
          "url": "https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job",
          "hiringOrganization": {
            "@type": "Organization",
            "name": "Cotiviti",
            "sameAs": "https://www.cotiviti.com/"
          },
          "jobLocation": [{
            "@type": "Place",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Remote",
              "addressRegion": "UNAVAILABLE",
              "addressCountry": "US"
            }
          }],
          "description": "<h2>Overview</h2><p>Build Spark and SQL data pipelines.</p>"
        }
        </script>
        </body></html>
        """

        details = parse_external_job_details_from_html(
            job_url="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job?mobile=false",
            final_url="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job?in_iframe=1",
            status_code=200,
            page_html=html,
        )

        self.assertEqual(details.job_url, "https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job")
        self.assertEqual(details.external_job_id, "icims:careers-cotiviti.icims.com:18377")
        self.assertEqual(details.title, "Data Engineer - AI")
        self.assertEqual(details.company, "Cotiviti")
        self.assertEqual(details.location, "Remote, United States")
        self.assertEqual(details.company_domain, "cotiviti.com")
        self.assertIn("Spark and SQL", details.description_text)

    def test_ignores_icims_company_domain_from_json_ld(self):
        html = """
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Data Engineer",
          "url": "https://careers-compsych.icims.com/jobs/2019/data-engineer/job",
          "hiringOrganization": {
            "@type": "Organization",
            "name": "ComPsych Corporation",
            "sameAs": "https://compsych.icims.com/"
          },
          "jobLocation": {"@type": "Place", "address": {"addressCountry": "US"}},
          "description": "Build and maintain data pipelines."
        }
        </script>
        """

        details = parse_external_job_details_from_html(
            job_url="https://careers-compsych.icims.com/jobs/2019/data-engineer/job",
            final_url="https://careers-compsych.icims.com/jobs/2019/data-engineer/job",
            status_code=200,
            page_html=html,
        )

        self.assertEqual(details.company, "ComPsych Corporation")
        self.assertEqual(details.company_domain, "")


class ExternalJobImportDatabaseTests(TestCase):
    def test_external_import_commit_creates_job_and_sets_company_domain(self):
        details = ExternalJobDetails(
            job_url="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job",
            final_url="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job",
            status_code=200,
            page_html="<html></html>",
            external_job_id="icims:careers-cotiviti.icims.com:18377",
            title="Data Engineer - AI",
            company="Cotiviti",
            location="Remote, United States",
            description_text="Build Spark, SQL, and cloud data pipelines.",
            apply_url="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job",
            company_domain="cotiviti.com",
            company_website="https://www.cotiviti.com/",
        )

        with mock.patch("core.services.external_job_import_service.fetch_external_job_details", return_value=details):
            result = run_external_job_url_import(
                raw_urls_text="https://careers-cotiviti.icims.com/jobs/18377/data-engineer---ai/job?tracking=abc",
                dry_run=False,
                use_openai_filter=False,
                apply_cooldown_filters=False,
            )

        self.assertEqual(result["created_jobs"], 1)
        job = JobPosting.objects.get()
        company = Company.objects.get()
        self.assertEqual(job.normalized_linkedin_url, details.job_url)
        self.assertEqual(job.apply_url, details.apply_url)
        self.assertEqual(job.status, JobPosting.Status.RECRUITERS_PENDING)
        self.assertEqual(company.normalized_name, "cotiviti")
        self.assertEqual(company.active_domain, "cotiviti.com")
        self.assertFalse(SentEmailLog.objects.exists())


class EmailCompositionTests(SimpleTestCase):
    def test_cold_email_subject_prefix_is_empty_by_default(self):
        self.assertEqual(
            apply_cold_email_subject_prefix("Data Scientist Role"),
            "Data Scientist Role",
        )
        self.assertEqual(
            apply_cold_email_subject_prefix("Gayathri - Data Scientist Role"),
            "Gayathri - Data Scientist Role",
        )

    def test_compact_subject_uses_named_team_only_when_specific(self):
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Blue Cross & Blue Shield of Rhode Island",
                job_title="Associate Health Data Analyst - Business Intelligence",
            ),
            "Health Data Analyst at BlueCross Rhode Island",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Zurich North America",
                job_title="Data Scientist Analyst OR Senior Data Scientist Analyst, Crop Insurance",
            ),
            "Crop Insurance Data Scientist at Zurich North America",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="McLane",
                job_title="Data Scientist - MBIS",
            ),
            "Data Scientist at McLane",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Q2",
                job_title="Machine Learning Engineer",
            ),
            "ML Engineer at Q2",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Tampa Bay Rays",
                job_title="Analyst, Business Strategy & Analytics",
            ),
            "Analytics Analyst at Tampa Bay Rays",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="Data Analyst/Process Optimization Specialist",
            ),
            "Data Analyst role",
        )

    def test_compact_subject_removes_employer_hiring_language(self):
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title=(
                    "We're hiring a Staff Backend Engineer on the Media Experiences team! "
                    "If you've dabbled in scaling media infrastructure, I'd love to chat."
                ),
            ),
            "Staff Backend Engineer role",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="We're #hiring a Research Scientist – RL Training Snorkel AI.",
            ),
            "Research Scientist role",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="🚨 Hiring – Data Analytics & Reporting Developer (Junior) 🚨",
            ),
            "Reporting Developer role",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="I’m hiring a Web Developer / Systems Analyst to join my team!",
            ),
            "Web Developer role",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="Looking for Senior Software Engineer (US-based, no sponsorship)",
            ),
            "Software Engineer role",
        )

    def test_generic_application_subject_is_rewritten_as_human_subject(self):
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Syren Cloud",
                job_title="Data Scientist",
                fallback_subject="Application for Data Scientist Role in Advertiser Sellers Team",
            ),
            "Data Scientist at Syren Cloud",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Advanced Tech Placement",
                job_title="Data Scientist",
                fallback_subject="Data Science Team - Application for Data Scientist Role",
            ),
            "Data Scientist at Advanced Tech Placement",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Tampa Bay Rays",
                job_title="Analyst, Business Strategy & Analytics",
                fallback_subject="Application for Analyst, Business Strategy & Analytics Role",
            ),
            "Analytics Analyst at Tampa Bay Rays",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="Manual",
                job_title="Data Analyst/Process Optimization Specialist",
                fallback_subject="Application for Data Analyst/Process Optimization Specialist Role",
            ),
            "Data Analyst role",
        )

    def test_human_subject_keeps_specific_non_generic_model_subject(self):
        self.assertEqual(humanize_company_name("AAON, Inc."), "AAON")
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="AAON, Inc.",
                job_title="Software Engineer I",
                fallback_subject="SWE I role at AAON, REST API background at 700M monthly hits",
            ),
            "Software Engineer at AAON",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="AAON, Inc.",
                job_title="Software Engineer I",
                fallback_subject="API-backed systems for AAON SWE I",
            ),
            "Software Engineer at AAON",
        )
        self.assertEqual(
            build_compact_cold_email_subject(
                company_name="AAON, Inc.",
                job_title="Software Engineer I",
                fallback_subject="Application for Software Engineer Role",
            ),
            "Software Engineer at AAON",
        )

    def test_model_finalizer_removes_legal_company_suffixes_from_body(self):
        result = _finalize_model_cold_email(
            parsed={
                "subject": "Software Engineer I role at AAON, Inc.",
                "email": "Quick note about the Software Engineer I opening at AAON, Inc. The API work stood out.",
            },
            headers={},
            body={},
            prompt_version="test",
            company_name="AAON, Inc.",
            allow_retry=False,
        )

        self.assertEqual(result["subject"], "Software Engineer at AAON")
        self.assertIn("AAON", result["email"])
        self.assertNotIn("AAON, Inc.", result["email"])

    def test_anthropic_email_request_retries_429_rate_limit(self):
        rate_limited = mock.Mock()
        rate_limited.status_code = 429
        rate_limited.headers = {"retry-after": "0.5"}
        rate_limited.text = '{"error":"rate limit"}'

        ok = mock.Mock()
        ok.status_code = 200
        ok.headers = {}
        ok.text = ""
        ok.json.return_value = {"content": [{"type": "text", "text": '{"subject":"Hello","email":"Body"}'}]}

        with mock.patch(
            "core.services.openai_cold_email_service.requests.post",
            side_effect=[rate_limited, ok],
        ), mock.patch("core.services.openai_cold_email_service.time.sleep") as sleep_mock:
            result = _send_anthropic_cold_email_request(headers={"x-api-key": "fake"}, body={"model": "claude"})

        self.assertEqual(result["subject"], "Hello")
        sleep_mock.assert_called_once_with(0.5)

    def test_model_body_detector_finds_footer_owned_phrases(self):
        matches = forbidden_model_body_phrases(
            "I would be grateful if you could consider my resume or point me to the right hiring contact. "
            "Please see my attached Resume."
        )

        self.assertIn("point me to the right hiring contact", " ".join(matches) or "point me to the right hiring contact")

    def test_resume_context_uses_nagarro_and_guard_blocks_trendlyne_source(self):
        resume_text = RESUME_TEXT.lower()
        self.assertIn("nagarro", resume_text)
        self.assertNotIn("trendlyne", resume_text)
        self.assertIn("trendlyne", invalid_resume_source_phrases("At Trendlyne.com, I built APIs."))

    def test_full_email_body_wraps_middle_paragraph_in_application_template(self):
        with mock.patch.dict(os.environ, {"SEND_ATTACH_RESUME": "0"}, clear=False):
            body = build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body=(
                    "This is Gayathri, with 4+ years of experience in data engineering and ETL pipelines. "
                    "I recently came across the Data Engineer opening at Xylem and after going through the role carefully, "
                    "felt it aligns really well with my background in building data pipelines and working with SQL workflows."
                ),
                job_linkedin_url="https://www.linkedin.com/jobs/view/data-engineer-at-xylem-4229164734",
            )

        self.assertTrue(body.startswith("Hi Samantha,\n\nI hope this message finds you well."))
        self.assertIn(
            "This is Gayathri, with 4+ years of experience in data engineering and ETL pipelines.",
            body,
        )
        self.assertIn(
            "exact skills you're seeking.\n\nMy background includes",
            build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body=(
                    "This is Gayathri, and I'm interested in the Data Engineer role at Xylem. "
                    "With 4+ years of experience building Python pipelines, writing SQL queries, and creating dashboards, "
                    "I have honed the exact skills you're seeking. "
                    "My background includes building reporting workflows and data quality checks, making me a strong fit for your team focused on analytics."
                ),
                job_linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
            ),
        )
        self.assertNotIn("Please find my attached resume.", body)
        self.assertNotIn("quick 5 to 10 minute chat", body)
        self.assertNotIn("forward my resume to the hiring manager", body)
        self.assertIn(
            'I really appreciate your time and help, Samantha. If helpful, you can find more about my work by searching "Gayathri Emuru", and I\'d be happy to send my resume as well.',
            body,
        )
        self.assertIn("Kind regards,\nGayathri Emuru", body)
        self.assertIn("Job posting: https://www.linkedin.com/jobs/view/4229164734/", body)
        self.assertNotIn("Portfolio:", body)
        self.assertNotIn("LinkedIn: https://www.linkedin.com/in/gayathri-emuru/", body)

    def test_full_email_body_uses_application_template_when_url_missing(self):
        with mock.patch.dict(os.environ, {"SEND_ATTACH_RESUME": "0"}, clear=False):
            body = build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body=(
                    "This is Gayathri, with 4+ years of experience in SQL and Python. "
                    "I recently came across the Data Engineer opening and felt it aligns really well with my background in ETL and reporting."
                ),
                job_linkedin_url="",
            )

        self.assertIn("Hi Samantha,", body)
        self.assertNotIn("Job posting:", body)
        self.assertNotIn("unavailable", body)
        self.assertIn("I hope this message finds you well.", body)
        self.assertIn(
            'I really appreciate your time and help, Samantha. If helpful, you can find more about my work by searching "Gayathri Emuru", and I\'d be happy to send my resume as well.',
            body,
        )

    def test_full_email_body_prefers_manual_job_reference_id_over_url(self):
        with mock.patch.dict(os.environ, {"SEND_ATTACH_RESUME": "0"}, clear=False):
            body = build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body="I saw the Data Engineer opening and felt it aligns well with my background.",
                job_linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
                manual_job_reference_id="REQ-2026-1042",
            )

        self.assertIn("Job ID: REQ-2026-1042", body)
        self.assertNotIn("Job posting: https://www.linkedin.com/jobs/view/4229164734/", body)

    def test_full_email_body_mentions_attached_resume_when_resume_toggle_is_on(self):
        with mock.patch.dict(os.environ, {"SEND_ATTACH_RESUME": "1"}, clear=False):
            body = build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body="I saw the Data Engineer opening and felt it aligns well with my background.",
                job_linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
            )

        self.assertIn(
            'I really appreciate your time and help, Samantha. If helpful, you can find more about my work by searching "Gayathri Emuru". I have attached my resume for your reference.',
            body,
        )
        self.assertNotIn("I'd be happy to send my resume as well", body)

    def test_full_email_body_can_omit_job_reference_for_manual_flow(self):
        with mock.patch.dict(os.environ, {"SEND_ATTACH_RESUME": "0"}, clear=False):
            body = build_full_email_body(
                recipient_name="Samantha Landefeld",
                base_body=(
                    "This is Gayathri, with 4+ years of experience in SQL and Python. "
                    "I recently came across the Data Engineer opening and felt it aligns really well with my background in ETL and reporting."
                ),
                job_linkedin_url="https://manual.local/job-email/token/1/",
                include_job_reference=False,
            )

        self.assertIn("Hi Samantha,", body)
        self.assertNotIn("Please find my attached resume.", body)
        self.assertNotIn("quick 5 to 10 minute chat", body)
        self.assertNotIn("forward my resume to the hiring manager", body)
        self.assertIn("Kind regards,\nGayathri Emuru", body)
        self.assertNotIn("Portfolio:", body)
        self.assertNotIn("LinkedIn: https://www.linkedin.com/in/gayathri-emuru/", body)
        self.assertNotIn("Job posting:", body)
        self.assertNotIn("manual.local", body)

    def test_full_email_body_removes_old_model_owned_ask(self):
        body = build_full_email_body(
            recipient_name="Samantha Landefeld",
            base_body=(
                "I saw the Data Engineer role at Xylem and noticed the SQL pipeline work. "
                "Would you be open to a 10-minute recruiter screen this week, or is there someone better I should contact?"
            ),
            job_linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
        )

        self.assertNotIn("quick chat about the role", body)
        self.assertNotIn("10-minute", body)

    def test_full_email_body_removes_ai_dash_punctuation_but_keeps_human_hyphens(self):
        body = build_full_email_body(
            recipient_name="Christopher Williams",
            base_body=(
                "Quick note on the Software Engineer I opening at AAON — the API-backed systems stood out. "
                "At Nagarro, I built high-volume REST APIs for a stock analytics platform. "
                "Would you be open to a 10-minute recruiter screen this week, or is there someone better I should contact?"
            ),
            job_linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
        )

        base_section = body.split("\n\nI really appreciate", 1)[0]
        self.assertNotIn("—", base_section)
        self.assertNotIn("10-minute", base_section)
        self.assertNotIn("Quick note", base_section)
        self.assertIn("The Software Engineer I opening at AAON stood out because", base_section)
        self.assertIn("API-backed", base_section)
        self.assertIn("high-volume", base_section)
        self.assertNotIn("Would you be open to a quick chat about the role this week?", base_section)

    def test_full_email_body_removes_degree_in_progress_wording(self):
        body = build_full_email_body(
            recipient_name="Christopher Williams",
            base_body=(
                "I'm a Data Scientist and ML Engineer with 4 years of production systems experience. "
                "At Nagarro, I built REST APIs handling 700M+ monthly traffic. "
                "I'm finishing an M.S. in Data Science and have worked end-to-end from system design through deployment."
            ),
            job_linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
        )

        base_section = body.split("\n\nI really appreciate", 1)[0]
        self.assertNotIn("finishing", base_section.lower())
        self.assertNotIn("M.S. in Data Science", base_section)
        self.assertNotIn("MSDS", base_section)
        self.assertIn("I have worked end-to-end", base_section)
        self.assertNotIn("Would you be open to a quick chat about the role this week?", base_section)

class OpenAIJobFilterPromptTemplateTests(TestCase):
    def test_default_job_filter_does_not_reject_on_role_family(self):
        prompt = DEFAULT_JOB_FILTER_SYSTEM_PROMPT.lower()

        self.assertIn("do not reject from title or role family alone", prompt)
        self.assertIn("apply whenever no hard reject condition appears", prompt)
        self.assertNotIn('{"decision":"reject","reason":"outside target role family"}', prompt)

    def test_classifier_uses_active_job_filter_prompt_template(self):
        prompt, _ = PromptTemplate.objects.update_or_create(
            purpose=PromptTemplate.Purpose.JOB_FILTER,
            name="applyreject_job",
            defaults={
                "content": "DB APPLY REJECT PROMPT",
                "is_active": True,
            },
        )
        PromptTemplate.objects.filter(purpose=PromptTemplate.Purpose.JOB_FILTER, is_active=True).exclude(id=prompt.id).update(is_active=False)

        fake_response = type("FakeResponse", (), {"output_text": "APPLY"})()
        fake_client = mock.Mock()
        fake_client.responses.create.return_value = fake_response

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False), mock.patch(
            "core.services.openai_filter_service._get_openai_client",
            return_value=fake_client,
        ):
            decision = classify_job_apply_or_reject("Data Analyst", "Build reporting dashboards.")

        self.assertEqual(decision, "APPLY")
        request_input = fake_client.responses.create.call_args.kwargs["input"]
        self.assertEqual(request_input[0]["role"], "system")
        self.assertEqual(request_input[0]["content"], "DB APPLY REJECT PROMPT")


class CompanyDomainMappingTests(TestCase):
    def test_legacy_template_includes_copyable_company_names_text(self):
        Company.objects.create(raw_name_latest="Alpha Co", normalized_name="alpha", legacy=True)
        Company.objects.create(raw_name_latest="Beta Co", normalized_name="beta", legacy=True)

        result = get_legacy_company_domain_mapping_template_text(
            start_range=1,
            end_range=2,
            only_missing=True,
        )

        self.assertEqual(result["company_names_text"], "alpha\nbeta")

    def test_legacy_template_filters_missing_domains_inside_selected_range(self):
        Company.objects.create(
            raw_name_latest="Alpha Co",
            normalized_name="alpha",
            legacy=True,
            active_domain="alpha.com",
            domain_status=Company.DomainStatus.SET,
        )
        Company.objects.create(raw_name_latest="Beta Co", normalized_name="beta", legacy=True)
        Company.objects.create(
            raw_name_latest="Gamma Co",
            normalized_name="gamma",
            legacy=True,
            active_domain="gamma.com",
            domain_status=Company.DomainStatus.SET,
        )
        Company.objects.create(raw_name_latest="Zeta Co", normalized_name="zeta", legacy=True)

        result = get_legacy_company_domain_mapping_template_text(
            start_range=1,
            end_range=3,
            only_missing=True,
        )

        self.assertEqual(result["total_legacy_companies_in_scope"], 4)
        self.assertEqual(result["selected_range_count"], 3)
        self.assertEqual(result["company_names_text"], "beta")
        self.assertEqual(result["returned"], 1)

    def test_blank_legacy_domain_mapping_skips_company(self):
        company = Company.objects.create(raw_name_latest="Tiny Co", normalized_name="tiny", legacy=True)

        result = apply_legacy_company_domain_mapping('{"tiny": ""}')

        self.assertEqual(result["skipped_blank"], 1)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())

    def test_null_legacy_domain_mapping_deletes_company_and_recruiters(self):
        company = Company.objects.create(raw_name_latest="Tiny Co", normalized_name="tiny", legacy=True)
        CompanyRecruiter.objects.create(company=company, person_name="Pat Person", email="pat@example.com")

        result = apply_legacy_company_domain_mapping('{"tiny": null}')

        self.assertEqual(result["removed"], 1)
        self.assertFalse(Company.objects.filter(pk=company.pk).exists())
        self.assertEqual(CompanyRecruiter.objects.count(), 0)

    def test_null_company_domain_mapping_deletes_non_legacy_company(self):
        company = Company.objects.create(raw_name_latest="Small Co", normalized_name="small", legacy=False)

        result = apply_company_domain_mapping('{"small": null}')

        self.assertEqual(result["removed"], 1)
        self.assertFalse(Company.objects.filter(pk=company.pk).exists())

    def test_python_none_mapping_deletes_company(self):
        company = Company.objects.create(raw_name_latest="Small Co", normalized_name="small", legacy=False)

        result = apply_company_domain_mapping("{'small': None}")

        self.assertEqual(result["removed"], 1)
        self.assertFalse(Company.objects.filter(pk=company.pk).exists())


class CompanyRecruiterSentStateTests(TestCase):
    def test_sent_recruiter_stays_blocked_when_email_changes(self):
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme")
        sent_date = timezone.localdate()
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Sam Recruiter",
            email="sam.old@example.com",
            email_sent=True,
            email_sent_date=sent_date,
        )

        recruiter.email = "sam.new@example.com"
        recruiter.save()
        recruiter.refresh_from_db()

        self.assertEqual(recruiter.email, "sam.new@example.com")
        self.assertTrue(recruiter.email_sent)
        self.assertEqual(recruiter.email_sent_date, sent_date)


class CompanyCooldownTests(TestCase):
    def test_same_day_company_job_triggers_cooldown(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme")
        JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company,
            linkedin_url="https://www.linkedin.com/jobs/view/111/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/111/",
            apply_url="https://example.com/apply",
            normalized_apply_url="https://example.com/apply",
            title="Data Analyst",
            company="Acme",
            location="United States",
            description="Analyze data.",
            normalized_company="acme",
            normalized_title="data analyst",
            normalized_location="united states",
            canonical_company="acme",
            canonical_title="data analyst",
            canonical_location="united states",
            dedupe_key="acme:data analyst:united states",
            sort_company="acme",
            sort_title="data analyst",
            sort_location="united states",
        )

        skip, reason = should_skip_new_job_for_company(
            canonical_company="acme",
            batch_date=batch.batch_date,
            cooldown_days=10,
        )

        self.assertTrue(skip)
        self.assertEqual(reason, "recent_job")

    def test_zero_company_cooldown_does_not_skip_recent_company(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme")
        JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company,
            linkedin_url="https://www.linkedin.com/jobs/view/111/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/111/",
            apply_url="https://example.com/apply",
            normalized_apply_url="https://example.com/apply",
            title="Data Analyst",
            company="Acme",
            location="United States",
            description="Analyze data.",
            normalized_company="acme",
            normalized_title="data analyst",
            normalized_location="united states",
            canonical_company="acme",
            canonical_title="data analyst",
            canonical_location="united states",
            dedupe_key="acme:data analyst:united states",
            sort_company="acme",
            sort_title="data analyst",
            sort_location="united states",
        )

        skip, reason = should_skip_new_job_for_company(
            canonical_company="acme",
            batch_date=batch.batch_date,
            cooldown_days=0,
        )

        self.assertFalse(skip)
        self.assertEqual(reason, "none")


class ImportPipelineCreditSavingTests(TestCase):
    def test_hard_experience_filter_rejects_clear_five_plus_requirement_before_openai(self):
        raw_jobs = [
            {
                "id": "job-1",
                "url": "https://www.linkedin.com/jobs/view/111/",
                "external_apply_url": "https://example.com/1",
                "title": "Senior Data Scientist",
                "organization": "Merck",
                "regions_derived": ["Pennsylvania"],
                "description_text": "Qualifications: Minimum 5 years of experience using Python, R, and advanced SQL.",
            },
        ]
        fake_key = type("FakeKey", (), {"key_name": "fake"})()

        with mock.patch(
            "core.services.import_pipeline_service.fetch_jobs_from_apify_with_rotation",
            return_value=(raw_jobs, fake_key, {"run_id": "run-1", "dataset_id": "dataset-1", "usage_total_usd": 0.015}),
        ), mock.patch(
            "core.services.import_pipeline_service.classify_job_apply_or_reject",
            return_value="APPLY",
        ) as classifier_mock:
            result = run_import_pipeline(lookback_hours=24, max_jobs=10)

        classifier_mock.assert_not_called()
        self.assertEqual(result["created_jobs"], 0)
        self.assertEqual(result["hard_rejected_experience"], 1)
        self.assertEqual(result["rejected_jobs"], 1)
        self.assertEqual(result["apify_estimated_dataset_cost_usd"], 0.0015)
        self.assertEqual(result["apify_reported_usage_total_usd"], 0.015)

    def test_hard_experience_filter_allows_lower_minimum_and_preferred_ranges(self):
        self.assertEqual(
            hard_reject_experience_requirement("Required: 1-4 years of experience in data science."),
            "",
        )
        self.assertEqual(
            hard_reject_experience_requirement("Preferred: 5+ years of experience with Python."),
            "",
        )
        self.assertIn(
            "required_experience_min_4_years",
            hard_reject_experience_requirement("Requirements: 4-6 years of experience in data engineering."),
        )

    def test_duplicate_company_inside_same_apify_run_skips_openai_after_first_seen(self):
        raw_jobs = [
            {
                "id": "job-1",
                "url": "https://www.linkedin.com/jobs/view/111/",
                "external_apply_url": "https://example.com/1",
                "title": "Data Engineer",
                "organization": "Slalom",
                "regions_derived": ["Texas"],
                "description_text": "Build data pipelines.",
            },
            {
                "id": "job-2",
                "url": "https://www.linkedin.com/jobs/view/222/",
                "external_apply_url": "https://example.com/2",
                "title": "Data Engineer",
                "organization": "Slalom",
                "regions_derived": ["Florida"],
                "description_text": "Build data pipelines.",
            },
        ]
        fake_key = type("FakeKey", (), {"key_name": "fake"})()

        with mock.patch(
            "core.services.import_pipeline_service.fetch_jobs_from_apify_with_rotation",
            return_value=(raw_jobs, fake_key, {"run_id": "run-1", "dataset_id": "dataset-1", "usage_total_usd": 0.03}),
        ) as fetch_mock, mock.patch(
            "core.services.import_pipeline_service.classify_job_apply_or_reject",
            return_value="REJECT",
        ) as classifier_mock:
            result = run_import_pipeline(
                lookback_hours=24,
                max_jobs=10,
                organization_exclusion_search=["Existing Co:*"],
                organization_slug_exclusion_filter=["existing-co"],
            )

        fetch_mock.assert_called_once()
        self.assertEqual(fetch_mock.call_args.kwargs["organization_exclusion_search"], ["Existing Co:*"])
        self.assertEqual(fetch_mock.call_args.kwargs["organization_slug_exclusion_filter"], ["existing-co"])
        self.assertEqual(result["raw_jobs"], 2)
        self.assertEqual(result["skipped_duplicate_company_in_run"], 1)
        self.assertEqual(classifier_mock.call_count, 1)


class RecruiterTitleGuardFallbackTests(SimpleTestCase):
    def test_domain_business_owner_titles_are_allowed_as_broad_fallbacks(self):
        allowed_titles = [
            "Managing Director",
            "Senior Managing Director",
            "Member of the Executive Committee",
            "Co-Head of US Secondaries & Primaries, & Senior Managing Director",
            "Deputy Head of US Co-Investment & Senior Managing Director",
            "Senior Investment Manager - Ardian Buyout",
            "Private Equity Analyst",
            "Investment Manager",
            "VP Buyout Fund",
            "Head of Americas Investor Relations",
            "Private Wealth Director",
            "Client Solutions Senior Associate",
            "Investor Relations Analyst",
            "Fund of Funds",
            "Managing Director, Co-investments",
        ]

        for title in allowed_titles:
            with self.subTest(title=title):
                self.assertTrue(is_domain_business_owner_contact_title(title))

    def test_domain_business_owner_still_rejects_low_value_titles(self):
        self.assertFalse(is_domain_business_owner_contact_title("Investment Intern"))
        self.assertFalse(is_domain_business_owner_contact_title("Executive Assistant"))


def _create_job_with_email(*, batch=None, company=None, approved=True, generated=True):
    batch = batch or DailyBatch.objects.create(batch_date=timezone.localdate())
    company = company or Company.objects.create(
        raw_name_latest="Acme",
        normalized_name="acme",
        active_domain="acme.com",
    )
    job = JobPosting.objects.create(
        daily_batch=batch,
        company_ref=company,
        linkedin_url="https://www.linkedin.com/jobs/view/123/",
        normalized_linkedin_url="https://www.linkedin.com/jobs/view/123/",
        title="Data Scientist",
        company=company.raw_name_latest,
        description="Build ML systems.",
        normalized_company=company.normalized_name,
        normalized_title="data scientist",
        normalized_location="remote",
        dedupe_key=f"{company.normalized_name}:data scientist:{batch.batch_date}",
        sort_company=company.normalized_name,
        sort_title="data scientist",
        sort_location="remote",
    )
    if approved:
        ApprovalRecord.objects.create(job_posting=job, is_approved=True, approved_at=timezone.now())
    if generated:
        GeneratedEmail.objects.create(
            job_posting=job,
            subject="Data Scientist Role",
            body="I am interested in the role. Please see my attached Resume.",
            generation_status=GeneratedEmail.GenerationStatus.GENERATED,
        )
    return job


class ApolloExactJobPosterTests(TestCase):
    def test_exact_job_poster_creates_only_one_selected_target(self):
        company = Company.objects.create(
            raw_name_latest="Poster Co",
            normalized_name="poster co exact",
            active_domain="posterco.com",
        )
        job = _create_job_with_email(company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.recruiter_name = "Casey Leader"
        job.recruiter_title = "Director of Data Science"
        job.recruiter_linkedin = "https://www.linkedin.com/in/caseyleader"
        job.save(update_fields=["status", "recruiter_name", "recruiter_title", "recruiter_linkedin", "updated_at"])

        broad = CompanyRecruiter.objects.create(
            company=company,
            person_name="Tara Recruiter",
            email="tara@posterco.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-broad",
            email_status="verified",
            apollo_title="Technical Recruiter",
            is_active=True,
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=broad,
            recipient_email_snapshot=broad.email,
            recipient_name_snapshot=broad.person_name,
            selection_order=1,
            is_selected_for_job=True,
        )
        payload = {
            "credits_consumed": 1,
            "person": {
                "id": "apollo-casey",
                "name": "Casey Leader",
                "title": "Director of Data Science",
                "city": "Austin",
                "state": "Texas",
                "country": "United States",
                "email": "casey@posterco.com",
                "email_status": "verified",
            },
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.match_person_email_from_apollo",
            return_value=payload,
        ):
            result = upsert_apify_person_recruiter_from_apollo(job=job)

        job.refresh_from_db()
        target = JobRecruiterTarget.objects.get(job_posting=job)
        exact = target.company_recruiter
        self.assertEqual(result["emails_found"], 1)
        self.assertEqual(target.recipient_email_snapshot, "casey@posterco.com")
        self.assertEqual(target.selection_order, 1)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job).count(), 1)
        self.assertTrue(exact.manually_targeted)
        self.assertEqual(exact.apollo_linkedin_url, "https://www.linkedin.com/in/caseyleader")
        self.assertEqual(job.status, JobPosting.Status.EMAIL_DISCOVERY_DONE)

    def test_pending_company_fetch_falls_back_when_exact_person_has_no_email(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Fallback Co",
            normalized_name="fallback co exact",
            active_domain="fallbackco.com",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.recruiter_name = "No Email"
        job.recruiter_title = "Director of Data Science"
        job.save(update_fields=["status", "recruiter_name", "recruiter_title", "updated_at"])

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.upsert_apify_person_recruiter_from_apollo",
            return_value={"job_id": job.id, "company": company.normalized_name, "person": "No Email", "emails_found": 0, "credits_consumed": 1},
        ) as exact_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.upsert_company_recruiters_from_apollo",
            return_value={
                "company": company.normalized_name,
                "created": 0,
                "updated": 0,
                "emails_found": 1,
                "verified_emails": 1,
                "credits_consumed": 1,
                "legacy_reused": 0,
            },
        ) as company_mock:
            result = run_apollo_recruiter_fetch_for_pending_companies()

        exact_mock.assert_called_once()
        company_mock.assert_called_once()
        self.assertEqual(result["totals"]["exact_person_jobs_seen"], 1)
        self.assertEqual(result["totals"]["exact_person_fallback_companies"], 1)
        self.assertEqual(result["totals"]["companies_seen"], 1)

    def test_company_fetch_skips_apollo_when_company_send_cap_is_full(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Cap Full Co",
            normalized_name="cap full co",
            active_domain="capfull.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior sends",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                sender_account=sender,
                to_email=f"sent{idx}@capfull.example",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        with mock.patch("core.services.apollo_recruiter_fetch_service.search_people_from_apollo") as search_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo"
        ) as broad_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo"
        ) as match_mock:
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        search_mock.assert_not_called()
        broad_mock.assert_not_called()
        match_mock.assert_not_called()
        self.assertEqual(stats["remaining_send_capacity"], 0)
        self.assertEqual(stats["credits_consumed"], 0)
        self.assertEqual(stats["emails_found"], 0)

    def test_company_fetch_limits_apollo_slots_to_remaining_company_send_capacity(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Partial Cap Co",
            normalized_name="partial cap co",
            active_domain="partialcap.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior sends",
        )
        for idx in range(8):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                sender_account=sender,
                to_email=f"sent{idx}@partialcap.example",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        people = [
            {
                "id": f"apollo-{idx}",
                "name": f"Data Manager {idx}",
                "title": "Data Science Manager",
                "has_email": True,
                "email_status": "verified",
            }
            for idx in range(5)
        ]

        def fake_bulk_match(person_ids):
            idx = int(str(person_ids[0]).rsplit("-", 1)[-1])
            return {
                "credits_consumed": 1,
                "matches": [
                    {
                        "id": f"apollo-{idx}",
                        "name": f"Data Manager {idx}",
                        "title": "Data Science Manager",
                        "email": f"manager{idx}@partialcap.example",
                        "email_status": "verified",
                    }
                ],
            }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=people,
        ) as search_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            side_effect=fake_bulk_match,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(search_mock.call_args.kwargs["max_people"], 2)
        self.assertEqual(stats["remaining_send_capacity"], 2)
        self.assertEqual(stats["emails_found"], 2)
        self.assertEqual(stats["credits_consumed"], 2)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).count(), 2)

    def test_company_fetch_does_not_count_sends_older_than_cooldown_against_capacity(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 10})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Cooldown Co",
            normalized_name="cooldown co",
            active_domain="cooldown.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(days=12),
            finished_at=timezone.now() - timedelta(days=12),
            notes="old sends",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                sender_account=sender,
                to_email=f"old{idx}@cooldown.example",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now() - timedelta(days=12),
            )

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[],
        ) as search_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["prior_real_initial_sends"], 0)
        self.assertEqual(stats["remaining_send_capacity"], 10)
        self.assertEqual(search_mock.call_args_list[0].kwargs["max_people"], 10)

    def test_company_fetch_does_not_count_historical_sends_when_cooldown_is_zero(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 0})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Zero Cooldown Co",
            normalized_name="zero cooldown co",
            active_domain="zerocooldown.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="historical sends",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                sender_account=sender,
                to_email=f"sent{idx}@zerocooldown.example",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[],
        ) as search_mock, mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["prior_real_initial_sends"], 0)
        self.assertEqual(stats["remaining_send_capacity"], 10)
        self.assertEqual(search_mock.call_args_list[0].kwargs["max_people"], 10)

    def test_pipeline_dashboard_does_not_count_historical_sends_when_cooldown_is_zero(self):
        from core.services.pipeline_dashboard_service import build_pipeline_dashboard_context

        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 0})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Zero Dashboard Co",
            normalized_name="zero dashboard co",
            active_domain="zerodashboard.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="historical sends",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=job,
                sender_account=sender,
                to_email=f"sent{idx}@zerodashboard.example",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        context = build_pipeline_dashboard_context(batch_date=batch.batch_date.isoformat())
        row = next(row for row in context["company_rows"] if row["normalized_name"] == "zero dashboard co")

        self.assertEqual(row["sent_count"], 0)
        self.assertEqual(row["remaining_send_capacity"], 10)
        self.assertEqual(row["apollo_slots_needed"], 10)
        self.assertTrue(row["ready_for_recruiter_fill"])

    def test_company_fetch_accepts_founder_as_last_resort_contact(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Founder Fallback Co",
            normalized_name="founder fallback co",
            active_domain="founderfallback.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        person = {
            "id": "apollo-founder-1",
            "name": "Casey Founder",
            "title": "CEO / Founder",
            "has_email": True,
            "email_status": "verified",
        }

        def fake_search(*, person_titles, **kwargs):
            return [person] if "founder" in person_titles else []

        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    **person,
                    "email": "casey@founderfallback.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            side_effect=fake_search,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_last_resort_title_emails"], 1)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "casey@founderfallback.example")
        self.assertEqual(target.company_recruiter.apollo_title, "CEO / Founder")

    def test_company_fetch_accepts_technical_profile_from_broad_last_resort_scan(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Engineer Fallback Co",
            normalized_name="engineer fallback co",
            active_domain="engineerfallback.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        person = {
            "id": "apollo-engineer-1",
            "name": "Riley Engineer",
            "title": "Founding Applied AI Engineer",
            "has_email": True,
            "email_status": "verified",
        }
        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    **person,
                    "email": "riley@engineerfallback.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[person],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_last_resort_title_emails"], 1)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "riley@engineerfallback.example")
        self.assertEqual(target.company_recruiter.apollo_title, "Founding Applied AI Engineer")

    def test_company_fetch_accepts_broader_technical_titles_as_last_resort(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Robotics Fallback Co",
            normalized_name="robotics fallback co",
            active_domain="roboticsfallback.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        people = [
            {
                "id": "apollo-robotics-1",
                "name": "Rory Robotics",
                "title": "Lead Robotics and Mechatronics Engineer",
                "has_email": True,
                "email_status": "verified",
            },
            {
                "id": "apollo-developer-1",
                "name": "Devon Developer",
                "title": "Full Stack Developer",
                "has_email": True,
                "email_status": "verified",
            },
        ]

        def fake_bulk_match(person_ids):
            person_by_id = {person["id"]: person for person in people}
            matches = []
            for person_id in person_ids:
                person = person_by_id[person_id]
                matches.append(
                    {
                        **person,
                        "email": f"{person['name'].split()[0].lower()}@roboticsfallback.example",
                        "email_status": "verified",
                    }
                )
            return {"credits_consumed": len(matches), "matches": matches}

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=people,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            side_effect=fake_bulk_match,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["emails_found"], 2)
        self.assertEqual(stats["accepted_last_resort_title_emails"], 2)
        self.assertEqual(
            set(JobRecruiterTarget.objects.filter(job_posting=job).values_list("recipient_email_snapshot", flat=True)),
            {"rory@roboticsfallback.example", "devon@roboticsfallback.example"},
        )

    def test_company_fetch_attempts_matching_last_resort_title_when_has_email_flag_is_missing(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Missing Flag Co",
            normalized_name="missing flag co",
            active_domain="missingflag.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        person = {
            "id": "apollo-ceo-1",
            "name": "Cory CEO",
            "title": "President & CEO",
            "email_status": "verified",
        }
        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    **person,
                    "email": "cory@missingflag.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[person],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ) as match_mock:
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        match_mock.assert_called_once()
        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_last_resort_title_emails"], 1)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "cory@missingflag.example")

    def test_company_fetch_skips_matching_last_resort_title_when_apollo_says_no_email_available(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="No Email Flag Co",
            normalized_name="no email flag co",
            active_domain="noemailflag.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        person = {
            "id": "apollo-ceo-no-email",
            "name": "Nora Noemail",
            "title": "President & CEO",
            "has_email": False,
            "email_status": "unavailable",
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[person],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
        ) as match_mock:
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        match_mock.assert_not_called()
        self.assertEqual(stats["emails_found"], 0)
        self.assertEqual(stats["skip_reasons"]["search:apollo_unavailable_email"], 2)

    def test_paid_apollo_reveal_with_recruiting_title_is_selected(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Paid Reveal Co",
            normalized_name="paid reveal co",
            active_domain="paidreveal.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        search_people = [
            {
                "id": "apollo-paid-1",
                "name": "Pat Paid",
                "title": "Data Science Manager",
                "has_email": True,
                "email_status": "verified",
            }
        ]
        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    "id": "apollo-paid-1",
                    "name": "Pat Paid",
                    "title": "Technical Recruiter",
                    "email": "pat@paidreveal.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=search_people,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["credits_consumed"], 1)
        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_paid_nonmatching_title_emails"], 0)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "pat@paidreveal.example")
        self.assertEqual(target.company_recruiter.apollo_title, "Technical Recruiter")


class ReadOnlyReviewGenerateEmailTests(TestCase):
    def test_review_page_shows_per_job_generate_button(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        job = _create_job_with_email(batch=batch, approved=False, generated=False)

        response = self.client.get(reverse("read_only_review_dashboard"), {"batch_date": batch.batch_date.isoformat()})

        self.assertContains(response, "Generate Email")
        self.assertContains(response, "Generate Missing Emails")
        self.assertContains(response, "Regenerate All Emails")
        self.assertContains(response, "Email AI provider")
        self.assertContains(response, reverse("set_email_generation_model_view"))
        self.assertContains(response, reverse("read_only_generate_job_email_view"))
        self.assertContains(response, reverse("read_only_regenerate_batch_emails_view"))
        self.assertContains(response, f'name="job_id" value="{job.id}"')

    def test_review_page_has_date_picker_and_send_control_link_for_selected_batch(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=3))
        _create_job_with_email(batch=batch, approved=False, generated=False)
        batch_date = batch.batch_date.isoformat()

        response = self.client.get(reverse("read_only_review_dashboard"), {"batch_date": batch_date})

        self.assertContains(response, f'type="date" name="batch_date" value="{batch_date}"')
        self.assertContains(response, f'{reverse("send_control_dashboard")}?batch_date={batch_date}')
        self.assertContains(response, f'/review-readonly/?batch_date={batch_date}')
        self.assertContains(response, "Send emails for this date")

    def test_generate_button_runs_single_job_generation_and_returns_to_job(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        job = _create_job_with_email(batch=batch, approved=False, generated=False)

        with mock.patch(
            "core.views.run_cold_email_generation_for_job",
            return_value={"job_id": job.id, "generated": 1, "error": ""},
        ) as generate_mock:
            response = self.client.post(
                reverse("read_only_generate_job_email_view"),
                {"job_id": str(job.id), "batch_date": batch.batch_date.isoformat()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/review-readonly/?batch_date={batch.batch_date.isoformat()}#job-{job.id}", response["Location"])
        generate_mock.assert_called_once()
        self.assertEqual(generate_mock.call_args.kwargs["job"].id, job.id)

    def test_bulk_regenerate_runs_for_whole_batch_and_overwrites_existing(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        _create_job_with_email(batch=batch, approved=False, generated=True)

        with mock.patch(
            "core.views.run_cold_email_generation_for_eligible_jobs",
            return_value={"totals": {"jobs_seen": 1, "generated": 1, "job_errors": 0}, "jobs": []},
        ) as generate_mock:
            response = self.client.post(
                reverse("read_only_regenerate_batch_emails_view"),
                {"batch_date": batch.batch_date.isoformat()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/review-readonly/?batch_date={batch.batch_date.isoformat()}", response["Location"])
        generate_mock.assert_called_once_with(
            max_jobs=None,
            batch_date=batch.batch_date.isoformat(),
            skip_existing=False,
        )

    def test_bulk_generate_missing_only_skips_existing(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        _create_job_with_email(batch=batch, approved=False, generated=True)

        with mock.patch(
            "core.views.run_cold_email_generation_for_eligible_jobs",
            return_value={"totals": {"jobs_seen": 0, "generated": 0, "job_errors": 0}, "jobs": []},
        ) as generate_mock:
            response = self.client.post(
                reverse("read_only_regenerate_batch_emails_view"),
                {"batch_date": batch.batch_date.isoformat(), "generation_scope": "missing"},
            )

        self.assertEqual(response.status_code, 302)
        generate_mock.assert_called_once_with(
            max_jobs=None,
            batch_date=batch.batch_date.isoformat(),
            skip_existing=True,
        )


class LegacyTargetSelectionTests(TestCase):
    def test_sync_allows_legacy_contacts_without_data_manager_title(self):
        company = Company.objects.create(
            raw_name_latest="Acme",
            normalized_name="acme",
            active_domain="acme.com",
        )
        job = _create_job_with_email(company=company)
        recruiters = []
        for idx in range(12):
            recruiters.append(
                CompanyRecruiter.objects.create(
                    company=company,
                    person_name=f"Legacy Person {idx:02d}",
                    legacy=True,
                    source=CompanyRecruiter.Source.LEGACY,
                    email=f"legacy{idx:02d}@acme.com",
                    is_active=True,
                )
            )

        sync_job_targets_for_job(job=job, max_targets=10, auto_select=True)

        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job).count(), 10)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).count(), 10)

    def test_sync_blocks_non_verified_apollo_contacts(self):
        company = Company.objects.create(
            raw_name_latest="Acme",
            normalized_name="acme apollo strict",
            active_domain="acme.com",
        )
        job = _create_job_with_email(company=company)
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Risky Apollo",
            email="risky@acme.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-risky",
            email_status="risky",
            apollo_title="Data Science Manager",
            is_active=True,
        )
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Verified Apollo",
            email="verified@acme.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-verified",
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )

        stats = sync_job_targets_for_job(job=job, max_targets=10, auto_select=True)

        self.assertEqual(stats["targets_upserted"], 1)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "verified@acme.com")

    def test_company_sync_includes_manual_linkedin_jobs_but_excludes_manual_email_jobs(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Manual LinkedIn Co",
            normalized_name="manual linkedin co",
            active_domain="example.com",
        )
        manual_linkedin_job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        manual_linkedin_job.is_manual_import = True
        manual_linkedin_job.is_manual_email_job = False
        manual_linkedin_job.save(update_fields=["is_manual_import", "is_manual_email_job"])
        manual_email_job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        manual_email_job.linkedin_url = "https://www.linkedin.com/jobs/view/456/"
        manual_email_job.normalized_linkedin_url = "https://www.linkedin.com/jobs/view/456/"
        manual_email_job.dedupe_key = f"{company.normalized_name}:manual-email:{batch.batch_date}"
        manual_email_job.is_manual_import = True
        manual_email_job.is_manual_email_job = True
        manual_email_job.save(
            update_fields=["linkedin_url", "normalized_linkedin_url", "dedupe_key", "is_manual_import", "is_manual_email_job"]
        )
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Apollo Person",
            email="apollo@example.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-1",
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )

        stats = sync_job_targets_for_company_pending_jobs(company=company, max_targets=10, auto_select=True)

        self.assertEqual(stats["jobs_seen"], 1)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=manual_linkedin_job, is_selected_for_job=True).count(), 1)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=manual_email_job).count(), 0)

    def test_sync_prefers_data_leadership_then_includes_paid_apollo_reveals(self):
        company = Company.objects.create(
            raw_name_latest="Abode Money",
            normalized_name="abode money",
            active_domain="abodemoney.com",
        )
        job = _create_job_with_email(company=company)
        cdo = CompanyRecruiter.objects.create(
            company=company,
            person_name="Amir Dargulov",
            email="amir@abodemoney.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-cdo",
            email_status="verified",
            apollo_title="Chief Data Officer",
            is_active=True,
        )
        cao = CompanyRecruiter.objects.create(
            company=company,
            person_name="Zack Green",
            email="zack@ownabode.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-cao",
            email_status="verified",
            apollo_title="Chief Analytics Officer",
            is_active=True,
        )
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Blocked CFO",
            email="cfo@abodemoney.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-cfo",
            email_status="verified",
            apollo_title="Chief Financial Officer",
            is_active=True,
        )

        stats = sync_job_targets_for_job(job=job, max_targets=10, auto_select=True, allow_fallback_contacts=True)

        self.assertEqual(stats["targets_upserted"], 3)
        selected = list(
            JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True)
            .order_by("selection_order")
            .values_list("company_recruiter__person_name", flat=True)
        )
        self.assertEqual(selected, ["Amir Dargulov", "Zack Green", "Blocked CFO"])
        cdo.refresh_from_db()
        cao.refresh_from_db()
        self.assertFalse(cdo.manually_targeted)
        self.assertFalse(cao.manually_targeted)

    def test_sync_keeps_paid_apollo_reveals_even_above_target_limit(self):
        company = Company.objects.create(
            raw_name_latest="Overflow Co",
            normalized_name="overflow co",
            active_domain="overflow.example",
        )
        job = _create_job_with_email(company=company)
        for idx in range(12):
            CompanyRecruiter.objects.create(
                company=company,
                person_name=f"Paid Reveal {idx:02d}",
                email=f"paid{idx:02d}@overflow.example",
                source=CompanyRecruiter.Source.APOLLO,
                apollo_person_id=f"apollo-paid-{idx}",
                email_status="verified",
                apollo_title="Technical Recruiter",
                is_active=True,
            )

        stats = sync_job_targets_for_job(job=job, max_targets=10, auto_select=True)

        self.assertEqual(stats["targets_upserted"], 12)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).count(), 12)

    def test_apollo_lookup_includes_recruiting_and_talent_titles(self):
        company = Company.objects.create(
            raw_name_latest="Talent Co",
            normalized_name="talent co",
            active_domain="talent.example",
        )
        job = _create_job_with_email(company=company)
        search_people = [
            {
                "id": "apollo-talent-1",
                "name": "Tara Talent",
                "title": "Technical Recruiter",
                "email": "tara@talent.example",
                "email_status": "verified",
            }
        ]
        match_payload = {
            "matches": [
                {
                    "id": "apollo-talent-1",
                    "name": "Tara Talent",
                    "title": "Technical Recruiter",
                    "email": "tara@talent.example",
                    "email_status": "verified",
                }
            ]
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=search_people,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_paid_nonmatching_title_emails"], 0)
        target = JobRecruiterTarget.objects.get(job_posting=job, is_selected_for_job=True)
        self.assertEqual(target.recipient_email_snapshot, "tara@talent.example")
        self.assertEqual(target.company_recruiter.apollo_title, "Technical Recruiter")
        self.assertTrue(target.company_recruiter.title_match)

    def test_company_fetch_rejects_non_verified_apollo_email_and_stores_stats(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Risky Apollo Co",
            normalized_name="risky apollo co",
            active_domain="riskyapollo.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.save(update_fields=["status", "updated_at"])
        person = {
            "id": "apollo-risky-1",
            "name": "Riley Risky",
            "title": "Data Science Manager",
            "has_email": True,
            "email_status": "risky",
        }
        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    **person,
                    "email": "riley@riskyapollo.example",
                    "email_status": "risky",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[person],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(company=company, location_hint="Austin, TX", max_people=10)

        company.refresh_from_db()
        self.assertEqual(stats["emails_found"], 0)
        self.assertEqual(stats["verified_emails"], 0)
        self.assertEqual(stats["unverified_emails"], 1)
        self.assertEqual(stats["apollo_email_status_counts"], {"risky": 1})
        self.assertEqual(stats["skip_reasons"]["apollo_non_verified_email:risky"], 1)
        self.assertEqual(stats["credits_not_converted_to_email"], 1)
        self.assertEqual(company.last_apollo_verified_emails_found, 0)
        self.assertEqual(company.last_apollo_unverified_emails_found, 1)
        self.assertEqual(company.last_apollo_email_status_counts, {"risky": 1})
        self.assertFalse(CompanyRecruiter.objects.filter(company=company).exists())
        self.assertFalse(JobRecruiterTarget.objects.filter(job_posting=job).exists())
        rejected = ApolloRejectedEmail.objects.get(company=company)
        self.assertEqual(rejected.email, "riley@riskyapollo.example")
        self.assertEqual(rejected.email_status, "risky")
        self.assertEqual(rejected.reason, "apollo_non_verified_email:risky")
        self.assertEqual(rejected.source_workflow, "apollo_company_topup")

    def test_company_fetch_credit_guard_stops_after_alternate_domain_reveal(self):
        company = Company.objects.create(
            raw_name_latest="Domain Drift Co",
            normalized_name="domain drift co",
            active_domain="domaindrift.example",
        )
        people = [
            {
                "id": "apollo-hr-1",
                "name": "Hannah HR",
                "title": "Human Resources Manager",
                "has_email": True,
            },
            {
                "id": "apollo-hr-2",
                "name": "Tara Talent",
                "title": "Technical Recruiter",
                "has_email": True,
            },
        ]
        first_bad_domain = {
            "credits_consumed": 1,
            "matches": [
                {
                    **people[0],
                    "email": "hannah@different-domain.example",
                    "email_status": "verified",
                }
            ],
        }
        second_would_have_spent = {
            "credits_consumed": 1,
            "matches": [
                {
                    **people[1],
                    "email": "tara@domaindrift.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=people,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            side_effect=[first_bad_domain, second_would_have_spent],
        ) as bulk_match:
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint="Austin, TX",
                max_people=10,
                allow_alternate_domain_emails=False,
            )

        self.assertEqual(bulk_match.call_count, 1)
        self.assertEqual(stats["credits_consumed"], 1)
        self.assertEqual(stats["emails_found"], 0)
        self.assertEqual(stats["credits_not_converted_to_email"], 1)
        self.assertEqual(stats["skip_reasons"]["credit_guard_stopped_enrichment"], 1)
        self.assertEqual(stats["skip_reasons"]["alternate_domain_email_blocked"], 1)
        self.assertFalse(CompanyRecruiter.objects.filter(company=company).exists())

    def test_company_fetch_batch_settings_accept_alternate_domain_verified_email(self):
        company = Company.objects.create(
            raw_name_latest="Domain Alias Co",
            normalized_name="domain alias co",
            active_domain="domainalias.example",
        )
        person = {
            "id": "apollo-alias-1",
            "name": "Hannah HR",
            "title": "Human Resources Manager",
            "has_email": True,
        }
        match_payload = {
            "credits_consumed": 1,
            "matches": [
                {
                    **person,
                    "email": "hannah@related-domain.example",
                    "email_status": "verified",
                }
            ],
        }

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            return_value=[person],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            return_value=match_payload,
        ):
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint="Austin, TX",
                max_people=10,
                allow_alternate_domain_emails=True,
            )

        recruiter = CompanyRecruiter.objects.get(company=company)
        self.assertEqual(stats["emails_found"], 1)
        self.assertEqual(stats["accepted_alternate_domain_emails"], 1)
        self.assertEqual(stats["credits_not_converted_to_email"], 0)
        self.assertEqual(recruiter.email, "hannah@related-domain.example")

    def test_company_fetch_broad_fallback_prioritizes_data_and_accepts_ceo_last(self):
        company = Company.objects.create(
            raw_name_latest="Broad Fallback Co",
            normalized_name="broad fallback co",
            active_domain="broadfallback.example",
        )
        people = [
            {
                "id": "apollo-ops-1",
                "name": "Olivia Ops",
                "title": "Operations Manager",
                "has_email": True,
            },
            {
                "id": "apollo-product-1",
                "name": "Priya Product",
                "title": "Director of Product",
                "has_email": True,
            },
            {
                "id": "apollo-engineering-1",
                "name": "Eli Engineering",
                "title": "Engineering Manager",
                "has_email": True,
            },
            {
                "id": "apollo-ceo-1",
                "name": "Casey CEO",
                "title": "CEO / Founder",
                "has_email": True,
            },
            {
                "id": "apollo-data-1",
                "name": "Dana Data",
                "title": "Data Scientist",
                "has_email": True,
            },
        ]

        def fake_search(*, person_titles, **kwargs):
            if "director of product" in person_titles:
                return people
            return []

        def fake_bulk(ids):
            matches = []
            for person in people:
                if person["id"] in ids:
                    matches.append({
                        **person,
                        "email": f"{person['name'].split()[0].lower()}@broadfallback.example",
                        "email_status": "verified",
                    })
            return {"credits_consumed": len(matches), "matches": matches}

        with mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_people_from_apollo",
            side_effect=fake_search,
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.search_company_people_from_apollo",
            return_value=[],
        ), mock.patch(
            "core.services.apollo_recruiter_fetch_service.bulk_match_people_from_apollo",
            side_effect=fake_bulk,
        ) as bulk_match:
            stats = upsert_company_recruiters_from_apollo(
                company=company,
                location_hint="Austin, TX",
                max_people=5,
                allow_last_resort_titles=False,
                allow_paid_nonmatching_titles=False,
                allow_broad_fallback_titles=True,
            )

        emails = list(
            CompanyRecruiter.objects.filter(company=company)
            .order_by("id")
            .values_list("email", flat=True)
        )
        self.assertEqual(stats["emails_found"], 5)
        self.assertEqual(stats["accepted_broad_fallback_title_emails"], 5)
        self.assertEqual(stats["accepted_paid_nonmatching_title_emails"], 0)
        self.assertEqual(
            set(emails),
            {
                "dana@broadfallback.example",
                "eli@broadfallback.example",
                "priya@broadfallback.example",
                "olivia@broadfallback.example",
                "casey@broadfallback.example",
            },
        )
        bulk_ids = [call.args[0][0] for call in bulk_match.call_args_list]
        self.assertEqual(bulk_ids[0], "apollo-data-1")
        self.assertEqual(bulk_ids[-1], "apollo-ceo-1")

    def test_sync_promotes_valid_contacts_from_lookup_history_without_apollo_call(self):
        company = Company.objects.create(
            raw_name_latest="Abode Money",
            normalized_name="abode money",
            active_domain="abodemoney.com",
        )
        job = _create_job_with_email(company=company)
        TargetedPeopleLookupRun.objects.create(
            company=company,
            status=TargetedPeopleLookupRun.Status.PARTIAL,
            result_rows=[
                {
                    "name": "Amir Dargulov",
                    "status": "skipped",
                    "reason": "non_data_science_manager_title",
                    "email": "amir@abodemoney.com",
                    "email_status": "verified",
                    "apollo_person_id": "apollo-amir",
                    "apollo_name": "Amir Dargulov",
                    "title": "Chief Data Officer",
                    "linkedin_url": "http://www.linkedin.com/in/amirdargulov",
                },
                {
                    "name": "Arnhav I.",
                    "status": "skipped",
                    "reason": "no_work_email_returned",
                    "email": "",
                    "title": "",
                },
            ],
        )

        stats = sync_job_targets_for_job(job=job, max_targets=10, auto_select=True, allow_fallback_contacts=True)

        self.assertEqual(stats["targets_upserted"], 1)
        target = JobRecruiterTarget.objects.get(job_posting=job)
        self.assertEqual(target.recipient_email_snapshot, "amir@abodemoney.com")
        self.assertTrue(target.company_recruiter.manually_targeted)

    def test_sync_uses_data_leadership_fallback_to_fill_remaining_slots(self):
        company = Company.objects.create(
            raw_name_latest="Acme",
            normalized_name="acme fallback",
            active_domain="acme.com",
        )
        job = _create_job_with_email(company=company)
        manager = CompanyRecruiter.objects.create(
            company=company,
            person_name="Tara Manager",
            email="tara@acme.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-manager",
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Chris CDO",
            email="cdo@acme.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-cdo",
            email_status="verified",
            apollo_title="Chief Data Officer",
            is_active=True,
        )

        sync_job_targets_for_job(job=job, max_targets=10, auto_select=True, allow_fallback_contacts=True)

        selected = list(
            JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).order_by("selection_order")
        )
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0].company_recruiter_id, manager.id)
        self.assertEqual(selected[1].company_recruiter.apollo_title, "Chief Data Officer")


class ManualLinkedInFlowDashboardTests(TestCase):
    def test_manual_linkedin_import_updates_existing_job_with_hiring_team_lead(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Cavallo", normalized_name="cavallo")
        job = JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company,
            is_manual_import=True,
            linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
            apply_url="https://www.linkedin.com/jobs/view/4417407332/",
            normalized_apply_url="https://www.linkedin.com/jobs/view/4417407332/",
            title="Data Scientist",
            company="Cavallo",
            location="Grand Rapids, MI",
            description="Build data science products.",
            normalized_company="cavallo",
            normalized_title="data scientist",
            normalized_location="michigan",
            canonical_company="cavallo",
            canonical_title="data scientist",
            canonical_location="michigan",
            dedupe_key="cavallo:data scientist:michigan",
            sort_company="cavallo",
            sort_title="data scientist",
            sort_location="michigan",
            status=JobPosting.Status.EMAIL_DISCOVERY_DONE,
        )

        result = run_manual_linkedin_import(
            raw_urls_text=job.linkedin_url,
            hiring_team_text=(
                "Meet the hiring team\n"
                "Sarah Ellis 3rd\n"
                "Vice President, Human Resources at Cavallo\n"
                "Job poster"
            ),
            cooldown_days=0,
            apply_cooldown_filters=True,
            skip_blocked_companies=True,
            use_openai_filter=False,
            dry_run=False,
        )

        job.refresh_from_db()
        self.assertEqual(result["skipped_existing_url"], 1)
        self.assertEqual(result["hiring_team_leads_stored"], 1)
        self.assertEqual(job.recruiter_name, "Sarah Ellis")
        self.assertEqual(job.recruiter_title, "Vice President, Human Resources at Cavallo")
        self.assertEqual(job.status, JobPosting.Status.RECRUITERS_PENDING)

    @mock.patch(
        "core.services.manual_linkedin_import_service.extract_company_linkedin_url_from_html",
        return_value="https://www.linkedin.com/company/cgi/",
    )
    @mock.patch("core.services.manual_linkedin_import_service.fetch_linkedin_job_details")
    def test_manual_linkedin_import_stores_scraped_job_poster(self, mock_fetch, _mock_company_linkedin):
        job_url = "https://www.linkedin.com/jobs/view/4417456159/"
        mock_fetch.return_value = LinkedInJobDetails(
            job_url=job_url,
            final_url=job_url,
            status_code=200,
            page_html="<html></html>",
            external_job_id="4417456159",
            title="Python Backend Engineer",
            company="CGI",
            location="Lafayette, LA",
            description_text="Build FastAPI services on AWS.",
            apply_url=job_url,
            recruiter_name="Michelle Podinker",
            recruiter_title="Senior Talent Acquistion Specialist at CGI",
            recruiter_linkedin="https://www.linkedin.com/in/michellepodinker",
        )

        result = run_manual_linkedin_import(
            raw_urls_text=job_url,
            cooldown_days=0,
            apply_cooldown_filters=True,
            skip_blocked_companies=True,
            use_openai_filter=False,
            dry_run=False,
        )

        job = JobPosting.objects.get(external_job_id="4417456159")
        self.assertEqual(result["created_jobs"], 1)
        self.assertEqual(result["hiring_team_leads_scraped"], 1)
        self.assertEqual(result["hiring_team_leads_stored"], 1)
        self.assertEqual(job.recruiter_name, "Michelle Podinker")
        self.assertEqual(job.recruiter_title, "Senior Talent Acquistion Specialist at CGI")
        self.assertEqual(job.recruiter_linkedin, "https://www.linkedin.com/in/michellepodinker")

    def test_result_dashboard_hides_raw_debug_sections(self):
        session = self.client.session
        session["manual_linkedin_flow_result"] = {
            "batch_date": "2026-05-16",
            "raw_lines": 3,
            "unique_urls": 3,
            "scrape_ok": 2,
            "created_jobs": 1,
            "filtered_reject": 1,
            "manual_summary": {
                "input_urls": 3,
                "unique_urls": 3,
                "scraped_jobs": 2,
                "created_jobs": 1,
                "not_useful_jobs": 2,
                "apply_jobs": 1,
                "reject_jobs": 1,
            },
            "stored_job_rows": [
                {
                    "job_id": 123,
                    "company": "Acme",
                    "title": "Data Analyst",
                    "location": "Remote",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/1",
                }
            ],
            "apply_decision_rows": [
                {
                    "company": "Acme",
                    "title": "Data Analyst",
                    "location": "Remote",
                    "reason": "matches data analyst role",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/1",
                }
            ],
            "reject_decision_rows": [
                {
                    "company": "Bad Co",
                    "title": "Senior Data Scientist",
                    "location": "CA",
                    "reason": "requires too much experience",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/2",
                }
            ],
            "not_imported_rows": [
                {
                    "status": "rejected",
                    "company": "Bad Co",
                    "title": "Senior Data Scientist",
                    "location": "CA",
                    "detail": "requires too much experience",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/2",
                }
            ],
        }
        session.save()

        response = self.client.get(reverse("manual_linkedin_flow"), HTTP_HOST="127.0.0.1:8000")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latest Run Result")
        self.assertContains(response, "Useful Stored Jobs")
        self.assertContains(response, "Rejected / Not Useful Jobs")
        self.assertNotContains(response, "Full raw result")
        self.assertNotContains(response, "URL Normalization")
        self.assertNotContains(response, "Raw Model Output")

    def test_manual_linkedin_flow_can_apply_rejected_jobs_again(self):
        session = self.client.session
        session["manual_linkedin_flow_result"] = {
            "batch_date": "2026-05-16",
            "raw_lines": 2,
            "unique_urls": 2,
            "scrape_ok": 2,
            "created_jobs": 0,
            "filtered_reject": 2,
            "filter_decision_rows": [
                {
                    "company": "Odoo",
                    "title": "Business Analyst - ERP",
                    "location": "Buffalo, NY",
                    "reason": "implementation consulting",
                    "decision": "REJECT",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/111/",
                },
                {
                    "company": "Doppel",
                    "title": "Software Engineer",
                    "location": "New York, NY",
                    "reason": "outside target role family",
                    "decision": "REJECT",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/222/",
                },
            ],
            "job_status_rows": [],
            "url_rows": [],
        }
        session.save()

        response = self.client.get(reverse("manual_linkedin_flow"), HTTP_HOST="127.0.0.1:8000")

        self.assertContains(response, reverse("manual_linkedin_apply_rejected"))
        self.assertContains(response, "Apply Selected Rejected Jobs")
        self.assertContains(response, "Apply All Rejected Jobs")
        self.assertContains(response, "Apply Selected Rejected Skips")
        self.assertContains(response, "Apply All Rejected Skips")

    def test_apply_selected_rejected_jobs_bypasses_filter_and_cooldown(self):
        session = self.client.session
        session["manual_linkedin_flow_result"] = {
            "batch_date": "2026-05-16",
            "reject_decision_rows": [
                {
                    "company": "Odoo",
                    "title": "Business Analyst - ERP",
                    "location": "Buffalo, NY",
                    "reason": "implementation consulting",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/111/",
                },
                {
                    "company": "Doppel",
                    "title": "Software Engineer",
                    "location": "New York, NY",
                    "reason": "outside target role family",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/222/",
                },
            ],
        }
        session.save()

        fake_result = {
            "created_jobs": 1,
            "job_errors": 0,
            "manual_summary": {"skipped_jobs": 0},
        }
        with mock.patch(
            "core.views.run_manual_linkedin_import",
            return_value=fake_result,
        ) as import_mock:
            response = self.client.post(
                reverse("manual_linkedin_apply_rejected"),
                {"action": "apply_selected_rejected", "reject_url": ["https://www.linkedin.com/jobs/view/111/"]},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("manual_linkedin_flow"))
        import_mock.assert_called_once()
        kwargs = import_mock.call_args.kwargs
        self.assertEqual(kwargs["raw_urls_text"], "https://www.linkedin.com/jobs/view/111/")
        self.assertEqual(kwargs["cooldown_days"], 0)
        self.assertFalse(kwargs["apply_cooldown_filters"])
        self.assertFalse(kwargs["use_openai_filter"])
        self.assertFalse(kwargs["dry_run"])
        self.assertTrue(kwargs["skip_blocked_companies"])

    def test_apply_all_skipped_rejected_jobs_uses_only_rejected_skip_urls(self):
        session = self.client.session
        session["manual_linkedin_flow_result"] = {
            "batch_date": "2026-05-16",
            "manual_summary": {"skipped_jobs": 3},
            "stored_job_rows": [],
            "apply_decision_rows": [],
            "reject_decision_rows": [],
            "not_imported_rows": [
                {
                    "status": "rejected",
                    "company": "Odoo",
                    "title": "Business Analyst - ERP",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/111/",
                },
                {
                    "status": "openai_reject",
                    "company": "Doppel",
                    "title": "Software Engineer",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/222/",
                },
                {
                    "status": "deduped",
                    "company": "Existing Co",
                    "title": "Data Analyst",
                    "linkedin_url": "https://www.linkedin.com/jobs/view/333/",
                },
            ],
        }
        session.save()

        fake_result = {"created_jobs": 2, "job_errors": 0, "manual_summary": {"skipped_jobs": 0}}
        with mock.patch("core.views.run_manual_linkedin_import", return_value=fake_result) as import_mock:
            response = self.client.post(
                reverse("manual_linkedin_apply_rejected"),
                {"action": "apply_all_skipped_rejected"},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        kwargs = import_mock.call_args.kwargs
        self.assertEqual(
            kwargs["raw_urls_text"],
            "https://www.linkedin.com/jobs/view/111/\nhttps://www.linkedin.com/jobs/view/222/",
        )
        self.assertNotIn("333", kwargs["raw_urls_text"])
        self.assertFalse(kwargs["use_openai_filter"])

    def test_pipeline_controls_allow_zero_company_cooldown(self):
        response = self.client.post(
            reverse("pipeline_set_controls_view"),
            {"company_cooldown_days": "0", "max_people_per_company": "15", "next": "/manual-linkedin-flow/"},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manual-linkedin-flow/")
        setting = AppSetting.get_solo()
        self.assertEqual(setting.company_cooldown_days, 0)
        self.assertEqual(setting.max_people_per_company, 15)

    def test_email_generation_model_selector_persists_provider_and_models(self):
        response = self.client.post(
            reverse("set_email_generation_model_view"),
            {
                "email_ai_provider": "anthropic",
                "openai_email_model": "gpt-5.4",
                "anthropic_email_model": "claude-sonnet-4-6",
                "next": "/review-readonly/",
            },
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/review-readonly/")
        setting = AppSetting.get_solo()
        self.assertEqual(setting.email_generation_provider, "anthropic")
        self.assertEqual(setting.openai_email_model, "gpt-5.4")
        self.assertEqual(setting.anthropic_email_model, "claude-sonnet-4-6")

    def test_manual_linkedin_flow_shows_send_control_and_sender_limit_form(self):
        SenderAccount.objects.create(email="a@example.com", app_password="pw", daily_limit=30)

        response = self.client.get(reverse("manual_linkedin_flow"), HTTP_HOST="127.0.0.1:8000")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send Control")
        self.assertContains(response, reverse("send_control_set_sender_limit_view"))
        self.assertContains(response, "Set Limit For All Senders")
        self.assertContains(response, "30</strong> per sender today")

    def test_sender_limit_form_can_return_to_manual_linkedin_flow(self):
        SenderAccount.objects.create(email="a@example.com", app_password="pw", daily_limit=15)

        response = self.client.post(
            reverse("send_control_set_sender_limit_view"),
            {"daily_limit": "30", "next": "/manual-linkedin-flow/"},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manual-linkedin-flow/")
        self.assertEqual(SenderAccount.objects.exclude(daily_limit=30).count(), 0)

    def test_pipeline_dashboard_can_select_past_populated_batch_date(self):
        DailyBatch.objects.create(batch_date=timezone.localdate())
        batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=4))
        _create_job_with_email(batch=batch, approved=False, generated=False)
        batch_date = batch.batch_date.isoformat()

        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"batch_date": batch_date},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Batch Date")
        self.assertContains(response, "Email AI provider")
        self.assertContains(response, reverse("set_email_generation_model_view"))
        self.assertContains(response, "Recent available dates")
        self.assertContains(response, "Write Missing Email Drafts")
        self.assertContains(response, "Rewrite All Email Drafts")
        self.assertContains(response, "Today's Batch")
        self.assertContains(response, "Plain English")
        self.assertContains(response, f'type="date" name="batch_date" value="{batch_date}"')
        self.assertContains(response, f'?batch_date={batch_date}')

    def test_pipeline_dashboard_company_regex_search_shows_matching_job_details(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Cavallo", normalized_name="cavallo")
        job = JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company,
            linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/4417407332/",
            apply_url="https://www.linkedin.com/jobs/view/4417407332/",
            normalized_apply_url="https://www.linkedin.com/jobs/view/4417407332/",
            title="Data Scientist",
            company="Cavallo",
            location="Grand Rapids, MI",
            description="Build dashboards, data pipelines, and predictive models.",
            normalized_company="cavallo",
            normalized_title="data scientist",
            normalized_location="michigan",
            canonical_company="cavallo",
            canonical_title="data scientist",
            canonical_location="michigan",
            dedupe_key="cavallo:data scientist:michigan",
            sort_company="cavallo",
            sort_title="data scientist",
            sort_location="michigan",
            status=JobPosting.Status.REAL_SENT,
        )
        GeneratedEmail.objects.create(job_posting=job, subject="Data Scientist Role", body="Body")
        ApprovalRecord.objects.create(job_posting=job, is_approved=True, approved_at=timezone.now())
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(run_type=SendRun.RunType.REAL, status=SendRun.Status.SUCCESS)
        SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job,
            sender_account=sender,
            to_email="sarah@cavallo.com",
            subject_snapshot="Data Scientist Role",
            body_snapshot="Body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=timezone.now(),
        )

        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"company_search": "cavall?o"},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Global Company Search")
        self.assertContains(response, "Cavallo")
        self.assertContains(response, "Data Scientist")
        self.assertContains(response, "sarah@cavallo.com")
        self.assertContains(response, "applied/sent")

    def test_pipeline_dashboard_company_regex_search_reports_invalid_regex(self):
        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"company_search": "["},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid regex")

    def test_pipeline_generate_emails_uses_selected_batch_date(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=2))
        _create_job_with_email(batch=batch, approved=False, generated=False)
        batch_date = batch.batch_date.isoformat()

        with mock.patch(
            "core.views.run_cold_email_generation_for_eligible_jobs",
            return_value={"totals": {"jobs_seen": 1, "generated": 1, "job_errors": 0}, "jobs": []},
        ) as generate_mock:
            response = self.client.post(
                reverse("pipeline_generate_emails_view"),
                {"batch_date": batch_date, "max_jobs": "1"},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/pipeline-dashboard/?batch_date={batch_date}")
        generate_mock.assert_called_once_with(
            max_jobs=1,
            batch_date=batch_date,
            skip_existing=True,
        )

    def test_pipeline_generate_emails_can_regenerate_all_for_selected_batch(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=2))
        _create_job_with_email(batch=batch, approved=False, generated=True)
        batch_date = batch.batch_date.isoformat()

        with mock.patch(
            "core.views.run_cold_email_generation_for_eligible_jobs",
            return_value={"totals": {"jobs_seen": 1, "generated": 1, "job_errors": 0}, "jobs": []},
        ) as generate_mock:
            response = self.client.post(
                reverse("pipeline_generate_emails_view"),
                {"batch_date": batch_date, "generation_scope": "all"},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        generate_mock.assert_called_once_with(
            max_jobs=None,
            batch_date=batch_date,
            skip_existing=False,
        )


class TargetedPeopleLookupTests(TestCase):
    def test_parse_target_names_accepts_commas_newlines_and_titles(self):
        self.assertEqual(
            parse_target_person_names("Jane Doe - Recruiter, Mark Smith\nJane Doe; Priya Patel"),
            ["Jane Doe", "Mark Smith", "Priya Patel"],
        )

    def test_targeted_lookup_uses_local_recruiter_before_apollo(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.is_manual_import = True
        job.save(update_fields=["is_manual_import"])
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Jane Doe",
            email="jane@target.example",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-local-1",
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )

        with mock.patch("core.services.targeted_people_lookup_service.match_person_email_from_apollo") as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=False,
                max_people=10,
            )

        apollo_mock.assert_not_called()
        self.assertEqual(result["totals"]["targeted_local"], 1)
        self.assertEqual(result["totals"]["credits_consumed"], 0)
        self.assertEqual(JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).count(), 1)
        self.assertTrue(TargetedPeopleLookupRun.objects.filter(company=company, emails_found=1).exists())

    def test_targeted_lookup_skips_existing_person_without_email_without_apollo_credit(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Jane Doe",
            email="none",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-local-1",
            email_status="unavailable",
            is_active=True,
        )

        with mock.patch("core.services.targeted_people_lookup_service.match_person_email_from_apollo") as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=False,
                max_people=10,
            )

        apollo_mock.assert_not_called()
        self.assertEqual(result["totals"]["credits_consumed"], 0)
        self.assertEqual(result["totals"]["skip_reasons"]["person_already_known_no_email"], 1)
        self.assertEqual(result["rows"][0]["reason"], "person_already_known_no_email")

    def test_targeted_lookup_uses_local_recruiter_for_safe_name_variant(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Michael Smith",
            email="michael@target.example",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-local-michael",
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )

        with mock.patch("core.services.targeted_people_lookup_service.match_person_email_from_apollo") as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Mike Smith",
                allow_regular_fallback=False,
                max_people=10,
            )

        apollo_mock.assert_not_called()
        self.assertEqual(result["totals"]["targeted_local"], 1)
        self.assertEqual(result["totals"]["credits_consumed"], 0)

    def test_targeted_lookup_selects_existing_non_recruiting_contact_when_user_names_them(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Founder Co",
            normalized_name="founder co",
            active_domain="founder.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        existing = CompanyRecruiter.objects.create(
            company=company,
            person_name="Casey Founder",
            email="casey@founder.example",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-founder-1",
            email_status="verified",
            apollo_title="Chief Data Officer",
            is_active=True,
        )

        with mock.patch("core.services.targeted_people_lookup_service.match_person_email_from_apollo") as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Casey Founder",
                allow_regular_fallback=False,
                max_people=10,
            )

        apollo_mock.assert_not_called()
        self.assertEqual(result["totals"]["targeted_local"], 1)
        self.assertEqual(result["rows"][0]["status"], "selected")
        existing.refresh_from_db()
        self.assertFalse(existing.manually_targeted)
        self.assertEqual(
            JobRecruiterTarget.objects.filter(job_posting=job, is_selected_for_job=True).values_list(
                "recipient_email_snapshot",
                flat=True,
            ).get(),
            "casey@founder.example",
        )
        self.assertEqual(result["rows"][0]["reason"], "already_in_local_db")

    def test_targeted_lookup_skips_no_email_recruiter_for_middle_name_variant(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Jane A Doe",
            email="none",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-local-jane",
            email_status="unavailable",
            is_active=True,
        )

        with mock.patch("core.services.targeted_people_lookup_service.match_person_email_from_apollo") as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=False,
                max_people=10,
            )

        apollo_mock.assert_not_called()
        self.assertEqual(result["totals"]["credits_consumed"], 0)
        self.assertEqual(result["totals"]["skip_reasons"]["person_already_known_no_email"], 1)
        self.assertEqual(result["rows"][0]["reason"], "person_already_known_no_email")

    def test_targeted_lookup_stores_apollo_details_for_verification(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        payload = {
            "credits_consumed": 1,
            "person": {
                "id": "apollo-123",
                "name": "Jane Doe",
                "title": "Data Science Manager",
                "city": "Dallas",
                "state": "Texas",
                "country": "United States",
                "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
                "email": "jane@target.example",
                "email_status": "verified",
                "organization": {"name": "Target Co"},
            },
        }

        with mock.patch(
            "core.services.targeted_people_lookup_service.match_person_email_from_apollo",
            return_value=payload,
        ):
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=False,
                max_people=10,
            )

        row = result["rows"][0]
        self.assertEqual(row["title"], "Data Science Manager")
        self.assertEqual(row["location"], "Dallas, Texas, United States")
        self.assertEqual(row["linkedin_url"], "https://www.linkedin.com/in/jane-doe/")
        self.assertEqual(row["email_status"], "verified")
        self.assertEqual(row["credits"], 1)
        self.assertEqual(TargetedPeopleLookupRun.objects.latest("id").result_rows[0]["apollo_person_id"], "apollo-123")

    def test_targeted_lookup_rejects_non_verified_apollo_email(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co risky",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        payload = {
            "credits_consumed": 1,
            "person": {
                "id": "apollo-risky-123",
                "name": "Jane Doe",
                "title": "Data Science Manager",
                "email": "jane@target.example",
                "email_status": "risky",
            },
        }

        with mock.patch(
            "core.services.targeted_people_lookup_service.match_person_email_from_apollo",
            return_value=payload,
        ):
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=False,
                max_people=10,
            )

        self.assertEqual(result["totals"]["targeted_apollo"], 0)
        self.assertEqual(result["totals"]["unverified_emails"], 1)
        self.assertEqual(result["totals"]["apollo_email_status_counts"], {"risky": 1})
        self.assertEqual(result["totals"]["skip_reasons"]["apollo_non_verified_email:risky"], 1)
        self.assertEqual(result["rows"][0]["status"], "skipped")
        self.assertEqual(result["rows"][0]["reason"], "apollo_non_verified_email:risky")
        self.assertFalse(CompanyRecruiter.objects.filter(company=company).exists())
        self.assertFalse(JobRecruiterTarget.objects.filter(job_posting=job).exists())
        rejected = ApolloRejectedEmail.objects.get(company=company)
        self.assertEqual(rejected.email, "jane@target.example")
        self.assertEqual(rejected.email_status, "risky")
        self.assertEqual(rejected.reason, "apollo_non_verified_email:risky")
        self.assertEqual(rejected.source_workflow, "targeted_people_lookup")

    def test_targeted_lookup_reuses_existing_row_when_apollo_returns_variant_name(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        existing = CompanyRecruiter.objects.create(
            company=company,
            person_name="Michael Smith",
            email="none",
            source=CompanyRecruiter.Source.APOLLO,
            is_active=True,
        )
        payload = {
            "credits_consumed": 1,
            "person": {
                "id": "apollo-michael",
                "name": "Michael Smith",
                "title": "Data Science Manager",
                "email": "michael@target.example",
                "email_status": "verified",
            },
        }

        with mock.patch(
            "core.services.targeted_people_lookup_service.match_person_email_from_apollo",
            return_value=payload,
        ):
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="M Smith",
                allow_regular_fallback=False,
                max_people=10,
            )

        existing.refresh_from_db()
        self.assertEqual(result["totals"]["targeted_local"], 0)
        self.assertEqual(result["totals"]["targeted_apollo"], 1)
        self.assertEqual(existing.email, "michael@target.example")
        self.assertEqual(existing.apollo_person_id, "apollo-michael")
        self.assertEqual(CompanyRecruiter.objects.filter(company=company).count(), 1)

    def test_targeted_lookup_caps_apollo_calls_to_open_slots(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        no_email_payload = {
            "credits_consumed": 0,
            "person": {"name": "No Email", "email": "", "email_status": "unavailable"},
        }

        with mock.patch(
            "core.services.targeted_people_lookup_service.match_person_email_from_apollo",
            return_value=no_email_payload,
        ) as apollo_mock:
            result = run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe, Mark Smith, Priya Patel, Sam Jones",
                allow_regular_fallback=False,
                max_people=2,
            )

        self.assertEqual(apollo_mock.call_count, 2)
        self.assertEqual(result["totals"]["targeted_apollo_attempt_limit"], 2)
        self.assertEqual(result["totals"]["targeted_apollo_attempts"], 2)
        self.assertEqual(result["totals"]["skip_reasons"]["targeted_attempt_limit_reached"], 2)

    def test_targeted_lookup_excludes_attempted_names_from_regular_fallback(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        no_email_payload = {
            "credits_consumed": 0,
            "person": {"name": "Jane Doe", "email": "", "email_status": "unavailable"},
        }

        with mock.patch(
            "core.services.targeted_people_lookup_service.match_person_email_from_apollo",
            return_value=no_email_payload,
        ), mock.patch(
            "core.services.targeted_people_lookup_service.upsert_company_recruiters_from_apollo",
            return_value={"emails_found": 0, "credits_consumed": 0, "credits_not_converted_to_email": 0},
        ) as fallback_mock:
            run_targeted_people_lookup(
                company=company,
                job=job,
                raw_names="Jane Doe",
                allow_regular_fallback=True,
                max_people=2,
            )

        fallback_mock.assert_called_once()
        self.assertEqual(fallback_mock.call_args.kwargs["exclude_person_names"], ["Jane Doe"])

    def test_bulk_targeted_lookup_dry_run_maps_domains_and_caps_names(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="",
        )
        _create_job_with_email(batch=batch, company=company, approved=False, generated=False)

        result = run_bulk_targeted_people_lookup(
            company_domain_map_text='{"Target Co": "target.example"}',
            domain_people_map_text='{"target.example": "Jane Doe, Mark Smith, Priya Patel"}',
            allow_regular_fallback=True,
            dry_run=True,
            max_people=2,
        )

        company.refresh_from_db()
        self.assertEqual(company.active_domain, "")
        self.assertEqual(result["domains_updated"], 0)
        self.assertEqual(result["lookups_planned"], 1)
        self.assertEqual(result["names_submitted"], 2)
        self.assertEqual(result["names_skipped_over_slot_limit"], 1)
        self.assertEqual(result["lookup_details"][0]["status"], "would_run")


class TargetedPeopleLookupViewTests(TestCase):
    def test_pipeline_dashboard_explains_apollo_people_lookup_in_plain_words(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        log_dir = Path(settings.MEDIA_ROOT) / "run_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        run_log_path = log_dir / "test_pipeline_dashboard_apollo_titles.log"
        run_log_path.write_text(
            "\n".join(
                [
                    "[2026-05-26 10:00:00] SEARCH_BROAD_SKIP person=[NONE] reason=non_data_science_manager_title title=Retail Planner",
                    "[2026-05-26 10:00:01] SEARCH_BROAD_SKIP person=[NONE] reason=non_data_science_manager_title title=Territory Manager",
                    "[2026-05-26 10:00:02] SEARCH_BROAD_SKIP person=[NONE] reason=non_data_science_manager_title title=Territory Manager",
                ]
            ),
            encoding="utf-8",
        )
        self.addCleanup(lambda: run_log_path.exists() and run_log_path.unlink())
        session = self.client.session
        session["pipeline_recruiter_result"] = {
            "totals": {
                "eligible_company_count_at_start": 3,
                "selected_company_count": 2,
                "companies_seen": 2,
                "apollo_slots_requested": 10,
                "apollo_emails": 4,
                "credits_consumed": 4,
                "credits_not_converted_to_email": 0,
                "stop_reason": "completed_selected_scope",
                "skip_reasons": {
                    "broad:non_data_science_manager_title": 12,
                    "broad:missing_contact_title": 1,
                },
            },
            "companies": [
                {
                    "company": "target co",
                    "requested_apollo_slots_at_click": 5,
                    "emails_found": 2,
                    "credits_consumed": 2,
                    "search_returned_people": 8,
                    "skip_reasons": {"broad:non_data_science_manager_title": 6},
                    "run_log_path": str(run_log_path),
                },
                {
                    "company": "seen co",
                    "requested_apollo_slots_at_click": 5,
                    "emails_found": 0,
                    "credits_consumed": 0,
                    "search_returned_people": 3,
                    "seen_title_counts": {"Data Engineer": 2, "Product Manager": 1},
                }
            ],
        }
        session.save()

        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"batch_date": batch.batch_date.isoformat()},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latest automatic people lookup result, in plain words")
        self.assertContains(response, "People Needed Before Run")
        self.assertContains(response, "People Emails Fetched")
        self.assertContains(response, "People Still Missing")
        self.assertContains(response, "Title was not data/ML/director enough")
        self.assertContains(response, "Titles Apollo Saw")
        self.assertContains(response, "Retail Planner")
        self.assertContains(response, "Territory Manager")
        self.assertContains(response, "Data Engineer")
        self.assertContains(response, "Product Manager")
        self.assertContains(response, "x2")
        self.assertContains(response, "target co")

    def test_pipeline_dashboard_shows_quick_open_people_buttons(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        _create_job_with_email(batch=batch, company=company, approved=False, generated=False)

        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"batch_date": batch.batch_date.isoformat()},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open Job + Data/ML Managers")
        self.assertContains(response, "data-job-url=")
        self.assertContains(response, "Data Science Director")
        self.assertContains(response, "Data Science Manager")
        self.assertContains(response, "ML Manager")
        self.assertContains(response, "['data science director', 'data science manager', 'ml manager', 'analytics director']")
        self.assertContains(response, "querySelectorAll('a[data-people-label]')")
        self.assertContains(response, "window.open(url, '_blank')")
        self.assertContains(response, "Delete This Job")
        self.assertContains(response, reverse("pipeline_delete_single_job_view"))
        self.assertContains(response, "Max people for this company")
        self.assertContains(response, 'name="max_people_for_company"')

    def test_pipeline_recruiter_topup_continues_after_exact_person_success_when_below_cap(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 0})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Hiring Lead Co",
            normalized_name="hiring lead co",
            active_domain="hiringlead.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.RECRUITERS_PENDING
        job.recruiter_name = "Casey Hiring"
        job.recruiter_title = "Director of Data Science"
        job.save(update_fields=["status", "recruiter_name", "recruiter_title", "updated_at"])

        with mock.patch(
            "core.views.upsert_apify_person_recruiter_from_apollo",
            return_value={
                "job_id": job.id,
                "company": company.normalized_name,
                "person": "Casey Hiring",
                "emails_found": 1,
                "credits_consumed": 1,
            },
        ) as exact_mock, mock.patch(
            "core.views.upsert_company_recruiters_from_apollo",
            return_value={
                "company": company.normalized_name,
                "created": 0,
                "updated": 0,
                "emails_found": 2,
                "verified_emails": 2,
                "credits_consumed": 2,
                "legacy_reused": 0,
            },
        ) as company_mock:
            response = self.client.post(
                reverse("pipeline_run_recruiter_topup_view"),
                {"batch_date": batch.batch_date.isoformat(), "limit": "all_continue"},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        exact_mock.assert_called_once()
        company_mock.assert_called_once()
        self.assertEqual(company_mock.call_args.kwargs["max_people"], 10)
        result = self.client.session["pipeline_recruiter_result"]
        self.assertEqual(result["totals"]["apify_person_jobs_seen"], 1)
        self.assertEqual(result["totals"]["companies_seen"], 1)
        self.assertEqual(result["totals"]["companies_skipped_after_exact_person_success"], 0)

    def test_pipeline_recruiter_topup_includes_partial_email_discovery_done_jobs(self):
        from core.services.pipeline_dashboard_service import build_pipeline_dashboard_context

        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10, "company_cooldown_days": 0})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Partial Co",
            normalized_name="partial co",
            active_domain="partial.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        job.status = JobPosting.Status.EMAIL_DISCOVERY_DONE
        job.save(update_fields=["status", "updated_at"])
        for idx in range(2):
            recruiter = CompanyRecruiter.objects.create(
                company=company,
                person_name=f"Person {idx}",
                email=f"person{idx}@partial.example",
                source=CompanyRecruiter.Source.APOLLO,
                apollo_person_id=f"apollo-partial-{idx}",
                email_status="verified",
                apollo_title="Technical Recruiter",
                is_active=True,
            )
            JobRecruiterTarget.objects.create(
                job_posting=job,
                company_recruiter=recruiter,
                recipient_email_snapshot=recruiter.email,
                recipient_name_snapshot=recruiter.person_name,
                selection_order=idx + 1,
                is_selected_for_job=True,
                is_verified_for_job=True,
            )

        context = build_pipeline_dashboard_context(batch_date=batch.batch_date.isoformat())
        ready_row = next(row for row in context["recruiter_ready_rows"] if row["normalized_name"] == "partial co")
        self.assertEqual(ready_row["usable_recipient_count"], 2)
        self.assertEqual(ready_row["apollo_slots_needed"], 8)
        self.assertEqual(ready_row["next_action"], "Run Apollo top-up")

        with mock.patch(
            "core.views.upsert_company_recruiters_from_apollo",
            return_value={
                "company": company.normalized_name,
                "created": 0,
                "updated": 0,
                "emails_found": 8,
                "verified_emails": 8,
                "credits_consumed": 8,
                "legacy_reused": 0,
            },
        ) as company_mock:
            response = self.client.post(
                reverse("pipeline_run_recruiter_topup_view"),
                {"batch_date": batch.batch_date.isoformat(), "limit": "all_continue"},
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        company_mock.assert_called_once()
        self.assertEqual(company_mock.call_args.kwargs["max_people"], 10)
        result = self.client.session["pipeline_recruiter_result"]
        self.assertEqual(result["totals"]["companies_seen"], 1)

    def test_pipeline_dashboard_delete_single_job_removes_only_selected_job(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        other_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        other_job = _create_job_with_email(batch=other_batch, company=company, approved=False, generated=False)

        response = self.client.post(
            reverse("pipeline_delete_single_job_view"),
            {"batch_date": batch.batch_date.isoformat(), "job_id": str(job.id)},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/pipeline-dashboard/?batch_date={batch.batch_date.isoformat()}")
        self.assertFalse(JobPosting.objects.filter(id=job.id).exists())
        self.assertTrue(JobPosting.objects.filter(id=other_job.id).exists())

    def test_pipeline_dashboard_shows_stored_fallback_people_after_lookup_run(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        _create_job_with_email(batch=batch, company=company, approved=False, generated=False)
        CompanyRecruiter.objects.create(
            company=company,
            person_name="Fallback Person",
            email="fallback@target.example",
            source=CompanyRecruiter.Source.APOLLO,
            email_status="verified",
            apollo_title="Data Science Manager",
            is_active=True,
        )
        TargetedPeopleLookupRun.objects.create(
            company=company,
            raw_names="",
            parsed_names=[],
            allow_regular_fallback=True,
            max_people=5,
            status=TargetedPeopleLookupRun.Status.SUCCESS,
            emails_found=1,
            credits_consumed=1,
            result_rows=[
                {
                    "name": "[regular fallback]",
                    "source": "regular_apollo_fallback",
                    "status": "completed",
                    "email": "",
                    "reason": "emails_found=1",
                }
            ],
            totals={"company": "target co"},
        )

        response = self.client.get(
            reverse("pipeline_dashboard"),
            {"batch_date": batch.batch_date.isoformat()},
            HTTP_HOST="127.0.0.1:8000",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Stored recipients for this company")
        self.assertContains(response, "Fallback Person")
        self.assertContains(response, "fallback@target.example")

    def test_single_targeted_lookup_only_targeted_disables_regular_fallback(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)

        with mock.patch(
            "core.views.run_targeted_people_lookup",
            return_value={"totals": {"targeted_local": 0, "targeted_apollo": 0, "regular_fallback": 0, "credits_consumed": 0}},
        ) as lookup_mock:
            response = self.client.post(
                reverse("pipeline_targeted_people_lookup_view"),
                {
                    "batch_date": batch.batch_date.isoformat(),
                    "company_id": str(company.id),
                    "job_id": str(job.id),
                    "target_names": "Jane Doe",
                    "allow_regular_fallback": "1",
                    "only_targeted_lookup": "1",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        lookup_mock.assert_called_once()
        self.assertFalse(lookup_mock.call_args.kwargs["allow_regular_fallback"])

    def test_single_targeted_lookup_uses_per_company_max_people_cap(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 15})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Target Co",
            normalized_name="target co",
            active_domain="target.example",
        )
        job = _create_job_with_email(batch=batch, company=company, approved=False, generated=False)

        with mock.patch(
            "core.views.run_targeted_people_lookup",
            return_value={"totals": {"targeted_local": 0, "targeted_apollo": 0, "regular_fallback": 0, "credits_consumed": 0}},
        ) as lookup_mock:
            response = self.client.post(
                reverse("pipeline_targeted_people_lookup_view"),
                {
                    "batch_date": batch.batch_date.isoformat(),
                    "company_id": str(company.id),
                    "job_id": str(job.id),
                    "target_names": "Jane Doe",
                    "allow_regular_fallback": "1",
                    "max_people_for_company": "5",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        lookup_mock.assert_called_once()
        self.assertEqual(lookup_mock.call_args.kwargs["max_people"], 5)

    def test_bulk_targeted_lookup_only_targeted_disables_regular_fallback(self):
        with mock.patch(
            "core.views.run_bulk_targeted_people_lookup",
            return_value={"lookups_planned": 1, "names_submitted": 1, "names_skipped_over_slot_limit": 0},
        ) as lookup_mock:
            response = self.client.post(
                reverse("pipeline_bulk_targeted_people_lookup_view"),
                {
                    "batch_date": timezone.localdate().isoformat(),
                    "company_domain_map": '{"Target Co": "target.example"}',
                    "domain_people_map": '{"target.example": "Jane Doe"}',
                    "allow_regular_fallback": "1",
                    "only_targeted_lookup": "1",
                    "action": "preview",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 302)
        lookup_mock.assert_called_once()
        self.assertFalse(lookup_mock.call_args.kwargs["allow_regular_fallback"])


class SenderAccountLimitPauseTests(TestCase):
    def test_daily_limit_error_detection(self):
        self.assertTrue(is_smtp_daily_limit_error("Daily user sending limit exceeded"))
        self.assertTrue(is_smtp_daily_limit_error("(550, b'Daily sending limit exceeded')"))
        self.assertFalse(is_smtp_daily_limit_error("Bad credentials"))

    def test_pause_sender_for_daily_limit(self):
        sender = SenderAccount.objects.create(email="limit@example.com", app_password="pw", is_paused=False)

        changed = pause_sender_for_daily_limit(sender, "Daily user sending limit exceeded")

        sender.refresh_from_db()
        self.assertTrue(changed)
        self.assertTrue(sender.is_paused)
        self.assertIn("Daily user sending limit exceeded", sender.notes)

    def test_send_control_sets_all_sender_daily_limits(self):
        SenderAccount.objects.create(email="a@example.com", app_password="pw", daily_limit=15)
        SenderAccount.objects.create(email="b@example.com", app_password="pw", daily_limit=20)

        response = self.client.post(reverse("send_control_set_sender_limit_view"), {"daily_limit": "30"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SenderAccount.objects.exclude(daily_limit=30).count(), 0)

    def test_send_control_rejects_invalid_sender_daily_limit(self):
        sender = SenderAccount.objects.create(email="a@example.com", app_password="pw", daily_limit=15)

        response = self.client.post(reverse("send_control_set_sender_limit_view"), {"daily_limit": "0"})

        sender.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sender.daily_limit, 15)


class MailProviderSettingsTests(SimpleTestCase):
    def test_smtp_settings_infer_gmail_and_outlook_hosts(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            gmail = smtp_settings_for_email("sender@gmail.com")
            outlook = smtp_settings_for_email("sender@outlook.com")

        self.assertEqual(gmail.host, "smtp.gmail.com")
        self.assertEqual(gmail.port, 587)
        self.assertEqual(gmail.provider, "gmail")
        self.assertEqual(outlook.host, "smtp-mail.outlook.com")
        self.assertEqual(outlook.port, 587)
        self.assertEqual(outlook.provider, "outlook")
        self.assertEqual(imap_host_for_email("sender@hotmail.com"), "outlook.office365.com")

    def test_microsoft365_domain_env_uses_office365_hosts(self):
        env = {"MICROSOFT365_SMTP_DOMAINS": "example.com, example.org"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = smtp_settings_for_email("sender@example.com")
            imap_host = imap_host_for_email("sender@example.org")

        self.assertEqual(settings.host, "smtp.office365.com")
        self.assertEqual(settings.provider, "microsoft365")
        self.assertEqual(imap_host, "outlook.office365.com")

    def test_global_smtp_host_override_still_wins(self):
        env = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "2525"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = smtp_settings_for_email("sender@outlook.com")

        self.assertEqual(settings.host, "smtp.example.test")
        self.assertEqual(settings.port, 2525)
        self.assertEqual(settings.provider, "env")

    def test_send_via_smtp_uses_outlook_host_for_outlook_account(self):
        message = build_mime_message(
            from_name="Sender",
            from_email="sender@outlook.com",
            to_email="recipient@example.com",
            subject="Hello",
            body_text="Body",
            attachment_paths=[],
        )

        env = {"EMAIL_SENDING_ENABLED": "1", "EMAIL_SENDING_PAUSED": "0"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "core.services.smtp_send_service.smtplib.SMTP"
        ) as smtp_cls:
            server = smtp_cls.return_value
            send_via_smtp(username="sender@outlook.com", app_password="pw", message=message)

        smtp_cls.assert_called_once()
        self.assertEqual(smtp_cls.call_args.args[:2], ("smtp-mail.outlook.com", 587))
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("sender@outlook.com", "pw")
        server.sendmail.assert_called_once()
        server.quit.assert_called_once()

    def test_sender_delivery_router_uses_graph_for_graph_sender(self):
        sender = SenderAccount(
            email="sender@outlook.com",
            auth_method=SenderAccount.AuthMethod.MICROSOFT_GRAPH,
            oauth_refresh_token="refresh",
        )
        message = build_mime_message(
            from_name="Sender",
            from_email=sender.email,
            to_email="recipient@example.com",
            subject="Hello",
            body_text="Body",
            attachment_paths=[],
        )

        with mock.patch("core.services.mail_delivery_service.send_via_microsoft_graph") as graph_mock, mock.patch(
            "core.services.mail_delivery_service.send_via_smtp"
        ) as smtp_mock:
            send_via_sender_account(sender=sender, message=message)

        graph_mock.assert_called_once_with(sender=sender, message=message)
        smtp_mock.assert_not_called()

    def test_sender_delivery_router_keeps_smtp_for_smtp_sender(self):
        sender = SenderAccount(
            email="sender@gmail.com",
            app_password="pw",
            auth_method=SenderAccount.AuthMethod.SMTP_PASSWORD,
        )
        message = build_mime_message(
            from_name="Sender",
            from_email=sender.email,
            to_email="recipient@example.com",
            subject="Hello",
            body_text="Body",
            attachment_paths=[],
        )

        with mock.patch("core.services.mail_delivery_service.send_via_microsoft_graph") as graph_mock, mock.patch(
            "core.services.mail_delivery_service.send_via_smtp"
        ) as smtp_mock:
            send_via_sender_account(sender=sender, message=message)

        graph_mock.assert_not_called()
        smtp_mock.assert_called_once_with(username=sender.email, app_password="pw", message=message)

    def test_microsoft_graph_sendmail_posts_base64_mime_payload(self):
        sender = SenderAccount(
            email="sender@outlook.com",
            auth_method=SenderAccount.AuthMethod.MICROSOFT_GRAPH,
            oauth_refresh_token="refresh",
        )
        message = build_mime_message(
            from_name="Sender",
            from_email=sender.email,
            to_email="recipient@example.com",
            subject="Graph hello",
            body_text="Body",
            attachment_paths=[],
        )
        response = mock.Mock(status_code=202, text="")

        env = {"EMAIL_SENDING_ENABLED": "1", "EMAIL_SENDING_PAUSED": "0"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "core.services.microsoft_graph_send_service.get_microsoft_access_token",
            return_value="access-token",
        ), mock.patch("core.services.microsoft_graph_send_service.requests.post", return_value=response) as post_mock:
            send_via_microsoft_graph(sender=sender, message=message)

        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.args[0], "https://graph.microsoft.com/v1.0/me/sendMail")
        self.assertEqual(post_mock.call_args.kwargs["headers"]["Authorization"], "Bearer access-token")
        self.assertEqual(post_mock.call_args.kwargs["headers"]["Content-Type"], "text/plain")
        decoded_payload = base64.b64decode(post_mock.call_args.kwargs["data"]).decode("utf-8", errors="replace")
        self.assertIn("Subject: Graph hello", decoded_payload)
        self.assertIn("recipient@example.com", decoded_payload)


class SendTimingTests(SimpleTestCase):
    def test_default_send_delay_is_random_between_27_and_200_seconds(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "core.services.send_timing_service.random.randint",
            return_value=73,
        ) as randint_mock:
            self.assertEqual(configured_send_delay_seconds(), 27)
            self.assertEqual(configured_send_delay_range_seconds(), (27, 200))
            self.assertEqual(randomized_send_delay_seconds(), 73)

        randint_mock.assert_called_once_with(27, 200)

    def test_send_delay_env_range_overrides_defaults(self):
        env = {"SEND_DELAY_SECONDS": "40", "SEND_DELAY_MIN_SECONDS": "50", "SEND_DELAY_MAX_SECONDS": "90"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "core.services.send_timing_service.random.randint",
            return_value=77,
        ) as randint_mock:
            self.assertEqual(configured_send_delay_seconds(), 40)
            self.assertEqual(configured_send_delay_range_seconds(), (50, 90))
            self.assertEqual(randomized_send_delay_seconds(), 77)

        randint_mock.assert_called_once_with(50, 90)

    def test_set_send_delay_range_updates_runtime_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = set_send_delay_range_seconds(min_seconds=45, max_seconds=120, persist_to_dotenv=False)

            self.assertEqual(result, (45, 120))
            self.assertEqual(configured_send_delay_seconds(), 45)
            self.assertEqual(configured_send_delay_range_seconds(), (45, 120))

    def test_set_send_delay_range_keeps_max_at_least_min(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = set_send_delay_range_seconds(min_seconds=90, max_seconds=30, persist_to_dotenv=False)

            self.assertEqual(result, (90, 90))
            self.assertEqual(configured_send_delay_range_seconds(), (90, 90))


class InboxMonitorDashboardTests(TestCase):
    def test_inbox_monitor_page_loads(self):
        response = self.client.get(reverse("inbox_monitor_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inbox Monitor")
        self.assertContains(response, "Enable Sound")
        self.assertContains(response, '<input id="maxMessages" type="number" min="1" max="500" step="1" value="100">', html=True)
        self.assertContains(response, "Returned Emails")

    def test_inbox_monitor_data_returns_scan_json(self):
        payload = {
            "checked_at": "May 15, 2026 4:00:00 PM",
            "duration_ms": 10,
            "totals": {"accounts": 1, "ok": 1, "unavailable": 0, "reply": 1, "bounce": 0, "blocked": 0, "notice": 0},
            "accounts": [],
            "messages": [],
            "latest_key": "sender@example.com:1",
        }
        with mock.patch("core.views.scan_inbox_monitor", return_value=payload) as scan_mock:
            response = self.client.get(reverse("inbox_monitor_data"), {"max_messages": "50"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["reply"], 1)
        scan_mock.assert_called_once_with(max_messages=50)

    def test_inbox_monitor_data_ignores_scan_days(self):
        payload = {
            "checked_at": "May 15, 2026 4:00:00 PM",
            "duration_ms": 10,
            "totals": {"accounts": 1, "ok": 1, "unavailable": 0, "reply": 1, "bounce": 0, "blocked": 0, "notice": 0},
            "accounts": [],
            "messages": [],
            "latest_key": "sender@example.com:1",
        }
        with mock.patch("core.views.scan_inbox_monitor", return_value=payload) as scan_mock:
            response = self.client.get(reverse("inbox_monitor_data"), {"max_messages": "50", "days": "99"})

        self.assertEqual(response.status_code, 200)
        scan_mock.assert_called_once_with(max_messages=50)

    def test_inbox_scan_now_button_stores_result_and_redirects_back(self):
        payload = {
            "totals": {"accounts": 1, "ok": 1, "reply": 2, "bounce": 1, "blocked": 0},
            "stored": {"created": 3, "suppressed": 1},
        }
        with mock.patch("core.views.scan_and_store_inbox_events", return_value=payload) as scan_mock:
            response = self.client.post(reverse("inbox_monitor_scan_now"), {"next": "/send-control/"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/send-control/")
        scan_mock.assert_called_once_with(max_messages=100)

    def test_inbox_monitor_messages_sort_newest_first(self):
        from core.services.inbox_monitor_service import _sort_messages_newest_first

        messages = [
            {"key": "old-reply", "date_ts": 100, "status": "reply", "account": "a@example.com", "subject": "Old"},
            {"key": "new-notice", "date_ts": 200, "status": "notice", "account": "b@example.com", "subject": "New"},
            {"key": "same-time-blocked", "date_ts": 200, "status": "blocked", "account": "c@example.com", "subject": "Blocked"},
        ]

        sorted_keys = [message["key"] for message in _sort_messages_newest_first(messages)]

        self.assertEqual(sorted_keys, ["same-time-blocked", "new-notice", "old-reply"])

    def test_inbox_monitor_latest_key_uses_sorted_latest_message(self):
        sender_a = SenderAccount.objects.create(email="a@example.com", app_password="pw", is_active=True)
        sender_b = SenderAccount.objects.create(email="b@example.com", app_password="pw", is_active=True)

        def fake_scan(sender, **kwargs):
            if sender.email == sender_a.email:
                return {
                    "account": sender.email,
                    "ok": True,
                    "counts": {"reply": 1, "bounce": 0, "blocked": 0, "notice": 0},
                    "messages": [
                        {"key": "older", "date_ts": 100, "status": "reply", "account": sender.email, "subject": "Older"}
                    ],
                    "latest_key": "older",
                }
            return {
                "account": sender.email,
                "ok": True,
                "counts": {"reply": 0, "bounce": 0, "blocked": 0, "notice": 1},
                "messages": [
                    {"key": "newer", "date_ts": 200, "status": "notice", "account": sender.email, "subject": "Newer"}
                ],
                "latest_key": "newer",
            }

        with mock.patch("core.services.inbox_monitor_service._scan_account", side_effect=fake_scan):
            from core.services.inbox_monitor_service import scan_inbox_monitor

            result = scan_inbox_monitor(max_messages=5)

        self.assertEqual([message["key"] for message in result["messages"]], ["newer", "older"])
        self.assertEqual(result["latest_key"], "newer")


class LiveCompanyReplyStopTests(TestCase):
    def setUp(self):
        super().setUp()
        self.reply_ai_env = mock.patch.dict(os.environ, {"LIVE_REPLY_STOP_AI_ENABLED": "0"}, clear=False)
        self.reply_ai_env.start()

    def tearDown(self):
        self.reply_ai_env.stop()
        super().tearDown()

    def _reply_event(self, *, job, message_date=None):
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
        )
        sent_log = SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job,
            sender_account=sender,
            to_email="recipient@example.com",
            subject_snapshot="Hello",
            body_snapshot="Body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=timezone.now(),
        )
        return InboxScanEvent.objects.create(
            sender_account=sender,
            message_key=f"reply-{job.id}",
            from_header="Recipient <recipient@example.com>",
            subject="Re: Hello",
            message_date=message_date or timezone.now(),
            classification=InboxScanEvent.Classification.REPLY,
            detected_email="recipient@example.com",
            matched_sent_log=sent_log,
        )

    def test_matched_reply_does_not_stop_company_automatically(self):
        job = _create_job_with_email()
        event = self._reply_event(job=job)

        stop = record_reply_stop_for_event(event)

        self.assertIsNone(stop)
        self.assertFalse(company_has_reply_stop_today(job))
        self.assertFalse(job.company_ref.is_blocked)

    def test_send_run_ignores_automatic_reply_stop_records(self):
        job = _create_job_with_email()
        recruiter = CompanyRecruiter.objects.create(
            company=job.company_ref,
            person_name="Recipient",
            normalized_person_name="recipient",
            source=CompanyRecruiter.Source.APOLLO,
            email_status="verified",
            email="recipient@example.com",
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="recipient@example.com",
            recipient_name_snapshot="Recipient",
            is_selected_for_job=True,
            is_verified_for_job=True,
        )
        event = self._reply_event(job=job)
        DailyCompanyReplyStop.objects.create(
            company=job.company_ref,
            stop_date=timezone.localdate(),
            reply_event=event,
            matched_sent_log=event.matched_sent_log,
            respondent_email="recipient@example.com",
            reply_at=timezone.now(),
            is_active=True,
            reply_decision=DailyCompanyReplyStop.ReplyDecision.STOP,
            decision_source=DailyCompanyReplyStop.DecisionSource.OPENAI,
            decision_confidence=1.0,
            reason="Legacy automatic stop",
        )

        env = {"EMAIL_SENDING_ENABLED": "1", "EMAIL_SENDING_PAUSED": "0", "SEND_ATTACH_RESUME": "0"}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.send_run_service.send_via_smtp"
        ) as smtp_mock:
            result = run_send_initial_for_batch(
                batch_date_str=job.daily_batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
            )

        self.assertEqual(result["totals"]["emails_attempted"], 1)
        self.assertEqual(result["totals"]["emails_skipped_company_reply_stop"], 0)
        smtp_mock.assert_called_once()

    def test_live_reply_dashboard_ignores_automatic_stop_records(self):
        job = _create_job_with_email()
        event = self._reply_event(job=job)
        DailyCompanyReplyStop.objects.create(
            company=job.company_ref,
            stop_date=timezone.localdate(),
            reply_event=event,
            matched_sent_log=event.matched_sent_log,
            respondent_email="recipient@example.com",
            reply_at=timezone.now(),
            is_active=True,
            reply_decision=DailyCompanyReplyStop.ReplyDecision.STOP,
            decision_source=DailyCompanyReplyStop.DecisionSource.OPENAI,
            decision_confidence=1.0,
            reason="Legacy automatic stop",
        )

        context = build_live_company_reply_dashboard_context(job.daily_batch.batch_date.isoformat())
        response = self.client.get(
            reverse("live_company_reply_dashboard"),
            {"batch_date": job.daily_batch.batch_date.isoformat()},
        )

        self.assertEqual(context["totals"]["companies"], 1)
        self.assertEqual(context["totals"]["stopped_by_reply"], 0)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Live Company Reply Stops")
        self.assertNotContains(response, "STOPPED TODAY")

    def test_manual_dashboard_control_can_stop_and_resume_company(self):
        job = _create_job_with_email()
        batch_date = job.daily_batch.batch_date.isoformat()

        stop_response = self.client.post(
            reverse("live_company_reply_manual_decision"),
            {"company_id": job.company_ref_id, "batch_date": batch_date, "action": "stop"},
        )
        self.assertEqual(stop_response.status_code, 302)
        self.assertTrue(company_has_reply_stop_today(job))

        resume_response = self.client.post(
            reverse("live_company_reply_manual_decision"),
            {"company_id": job.company_ref_id, "batch_date": batch_date, "action": "resume"},
        )
        self.assertEqual(resume_response.status_code, 302)
        self.assertFalse(company_has_reply_stop_today(job))

    def test_inbox_scan_does_not_create_daily_company_reply_stop(self):
        job = _create_job_with_email()
        prior_event = self._reply_event(job=job)
        sender = prior_event.sender_account
        prior_event.delete()
        payload = {
            "totals": {"accounts": 1, "ok": 1, "reply": 1, "bounce": 0, "blocked": 0, "notice": 0},
            "messages": [
                {
                    "account": sender.email,
                    "key": "live-reply-message",
                    "from": "Recipient <recipient@example.com>",
                    "subject": "Re: Hello",
                    "date_raw": timezone.now().strftime("%a, %d %b %Y %H:%M:%S %z"),
                    "status": "reply",
                    "detected_email": "recipient@example.com",
                    "snippet": "Thanks for reaching out.",
                }
            ],
        }

        with mock.patch("core.services.inbox_monitor_service.scan_inbox_monitor", return_value=payload):
            result = scan_and_store_inbox_events(max_messages=10)

        self.assertEqual(result["stored"]["reply_stops"], 0)
        self.assertFalse(company_has_reply_stop_today(job))

    def test_inbox_monitor_limits_combined_messages_globally(self):
        sender_a = SenderAccount.objects.create(email="a@example.com", app_password="pw", is_active=True)
        sender_b = SenderAccount.objects.create(email="b@example.com", app_password="pw", is_active=True)

        def fake_scan(sender, **kwargs):
            if sender.email == sender_a.email:
                return {
                    "account": sender.email,
                    "ok": True,
                    "counts": {"reply": 2, "bounce": 0, "blocked": 0, "notice": 0},
                    "messages": [
                        {"key": "a-older", "date_ts": 100, "status": "reply", "account": sender.email, "subject": "A older"},
                        {"key": "a-newer", "date_ts": 300, "status": "reply", "account": sender.email, "subject": "A newer"},
                    ],
                    "latest_key": "a-newer",
                }
            return {
                "account": sender.email,
                "ok": True,
                "counts": {"reply": 2, "bounce": 0, "blocked": 0, "notice": 0},
                "messages": [
                    {"key": "b-older", "date_ts": 200, "status": "reply", "account": sender.email, "subject": "B older"},
                    {"key": "b-newer", "date_ts": 400, "status": "reply", "account": sender.email, "subject": "B newer"},
                ],
                "latest_key": "b-newer",
            }

        with mock.patch("core.services.inbox_monitor_service._scan_account", side_effect=fake_scan):
            from core.services.inbox_monitor_service import scan_inbox_monitor

            result = scan_inbox_monitor(max_messages=2)

        self.assertEqual([message["key"] for message in result["messages"]], ["b-newer", "a-newer"])
        self.assertEqual(result["latest_key"], "b-newer")
        self.assertEqual(result["requested_max_messages"], 2)
        self.assertEqual(result["available_message_count"], 4)
        self.assertEqual(result["returned_message_count"], 2)


class TestEmailDeliveryDashboardTests(TestCase):
    def test_delivery_creates_missing_test_recipient_before_sending(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        job = JobPosting.objects.create(
            daily_batch=batch,
            title="Data Engineer",
            company="Acme",
            location="Remote",
            description="Build data pipelines.",
            normalized_company="acme",
            normalized_title="data engineer",
            normalized_location="remote",
            dedupe_key="acme|data engineer|remote",
            sort_company="acme",
            sort_title="data engineer",
            sort_location="remote",
            linkedin_url="https://www.linkedin.com/jobs/view/123/",
        )
        GeneratedEmail.objects.create(
            job_posting=job,
            subject="Data Engineer role",
            body="I am interested in this role.",
        )
        SenderAccount.objects.create(
            email="sender@example.com",
            app_password="pw",
            is_active=True,
            is_paused=False,
        )

        from core.services.test_email_delivery_service import run_test_email_delivery_for_job

        with mock.patch("core.services.test_email_delivery_service.is_email_sending_enabled", return_value=True), \
            mock.patch("core.services.test_email_delivery_service._resume_attachments", return_value=[]), \
            mock.patch("core.services.test_email_delivery_service.send_via_smtp"):
            result = run_test_email_delivery_for_job(
                job_id=job.id,
                delay_seconds=0,
                test_recipient_emails=["newtest@example.com"],
                use_openai_email=False,
            )

        self.assertTrue(TestEmailAccount.objects.filter(email="newtest@example.com", is_active=True).exists())
        self.assertEqual(result["totals"]["created_test_recipients"], ["newtest@example.com"])
        self.assertEqual(result["totals"]["test_recipients"], 1)
        self.assertEqual(result["totals"]["emails_sent"], 1)

    def test_delivery_form_values_persist_after_redirect(self):
        payload = {
            "job_id": "123",
            "send_mode": "one_sender",
            "sender_email": "sender@example.com",
            "recipient_emails": "newtest@example.com",
            "delay_seconds": "7",
            "use_openai_email": "on",
            "prefix_subject_with_test_tag": "on",
        }
        result = {
            "totals": {
                "send_run_id": 1,
                "job_id": 123,
                "senders": 1,
                "test_recipients": 1,
                "created_test_recipients": ["newtest@example.com"],
                "emails_attempted": 1,
                "emails_sent": 1,
                "emails_failed": 0,
                "run_log_path": "run.log",
            }
        }

        with mock.patch("core.views.run_test_email_delivery_for_job", return_value=result):
            response = self.client.post(reverse("run_test_email_delivery_view"), payload)

        self.assertEqual(response.status_code, 302)

        page = self.client.get(reverse("operations_dashboard"))
        self.assertContains(page, 'value="123"')
        self.assertContains(page, 'value="sender@example.com"')
        self.assertContains(page, "newtest@example.com")
        self.assertContains(page, 'value="7"')


class LinkedInJobIdMapperViewTests(TestCase):
    def test_linkedin_job_id_mapper_page_loads(self):
        response = self.client.get(reverse("linkedin_job_id_mapper"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LinkedIn Job ID Mapper")
        self.assertContains(response, "Build Job IDs")

    def test_linkedin_job_id_mapper_prefills_latest_batch_urls(self):
        older_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        latest_batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company_old = Company.objects.create(raw_name_latest="Older Co", normalized_name="older co", active_domain="older.com")
        company_new = Company.objects.create(raw_name_latest="Latest Co", normalized_name="latest co", active_domain="latest.com")

        JobPosting.objects.create(
            daily_batch=older_batch,
            company_ref=company_old,
            linkedin_url="https://www.linkedin.com/jobs/view/111/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/111/",
            title="Older Role",
            company=company_old.raw_name_latest,
            description="Older description",
            normalized_company=company_old.normalized_name,
            normalized_title="older role",
            normalized_location="remote",
            dedupe_key="older:role",
            sort_company=company_old.normalized_name,
            sort_title="older role",
            sort_location="remote",
        )
        JobPosting.objects.create(
            daily_batch=latest_batch,
            company_ref=company_new,
            linkedin_url="https://www.linkedin.com/jobs/view/222/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/222/",
            title="Latest Role",
            company=company_new.raw_name_latest,
            description="Latest description",
            normalized_company=company_new.normalized_name,
            normalized_title="latest role",
            normalized_location="remote",
            dedupe_key="latest:role",
            sort_company=company_new.normalized_name,
            sort_title="latest role",
            sort_location="remote",
        )

        response = self.client.get(reverse("linkedin_job_id_mapper"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://www.linkedin.com/jobs/view/222/")
        self.assertNotContains(response, "https://www.linkedin.com/jobs/view/111/")
        self.assertContains(response, latest_batch.batch_date.isoformat())

    def test_pipeline_dashboard_shows_and_saves_manual_job_reference_ids(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Latest Co", normalized_name="latest co", active_domain="latest.com")
        job = JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company,
            is_manual_import=True,
            source_platform=JobPosting.SourcePlatform.LINKEDIN,
            external_job_id="4229164734",
            linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/4229164734/",
            title="Latest Role",
            company=company.raw_name_latest,
            description="Latest description",
            normalized_company=company.normalized_name,
            normalized_title="latest role",
            normalized_location="remote",
            dedupe_key="latest:role",
            sort_company=company.normalized_name,
            sort_title="latest role",
            sort_location="remote",
        )

        response = self.client.get(reverse("pipeline_dashboard"), {"batch_date": batch.batch_date.isoformat()})
        self.assertContains(response, "Step 1B: Add Website Job IDs")
        self.assertContains(response, f"manual_job_reference_id_{job.id}")
        self.assertContains(response, "https://www.linkedin.com/jobs/view/4229164734/")

        response = self.client.post(
            reverse("pipeline_update_manual_job_ids_view"),
            {
                "batch_date": batch.batch_date.isoformat(),
                f"manual_job_reference_id_{job.id}": "REQ-2026-1042",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.manual_job_reference_id, "REQ-2026-1042")
        self.assertEqual(job.external_job_id, "4229164734")

    def test_pipeline_manual_job_reference_save_skips_duplicate_ids(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company_one = Company.objects.create(raw_name_latest="Arm", normalized_name="arm", active_domain="arm.com")
        company_two = Company.objects.create(raw_name_latest="Ascot", normalized_name="ascot", active_domain="ascot.com")
        job_one = JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company_one,
            is_manual_import=True,
            source_platform=JobPosting.SourcePlatform.LINKEDIN,
            external_job_id="111",
            manual_job_reference_id="2025-13762",
            linkedin_url="https://www.linkedin.com/jobs/view/111/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/111/",
            title="Arm Role",
            company=company_one.raw_name_latest,
            description="Description",
            normalized_company=company_one.normalized_name,
            normalized_title="arm role",
            normalized_location="remote",
            dedupe_key="arm:role",
            sort_company=company_one.normalized_name,
            sort_title="arm role",
            sort_location="remote",
        )
        job_two = JobPosting.objects.create(
            daily_batch=batch,
            company_ref=company_two,
            is_manual_import=True,
            source_platform=JobPosting.SourcePlatform.LINKEDIN,
            external_job_id="222",
            linkedin_url="https://www.linkedin.com/jobs/view/222/",
            normalized_linkedin_url="https://www.linkedin.com/jobs/view/222/",
            title="Ascot Role",
            company=company_two.raw_name_latest,
            description="Description",
            normalized_company=company_two.normalized_name,
            normalized_title="ascot role",
            normalized_location="remote",
            dedupe_key="ascot:role",
            sort_company=company_two.normalized_name,
            sort_title="ascot role",
            sort_location="remote",
        )

        response = self.client.post(
            reverse("pipeline_update_manual_job_ids_view"),
            {
                "batch_date": batch.batch_date.isoformat(),
                f"manual_job_reference_id_{job_one.id}": "2025-13762",
                f"manual_job_reference_id_{job_two.id}": "2025-13762",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        job_one.refresh_from_db()
        job_two.refresh_from_db()
        self.assertEqual(job_one.manual_job_reference_id, "2025-13762")
        self.assertEqual(job_two.manual_job_reference_id, "")


class FollowupDashboardTests(TestCase):
    def _create_sent_initial(self, *, company, email, person_name, days_ago=4, title="Data Science Manager"):
        batch, _ = DailyBatch.objects.get_or_create(batch_date=timezone.localdate() - timedelta(days=days_ago))
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name=person_name,
            email=email,
            source=CompanyRecruiter.Source.APOLLO,
            email_status="verified",
            apollo_title=title,
            title_match=True,
            is_active=True,
        )
        target = JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot=email,
            recipient_name_snapshot=person_name,
            selection_order=1,
            is_selected_for_job=True,
            is_verified_for_job=True,
            is_sent_real=True,
        )
        sender = SenderAccount.objects.create(
            email=f"{email.split('@', 1)[0]}.sender@example.com",
            app_password="pw",
            is_active=True,
        )
        run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(days=days_ago),
            finished_at=timezone.now() - timedelta(days=days_ago),
        )
        return SentEmailLog.objects.create(
            send_run=run,
            job_posting=job,
            job_recruiter_target=target,
            sender_account=sender,
            to_email=email,
            subject_snapshot="Initial",
            body_snapshot="Initial body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=timezone.now() - timedelta(days=days_ago),
        )

    def test_followup_dashboard_groups_due_people_by_company(self):
        company_a = Company.objects.create(raw_name_latest="Alpha", normalized_name="alpha", active_domain="alpha.com")
        company_b = Company.objects.create(raw_name_latest="Beta", normalized_name="beta", active_domain="beta.com")
        self._create_sent_initial(company=company_a, email="a1@alpha.com", person_name="Alpha One")
        self._create_sent_initial(company=company_a, email="a2@alpha.com", person_name="Alpha Two")
        self._create_sent_initial(company=company_b, email="b1@beta.com", person_name="Beta One")

        with mock.patch.dict(os.environ, {"FOLLOWUP_MIN_DAYS_SINCE_LAST": "3"}, clear=False):
            context = build_followup_dashboard_context()

        due_by_company = {row["company_name"]: row["due_count"] for row in context["rows"]}
        self.assertEqual(due_by_company, {"alpha": 2, "beta": 1})
        self.assertEqual(context["totals"]["companies_due"], 2)
        self.assertEqual(context["totals"]["people_due"], 3)
        self.assertIn("Dear Alpha", context["preview_body"])
        self.assertNotIn("attached my resume", context["preview_body"].lower())

    def test_followup_dashboard_excludes_contacts_that_replied(self):
        company = Company.objects.create(raw_name_latest="Reply Co", normalized_name="reply co", active_domain="reply.co")
        log = self._create_sent_initial(company=company, email="reply@reply.co", person_name="Reply Person")

        InboxScanEvent.objects.create(
            sender_account=log.sender_account,
            message_key="reply-1",
            from_header="Reply Person <reply@reply.co>",
            subject="Re: Initial",
            message_date=timezone.now(),
            classification=InboxScanEvent.Classification.REPLY,
            detected_email="reply@reply.co",
            matched_sent_log=log,
        )

        context = build_followup_dashboard_context()

        self.assertEqual(context["totals"]["people_due"], 0)
        self.assertEqual(context["totals"]["replied"], 1)
        self.assertEqual(context["rows"], [])

    def test_company_followup_send_uses_selected_counts(self):
        SenderAccount.objects.all().delete()
        SenderAccount.objects.create(email="sender@example.com", app_password="pw", is_active=True)
        company_a = Company.objects.create(raw_name_latest="Alpha", normalized_name="alpha", active_domain="alpha.com")
        company_b = Company.objects.create(raw_name_latest="Beta", normalized_name="beta", active_domain="beta.com")
        self._create_sent_initial(company=company_a, email="a1@alpha.com", person_name="Alpha One")
        self._create_sent_initial(company=company_a, email="a2@alpha.com", person_name="Alpha Two")
        self._create_sent_initial(company=company_b, email="b1@beta.com", person_name="Beta One")

        context = build_followup_dashboard_context()
        post_data = {"max_total_followups": "2"}
        for row in context["rows"]:
            post_data[f"followup_count__{row['key']}"] = "2" if row["company_name"] == "alpha" else "1"
        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "FOLLOWUP_MIN_DAYS_SINCE_LAST": "3",
            "FOLLOWUP_MAX_PER_PERSON": "1",
            "SENDER_MIN_GAP_SECONDS": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.followup_dashboard_service.send_via_smtp"
        ) as smtp_mock:
            result = run_company_followups_from_dashboard(post_data=post_data, delay_seconds=0)

        self.assertEqual(result["totals"]["selected"], 2)
        self.assertEqual(result["totals"]["emails_sent"], 2)
        self.assertEqual(smtp_mock.call_count, 2)
        followups = SentEmailLog.objects.filter(message_type=SentEmailLog.MessageType.FOLLOW_UP).order_by("id")
        self.assertEqual(followups.count(), 2)
        self.assertNotIn("attached my resume", followups[0].body_snapshot.lower())
        self.assertEqual(set(followups.values_list("to_email", flat=True)), {"a1@alpha.com", "b1@beta.com"})


class LinkedInPostOutreachTests(TestCase):
    def test_linkedin_post_outreach_prepares_editable_missing_fields(self):
        preview = {
            "source_url": "https://www.linkedin.com/posts/example",
            "canonical_url": "https://www.linkedin.com/posts/example",
            "poster_name": "Irene Poster",
            "poster_linkedin_url": "https://www.linkedin.com/in/irene-poster",
            "company_name": "Acme AI",
            "company_linkedin_url": "https://www.linkedin.com/company/acme-ai",
            "post_text": "We are hiring a Senior Data Scientist for our AI platform team.",
            "shared_post_text": "",
            "job_title": "Senior Data Scientist",
            "job_company": "Acme AI",
            "job_location": "Remote",
            "job_url": "https://www.linkedin.com/jobs/view/123/",
            "raw_text_preview": "",
        }

        with mock.patch("core.services.linkedin_post_outreach_service.preview_linkedin_post", return_value=preview):
            result = run_linkedin_post_outreach(
                raw_urls_text="https://www.linkedin.com/posts/example",
                find_emails=False,
                ai_extract_details=False,
                create_review_batch=False,
            )

        self.assertEqual(result["totals"]["extracted_posts"], 1)
        self.assertEqual(result["totals"]["needs_manual_input"], 1)
        row = result["rows"][0]
        self.assertFalse(row["ready_for_review"])
        self.assertIn("email", row["missing_fields"])
        self.assertEqual(row["company"], "Acme AI")
        self.assertEqual(row["role"], "Senior Data Scientist")
        self.assertIn("We are hiring", row["post_text"])

    def test_linkedin_post_outreach_uses_chatgpt_extraction_before_apollo(self):
        preview = {
            "source_url": "https://www.linkedin.com/posts/example",
            "canonical_url": "https://www.linkedin.com/posts/example",
            "poster_name": "",
            "poster_linkedin_url": "https://www.linkedin.com/in/irene-poster",
            "company_name": "",
            "company_linkedin_url": "",
            "post_text": "Hiring a Data Scientist at Acme AI. Email irene@example.com.",
            "shared_post_text": "",
            "job_title": "",
            "job_company": "",
            "job_location": "",
            "job_url": "",
            "raw_text_preview": "",
        }
        ai_result = {
            "status": "ok",
            "model": "gpt-test",
            "poster_name": "Irene Poster",
            "poster_email": "irene@example.com",
            "company_name": "Acme AI",
            "role_title": "Data Scientist",
            "location": "Remote",
            "job_url": "",
            "company_website": "",
            "confidence": "high",
            "notes": "email listed in post",
        }

        with mock.patch("core.services.linkedin_post_outreach_service.preview_linkedin_post", return_value=preview), mock.patch(
            "core.services.linkedin_post_outreach_service.openai_linkedin_post_extraction_configured",
            return_value=True,
        ), mock.patch(
            "core.services.linkedin_post_outreach_service.extract_linkedin_post_details_with_openai",
            return_value=ai_result,
        ), mock.patch("core.services.linkedin_post_outreach_service.match_person_email_from_apollo") as apollo_mock:
            result = run_linkedin_post_outreach(
                raw_urls_text="https://www.linkedin.com/posts/example",
                find_emails=True,
                ai_extract_details=True,
                create_review_batch=False,
            )

        apollo_mock.assert_not_called()
        row = result["rows"][0]
        self.assertTrue(row["ready_for_review"])
        self.assertEqual(row["poster_name"], "Irene Poster")
        self.assertEqual(row["email"], "irene@example.com")
        self.assertEqual(row["company"], "Acme AI")
        self.assertEqual(row["role"], "Data Scientist")
        self.assertEqual(row["ai_status"], "ok")
        self.assertEqual(result["totals"]["chatgpt_extracted"], 1)

    def test_linkedin_post_outreach_uses_apollo_linkedin_url_without_company(self):
        preview = {
            "source_url": "https://www.linkedin.com/posts/example",
            "canonical_url": "https://www.linkedin.com/posts/example",
            "poster_name": "Irene Poster",
            "poster_linkedin_url": "https://www.linkedin.com/in/irene-poster",
            "company_name": "",
            "company_linkedin_url": "",
            "post_text": "I am hiring for a data role. Message me if interested.",
            "shared_post_text": "",
            "job_title": "Data Scientist",
            "job_company": "",
            "job_location": "",
            "job_url": "",
            "raw_text_preview": "",
        }
        apollo_payload = {
            "credits_consumed": 1,
            "person": {
                "email": "irene@example.com",
                "email_status": "verified",
                "linkedin_url": "https://www.linkedin.com/in/irene-poster",
                "title": "Recruiter",
            },
        }

        with mock.patch("core.services.linkedin_post_outreach_service.preview_linkedin_post", return_value=preview), mock.patch(
            "core.services.linkedin_post_outreach_service.match_person_email_from_apollo_linkedin_url",
            return_value=apollo_payload,
        ) as linkedin_match, mock.patch(
            "core.services.linkedin_post_outreach_service.match_person_email_from_apollo"
        ) as name_company_match:
            result = run_linkedin_post_outreach(
                raw_urls_text="https://www.linkedin.com/posts/example",
                find_emails=True,
                ai_extract_details=False,
                create_review_batch=False,
            )

        linkedin_match.assert_called_once_with(linkedin_url="https://www.linkedin.com/in/irene-poster")
        name_company_match.assert_not_called()
        row = result["rows"][0]
        self.assertEqual(row["email"], "irene@example.com")
        self.assertEqual(row["email_status"], "verified")
        self.assertEqual(row["apollo_lookup_type"], "linkedin_url")
        self.assertEqual(row["apollo_credits"], 1)
        self.assertEqual(result["totals"]["apollo_attempts"], 1)
        self.assertEqual(result["totals"]["apollo_credits"], 1)

    def test_linkedin_post_review_batch_creates_jobs_without_generating_drafts(self):
        rows = [
            {
                "include": True,
                "poster_name": "Irene Poster",
                "email": "irene@example.com",
                "company": "Acme AI",
                "role": "Senior Data Scientist",
                "location": "Remote",
                "canonical_url": "https://www.linkedin.com/posts/example",
                "poster_linkedin_url": "https://www.linkedin.com/in/irene-poster",
                "job_url": "https://www.linkedin.com/jobs/view/123/",
                "post_text": "We are hiring for model evaluation and data product work.",
                "manual_notes": "Mention production ML and analytics pipelines.",
            }
        ]

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
        ) as generation_mock:
            result = create_linkedin_post_review_batch_from_rows(rows)

        generation_mock.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["totals"]["created_jobs"], 1)
        self.assertEqual(result["totals"]["generated"], 0)
        job = JobPosting.objects.get(id=result["rows"][0]["job_id"])
        self.assertTrue(job.description.startswith("Role: Senior Data Scientist\nCompany: Acme AI"))
        self.assertIn("Manual notes:\nMention production ML", job.description)
        self.assertIn("LinkedIn post URL: https://www.linkedin.com/posts/example", job.description)
        self.assertEqual(job.title, "Senior Data Scientist")
        self.assertFalse(GeneratedEmail.objects.filter(job_posting=job).exists())
        self.assertEqual(result["rows"][0]["status"], "draft_not_generated")

    def test_linkedin_post_page_renders_editable_review_form(self):
        rows = prepare_linkedin_post_rows_for_review(
            [
                {
                    "row_number": 1,
                    "status": "extracted",
                    "url": "https://www.linkedin.com/posts/example",
                    "canonical_url": "https://www.linkedin.com/posts/example",
                    "poster_name": "Irene Poster",
                    "email": "",
                    "company": "Acme AI",
                    "role": "Senior Data Scientist",
                    "post_text": "Hiring a Senior Data Scientist.",
                    "apollo_status": "not_requested",
                }
            ]
        )
        fake_result = {
            "ok": True,
            "totals": {
                "input_urls": 1,
                "extracted_posts": 1,
                "emails_found": 0,
                "apollo_credits": 0,
                "extract_errors": 0,
                "ready_for_review": 0,
                "needs_manual_input": 1,
            },
            "rows": rows,
            "invalid_rows": [],
            "review_batch": None,
        }

        with mock.patch("core.views.run_linkedin_post_outreach", return_value=fake_result):
            response = self.client.post(
                reverse("linkedin_post_preview"),
                {
                    "action": "extract",
                    "linkedin_post_urls": "https://www.linkedin.com/posts/example",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="action" value="create_review_from_rows"')
        self.assertContains(response, 'name="email_0"')
        self.assertContains(response, 'name="post_text_0"')
        self.assertContains(response, "Create LinkedIn Post Review Batch")

    def test_linkedin_post_extract_does_not_auto_create_review_batch(self):
        fake_result = {
            "ok": True,
            "totals": {
                "input_urls": 1,
                "extracted_posts": 1,
                "emails_found": 1,
                "apollo_credits": 0,
                "extract_errors": 0,
                "ready_for_review": 1,
                "needs_manual_input": 0,
            },
            "rows": [],
            "invalid_rows": [],
            "review_batch": None,
        }

        with mock.patch("core.views.run_linkedin_post_outreach", return_value=fake_result) as outreach_mock:
            response = self.client.post(
                reverse("linkedin_post_preview"),
                {
                    "action": "extract",
                    "linkedin_post_urls": "https://www.linkedin.com/posts/example",
                    "find_emails": "on",
                    "use_chatgpt_extraction": "on",
                    "create_review_batch": "on",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(outreach_mock.call_args.kwargs["create_review_batch"])

    def test_linkedin_post_create_review_redirects_without_generating_drafts(self):
        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
        ) as generation_mock:
            response = self.client.post(
                reverse("linkedin_post_preview"),
                {
                    "action": "create_review_from_rows",
                    "row_count": "1",
                    "include_0": "on",
                    "url_0": "https://www.linkedin.com/posts/example",
                    "canonical_url_0": "https://www.linkedin.com/posts/example",
                    "poster_name_0": "Irene Poster",
                    "poster_linkedin_url_0": "https://www.linkedin.com/in/irene-poster",
                    "company_0": "Acme AI",
                    "role_0": "Senior Data Scientist",
                    "email_0": "irene@example.com",
                    "post_text_0": "Hiring a Senior Data Scientist.",
                },
                HTTP_HOST="127.0.0.1:8000",
            )

        generation_mock.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/manual-bulk-email/job-review/", response["Location"])
        self.assertEqual(JobPosting.objects.filter(is_manual_email_job=True).count(), 1)
        self.assertEqual(GeneratedEmail.objects.count(), 0)

    def test_linkedin_post_manual_review_shows_source_links(self):
        result = create_linkedin_post_review_batch_from_rows(
            [
                {
                    "include": True,
                    "poster_name": "Irene Poster",
                    "email": "irene@example.com",
                    "company": "Acme AI",
                    "role": "Senior Data Scientist",
                    "canonical_url": "https://www.linkedin.com/posts/example",
                    "poster_linkedin_url": "https://www.linkedin.com/in/irene-poster",
                    "job_url": "https://www.linkedin.com/jobs/view/123/",
                    "post_text": "Hiring a Senior Data Scientist. Please apply if you work with forecasting.",
                }
            ]
        )

        response = self.client.get(reverse("manual_job_email_review", args=[result["token"]]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LinkedIn post")
        self.assertContains(response, 'href="https://www.linkedin.com/posts/example"')
        self.assertContains(response, "Poster profile")
        self.assertContains(response, 'href="https://www.linkedin.com/in/irene-poster"')
        self.assertContains(response, "Job link")
        self.assertContains(response, 'href="https://www.linkedin.com/jobs/view/123/"')
        self.assertContains(response, "Post Text")
        self.assertContains(response, '<div class="source-text">')
        self.assertContains(response, "Hiring a Senior Data Scientist. Please apply if you work with forecasting.")
        self.assertContains(response, "Update Recipient")
        self.assertContains(response, 'name="recipient_name"')
        self.assertContains(response, 'name="recipient_email"')
        self.assertContains(response, 'id="manual-send-form"')
        self.assertContains(response, 'form="manual-send-form" type="checkbox"')

    def test_manual_job_email_recipient_update_changes_target_and_reviewer_context(self):
        result = create_linkedin_post_review_batch_from_rows(
            [
                {
                    "include": True,
                    "poster_name": "Irene Poster",
                    "email": "irene@example.com",
                    "company": "Acme AI",
                    "role": "Senior Data Scientist",
                    "post_text": "Hiring a Senior Data Scientist.",
                }
            ]
        )
        job_id = result["rows"][0]["job_id"]

        update_result = update_manual_job_email_recipient(
            token=result["token"],
            job_id=job_id,
            name="Correct Person",
            email="correct@example.com",
        )
        context = build_manual_job_email_review_context(token=result["token"])
        target = JobRecruiterTarget.objects.get(job_posting_id=job_id)

        self.assertTrue(update_result["ok"])
        self.assertEqual(target.recipient_name_snapshot, "Correct Person")
        self.assertEqual(target.recipient_email_snapshot, "correct@example.com")
        self.assertEqual(target.company_recruiter.person_name, "Correct Person")
        self.assertEqual(target.company_recruiter.email, "correct@example.com")
        self.assertEqual(context["rows"][0]["name"], "Correct Person")
        self.assertEqual(context["rows"][0]["email"], "correct@example.com")

    def test_manual_job_email_recipient_update_view_redirects_to_review(self):
        result = create_linkedin_post_review_batch_from_rows(
            [
                {
                    "include": True,
                    "poster_name": "Irene Poster",
                    "email": "irene@example.com",
                    "company": "Acme AI",
                    "role": "Senior Data Scientist",
                    "post_text": "Hiring a Senior Data Scientist.",
                }
            ]
        )

        response = self.client.post(
            reverse("manual_job_email_recipient_update", args=[result["token"]]),
            {
                "job_id": result["rows"][0]["job_id"],
                "recipient_name": "Correct Person",
                "recipient_email": "correct@example.com",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("manual_job_email_review", args=[result["token"]]))
        self.assertTrue(JobRecruiterTarget.objects.filter(recipient_email_snapshot="correct@example.com").exists())

    def test_linkedin_post_review_batch_skips_prior_real_initial_recipient(self):
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        prior_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        prior_job = _create_job_with_email(batch=prior_batch)
        prior_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior global send",
        )
        SentEmailLog.objects.create(
            send_run=prior_run,
            job_posting=prior_job,
            sender_account=sender,
            to_email="irene@example.com",
            subject_snapshot="Prior",
            body_snapshot="Prior body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=timezone.now(),
        )

        result = create_linkedin_post_review_batch_from_rows(
            [
                {
                    "include": True,
                    "poster_name": "Irene Poster",
                    "email": "irene@example.com",
                    "company": "Acme AI",
                    "role": "Senior Data Scientist",
                    "post_text": "Hiring a Senior Data Scientist.",
                }
            ]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["totals"]["valid_unique_rows"], 0)
        self.assertEqual(result["invalid_rows"][0]["reason"], "already_sent_or_pending_real_initial")
        self.assertEqual(JobPosting.objects.filter(is_manual_email_job=True).count(), 0)


class SendControlSendOnlyTests(TestCase):
    def test_send_only_preverifies_and_skips_invalid_before_send_log(self):
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme preverify", active_domain="acme.com")
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Invalid Person",
            email="invalid@example.com",
            source=CompanyRecruiter.Source.LEGACY,
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="invalid@example.com",
            recipient_name_snapshot="Invalid Person",
            selection_order=1,
            is_selected_for_job=True,
        )
        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "EMAIL_VERIFIER_ENFORCE": "1",
            "EMAILVERIFIER_ACTIVE_KEY": "test-key",
            "EMAIL_VERIFIER_API_URL": "https://verifier.example/verify",
            "SEND_ATTACH_RESUME": "0",
        }
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "invalid", "is_valid": False}

        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.email_verification_service.requests.post",
            return_value=response,
        ), mock.patch("core.services.send_run_service.send_via_smtp") as smtp_mock:
            result = run_send_initial_for_batch(
                batch_date_str=batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
                skip_pending_recipients=True,
                source_label="Send control send-only",
            )

        smtp_mock.assert_not_called()
        self.assertEqual(result["totals"]["emails_attempted"], 0)
        self.assertEqual(result["totals"]["emails_failed"], 0)
        self.assertEqual(result["totals"]["emails_skipped_suppressed"], 1)
        self.assertFalse(SentEmailLog.objects.filter(to_email="invalid@example.com").exists())

    def test_send_only_mode_does_not_call_apollo_when_no_recipients_exist(self):
        job = _create_job_with_email()

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "APOLLO_API_KEY": "fake-key",
            "AUTO_APOLLO_FETCH_ON_SEND": "1",
            "SEND_ATTACH_RESUME": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.send_run_service.upsert_company_recruiters_from_apollo"
        ) as apollo_mock, mock.patch("core.services.send_run_service.sync_job_targets_for_job") as sync_mock:
            result = run_send_initial_for_batch(
                batch_date_str=job.daily_batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
                skip_pending_recipients=True,
                source_label="Send control send-only",
            )

        apollo_mock.assert_not_called()
        sync_mock.assert_not_called()
        self.assertEqual(result["totals"]["emails_attempted"], 0)
        self.assertEqual(result["totals"]["jobs_skipped_no_recipients"], 1)
        self.assertEqual(SentEmailLog.objects.count(), 0)
        self.assertIn("Send control send-only", SendRun.objects.latest("id").notes)

    def test_manual_job_email_flow_blocks_prior_real_initial_recipient(self):
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        prior_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        prior_job = _create_job_with_email(batch=prior_batch)
        prior_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior global send",
        )
        SentEmailLog.objects.create(
            send_run=prior_run,
            job_posting=prior_job,
            sender_account=sender,
            to_email="repeat@example.com",
            subject_snapshot="Prior",
            body_snapshot="Prior body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=timezone.now(),
        )

        def fake_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Manual Role",
                body="Manual body. Please see my attached Resume.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
            )
            return {"generated": 1, "error": ""}

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "SEND_ATTACH_RESUME": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=fake_generation,
        ), mock.patch("core.services.manual_job_email_service.send_via_smtp") as smtp_mock:
            batch_result = create_manual_job_email_batch(
                names=["Repeat Person"],
                emails=["repeat@example.com"],
                job_texts=["Hiring: Manual Duplicate Bypass"],
            )
            send_result = send_manual_job_email_batch(
                token=batch_result["token"],
                job_ids=[batch_result["rows"][0]["job_id"]],
                delay_seconds=0,
            )

        smtp_mock.assert_not_called()
        self.assertEqual(batch_result["totals"]["created_jobs"], 1)
        self.assertEqual(send_result["totals"]["emails_sent"], 0)
        self.assertEqual(send_result["totals"]["emails_attempted"], 0)
        self.assertEqual(send_result["totals"]["skipped_already_sent_or_pending"], 1)
        self.assertEqual(SentEmailLog.objects.filter(to_email="repeat@example.com", status=SentEmailLog.SendStatus.SENT).count(), 1)

    def test_manual_job_review_repairs_generated_hiring_subjects(self):
        def fake_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Gayathri - We're hiring a Staff Backend Engineer on the Media Experiences team!",
                body="Manual body.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
            )
            return {"generated": 1, "error": ""}

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=fake_generation,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene"],
                emails=["irene@example.com"],
                job_texts=[
                    "We're hiring a Staff Backend Engineer on the Media Experiences team! "
                    "If you've dabbled in scaling media infrastructure, I'd love to chat."
                ],
            )

        context = build_manual_job_email_review_context(token=batch_result["token"])
        generated = GeneratedEmail.objects.get(job_posting_id=batch_result["rows"][0]["job_id"])

        self.assertEqual(context["rows"][0]["subject"], "Staff Backend Engineer role")
        self.assertEqual(generated.subject, "Staff Backend Engineer role")

    def test_manual_job_review_uses_role_line_when_paste_starts_with_page_chrome(self):
        def fake_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Gayathri - Company Logo",
                body="Manual body.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
            )
            return {"generated": 1, "error": ""}

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=fake_generation,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene"],
                emails=["irene@example.com"],
                job_texts=[
                    "Company Logo\n-\njd banner\nAnalyst - TO Analytics\n"
                    "Location Chicago, Illinois, United States This job is associated with 2 categories"
                ],
            )

        context = build_manual_job_email_review_context(token=batch_result["token"])
        job = JobPosting.objects.get(id=batch_result["rows"][0]["job_id"])
        generated = GeneratedEmail.objects.get(job_posting=job)

        self.assertEqual(job.title, "Analyst - TO Analytics")
        self.assertEqual(context["rows"][0]["subject"], "TO Analytics Analyst role")
        self.assertEqual(generated.subject, "TO Analytics Analyst role")

    def test_manual_job_review_preserves_manually_edited_subjects(self):
        def fake_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Custom subject from admin",
                body="Manual body.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
                edited_manually=True,
            )
            return {"generated": 1, "error": ""}

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=fake_generation,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene"],
                emails=["irene@example.com"],
                job_texts=["We're #hiring a Research Scientist - RL Training Snorkel AI."],
            )

        context = build_manual_job_email_review_context(token=batch_result["token"])
        generated = GeneratedEmail.objects.get(job_posting_id=batch_result["rows"][0]["job_id"])

        self.assertEqual(context["rows"][0]["subject"], "Custom subject from admin")
        self.assertEqual(generated.subject, "Custom subject from admin")

    def test_manual_job_review_page_has_bulk_draft_generation_buttons(self):
        def fake_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Manual Role",
                body="Manual body.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
            )
            return {"generated": 1, "error": "", "provider": "openai", "model": "gpt-5.4"}

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=fake_generation,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene"],
                emails=["irene@example.com"],
                job_texts=["Hiring: Data Analyst"],
            )

        response = self.client.get(reverse("manual_job_email_review", args=[batch_result["token"]]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Generate Missing Drafts")
        self.assertContains(response, "Regenerate All Drafts")
        self.assertContains(response, reverse("manual_job_email_generate", args=[batch_result["token"]]))

    def test_manual_job_bulk_generation_missing_only_skips_existing_drafts(self):
        def initial_generation(*, job, run_log_path):
            GeneratedEmail.objects.create(
                job_posting=job,
                subject="Manual Role",
                body="Manual body.",
                generation_status=GeneratedEmail.GenerationStatus.GENERATED,
            )
            return {"generated": 1, "error": "", "provider": "openai", "model": "gpt-5.4"}

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            side_effect=initial_generation,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene", "Sam"],
                emails=["irene@example.com", "sam@example.com"],
                job_texts=["Hiring: Data Analyst", "Hiring: Data Engineer"],
            )

        missing_job_id = batch_result["rows"][1]["job_id"]
        GeneratedEmail.objects.filter(job_posting_id=missing_job_id).delete()

        with mock.patch(
            "core.services.manual_job_email_service.run_cold_email_generation_for_job",
            return_value={"generated": 1, "error": "", "provider": "openai", "model": "gpt-5.4"},
        ) as generate_mock:
            result = run_manual_job_email_generation_for_token(token=batch_result["token"], skip_existing=True)

        self.assertEqual(result["totals"]["jobs_found"], 2)
        self.assertEqual(result["totals"]["skipped_existing"], 1)
        self.assertEqual(result["totals"]["generated"], 1)
        generate_mock.assert_called_once()
        self.assertEqual(generate_mock.call_args.kwargs["job"].id, missing_job_id)

    def test_manual_job_review_uses_dedicated_prompt_and_omits_placeholder_company(self):
        captured = {}

        def fake_gpt(**kwargs):
            captured.update(kwargs)
            return {
                "subject": "Application for Business Analyst Role",
                "email": "I'm excited to apply for the Business Analyst position at Manual Job Email placeholder #1.",
                "prompt_version": "manual_review:test",
            }

        with mock.patch(
            "core.services.manual_job_email_service.generate_cold_email_with_provider_custom_prompt",
            side_effect=fake_gpt,
        ):
            batch_result = create_manual_job_email_batch(
                names=["Irene"],
                emails=["irene@example.com"],
                job_texts=["Hiring: Business Analyst - Data Reporting"],
            )

        self.assertEqual(batch_result["totals"]["generated"], 1)
        self.assertIn("manual job review flow", captured["prompt_text"].lower())
        self.assertEqual(captured["provider"], "openai")
        self.assertEqual(captured["company_name"], "")
        self.assertNotIn("Manual Job Email", captured["company_context"])
        self.assertIn("Role title:", captured["company_context"])
        self.assertIn("Business Analyst", captured["job_title"])
        generated = GeneratedEmail.objects.get(job_posting_id=batch_result["rows"][0]["job_id"])
        context = build_manual_job_email_review_context(token=batch_result["token"])
        self.assertNotIn("Manual Job Email", generated.body)
        self.assertNotIn("Manual Job Email", context["rows"][0]["final_body"])
        self.assertNotIn("Kindly find the job posting here for reference", context["rows"][0]["final_body"])
        self.assertNotIn("manual.local", context["rows"][0]["final_body"])

    def test_send_control_worker_disables_discovery_and_skips_pending_recipients(self):
        with mock.patch("core.services.send_control_dashboard_service.run_send_initial_for_batch") as send_mock:
            from core.services.send_control_dashboard_service import _run_send_worker

            _run_send_worker("2026-04-21")

        send_mock.assert_called_once_with(
            batch_date_str="2026-04-21",
            send_type="real",
            allow_recipient_discovery=False,
            skip_pending_recipients=True,
            source_label="Send control send-only",
        )

    def test_send_control_sent_count_excludes_manual_email_jobs(self):
        from core.services.send_control_dashboard_service import _sent_log_count

        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="send control batch_date=%s" % batch.batch_date.isoformat(),
        )
        pipeline_job = _create_job_with_email(batch=batch)
        manual_company = Company.objects.create(
            raw_name_latest="Manual Co",
            normalized_name="manual co",
            active_domain="manual.example.com",
        )
        manual_job = _create_job_with_email(batch=batch, company=manual_company)
        manual_job.is_manual_email_job = True
        manual_job.save(update_fields=["is_manual_email_job"])
        for job, email in ((pipeline_job, "pipeline@example.com"), (manual_job, "manual@example.com")):
            SentEmailLog.objects.create(
                send_run=run,
                job_posting=job,
                sender_account=sender,
                to_email=email,
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        self.assertEqual(_sent_log_count(batch), 1)

    def test_send_control_sender_gap_wait_is_not_marked_stalled(self):
        from core.services.send_control_dashboard_service import _send_run_status_summary

        now = timezone.now()
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        job = _create_job_with_email(batch=batch)
        sender = SenderAccount.objects.create(
            email="sender@example.com",
            app_password="pw",
            last_used_at=now - timedelta(minutes=11),
        )
        run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.RUNNING,
            started_at=now - timedelta(minutes=12),
            notes=f"send control batch_date={batch.batch_date.isoformat()}",
        )
        SentEmailLog.objects.create(
            send_run=run,
            job_posting=job,
            sender_account=sender,
            to_email="recent@example.com",
            subject_snapshot="Subject",
            body_snapshot="Body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.SENT,
            sent_at=now - timedelta(minutes=11),
        )

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "SENDER_MIN_GAP_SECONDS": "1200",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            status = _send_run_status_summary(run, plan={})

        self.assertTrue(status["is_waiting_for_sender"])
        self.assertFalse(status["is_stalled"])

    def test_send_switch_blocks_run_marked_stopped_from_dashboard(self):
        from core.services.file_run_logger import create_run_log_path
        from core.services.send_run_service import _send_switch_allows_send

        run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.STOPPED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="dashboard stopped",
        )
        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            allowed = _send_switch_allows_send(
                send_run=run,
                run_log_path=create_run_log_path("test_send_switch", "dashboard_stop"),
            )

        self.assertFalse(allowed)

    def test_send_only_real_run_round_robins_recipients_by_company(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company_a = Company.objects.create(raw_name_latest="Alpha", normalized_name="alpha", active_domain="alpha.com")
        company_b = Company.objects.create(raw_name_latest="Beta", normalized_name="beta", active_domain="beta.com")
        job_a = _create_job_with_email(batch=batch, company=company_a)
        job_b = _create_job_with_email(batch=batch, company=company_b)
        SenderAccount.objects.create(email="sender@example.com", app_password="pw")

        for idx in range(2):
            recruiter = CompanyRecruiter.objects.create(
                company=company_a,
                person_name=f"Alpha Person {idx + 1}",
                email=f"a{idx + 1}@alpha.com",
                source=CompanyRecruiter.Source.LEGACY,
                apollo_title="Data Science Manager",
            )
            JobRecruiterTarget.objects.create(
                job_posting=job_a,
                company_recruiter=recruiter,
                recipient_email_snapshot=recruiter.email,
                recipient_name_snapshot=recruiter.person_name,
                selection_order=idx + 1,
                is_selected_for_job=True,
            )
        for idx in range(2):
            recruiter = CompanyRecruiter.objects.create(
                company=company_b,
                person_name=f"Beta Person {idx + 1}",
                email=f"b{idx + 1}@beta.com",
                source=CompanyRecruiter.Source.LEGACY,
                apollo_title="Data Science Manager",
            )
            JobRecruiterTarget.objects.create(
                job_posting=job_b,
                company_recruiter=recruiter,
                recipient_email_snapshot=recruiter.email,
                recipient_name_snapshot=recruiter.person_name,
                selection_order=idx + 1,
                is_selected_for_job=True,
            )

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "SEND_ATTACH_RESUME": "0",
            "SENDER_MIN_GAP_SECONDS": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("core.services.send_run_service.send_via_smtp"):
            result = run_send_initial_for_batch(
                batch_date_str=batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
                skip_pending_recipients=True,
                source_label="Send control send-only",
            )

        sent_order = list(
            SentEmailLog.objects.filter(send_run_id=result["send_run_id"]).order_by("id").values_list("to_email", flat=True)
        )
        self.assertEqual(sent_order, ["a1@alpha.com", "b1@beta.com", "a2@alpha.com", "b2@beta.com"])
        self.assertEqual(result["totals"]["emails_sent"], 4)

    def test_send_plan_skips_pending_recipients_until_resolved(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme", active_domain="acme.com")
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Pending Person",
            email="pending@example.com",
            source=CompanyRecruiter.Source.LEGACY,
            apollo_title="Data Science Manager",
        )
        target = JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="pending@example.com",
            recipient_name_snapshot="Pending",
            selection_order=1,
            is_selected_for_job=True,
        )
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.STOPPED,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="interrupted send",
        )
        SentEmailLog.objects.create(
            send_run=send_run,
            job_posting=job,
            job_recruiter_target=target,
            sender_account=sender,
            to_email="pending@example.com",
            subject_snapshot="Subject",
            body_snapshot="Body",
            send_type=SentEmailLog.SendType.REAL,
            message_type=SentEmailLog.MessageType.INITIAL,
            status=SentEmailLog.SendStatus.PENDING,
        )

        plan = build_send_plan_for_batch(batch)

        self.assertEqual(plan["totals"]["recipients"], 0)
        self.assertEqual(plan["totals"]["skipped_pending_send"], 1)

    def test_send_plan_skips_non_verified_apollo_target(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme apollo send", active_domain="acme.com")
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Risky Apollo",
            email="risky@example.com",
            source=CompanyRecruiter.Source.APOLLO,
            apollo_person_id="apollo-risky-send",
            email_status="risky",
            apollo_title="Data Science Manager",
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="risky@example.com",
            recipient_name_snapshot="Risky Apollo",
            selection_order=1,
            is_selected_for_job=True,
        )

        plan = build_send_plan_for_batch(batch)

        self.assertEqual(plan["totals"]["recipients"], 0)
        self.assertEqual(plan["totals"]["skipped_no_recipients"], 1)

    def test_send_plan_skips_verifier_blocked_email(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme verifier blocked", active_domain="acme.com")
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Blocked Person",
            email="blocked@example.com",
            source=CompanyRecruiter.Source.LEGACY,
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="blocked@example.com",
            recipient_name_snapshot="Blocked Person",
            selection_order=1,
            is_selected_for_job=True,
        )
        EmailVerification.objects.create(
            email="blocked@example.com",
            decision=EmailVerification.Decision.BLOCK,
            provider_status="invalid",
            reason="Provider explicitly rejected the address.",
            expires_at=timezone.now() + timedelta(days=30),
        )

        plan = build_send_plan_for_batch(batch)

        self.assertEqual(plan["totals"]["recipients"], 0)
        self.assertEqual(plan["totals"]["skipped_suppressed"], 1)

    def test_send_plan_skips_manually_stopped_company_but_keeps_other_companies(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        stopped_company = Company.objects.create(
            raw_name_latest="Stopped Co",
            normalized_name="stopped co",
            active_domain="stopped.example.com",
            is_blocked=True,
        )
        active_company = Company.objects.create(
            raw_name_latest="Active Co",
            normalized_name="active co",
            active_domain="active.example.com",
        )
        stopped_job = _create_job_with_email(batch=batch, company=stopped_company)
        active_job = _create_job_with_email(batch=batch, company=active_company)
        for job, company, email in (
            (stopped_job, stopped_company, "stopped@example.com"),
            (active_job, active_company, "active@example.com"),
        ):
            recruiter = CompanyRecruiter.objects.create(
                company=company,
                person_name="Person",
                email=email,
                source=CompanyRecruiter.Source.LEGACY,
                apollo_title="Data Science Manager",
            )
            JobRecruiterTarget.objects.create(
                job_posting=job,
                company_recruiter=recruiter,
                recipient_email_snapshot=email,
                recipient_name_snapshot="Person",
                selection_order=1,
                is_selected_for_job=True,
            )

        plan = build_send_plan_for_batch(batch)

        self.assertEqual(plan["totals"]["recipients"], 1)
        self.assertEqual(plan["totals"]["skipped_company_blocked"], 1)
        self.assertEqual([row["company"] for row in plan["companies"]], ["active co"])

    def test_send_control_company_block_view_marks_company_blocked(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        company = Company.objects.create(
            raw_name_latest="Reply Co",
            normalized_name="reply co",
            active_domain="reply.example.com",
        )
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="Person",
            email="person@example.com",
            source=CompanyRecruiter.Source.LEGACY,
            apollo_title="Data Science Manager",
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="person@example.com",
            recipient_name_snapshot="Person",
            selection_order=1,
            is_selected_for_job=True,
        )

        response = self.client.get(reverse("send_control_dashboard"), {"batch_date": batch.batch_date.isoformat()})
        self.assertContains(response, "Stop Sends To This Company")

        response = self.client.post(
            reverse("send_control_company_block_view"),
            {
                "batch_date": batch.batch_date.isoformat(),
                "company_id": company.id,
                "action": "block",
                "reason": "manual reply",
            },
        )
        company.refresh_from_db()
        plan = build_send_plan_for_batch(batch)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(company.is_blocked)
        self.assertIn("manual reply", company.notes)
        self.assertEqual(plan["totals"]["recipients"], 0)
        self.assertEqual(plan["totals"]["skipped_company_blocked"], 1)

    def test_send_plan_enforces_ten_successful_initial_emails_per_company(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        prior_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme", active_domain="acme.com")
        prior_job = _create_job_with_email(batch=prior_batch, company=company)
        prior_job.linkedin_url = "https://www.linkedin.com/jobs/view/999/"
        prior_job.normalized_linkedin_url = "https://www.linkedin.com/jobs/view/999/"
        prior_job.dedupe_key = f"{company.normalized_name}:prior:{prior_batch.batch_date}"
        prior_job.save(update_fields=["linkedin_url", "normalized_linkedin_url", "dedupe_key"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior send",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=prior_job,
                sender_account=sender,
                to_email=f"sent{idx}@example.com",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )

        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="New Person",
            email="new@example.com",
            source=CompanyRecruiter.Source.LEGACY,
            apollo_title="Data Science Manager",
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="new@example.com",
            recipient_name_snapshot="New",
            selection_order=1,
            is_selected_for_job=True,
        )

        plan = build_send_plan_for_batch(batch)

        self.assertEqual(plan["totals"]["recipients"], 1)
        self.assertEqual(plan["totals"]["skipped_company_cap"], 0)

    def test_real_sender_sends_unsent_revealed_email_even_when_company_already_has_ten_successful_initial_sends(self):
        AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": 10})
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        prior_batch = DailyBatch.objects.create(batch_date=timezone.localdate() - timedelta(days=1))
        company = Company.objects.create(raw_name_latest="Acme", normalized_name="acme", active_domain="acme.com")
        prior_job = _create_job_with_email(batch=prior_batch, company=company)
        prior_job.linkedin_url = "https://www.linkedin.com/jobs/view/999/"
        prior_job.normalized_linkedin_url = "https://www.linkedin.com/jobs/view/999/"
        prior_job.dedupe_key = f"{company.normalized_name}:prior:{prior_batch.batch_date}"
        prior_job.save(update_fields=["linkedin_url", "normalized_linkedin_url", "dedupe_key"])
        sender = SenderAccount.objects.create(email="sender@example.com", app_password="pw")
        send_run = SendRun.objects.create(
            run_type=SendRun.RunType.REAL,
            status=SendRun.Status.SUCCESS,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            notes="prior send",
        )
        for idx in range(10):
            SentEmailLog.objects.create(
                send_run=send_run,
                job_posting=prior_job,
                sender_account=sender,
                to_email=f"already{idx}@example.com",
                subject_snapshot="Subject",
                body_snapshot="Body",
                send_type=SentEmailLog.SendType.REAL,
                message_type=SentEmailLog.MessageType.INITIAL,
                status=SentEmailLog.SendStatus.SENT,
                sent_at=timezone.now(),
            )
        job = _create_job_with_email(batch=batch, company=company)
        recruiter = CompanyRecruiter.objects.create(
            company=company,
            person_name="New Person",
            email="new@example.com",
            source=CompanyRecruiter.Source.LEGACY,
            apollo_title="Data Science Manager",
        )
        JobRecruiterTarget.objects.create(
            job_posting=job,
            company_recruiter=recruiter,
            recipient_email_snapshot="new@example.com",
            recipient_name_snapshot="New",
            selection_order=1,
            is_selected_for_job=True,
        )

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "SEND_ATTACH_RESUME": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.send_run_service.send_via_smtp"
        ) as smtp_mock:
            result = run_send_initial_for_batch(
                batch_date_str=batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
                skip_pending_recipients=True,
                source_label="Send control send-only",
            )

        smtp_mock.assert_called_once()
        self.assertEqual(result["totals"]["emails_attempted"], 1)
        self.assertEqual(result["totals"]["emails_sent"], 1)
        self.assertEqual(result["totals"]["emails_skipped_company_cap"], 0)

    def test_real_sender_skips_manually_stopped_company_and_continues_other_companies(self):
        batch = DailyBatch.objects.create(batch_date=timezone.localdate())
        stopped_company = Company.objects.create(
            raw_name_latest="Stopped Co",
            normalized_name="stopped co",
            active_domain="stopped.example.com",
            is_blocked=True,
        )
        active_company = Company.objects.create(
            raw_name_latest="Active Co",
            normalized_name="active co",
            active_domain="active.example.com",
        )
        stopped_job = _create_job_with_email(batch=batch, company=stopped_company)
        active_job = _create_job_with_email(batch=batch, company=active_company)
        for job, company, email in (
            (stopped_job, stopped_company, "stopped@example.com"),
            (active_job, active_company, "active@example.com"),
        ):
            recruiter = CompanyRecruiter.objects.create(
                company=company,
                person_name="Person",
                email=email,
                source=CompanyRecruiter.Source.LEGACY,
                apollo_title="Data Science Manager",
            )
            JobRecruiterTarget.objects.create(
                job_posting=job,
                company_recruiter=recruiter,
                recipient_email_snapshot=email,
                recipient_name_snapshot="Person",
                selection_order=1,
                is_selected_for_job=True,
            )
        SenderAccount.objects.create(email="sender@example.com", app_password="pw")

        env = {
            "EMAIL_SENDING_ENABLED": "1",
            "EMAIL_SENDING_PAUSED": "0",
            "SEND_ATTACH_RESUME": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "core.services.send_run_service.send_via_smtp"
        ) as smtp_mock:
            result = run_send_initial_for_batch(
                batch_date_str=batch.batch_date.isoformat(),
                send_type="real",
                delay_seconds=0,
                allow_recipient_discovery=False,
                skip_pending_recipients=True,
                source_label="Send control send-only",
            )

        smtp_mock.assert_called_once()
        self.assertEqual(result["totals"]["emails_sent"], 1)
        self.assertEqual(result["totals"]["emails_skipped_company_blocked"], 1)
        self.assertEqual(list(SentEmailLog.objects.values_list("to_email", flat=True)), ["active@example.com"])



