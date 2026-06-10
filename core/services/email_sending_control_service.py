from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final


EMAIL_SENDING_ENABLED_VAR: Final[str] = "EMAIL_SENDING_ENABLED"
EMAIL_SENDING_PAUSED_VAR: Final[str] = "EMAIL_SENDING_PAUSED"
SEND_ATTACH_RESUME_VAR: Final[str] = "SEND_ATTACH_RESUME"

_TRUE_VALUES: Final[set[str]] = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: str = "0") -> bool:
    value = _control_value(name, default)
    return str(value).strip().lower() in _TRUE_VALUES


def _dotenv_value(name: str) -> str:
    try:
        path = _dotenv_path()
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def _control_value(name: str, default: str = "0") -> str:
    if "test" in sys.argv:
        return os.getenv(name, default)
    # Worker subprocesses inherit an environment snapshot at launch. Read the
    # persisted switch first so Stop/Resume changes made in the dashboard are
    # visible to already-running workers.
    persisted = _dotenv_value(name)
    if persisted != "":
        return persisted
    return os.getenv(name, default)


def get_email_sending_state() -> dict:
    """
    Returns a runtime-effective view of sending state.

    - EMAIL_SENDING_ENABLED: master safety gate (typically set in .env)
    - EMAIL_SENDING_PAUSED: dashboard kill-switch (can be toggled at runtime)
    """
    env_enabled = _env_bool(EMAIL_SENDING_ENABLED_VAR, "0")
    paused = _env_bool(EMAIL_SENDING_PAUSED_VAR, "0")
    return {
        "env_enabled": env_enabled,
        "paused": paused,
        "effective_enabled": bool(env_enabled and not paused),
    }


def get_resume_attachment_state() -> dict:
    enabled = _env_bool(SEND_ATTACH_RESUME_VAR, "1")
    return {
        "enabled": enabled,
        "raw": _control_value(SEND_ATTACH_RESUME_VAR, "1"),
    }


def is_email_sending_enabled() -> bool:
    return bool(get_email_sending_state()["effective_enabled"])


def _project_root() -> Path:
    # core/services/<file>.py -> project root is 2 parents up.
    return Path(__file__).resolve().parents[2]


def _dotenv_path() -> Path:
    return _project_root() / ".env"


def _set_dotenv_key(path: Path, key: str, value: str) -> None:
    """
    Minimal .env editor: replace KEY=... if present, otherwise append.
    Preserves unrelated lines exactly.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""

    lines = original.splitlines()
    out: list[str] = []
    found = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue

        k = line.split("=", 1)[0].strip()
        if k == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)

    if not found:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{key}={value}")

    text = "\n".join(out).rstrip() + "\n"
    path.write_text(text, encoding="utf-8")


def set_email_sending_paused(*, paused: bool, persist_to_dotenv: bool = True) -> None:
    """
    Pauses/resumes all email sending (including tests) for the current process,
    and optionally persists the pause flag to `.env` for the next restart.
    """
    os.environ[EMAIL_SENDING_PAUSED_VAR] = "1" if paused else "0"

    if not persist_to_dotenv:
        return

    dotenv_path = _dotenv_path()
    try:
        _set_dotenv_key(dotenv_path, EMAIL_SENDING_PAUSED_VAR, "1" if paused else "0")
    except Exception:
        # Never fail the request just because we couldn't persist the toggle.
        pass


def set_resume_attachment_enabled(*, enabled: bool, persist_to_dotenv: bool = True) -> None:
    """
    Enables/disables resume attachments for the current process and workers
    that read the persisted dashboard control.
    """
    os.environ[SEND_ATTACH_RESUME_VAR] = "1" if enabled else "0"

    if not persist_to_dotenv:
        return

    dotenv_path = _dotenv_path()
    try:
        _set_dotenv_key(dotenv_path, SEND_ATTACH_RESUME_VAR, "1" if enabled else "0")
    except Exception:
        pass
