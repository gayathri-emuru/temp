from __future__ import annotations

import os
import json
import threading
import time
from datetime import datetime

import requests
from django.db.models import Count, Q
from django.utils import timezone

from core.models import (
    ApprovalRecord,
    Company,
    DailyCompanyReplyStop,
    InboxScanEvent,
    JobPosting,
    SentEmailLog,
)
from core.utils import safe_str


_REFRESH_LOCK = threading.Lock()
_LAST_REFRESH_MONOTONIC = 0.0
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT_SECONDS = 30


def record_reply_stop_for_event(event: InboxScanEvent) -> DailyCompanyReplyStop | None:
    # Replies are recorded by the inbox monitor, but company-level send stops are manual-only.
    return None


def _record_automatic_reply_stop_for_event(event: InboxScanEvent) -> DailyCompanyReplyStop | None:
    if event.classification != InboxScanEvent.Classification.REPLY or not event.matched_sent_log_id:
        return None
    sent_log = event.matched_sent_log
    job = getattr(sent_log, "job_posting", None)
    company = getattr(job, "company_ref", None)
    if not company and job:
        company = Company.objects.filter(normalized_name__iexact=safe_str(job.normalized_company).strip()).first()
    if not company:
        return None

    reply_at = event.message_date or event.created_at or timezone.now()
    stop_date = timezone.localtime(reply_at).date()
    existing = DailyCompanyReplyStop.objects.filter(company=company, stop_date=stop_date).first()
    if existing and existing.decision_source == DailyCompanyReplyStop.DecisionSource.MANUAL:
        return existing if existing.is_active else None
    if existing and existing.reply_event_id == event.id and existing.decision_source == DailyCompanyReplyStop.DecisionSource.OPENAI:
        return existing if existing.is_active else None

    decision = classify_reply_stop_decision(event)
    should_stop = decision["decision"] in {"stop", "review"}
    reason = decision["reason"] or f"Matched reply from {safe_str(event.detected_email).strip().lower() or 'recipient'}"
    obj, _ = DailyCompanyReplyStop.objects.update_or_create(
        company=company,
        stop_date=stop_date,
        defaults={
            "reply_event": event,
            "matched_sent_log": sent_log,
            "respondent_email": safe_str(event.detected_email).strip().lower()[:254],
            "reply_at": reply_at,
            "is_active": should_stop,
            "reply_decision": decision["decision"],
            "decision_source": decision["source"],
            "decision_confidence": decision["confidence"],
            "reason": reason[:500],
            "reply_excerpt": _reply_excerpt(event)[:4000],
        },
    )
    return obj if obj.is_active else None


def company_has_reply_stop_today(job: JobPosting) -> bool:
    company_id = getattr(job, "company_ref_id", None)
    if not company_id:
        return False
    return DailyCompanyReplyStop.objects.filter(
        company_id=company_id,
        stop_date=timezone.localdate(),
        is_active=True,
        decision_source=DailyCompanyReplyStop.DecisionSource.MANUAL,
    ).exists()


def manually_set_company_reply_stop(
    *,
    company: Company,
    stop_date,
    should_stop: bool,
    note: str = "",
) -> DailyCompanyReplyStop:
    reason = "Manual stop: do not send more initial emails to this company today."
    decision = DailyCompanyReplyStop.ReplyDecision.STOP
    if not should_stop:
        reason = "Manual resume: sending is allowed for this company today."
        decision = DailyCompanyReplyStop.ReplyDecision.CONTINUE
    obj, _ = DailyCompanyReplyStop.objects.update_or_create(
        company=company,
        stop_date=stop_date,
        defaults={
            "is_active": bool(should_stop),
            "reply_decision": decision,
            "decision_source": DailyCompanyReplyStop.DecisionSource.MANUAL,
            "decision_confidence": 1.0,
            "reason": reason,
            "manual_note": safe_str(note).strip()[:4000],
        },
    )
    return obj


def classify_reply_stop_decision(event: InboxScanEvent) -> dict:
    if not _openai_reply_decision_enabled():
        return {
            "decision": DailyCompanyReplyStop.ReplyDecision.STOP,
            "source": DailyCompanyReplyStop.DecisionSource.RULE,
            "confidence": 1.0,
            "reason": f"Matched reply from {safe_str(event.detected_email).strip().lower() or 'recipient'}",
        }
    try:
        parsed = _call_openai_reply_decision(event)
    except Exception as exc:
        return {
            "decision": DailyCompanyReplyStop.ReplyDecision.REVIEW,
            "source": DailyCompanyReplyStop.DecisionSource.OPENAI,
            "confidence": 0.0,
            "reason": f"AI reply decision failed, stopping until manual review: {safe_str(exc)[:220]}",
        }

    decision = safe_str(parsed.get("decision")).strip().lower()
    if decision not in {"stop", "continue", "review"}:
        decision = "review"
    return {
        "decision": decision,
        "source": DailyCompanyReplyStop.DecisionSource.OPENAI,
        "confidence": _bounded_float(parsed.get("confidence")),
        "reason": safe_str(parsed.get("reason")).strip()[:500],
    }


def refresh_reply_stops_if_due(*, force: bool = False) -> dict:
    return {"scanned": False, "reason": "manual_only"}


def _refresh_automatic_reply_stops_if_due(*, force: bool = False) -> dict:
    global _LAST_REFRESH_MONOTONIC
    interval = _positive_int("LIVE_REPLY_SCAN_INTERVAL_SECONDS", 5)
    now = time.monotonic()
    if not force and now - _LAST_REFRESH_MONOTONIC < interval:
        return {"scanned": False, "reason": "throttled"}
    if not _REFRESH_LOCK.acquire(blocking=False):
        return {"scanned": False, "reason": "already_scanning"}
    try:
        now = time.monotonic()
        if not force and now - _LAST_REFRESH_MONOTONIC < interval:
            return {"scanned": False, "reason": "throttled"}
        from core.services.inbox_monitor_service import scan_and_store_inbox_events

        result = scan_and_store_inbox_events(
            max_messages=_positive_int("LIVE_REPLY_SCAN_MAX_MESSAGES", 50),
        )
        _LAST_REFRESH_MONOTONIC = time.monotonic()
        return {"scanned": True, "result": result}
    finally:
        _REFRESH_LOCK.release()


def build_live_company_reply_dashboard_context(batch_date: str = "") -> dict:
    selected_date = _parse_date(batch_date) or _latest_approved_batch_date() or timezone.localdate()
    live_date = timezone.localdate()
    approved_jobs = list(
        JobPosting.objects.filter(
            daily_batch__batch_date=selected_date,
            is_manual_email_job=False,
            approval_record__is_approved=True,
        )
        .select_related("company_ref", "approval_record")
        .order_by("company_ref__normalized_name", "company", "id")
    )
    company_ids = {job.company_ref_id for job in approved_jobs if job.company_ref_id}
    decisions = {
        stop.company_id: stop
        for stop in DailyCompanyReplyStop.objects.filter(
            company_id__in=company_ids,
            stop_date=live_date,
            decision_source=DailyCompanyReplyStop.DecisionSource.MANUAL,
        ).select_related("company", "reply_event", "matched_sent_log")
    }
    sent_counts = dict(
        SentEmailLog.objects.filter(
            job_posting__company_ref_id__in=company_ids,
            send_type=SentEmailLog.SendType.REAL,
            status=SentEmailLog.SendStatus.SENT,
            message_type=SentEmailLog.MessageType.INITIAL,
            sent_at__date=live_date,
        )
        .values_list("job_posting__company_ref_id")
        .annotate(count=Count("id"))
    )
    remaining_counts = dict(
        JobPosting.objects.filter(
            daily_batch__batch_date=selected_date,
            company_ref_id__in=company_ids,
            approval_record__is_approved=True,
            targets__is_selected_for_job=True,
            targets__is_sent_real=False,
            targets__company_recruiter__email_sent=False,
        )
        .exclude(targets__recipient_email_snapshot__in=["", "none"])
        .values_list("company_ref_id")
        .annotate(count=Count("targets", distinct=True))
    )

    rows_by_company: dict[int, dict] = {}
    unresolved_rows: dict[str, dict] = {}
    for job in approved_jobs:
        if job.company_ref_id:
            company = job.company_ref
            row = rows_by_company.setdefault(
                company.id,
                {
                    "company": company,
                    "name": safe_str(company.normalized_name).strip() or safe_str(company.raw_name_latest).strip(),
                    "jobs": [],
                    "decision": decisions.get(company.id),
                    "stop": decisions.get(company.id) if decisions.get(company.id) and decisions.get(company.id).is_active else None,
                    "sent_today": int(sent_counts.get(company.id) or 0),
                    "remaining": int(remaining_counts.get(company.id) or 0),
                },
            )
        else:
            name = safe_str(job.company).strip() or "Unknown company"
            row = unresolved_rows.setdefault(
                name.lower(),
                {"company": None, "name": name, "jobs": [], "stop": None, "sent_today": 0, "remaining": 0},
            )
        row["jobs"].append(job)

    rows = sorted(
        [*rows_by_company.values(), *unresolved_rows.values()],
        key=lambda row: (0 if row["stop"] else 1 if row["remaining"] > 0 else 2, row["name"].lower()),
    )
    stopped_count = sum(1 for row in rows if row["stop"])
    active_count = sum(1 for row in rows if not row["stop"] and row["remaining"] > 0)
    complete_count = sum(1 for row in rows if not row["stop"] and row["remaining"] <= 0)
    return {
        "selected_date": selected_date,
        "today": live_date,
        "rows": rows,
        "totals": {
            "companies": len(rows),
            "approved_jobs": len(approved_jobs),
            "stopped_by_reply": stopped_count,
            "still_active": active_count,
            "complete": complete_count,
            "sent_today": sum(row["sent_today"] for row in rows),
            "remaining": sum(row["remaining"] for row in rows),
        },
    }


def _openai_reply_decision_enabled() -> bool:
    if os.getenv("LIVE_REPLY_STOP_AI_ENABLED", "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _call_openai_reply_decision(event: InboxScanEvent) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing.")

    schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["stop", "continue", "review"]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["decision", "confidence", "reason"],
        "additionalProperties": False,
    }
    developer_message = (
        "You decide whether a reply to a cold recruiting/job outreach email should stop all remaining "
        "initial emails to the same company for today. Return JSON only. "
        "Decision rules: "
        "- stop: human complaint, unsubscribe/do-not-contact, angry response, clear negative company-level response, "
        "or anything that creates reputation/spam risk if more people at that company are emailed today. "
        "- continue: automated out-of-office, delivery notice, vacation responder, simple acknowledgement, referral, "
        "or a wrong-person/not-responsible reply that does not complain and does not ask to stop contact. "
        "- review: unclear or risky. Use review when you cannot confidently choose. "
        "When in doubt between stop and review, choose review."
    )
    user_payload = {
        "from": safe_str(event.from_header)[:1000],
        "subject": safe_str(event.subject)[:1000],
        "detected_email": safe_str(event.detected_email)[:320],
        "snippet": safe_str(event.snippet)[:2500],
        "raw_detail": safe_str(event.raw_detail)[:1500],
    }
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("LIVE_REPLY_STOP_OPENAI_MODEL", "gpt-5-mini"),
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": developer_message}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_payload)}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "live_reply_stop_decision",
                    "strict": True,
                    "schema": schema,
                }
            },
        },
        timeout=OPENAI_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI status={response.status_code} body={response.text[:500]}")
    text = _extract_output_text(response.json())
    if not text:
        raise RuntimeError("OpenAI response did not include output_text.")
    return json.loads(text)


def _extract_output_text(payload: dict) -> str:
    text = safe_str(payload.get("output_text"))
    if text:
        return text
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text":
                text = safe_str(block.get("text"))
                if text:
                    return text
    return ""


def _reply_excerpt(event: InboxScanEvent) -> str:
    parts = [
        f"Subject: {safe_str(event.subject).strip()}",
        safe_str(event.snippet).strip(),
    ]
    return "\n".join(part for part in parts if part)


def _bounded_float(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except Exception:
        return 0.0


def _latest_approved_batch_date():
    return (
        ApprovalRecord.objects.filter(
            is_approved=True,
            job_posting__is_manual_email_job=False,
        )
        .order_by("-job_posting__daily_batch__batch_date", "-job_posting_id")
        .values_list("job_posting__daily_batch__batch_date", flat=True)
        .first()
    )


def _parse_date(value: str):
    try:
        return datetime.strptime(safe_str(value).strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except Exception:
        return default
