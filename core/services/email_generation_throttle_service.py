from __future__ import annotations

import os
import time
from typing import Callable

from core.utils import safe_str


DEFAULT_ANTHROPIC_BULK_DELAY_SECONDS = 12.0
DEFAULT_ANTHROPIC_RATE_LIMIT_BACKOFF_SECONDS = 65.0


def _float_env(names: tuple[str, ...], default: float) -> float:
    for name in names:
        raw = safe_str(os.getenv(name)).strip()
        if not raw:
            continue
        try:
            return max(0.0, float(raw))
        except Exception:
            continue
    return default


def _is_anthropic_provider(provider: str) -> bool:
    return safe_str(provider).strip().lower() == "anthropic"


def email_generation_batch_delay_seconds(provider: str) -> float:
    if not _is_anthropic_provider(provider):
        return 0.0
    return _float_env(
        ("ANTHROPIC_EMAIL_BATCH_DELAY_SECS", "COLD_EMAIL_BATCH_DELAY_SECS"),
        DEFAULT_ANTHROPIC_BULK_DELAY_SECONDS,
    )


def email_generation_rate_limit_backoff_seconds(provider: str) -> float:
    if not _is_anthropic_provider(provider):
        return 0.0
    return _float_env(
        ("ANTHROPIC_EMAIL_BATCH_RATE_LIMIT_BACKOFF_SECS", "COLD_EMAIL_BATCH_RATE_LIMIT_BACKOFF_SECS"),
        DEFAULT_ANTHROPIC_RATE_LIMIT_BACKOFF_SECONDS,
    )


def is_email_generation_rate_limit_error(error_text: str) -> bool:
    lowered = safe_str(error_text).lower()
    return (
        "rate_limit" in lowered
        or "rate limit" in lowered
        or "status=429" in lowered
        or '"status": 429' in lowered
    )


def sleep_between_email_generation_requests(
    provider: str,
    *,
    is_last: bool,
    run_log_path: str = "",
    log_func: Callable[[str, str], None] | None = None,
) -> float:
    if is_last:
        return 0.0

    delay_seconds = email_generation_batch_delay_seconds(provider)
    if delay_seconds <= 0:
        return 0.0

    if run_log_path and log_func:
        log_func(run_log_path, f"BATCH_THROTTLE_SLEEP provider={safe_str(provider) or 'unknown'} seconds={delay_seconds}")
    time.sleep(delay_seconds)
    return delay_seconds


def sleep_for_email_generation_rate_limit(
    provider: str,
    *,
    run_log_path: str = "",
    log_func: Callable[[str, str], None] | None = None,
) -> float:
    backoff_seconds = email_generation_rate_limit_backoff_seconds(provider)
    if backoff_seconds <= 0:
        return 0.0

    if run_log_path and log_func:
        log_func(run_log_path, f"RATE_LIMIT_BACKOFF provider={safe_str(provider) or 'unknown'} seconds={backoff_seconds}")
    time.sleep(backoff_seconds)
    return backoff_seconds
