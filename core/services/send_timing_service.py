from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Optional


DEFAULT_SEND_DELAY_SECONDS = 27
DEFAULT_SEND_DELAY_MAX_SECONDS = 200
SEND_DELAY_SECONDS_VAR = "SEND_DELAY_SECONDS"
SEND_DELAY_MIN_SECONDS_VAR = "SEND_DELAY_MIN_SECONDS"
SEND_DELAY_MAX_SECONDS_VAR = "SEND_DELAY_MAX_SECONDS"


def configured_send_delay_seconds() -> int:
    raw = os.getenv(SEND_DELAY_SECONDS_VAR, "").strip() or os.getenv("DEFAULT_SEND_DELAY_SECONDS", "").strip()
    try:
        return max(0, int(raw or DEFAULT_SEND_DELAY_SECONDS))
    except Exception:
        return DEFAULT_SEND_DELAY_SECONDS


def configured_send_delay_range_seconds(base_delay_seconds: Optional[int] = None) -> tuple[int, int]:
    if base_delay_seconds is not None and int(base_delay_seconds or 0) <= 0:
        return 0, 0

    base = configured_send_delay_seconds() if base_delay_seconds is None else max(0, int(base_delay_seconds or 0))
    if base <= 0:
        return 0, 0

    try:
        min_delay = max(0, int(os.getenv(SEND_DELAY_MIN_SECONDS_VAR, "") or base))
    except Exception:
        min_delay = base

    try:
        max_delay = max(min_delay, int(os.getenv(SEND_DELAY_MAX_SECONDS_VAR, "") or max(base, DEFAULT_SEND_DELAY_MAX_SECONDS)))
    except Exception:
        max_delay = max(base, DEFAULT_SEND_DELAY_MAX_SECONDS)

    return min_delay, max_delay


def randomized_send_delay_seconds(base_delay_seconds: Optional[int] = None) -> int:
    min_delay, max_delay = configured_send_delay_range_seconds(base_delay_seconds)
    if max_delay <= min_delay:
        return min_delay
    return random.randint(min_delay, max_delay)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dotenv_path() -> Path:
    return _project_root() / ".env"


def _set_dotenv_key(path: Path, key: str, value: str) -> None:
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
        current_key = line.split("=", 1)[0].strip()
        if current_key == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)

    if not found:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{key}={value}")

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def set_send_delay_range_seconds(*, min_seconds: int, max_seconds: int, persist_to_dotenv: bool = True) -> tuple[int, int]:
    min_seconds = max(0, int(min_seconds or 0))
    max_seconds = max(min_seconds, int(max_seconds or 0))

    os.environ[SEND_DELAY_SECONDS_VAR] = str(min_seconds)
    os.environ[SEND_DELAY_MIN_SECONDS_VAR] = str(min_seconds)
    os.environ[SEND_DELAY_MAX_SECONDS_VAR] = str(max_seconds)

    if persist_to_dotenv:
        path = _dotenv_path()
        _set_dotenv_key(path, SEND_DELAY_SECONDS_VAR, str(min_seconds))
        _set_dotenv_key(path, SEND_DELAY_MIN_SECONDS_VAR, str(min_seconds))
        _set_dotenv_key(path, SEND_DELAY_MAX_SECONDS_VAR, str(max_seconds))

    return min_seconds, max_seconds
