import sys
from datetime import datetime

from core.models import SystemLog


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def console_log(message: str, level: str = "INFO"):
    print(f"[{_now_text()}] [{level.upper()}] {message}", flush=True, file=sys.stdout)


def log_system_event(event_type: str, message: str, job_posting=None, level: str = "INFO"):
    company = ""
    title = ""

    if job_posting is not None:
        company = getattr(job_posting, "company", "") or ""
        title = getattr(job_posting, "title", "") or ""

    if company or title:
        console_log(f"{event_type} | {company} | {title} | {message}", level=level)
    else:
        console_log(f"{event_type} | {message}", level=level)

    return SystemLog.objects.create(
        event_type=event_type,
        message=message,
        job_posting=job_posting,
    )


def log_step_start(step_name: str, extra: str = ""):
    message = f"START | {step_name}"
    if extra:
        message += f" | {extra}"
    console_log(message, level="INFO")


def log_step_progress(step_name: str, extra: str = ""):
    message = f"PROGRESS | {step_name}"
    if extra:
        message += f" | {extra}"
    console_log(message, level="INFO")


def log_step_success(step_name: str, extra: str = ""):
    message = f"SUCCESS | {step_name}"
    if extra:
        message += f" | {extra}"
    console_log(message, level="SUCCESS")


def log_step_warning(step_name: str, extra: str = ""):
    message = f"WARNING | {step_name}"
    if extra:
        message += f" | {extra}"
    console_log(message, level="WARNING")


def log_step_error(step_name: str, extra: str = ""):
    message = f"ERROR | {step_name}"
    if extra:
        message += f" | {extra}"
    console_log(message, level="ERROR")
