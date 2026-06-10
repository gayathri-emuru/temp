import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from django.conf import settings


SCRIPT_NAME = "test_new_script.py"


def _script_path() -> Path:
    return Path(settings.BASE_DIR) / SCRIPT_NAME


def _log_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / "script_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _latest_log_path() -> Path:
    return _log_dir() / "test_new_script_latest.log"


def _latest_meta_path() -> Path:
    return _log_dir() / "test_new_script_latest.meta"


def launch_test_new_script():
    script_path = _script_path()
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    log_path = _latest_log_path()
    meta_path = _latest_meta_path()

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(f"[{started_at}] Starting {SCRIPT_NAME}\n")
        log_file.write(f"Python: {sys.executable}\n")
        log_file.write(f"Script: {script_path}\n")
        log_file.write("-" * 80 + "\n")
        log_file.flush()

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"

        process = subprocess.Popen(
            [sys.executable, "-u", str(script_path)],
            cwd=str(settings.BASE_DIR),
            stdout=log_file,
            stderr=log_file,
            text=True,
            creationflags=creationflags,
            env=child_env,
        )

    with open(meta_path, "w", encoding="utf-8") as meta_file:
        meta_file.write(f"pid={process.pid}\n")
        meta_file.write(f"started_at={started_at}\n")
        meta_file.write(f"log_path={log_path}\n")

    return {
        "script_path": str(script_path),
        "pid": process.pid,
        "started_at": started_at,
        "log_path": str(log_path),
        "success": True,
    }


def get_test_new_script_status():
    log_path = _latest_log_path()
    meta_path = _latest_meta_path()

    meta = {
        "pid": "",
        "started_at": "",
        "log_path": str(log_path),
    }

    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                meta[key.strip()] = value.strip()

    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")

    return {
        "script_path": str(_script_path()),
        "pid": meta.get("pid", ""),
        "started_at": meta.get("started_at", ""),
        "log_path": meta.get("log_path", str(log_path)),
        "log_text": log_text,
        "has_log": log_path.exists(),
    }
