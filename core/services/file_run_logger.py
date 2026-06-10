from datetime import datetime
from pathlib import Path
import traceback


BASE_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = BASE_DIR / "media" / "run_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(value: str) -> str:
    value = (value or "run").strip().lower()
    cleaned = []
    for ch in value:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in {" ", "-", "_"}:
            cleaned.append("_")
    text = "".join(cleaned).strip("_")
    return text or "run"


def create_run_log_path(prefix: str, company_name: str = "") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_company = _safe_name(company_name) if company_name else "all"
    filename = f"{prefix}_{safe_company}_{stamp}.log"
    return str(LOG_DIR / filename)


def append_log(log_path: str, message: str):
    line = f"[{_ts()}] {message}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def append_and_print(log_path: str, message: str):
    append_log(log_path, message)
    try:
        print(f"[{_ts()}] {message}", flush=True)
    except OSError:
        pass


def append_exception(log_path: str, label: str, exc: Exception):
    tb = traceback.format_exc()
    append_and_print(log_path, f"{label} | ERROR={exc}")
    append_and_print(log_path, tb)
