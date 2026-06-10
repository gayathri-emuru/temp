from typing import Optional

from django.db.models import Q

from core.models import JobPosting, GeneratedEmail
from core.services.file_run_logger import create_run_log_path, append_and_print, append_exception
from core.services.email_ai_settings_service import get_email_ai_generation_settings
from core.services.email_generation_throttle_service import (
    email_generation_batch_delay_seconds,
    email_generation_rate_limit_backoff_seconds,
    is_email_generation_rate_limit_error,
    sleep_between_email_generation_requests,
    sleep_for_email_generation_rate_limit,
)
from core.services.openai_cold_email_service import generate_cold_email_with_provider
from core.utils import safe_str


RESUME_TEXT = """
Gayathri Emuru
Data Scientist with 4+ years of experience building and deploying production ML systems across fintech, research, and enterprise domains.
Specializes in end-to-end ML delivery — from data engineering and model development to deployment and monitoring — with production systems serving 10M+ users and handling 700M+ monthly API requests.
Peer-reviewed research contributions at Scientific Data (Nature) on large-scale multimodal NLP and data extraction pipelines.
AWS Certified Machine Learning Engineer – Associate with hands-on experience in cloud deployment, MLOps, Python, and SQL.

Recent relevant experience:
- At the University of Delaware: built production-ready RAG-style semantic search across 24,475 bilingual documents with FAISS, BM25, reranking, and LLM APIs.
- At the University of Delaware: engineered scalable data pipelines across heterogeneous PDF and document sources with SQL, retrieval, Bayesian modeling, and Power BI.
- At Nagarro: productionized an XGBoost risk-classification model on AWS SageMaker for risk-classification workflows.
- At Nagarro: built and scaled REST APIs for a high-volume analytics platform.
- In fintech data engineering work: engineered transaction data pipelines for a financial system serving 10M users with 99.9% transaction accuracy.
Skills: Python, SQL, AWS, NLP, computer vision, ML systems, ETL, REST APIs, Django, MySQL, Git, CI/CD, PySpark, Power BI, TensorFlow, PyTorch, MLFlow.
""".strip()


SENDER_SIGNATURE = """
Best regards,
Gayathri Emuru
# Portfolio: https://gayathri-emuru.github.io/
# LinkedIn: https://www.linkedin.com/in/gayathri-emuru/
""".strip()


def _get_best_recipient(job: JobPosting):
    target = (
        job.targets
        .filter(
            Q(recipient_email_snapshot__isnull=False)
            & ~Q(recipient_email_snapshot="")
            & ~Q(recipient_email_snapshot="none")
            & Q(company_recruiter__email_sent=False)
        )
        .order_by("selection_order", "id")
        .first()
    )

    if target:
        return {
            "recipient_name": safe_str(target.recipient_name_snapshot) or "there",
            "recipient_role": "Recruiter",
            "recipient_email": safe_str(target.recipient_email_snapshot).lower(),
        }

    return None


def _build_company_context(job: JobPosting) -> str:
    parts = []

    if job.title:
        parts.append(f"Role title: {job.title}.")
    if job.location:
        parts.append(f"Location: {job.location}.")
    if job.company:
        parts.append(f"Company: {job.company}.")
    if job.salary:
        parts.append(f"Salary info: {job.salary}.")
    if job.description:
        trimmed = " ".join(job.description.split())
        parts.append(f"Job description summary source: {trimmed[:1200]}")

    return " ".join(parts).strip()


def _build_value_prop(job: JobPosting) -> str:
    title = safe_str(job.title).lower()
    description = safe_str(job.description).lower()

    if any(x in title or x in description for x in ["machine learning", "ml", "nlp", "ai", "llm", "data scientist"]):
        return (
            "My background lines up well with production ML delivery, NLP/search systems, "
            "AWS deployment, and high-scale API-backed data products."
        )

    if any(x in title or x in description for x in ["data engineer", "etl", "pipeline", "spark"]):
        return (
            "My background lines up well with scalable ETL, Python/SQL data engineering, "
            "cloud workflows, and production analytics systems."
        )

    if any(x in title or x in description for x in ["software", "developer", "backend", "api", "django"]):
        return (
            "My background lines up well with Python backend development, REST APIs, "
            "data-intensive systems, and production deployment."
        )

    return (
        "My background combines production ML systems, data engineering, cloud deployment, "
        "and high-scale API-driven product work."
    )


def run_cold_email_generation_for_job(job: JobPosting, run_log_path: str = ""):
    if not run_log_path:
        run_log_path = create_run_log_path("cold_email_job", f"{job.company}_{job.id}")

    stats = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "generated": 0,
        "skipped_no_recipient": 0,
        "skipped_no_description": 0,
        "error": "",
        "provider": "",
        "model": "",
        "run_log_path": run_log_path,
    }

    append_and_print(
        run_log_path,
        f"START job_id={job.id} company={job.company} title={job.title}",
    )

    recipient = _get_best_recipient(job)
    if not recipient:
        stats["skipped_no_recipient"] = 1
        append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_recipient_email")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    if not safe_str(job.description).strip():
        stats["skipped_no_description"] = 1
        append_and_print(run_log_path, f"SKIP job_id={job.id} reason=no_job_description")
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    company_context = _build_company_context(job)
    value_prop = _build_value_prop(job)
    ai_settings = get_email_ai_generation_settings()
    stats["provider"] = ai_settings["provider"]
    stats["model"] = ai_settings["model"]

    append_and_print(
        run_log_path,
        f"PROMPT_INPUT job_id={job.id} recipient_name={recipient['recipient_name']} "
        f"recipient_role={recipient['recipient_role']} recipient_email={recipient['recipient_email']}",
    )
    append_and_print(
        run_log_path,
        f"PROMPT_CONTEXT job_id={job.id} company_context={company_context[:1500]}",
    )
    append_and_print(
        run_log_path,
        f"PROMPT_VALUE_PROP job_id={job.id} value_prop={value_prop}",
    )
    append_and_print(
        run_log_path,
        f"PROMPT_RESUME job_id={job.id} resume_text={RESUME_TEXT[:2500]}",
    )
    append_and_print(
        run_log_path,
        f"EMAIL_AI_SELECTION job_id={job.id} provider={ai_settings['provider']} model={ai_settings['model']}",
    )

    try:
        result = generate_cold_email_with_provider(
            provider=ai_settings["provider"],
            model=ai_settings["model"],
            sender_name="Gayathri Emuru",
            sender_role="Data Scientist",
            sender_signature=SENDER_SIGNATURE,
            company_name=safe_str(job.company),
            recipient_name=recipient["recipient_name"],
            recipient_role=recipient["recipient_role"],
            job_title=safe_str(job.title),
            job_description=safe_str(job.description),
            company_context=company_context,
            value_proposition=value_prop,
            resume_text=RESUME_TEXT,
        )
    except Exception as exc:
        stats["error"] = str(exc)
        append_exception(run_log_path, f"AI_COLD_EMAIL_ERROR job_id={job.id}", exc)
        append_and_print(run_log_path, f"END stats={stats}")
        return stats

    subject = safe_str(result.get("subject"))
    email_body = safe_str(result.get("email"))
    prompt_version = safe_str(result.get("prompt_version")) or "cold_email_prompt_unknown"

    append_and_print(
        run_log_path,
        f"AI_RESULT job_id={job.id} provider={stats['provider']} model={stats['model']} subject={subject}",
    )
    append_and_print(
        run_log_path,
        f"EMAIL_BODY job_id={job.id} body={email_body}",
    )

    generated, _ = GeneratedEmail.objects.update_or_create(
        job_posting=job,
        defaults={
            "subject": subject,
            "body": email_body,
            "generation_status": GeneratedEmail.GenerationStatus.GENERATED,
            "prompt_version": prompt_version,
            "edited_manually": False,
        },
    )

    stats["generated"] = 1
    append_and_print(run_log_path, f"SAVED job_id={job.id} generated_email_id={generated.id}")
    append_and_print(run_log_path, f"END stats={stats}")
    return stats


def run_cold_email_generation_for_eligible_jobs(
    company_name: str = "",
    max_jobs: Optional[int] = 100,
    batch_date: str = "",
    skip_existing: bool = False,
):
    scope = company_name or (f"batch_{batch_date}" if batch_date else "all")
    master_log_path = create_run_log_path("cold_email_master", scope)

    append_and_print(
        master_log_path,
        f"MASTER_START company_filter={company_name or '[ALL]'} batch_date={batch_date or '[ALL]'} max_jobs={max_jobs or '[ALL]'} skip_existing={skip_existing}",
    )

    qs = JobPosting.objects.filter(is_manual_email_job=False).order_by("-updated_at", "-id")

    if company_name:
        qs = qs.filter(company_ref__normalized_name=company_name)
    if batch_date:
        qs = qs.filter(daily_batch__batch_date=batch_date)
    if skip_existing:
        qs = qs.filter(generated_email__isnull=True)

    if max_jobs is None:
        jobs = list(qs)
    else:
        jobs = list(qs[:max_jobs])

    append_and_print(master_log_path, f"CANDIDATE_JOB_COUNT count={len(jobs)}")

    selected_jobs = []
    for job in jobs:
        recipient = _get_best_recipient(job)
        reasons = []

        if not recipient:
            reasons.append("no_recipient_email")
        if not safe_str(job.description).strip():
            reasons.append("no_job_description")

        append_and_print(
            master_log_path,
            f"JOB_SELECTION_CHECK job_id={job.id} company={job.company} title={job.title} "
            f"decision={'SELECT' if not reasons else 'SKIP'} reasons={reasons if reasons else ['eligible']}",
        )

        if not reasons:
            selected_jobs.append(job)

    append_and_print(
        master_log_path,
        f"MASTER_SELECTION job_ids={[job.id for job in selected_jobs]}",
    )

    totals = {
        "jobs_seen": 0,
        "generated": 0,
        "job_errors": 0,
        "skip_existing": bool(skip_existing),
        "candidate_jobs": len(jobs),
        "selected_jobs": len(selected_jobs),
        "provider": "",
        "model": "",
        "master_log_path": master_log_path,
    }
    all_stats = []
    ai_settings = get_email_ai_generation_settings()
    totals["provider"] = ai_settings["provider"]
    totals["model"] = ai_settings["model"]
    append_and_print(
        master_log_path,
        f"EMAIL_AI_SELECTION provider={ai_settings['provider']} model={ai_settings['model']}",
    )

    total_to_process = len(selected_jobs)
    per_request_delay_seconds = email_generation_batch_delay_seconds(ai_settings["provider"])
    rate_limit_backoff_seconds = email_generation_rate_limit_backoff_seconds(ai_settings["provider"])
    append_and_print(
        master_log_path,
        f"BATCH_THROTTLE provider={ai_settings['provider']} per_request_delay={per_request_delay_seconds}s rate_limit_backoff={rate_limit_backoff_seconds}s",
    )

    for index, job in enumerate(selected_jobs):
        job_log_path = create_run_log_path("cold_email_job", f"{job.company}_{job.id}")
        totals["jobs_seen"] += 1

        append_and_print(
            master_log_path,
            f"JOB_START job_id={job.id} company={job.company} title={job.title} job_log={job_log_path}",
        )

        stats = run_cold_email_generation_for_job(job=job, run_log_path=job_log_path)
        all_stats.append(stats)

        totals["generated"] += stats["generated"]
        error_text = safe_str(stats.get("error")).lower()
        is_rate_limit = is_email_generation_rate_limit_error(error_text)

        if stats.get("error"):
            totals["job_errors"] += 1
            append_and_print(
                master_log_path,
                f"JOB_ERROR job_id={job.id} error={stats['error']} job_log={job_log_path}",
            )
            if is_rate_limit and index < total_to_process - 1:
                sleep_for_email_generation_rate_limit(
                    ai_settings["provider"],
                    run_log_path=master_log_path,
                    log_func=append_and_print,
                )
                continue
        else:
            append_and_print(
                master_log_path,
                f"JOB_DONE job_id={job.id} generated={stats['generated']} job_log={job_log_path}",
            )

        sleep_between_email_generation_requests(
            ai_settings["provider"],
            is_last=index >= total_to_process - 1,
            run_log_path=master_log_path,
            log_func=append_and_print,
        )

    append_and_print(master_log_path, f"MASTER_END totals={totals}")

    return {
        "totals": totals,
        "jobs": all_stats,
    }
