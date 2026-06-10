from django import forms
from django.db import models

from core.constants import DEFAULT_APIFY_ACTOR_ID, DEFAULT_FETCH_LIMIT, DEFAULT_LOOKBACK_HOURS


class ImportPipelineForm(forms.Form):
    lookback_hours = forms.IntegerField(
        initial=DEFAULT_LOOKBACK_HOURS,
        min_value=1,
        label="Lookback hours",
    )
    limit = forms.IntegerField(
        initial=DEFAULT_FETCH_LIMIT,
        min_value=1,
        label="Max jobs to fetch",
    )
    actor_id = forms.CharField(
        initial=DEFAULT_APIFY_ACTOR_ID,
        required=True,
        label="Apify actor ID",
    )


class RecruiterJsonProcessForm(forms.Form):
    recruiter_json_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 16, "placeholder": "Paste one or more JSON blocks here"}),
        label="Recruiter JSON blocks",
    )


class RecruiterDecisionApplyForm(forms.Form):
    decision_file_path = forms.CharField(
        widget=forms.TextInput(attrs={"placeholder": r"C:\path\to\filled_decision_file.json"}),
        label="Decision file path",
    )


class ApolloRecruiterFetchForm(forms.Form):
    company = forms.CharField(
        required=False,
        label="Normalized company name (optional)",
        widget=forms.TextInput(attrs={"placeholder": "Leave blank to process pending companies"}),
    )

class GenerateColdEmailsForm(forms.Form):
    company = forms.CharField(
        required=False,
        label="Normalized company name (optional)",
        widget=forms.TextInput(attrs={"placeholder": "Leave blank to process eligible jobs"}),
    )
    max_jobs = forms.IntegerField(
        initial=100,
        min_value=1,
        label="Max jobs to process",
    )


class LinkedInManualImportForm(forms.Form):
    linkedin_job_urls = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": "Paste one LinkedIn job URL per line (jobs/view/...). Queries/tracking params are OK.",
            }
        ),
        label="LinkedIn job URLs",
    )
    hiring_team_text = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": (
                    "Only use this if LinkedIn's public scrape missed a visible job-poster card.\n\n"
                    "Optional fallback example:\n"
                    "https://www.linkedin.com/jobs/view/4417407332/\n"
                    "Sarah Ellis\n"
                    "Vice President, Human Resources at Cavallo\n"
                    "https://www.linkedin.com/in/sarah-ellis-..."
                ),
            }
        ),
        label="Optional manual job-poster fallback",
        help_text=(
            "Leave this blank for the normal URL-only flow. The scraper automatically captures public "
            "LinkedIn job-poster cards when they are present."
        ),
    )
    cooldown_days = forms.IntegerField(
        initial=0,
        min_value=0,
        label="Company cooldown days",
        help_text="Uses exactly this number. 0 means no company cooldown. Skips companies with a recent REAL send or a recent job import within this window.",
    )
    apply_cooldown_filters = forms.BooleanField(
        required=False,
        initial=True,
        label="Apply company cooldown filters (always on)",
    )
    skip_blocked_companies = forms.BooleanField(
        required=False,
        initial=True,
        label="Skip blocked companies",
        help_text="Skips if Company.is_blocked is set for the normalized company.",
    )
    use_openai_filter = forms.BooleanField(
        required=False,
        initial=True,
        label="Use OpenAI APPLY/REJECT classifier (optional)",
        help_text="Requires OPENAI_API_KEY; useful if your pasted list is noisy.",
    )
    dry_run = forms.BooleanField(
        required=False,
        initial=False,
        label="Dry run (do not scrape/store)",
    )
    force_refetch = forms.BooleanField(
        required=False,
        initial=False,
        label="Force re-fetch existing URLs",
        help_text="If a URL already exists in the DB, re-scrape it and update the title, company, description, and location instead of skipping it.",
    )


class DiceManualImportForm(forms.Form):
    dice_job_urls = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": "Paste one Dice job URL per line, for example https://www.dice.com/job-detail/...",
            }
        ),
        label="Dice job URLs",
    )
    cooldown_days = forms.IntegerField(
        initial=0,
        min_value=0,
        label="Company cooldown days",
        help_text="Uses exactly this number. 0 means no company cooldown. Skips companies with a recent REAL send or recent job import within this window.",
    )
    skip_blocked_companies = forms.BooleanField(
        required=False,
        initial=True,
        label="Skip blocked companies",
        help_text="Skips if Company.is_blocked is set for the normalized company.",
    )
    use_openai_filter = forms.BooleanField(
        required=False,
        initial=True,
        label="Use OpenAI APPLY/REJECT classifier",
        help_text="Recommended if pasted Dice lists are noisy.",
    )
    dry_run = forms.BooleanField(
        required=False,
        initial=False,
        label="Dry run (preview only, do not store)",
    )
    force_refetch = forms.BooleanField(
        required=False,
        initial=False,
        label="Force re-fetch existing URLs",
        help_text="If a URL already exists in the DB, re-scrape it and update the stored job instead of skipping it.",
    )


class LinkedInPostPreviewForm(forms.Form):
    linkedin_post_url = forms.URLField(
        label="LinkedIn post URL",
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://www.linkedin.com/posts/... or https://www.linkedin.com/feed/update/...",
            }
        ),
    )


class LinkedInPostOutreachForm(forms.Form):
    linkedin_post_urls = forms.CharField(
        label="LinkedIn post URLs",
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": (
                    "Paste one LinkedIn post URL per line.\n"
                    "https://www.linkedin.com/posts/...\n"
                    "https://www.linkedin.com/feed/update/..."
                ),
            }
        ),
    )
    find_emails = forms.BooleanField(
        required=False,
        initial=True,
        label="Find poster emails with Apollo",
        help_text="Uses Apollo person match only when a poster name and company can be extracted.",
    )
    use_chatgpt_extraction = forms.BooleanField(
        required=False,
        initial=True,
        label="Use ChatGPT to extract names, emails, companies, and roles from posts",
        help_text="Uses OPENAI_API_KEY. If unavailable, rows still fall back to scraper, Apollo, and manual fields.",
    )
    create_review_batch = forms.BooleanField(
        required=False,
        initial=False,
        label="Auto-create review batch for complete rows",
        help_text="Generates draft emails for rows that already have enough details. Sending still happens only from the review page.",
    )


class ManualBulkEmailForm(forms.Form):
    named_recipient_map = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": '{\n  "Jane Doe": "jane@example.com",\n  "Sam Recruiter": "sam@example.com"\n}',
            }
        ),
        label="Known names dict",
        help_text='Optional. Use {"Person Name": "email@example.com"} or {"email@example.com": "Person Name"}.',
    )
    recipient_emails = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "Paste emails without known names here. Commas, spaces, and one-per-line are all OK.",
            }
        ),
        label="Emails without known names",
    )
    subject = forms.CharField(
        max_length=500,
        label="Email subject",
        widget=forms.TextInput(attrs={"placeholder": "Gayathri - Data Scientist Role"}),
    )
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "placeholder": "Paste the exact email body to send. Use {name} or {{name}} if you want personalization. The configured resume will be attached automatically.",
            }
        ),
        label="Email body",
    )
    delay_seconds = forms.IntegerField(
        initial=300,
        min_value=0,
        label="Delay seconds between emails",
    )
    confirm_send = forms.BooleanField(
        required=True,
        label="I reviewed this recipient list and want to send real emails",
    )


DEFAULT_CONSULTANCY_OUTREACH_SUBJECT = "Gayathri Emuru - Exploring Data Scientist Opportunities"

DEFAULT_CONSULTANCY_OUTREACH_BODY = """Hi,

I hope you are doing well. I am reaching out to explore Data Scientist, AI Engineer, or Data Analyst opportunities.

I have 4+ years of hands-on experience building machine learning models, working with data pipelines, analyzing business data, and developing AI-powered applications. I am comfortable with Python, SQL, ML workflows, analytics, and production-focused problem solving.

I have attached my resume for your review. I would be grateful if you could consider me for any current or upcoming roles that may be a fit.

Thank you for your time.

Best regards,
Gayathri Emuru

Portfolio: https://gayathri-emuru.github.io/
LinkedIn: https://www.linkedin.com/in/gayathri-emuru/
"""


class ConsultancyOutreachForm(forms.Form):
    consultancy_json = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 16,
                "placeholder": '{\n  "Company Name": {\n    "website": "https://example.com/",\n    "email_address": "careers@example.com"\n  }\n}',
            }
        ),
        label="Consultancy JSON",
    )
    subject = forms.CharField(
        max_length=500,
        initial=DEFAULT_CONSULTANCY_OUTREACH_SUBJECT,
        label="Subject",
        widget=forms.TextInput(attrs={"placeholder": DEFAULT_CONSULTANCY_OUTREACH_SUBJECT}),
    )
    body = forms.CharField(
        initial=DEFAULT_CONSULTANCY_OUTREACH_BODY,
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "placeholder": DEFAULT_CONSULTANCY_OUTREACH_BODY,
            }
        ),
        label="Message",
    )
    delay_seconds = forms.IntegerField(
        initial=300,
        min_value=0,
        label="Delay seconds between emails",
    )
    confirm_send = forms.BooleanField(
        required=False,
        label="I reviewed this consultancy list and want to send real emails",
    )


class TestEmailDeliveryForm(forms.Form):
    class SendMode(models.TextChoices):
        ONE_SENDER = "one_sender", "1 sender → all test accounts"
        ALL_SENDERS = "all_senders", "All senders → all test accounts"

    job_id = forms.IntegerField(
        min_value=1,
        label="Job id (for subject/body/logging)",
        help_text="Used to pull the job URL and attach the run logs to a JobPosting.",
    )
    send_mode = forms.ChoiceField(
        choices=SendMode.choices,
        initial=SendMode.ONE_SENDER,
        label="Send mode",
    )
    sender_email = forms.EmailField(
        required=False,
        label="Sender email (optional)",
        help_text="Only used for 1-sender mode. Leave blank to use the first active sender.",
    )
    recipient_emails = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Optional: paste recipient emails. New emails are saved as active test accounts. Leave blank to use all active test accounts."}),
        label="Recipient emails (optional)",
    )
    delay_seconds = forms.IntegerField(
        required=False,
        initial=3,
        min_value=0,
        label="Delay seconds",
    )
    use_openai_email = forms.BooleanField(
        required=False,
        initial=True,
        label="Use OpenAI to write the email",
        help_text="Requires OPENAI_API_KEY. If enabled, generates a job-tailored subject/body for the test send.",
    )
    regenerate_openai_each_run = forms.BooleanField(
        required=False,
        initial=False,
        label="Regenerate even if GeneratedEmail exists",
        help_text="If unchecked and the job already has a GeneratedEmail, that stored subject/body is used.",
    )
    prefix_subject_with_test_tag = forms.BooleanField(
        required=False,
        initial=True,
        label="Prefix subject with [DELIVERY TEST]",
    )


class PromptExperimentForm(forms.Form):
    job_id = forms.IntegerField(
        initial=209,
        min_value=1,
        label="Job id",
        help_text="JobPosting id to use for the prompt experiment. This does not send emails.",
    )
    prompt_text = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 14,
                "placeholder": "Paste a full prompt here. This will be used as the system prompt for Claude generation.",
            }
        ),
        label="Prompt text",
        help_text="Temporary tool: generates subject/body for the selected Job id using Claude. Does not send emails.",
    )


class CompanyDomainMappingTemplateForm(forms.Form):
    only_missing = forms.BooleanField(
        required=False,
        initial=False,
        label="Only companies missing domains",
        help_text="If unchecked, includes all non-blocked companies that have jobs.",
    )


class CompanyDomainMappingApplyForm(forms.Form):
    mapping_json = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "placeholder": (
                    "Paste JSON mapping here, e.g.\n"
                    "{\n"
                    "  \"trendlyne\": \"trendlyne.com\",\n"
                    "  \"Bird\": \"bird.co\",\n"
                    "  \"tiny invalid company\": null\n"
                    "}\n"
                    "Leave blank (or \"NAN\") to skip. Use null/None to delete the company and related rows."
                ),
            }
        ),
        label="Company → domain mapping (JSON)",
    )


class LegacyCompanyDomainRangeTemplateForm(forms.Form):
    start_range = forms.IntegerField(
        initial=1,
        min_value=1,
        label="Start legacy company row",
        help_text="1-based position in legacy companies ordered by normalized name.",
    )
    end_range = forms.IntegerField(
        initial=50,
        min_value=1,
        label="End legacy company row",
    )
    only_missing = forms.BooleanField(
        required=False,
        initial=True,
        label="Only legacy companies missing usable domains",
    )


class LegacyCompanyDomainMappingApplyForm(forms.Form):
    mapping_json = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "placeholder": (
                    "Paste populated legacy company domain JSON here, e.g.\n"
                    "{\n"
                    "  \"post holdings\": \"postholdings.com\",\n"
                    "  \"walmart\": \"walmart.com\",\n"
                    "  \"tiny invalid company\": null\n"
                    "}\n"
                    "Blank or \"NAN\" values are skipped. Null/None deletes the legacy company and related rows."
                ),
            }
        ),
        label="Legacy company domain mapping (JSON)",
    )
