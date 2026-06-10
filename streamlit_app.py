from __future__ import annotations

import os
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
STREAMLIT_ONLY_SENDER_EMAIL = "emurugayathri@gmail.com"


def _apply_streamlit_secrets_to_env() -> None:
    """Expose Streamlit secrets as env vars before Django settings are loaded."""
    for key in (
        "SECRET_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "APIFY_API_KEY",
        "APOLLO_API_KEY",
        "EMAIL_SENDING_ENABLED",
        "EMAIL_SENDING_PAUSED",
        "SEND_ATTACH_RESUME",
        "DEFAULT_RESUME_PATH",
        "SENDER_EMAIL",
        "SENDER_APP_PASSWORD",
        "SENDER_DISPLAY_NAME",
        "SMTP_HOST",
        "SMTP_PORT",
        "MICROSOFT_GRAPH_CLIENT_ID",
        "MICROSOFT_GRAPH_TENANT",
        "MICROSOFT_GRAPH_SCOPES",
    ):
        try:
            value = st.secrets.get(key, None)
        except Exception:
            return
        if value is not None and str(value).strip():
            os.environ[key] = str(value).strip()


@st.cache_resource
def _setup_django() -> bool:
    _apply_streamlit_secrets_to_env()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SENDER_ONLY_EMAIL", STREAMLIT_ONLY_SENDER_EMAIL)
    os.environ.setdefault("SENDER_EMAIL", STREAMLIT_ONLY_SENDER_EMAIL)
    os.environ.setdefault("SEND_ATTACH_RESUME", "1")
    import django

    django.setup()
    _bootstrap_hosted_database()
    return True


def _bootstrap_hosted_database() -> None:
    if os.getenv("STREAMLIT_AUTO_MIGRATE", "1").strip().lower() in {"1", "true", "yes", "on"}:
        from django.core.management import call_command

        call_command("migrate", interactive=False, verbosity=0)

    from core.models import AppSetting, SenderAccount

    AppSetting.get_solo()
    sender_email = os.getenv("SENDER_EMAIL", STREAMLIT_ONLY_SENDER_EMAIL).strip().lower()
    app_password = (
        os.getenv("SENDER_APP_PASSWORD", "").strip()
        or os.getenv("GMAIL_APP_PASSWORD", "").strip()
        or os.getenv("EMAIL_APP_PASSWORD", "").strip()
    )
    if not sender_email or not app_password:
        return

    sender, _ = SenderAccount.objects.get_or_create(
        email=sender_email,
        defaults={
            "display_name": os.getenv("SENDER_DISPLAY_NAME", "Gayathri Emuru").strip() or "Gayathri Emuru",
            "app_password": app_password,
            "is_active": True,
            "is_paused": False,
            "daily_limit": 50,
        },
    )
    changed = []
    if sender.app_password != app_password:
        sender.app_password = app_password
        changed.append("app_password")
    display_name = os.getenv("SENDER_DISPLAY_NAME", sender.display_name or "Gayathri Emuru").strip()
    if display_name and sender.display_name != display_name:
        sender.display_name = display_name[:255]
        changed.append("display_name")
    if not sender.is_active:
        sender.is_active = True
        changed.append("is_active")
    if sender.is_paused:
        sender.is_paused = False
        sender.paused_until = None
        sender.pause_reason = ""
        changed.extend(["is_paused", "paused_until", "pause_reason"])
    if changed:
        sender.save(update_fields=[*changed, "updated_at"])


_setup_django()

from core.models import GeneratedEmail  # noqa: E402
from core.services.email_ai_settings_service import get_email_ai_generation_settings  # noqa: E402
from core.services.email_composition_service import build_full_email_body  # noqa: E402
from core.services.email_sending_control_service import get_email_sending_state  # noqa: E402
from core.services.inbox_monitor_service import (  # noqa: E402
    DEFAULT_INBOX_MONITOR_MAX_MESSAGES,
    build_inbox_monitor_context,
    scan_and_store_inbox_events,
    scan_inbox_monitor,
)
from core.services.linkedin_post_outreach_service import (  # noqa: E402
    build_linkedin_post_review_history,
    create_linkedin_post_review_batch_from_rows,
    run_linkedin_post_outreach,
)
from core.services.manual_job_email_service import (  # noqa: E402
    build_manual_job_email_review_context,
    create_manual_job_email_batch,
    run_manual_job_email_generation_for_token,
    send_manual_job_email_batch,
    update_manual_job_email_recipient,
)
from core.utils import safe_str  # noqa: E402


st.set_page_config(page_title="LinkedIn Email Review", page_icon="✉️", layout="wide")


def _success(message: str) -> None:
    st.session_state["last_message"] = {"kind": "success", "text": message}


def _error(message: str) -> None:
    st.session_state["last_message"] = {"kind": "error", "text": message}


def _show_last_message() -> None:
    message = st.session_state.pop("last_message", None)
    if not message:
        return
    if message.get("kind") == "error":
        st.error(message.get("text", "Something went wrong."))
    else:
        st.success(message.get("text", "Done."))


def _save_generated_email(job_id: int, subject: str, body: str) -> None:
    generated = GeneratedEmail.objects.select_related("job_posting").get(job_posting_id=int(job_id))
    generated.subject = safe_str(subject).strip()[:500]
    generated.body = safe_str(body).strip()
    generated.edited_manually = True
    generated.generation_status = GeneratedEmail.GenerationStatus.GENERATED
    generated.save(update_fields=["subject", "body", "edited_manually", "generation_status", "updated_at"])


def _make_manual_rows(row_count: int) -> tuple[list[str], list[str], list[str]]:
    names, emails, job_texts = [], [], []
    for index in range(row_count):
        names.append(safe_str(st.session_state.get(f"manual_name_{index}", "")).strip())
        emails.append(safe_str(st.session_state.get(f"manual_email_{index}", "")).strip())
        job_texts.append(safe_str(st.session_state.get(f"manual_text_{index}", "")).strip())
    return names, emails, job_texts


def _render_status_bar() -> None:
    ai = get_email_ai_generation_settings()
    sending = get_email_sending_state()
    cols = st.columns(4)
    cols[0].metric("Provider", ai.get("provider_label") or ai.get("provider") or "-")
    cols[1].metric("Model", ai.get("model") or "-")
    cols[2].metric("OpenAI key", "set" if ai.get("openai_configured") else "missing")
    cols[3].metric("Sending", "ON" if sending.get("effective_enabled") else "OFF")
    st.caption(f"Real sends from this Streamlit app are limited to {STREAMLIT_ONLY_SENDER_EMAIL}.")
    st.caption("Resume attachment is required for sends.")


def _status_label(status: str) -> str:
    status = safe_str(status).strip().lower()
    if status == "reply":
        return "Human / recruiter message"
    if status == "blocked":
        return "Block warning"
    if status == "bounce":
        return "Hard bounce"
    return "System notice"


def _render_inbox_monitor() -> None:
    st.subheader("Inbox Monitor")
    st.caption("Scan the remote sender inbox, store reply/bounce events, and review latest activity.")

    context = build_inbox_monitor_context()
    c1, c2, c3 = st.columns([1, 1, 2])
    max_messages = c1.number_input(
        "Recent emails",
        min_value=1,
        max_value=500,
        value=int(context.get("default_max_messages") or DEFAULT_INBOX_MONITOR_MAX_MESSAGES),
        step=25,
    )
    store_events = c2.checkbox("Scan + store", value=True)
    c3.metric("Active inboxes", context.get("active_account_count", 0))

    if st.button("Scan Now", type="primary"):
        try:
            with st.spinner("Scanning inbox..."):
                result = (
                    scan_and_store_inbox_events(max_messages=int(max_messages))
                    if store_events
                    else scan_inbox_monitor(max_messages=int(max_messages))
                )
            st.session_state["latest_inbox_result"] = result
            _success("Inbox scan finished.")
            st.rerun()
        except Exception as exc:
            _error(str(exc))
            st.rerun()

    result = st.session_state.get("latest_inbox_result")
    if not result:
        st.info("Click Scan Now to load inbox activity.")
        return

    totals = result.get("totals") or {}
    metrics = st.columns(6)
    metrics[0].metric("Accounts Checked", f"{totals.get('ok', 0)}/{totals.get('accounts', 0)}")
    metrics[1].metric("Replies", totals.get("reply", 0))
    metrics[2].metric("Block Warnings", totals.get("blocked", 0))
    metrics[3].metric("Hard Bounces", totals.get("bounce", 0))
    metrics[4].metric("Unavailable", totals.get("unavailable", 0))
    metrics[5].metric("Returned Emails", result.get("returned_message_count", len(result.get("messages") or [])))

    stored = result.get("stored") or {}
    if stored:
        st.caption(
            f"Stored: {stored.get('created', 0)} new, {stored.get('updated', 0)} updated, "
            f"{stored.get('matched', 0)} matched, {stored.get('reply_stops', 0)} reply stops."
        )
    st.caption(f"Last scan {result.get('checked_at', '-')} | {result.get('duration_ms', 0)} ms")

    left, right = st.columns([0.8, 2.2])
    with left:
        st.markdown("**Mailbox Health**")
        accounts = result.get("accounts") or []
        if not accounts:
            st.warning("No active sender inbox found. Add `SENDER_APP_PASSWORD` in Streamlit secrets.")
        for account in accounts:
            with st.container(border=True):
                st.markdown(f"**{account.get('account', '')}**")
                if account.get("ok"):
                    st.success("OK")
                else:
                    st.error(account.get("error") or "Unavailable")
                counts = account.get("counts") or {}
                st.caption(
                    f"{counts.get('reply', 0)} replies | {counts.get('blocked', 0)} blocked | "
                    f"{counts.get('bounce', 0)} bounces | {counts.get('notice', 0)} notices"
                )
                st.caption(f"{account.get('checked_at', '-')} | {account.get('duration_ms', 0)} ms")

    with right:
        st.markdown("**Latest Inbox Activity**")
        messages = result.get("messages") or []
        if not messages:
            st.info("No inbox messages found.")
        for index, message in enumerate(messages):
            with st.container(border=True):
                head = st.columns([2.5, 1])
                head[0].caption(f"{message.get('account', '')} | {message.get('from', '')}")
                head[1].caption(_status_label(message.get("status", "")))
                st.markdown(f"**{message.get('subject') or '(no subject)'}**")
                unread = " | unread" if message.get("unread") else ""
                st.caption(f"{message.get('date', '-')}{unread}")
                st.text_area(
                    "Snippet",
                    safe_str(message.get("snippet")).strip(),
                    height=110,
                    disabled=True,
                    key=f"inbox_msg_{index}_{safe_str(message.get('key'))[:60]}",
                    label_visibility="collapsed",
                )


def _render_create_batch() -> None:
    st.subheader("Create Review Batch")
    source_mode = st.radio(
        "Input type",
        ["LinkedIn post URLs", "Manual paste"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if source_mode == "LinkedIn post URLs":
        urls = st.text_area(
            "LinkedIn post URLs",
            height=150,
            placeholder="Paste one or more LinkedIn post URLs here.",
        )
        col_a, col_b, col_c = st.columns([1, 1, 2])
        find_emails = col_a.checkbox("Find emails", value=True)
        ai_extract = col_b.checkbox("AI extract details", value=True)
        if col_c.button("Extract and Create Batch", type="primary", use_container_width=True):
            try:
                result = run_linkedin_post_outreach(
                    raw_urls_text=urls,
                    find_emails=find_emails,
                    create_review_batch=True,
                    ai_extract_details=ai_extract,
                )
                batch = result.get("review_batch") or {}
                st.session_state["latest_extract_result"] = result
                if batch.get("ok") is False:
                    _error(batch.get("error") or "No review batch was created.")
                else:
                    token = safe_str(batch.get("token")).strip()
                    st.session_state["active_token"] = token
                    _success(f"Created review batch {token}. Generate drafts next.")
                    st.rerun()
            except Exception as exc:
                _error(str(exc))
                st.rerun()

        result = st.session_state.get("latest_extract_result")
        if result:
            totals = result.get("totals") or {}
            st.caption(
                f"Extracted {totals.get('extracted_posts', 0)} post(s), "
                f"found {totals.get('emails_found', 0)} email(s), "
                f"Apollo credits {totals.get('apollo_credits', 0)}."
            )
            with st.expander("Extraction rows"):
                st.dataframe(result.get("rows") or [], use_container_width=True)
        return

    row_count = st.number_input("Rows", min_value=1, max_value=20, value=1, step=1)
    for index in range(int(row_count)):
        with st.expander(f"Row {index + 1}", expanded=index == 0):
            c1, c2 = st.columns(2)
            c1.text_input("Recipient name", key=f"manual_name_{index}")
            c2.text_input("Recipient email", key=f"manual_email_{index}")
            st.text_area(
                "LinkedIn post, job description, notes, and links",
                key=f"manual_text_{index}",
                height=180,
            )

    if st.button("Create Batch", type="primary"):
        try:
            names, emails, job_texts = _make_manual_rows(int(row_count))
            result = create_manual_job_email_batch(
                names=names,
                emails=emails,
                job_texts=job_texts,
                generate_immediately=False,
            )
            if result.get("ok") is False:
                _error(result.get("error") or "No review batch was created.")
            else:
                token = safe_str(result.get("token")).strip()
                st.session_state["active_token"] = token
                _success(f"Created review batch {token}. Generate drafts next.")
            st.rerun()
        except Exception as exc:
            _error(str(exc))
            st.rerun()


def _render_history() -> None:
    history = build_linkedin_post_review_history(limit=20)
    rows = history.get("rows") or []
    if not rows:
        st.caption("No previous LinkedIn post review batches found.")
        return

    labels = [
        f"{row['token']} | {row.get('display_company') or 'LinkedIn'} | "
        f"{row.get('job_count', 0)} row(s) | sent {row.get('sent_count', 0)}"
        for row in rows
    ]
    selected = st.selectbox("Open previous batch", [""] + labels)
    if selected:
        index = labels.index(selected)
        st.session_state["active_token"] = rows[index]["token"]
        st.rerun()


def _render_review_batch(token: str) -> None:
    context = build_manual_job_email_review_context(token=token)
    rows = context.get("rows") or []
    totals = context.get("totals") or {}

    st.subheader(f"Review Batch: {token}")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    c1.metric("Rows", totals.get("jobs", 0))
    c2.metric("Ready", totals.get("ready", 0))
    c3.metric("Token", token)
    if c4.button("Generate Missing Drafts", use_container_width=True):
        try:
            result = run_manual_job_email_generation_for_token(token=token, skip_existing=True)
            _success(f"Generated {result.get('totals', {}).get('generated', 0)} draft(s).")
            st.rerun()
        except Exception as exc:
            _error(str(exc))
            st.rerun()

    if st.button("Regenerate All Drafts", type="secondary"):
        try:
            result = run_manual_job_email_generation_for_token(token=token, skip_existing=False)
            _success(f"Regenerated {result.get('totals', {}).get('generated', 0)} draft(s).")
            st.rerun()
        except Exception as exc:
            _error(str(exc))
            st.rerun()

    selected_job_ids: list[int] = []
    for row in rows:
        job = row["job"]
        generated = row.get("generated")
        title = safe_str(getattr(job, "title", "")).strip() or f"Job {job.id}"
        ready = bool(row.get("ready"))
        with st.container(border=True):
            top = st.columns([2, 1, 1])
            top[0].markdown(f"**{title}**")
            top[1].caption(f"Job ID: {job.id}")
            top[2].caption("Ready" if ready else "Needs attention")

            c1, c2 = st.columns(2)
            name = c1.text_input("Recipient name", value=row.get("name") or "", key=f"name_{job.id}")
            email = c2.text_input("Recipient email", value=row.get("email") or "", key=f"email_{job.id}")
            if st.button("Save Recipient", key=f"save_recipient_{job.id}"):
                try:
                    update_manual_job_email_recipient(token=token, job_id=job.id, name=name, email=email)
                    _success("Recipient saved.")
                    st.rerun()
                except Exception as exc:
                    _error(str(exc))
                    st.rerun()

            if row.get("source_linkedin_post_url") or row.get("source_job_url") or row.get("poster_linkedin_url"):
                links = []
                if row.get("source_linkedin_post_url"):
                    links.append(f"[LinkedIn post]({row['source_linkedin_post_url']})")
                if row.get("source_job_url"):
                    links.append(f"[Job link]({row['source_job_url']})")
                if row.get("poster_linkedin_url"):
                    links.append(f"[Poster profile]({row['poster_linkedin_url']})")
                st.markdown(" | ".join(links))

            if row.get("source_post_text") or row.get("manual_notes"):
                with st.expander("Source post and notes"):
                    if row.get("manual_notes"):
                        st.text_area("Manual notes", row["manual_notes"], height=120, disabled=True, key=f"notes_{job.id}")
                    if row.get("source_post_text"):
                        st.text_area("LinkedIn post/context", row["source_post_text"], height=180, disabled=True, key=f"source_{job.id}")

            subject_default = safe_str(row.get("subject")).strip()
            body_default = safe_str(getattr(generated, "body", "") if generated else "").strip()
            subject = st.text_input("Subject", value=subject_default, key=f"subject_{job.id}", disabled=not generated)
            body = st.text_area("Email body", value=body_default, height=260, key=f"body_{job.id}", disabled=not generated)

            if generated:
                preview_body = build_full_email_body(
                    recipient_name=name or "there",
                    base_body=body,
                    job_linkedin_url=safe_str(job.normalized_linkedin_url) or safe_str(job.linkedin_url),
                    include_job_reference=False,
                )
                with st.expander("Final send preview", expanded=True):
                    st.markdown(f"**Subject:** {subject}")
                    st.text_area("Final body", preview_body, height=220, disabled=True, key=f"preview_{job.id}")

            action_cols = st.columns([1, 1, 2])
            if action_cols[0].button("Save Draft", key=f"save_draft_{job.id}", disabled=not generated):
                try:
                    _save_generated_email(job.id, subject, body)
                    _success("Draft saved.")
                    st.rerun()
                except Exception as exc:
                    _error(str(exc))
                    st.rerun()

            include = action_cols[1].checkbox(
                "Send",
                value=ready,
                disabled=not ready,
                key=f"send_{job.id}",
            )
            if include and ready:
                selected_job_ids.append(int(job.id))

            if row.get("already_real_sent_or_pending"):
                st.warning("This recipient already has a real initial or pending email. Sending is blocked.")
            elif not ready:
                st.warning("Generate a draft and confirm recipient details before sending.")

    st.divider()
    send_delay = st.number_input("Delay seconds between sends", min_value=0, max_value=3600, value=300, step=30)
    confirm_send = st.checkbox("I reviewed the selected edited drafts and want to send real emails.")
    if st.button("Send Checked Emails", type="primary", disabled=not selected_job_ids or not confirm_send):
        try:
            for job_id in selected_job_ids:
                _save_generated_email(
                    job_id,
                    st.session_state.get(f"subject_{job_id}", ""),
                    st.session_state.get(f"body_{job_id}", ""),
                )
            result = send_manual_job_email_batch(token=token, job_ids=selected_job_ids, delay_seconds=int(send_delay))
            sent = result.get("totals", {}).get("emails_sent", 0)
            failed = result.get("totals", {}).get("emails_failed", 0)
            _success(f"Send finished: {sent} sent, {failed} failed.")
            st.session_state["latest_send_result"] = result
            st.rerun()
        except Exception as exc:
            _error(str(exc))
            st.rerun()

    if st.session_state.get("latest_send_result"):
        with st.expander("Latest send result"):
            st.json(st.session_state["latest_send_result"])


def main() -> None:
    st.title("LinkedIn Job Post Email Review")
    st.caption("Create drafts, edit every subject and body, send approved emails, and monitor the inbox remotely.")
    _show_last_message()
    _render_status_bar()

    with st.sidebar:
        section = st.radio("View", ["Email Review", "Inbox Monitor"], label_visibility="collapsed")
        st.divider()
        st.header("Batch")
        token = st.text_input("Review token", value=safe_str(st.session_state.get("active_token", "")).strip())
        if token != st.session_state.get("active_token"):
            st.session_state["active_token"] = token.strip()
        _render_history()
        if st.button("New batch"):
            st.session_state["active_token"] = ""
            st.rerun()

    if section == "Inbox Monitor":
        _render_inbox_monitor()
    else:
        active_token = safe_str(st.session_state.get("active_token", "")).strip()
        if active_token:
            _render_review_batch(active_token)
        else:
            _render_create_batch()


if __name__ == "__main__":
    main()
