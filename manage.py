#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import socket
import socketserver
import subprocess
import sys
from pathlib import Path


def _reexec_local_python_if_available():
    if os.environ.get("COLD_EMAIL_MANAGE_REEXEC") == "1":
        return False

    base_dir = Path(__file__).resolve().parent
    candidates = [
        base_dir / ".venv38" / "Scripts" / "python.exe",
        base_dir / ".venv" / "Scripts" / "python.exe",
        base_dir / "venv" / "Scripts" / "python.exe",
    ]
    current = Path(sys.executable).resolve()
    if any(candidate.exists() and candidate.resolve() == current for candidate in candidates):
        return False

    for candidate in candidates:
        if not candidate.exists():
            continue
        os.environ["COLD_EMAIL_MANAGE_REEXEC"] = "1"
        completed = subprocess.call([str(candidate), *sys.argv])
        sys.exit(completed)
    return False


def _suppress_dev_server_socket_timeout_tracebacks():
    """
    Django's local threaded dev server can print noisy tracebacks when a browser
    leaves an idle keep-alive connection open until it times out. Those are not
    application errors, so keep real server errors visible and silence only this.
    """
    if "runserver" not in sys.argv:
        return

    original_handle_error = socketserver.BaseServer.handle_error

    def handle_error(self, request, client_address):
        exc_type, exc, _traceback = sys.exc_info()
        if exc_type is socket.timeout or isinstance(exc, socket.timeout):
            return
        return original_handle_error(self, request, client_address)

    socketserver.BaseServer.handle_error = handle_error


def main():
    """Run administrative tasks."""
    _reexec_local_python_if_available()
    _suppress_dev_server_socket_timeout_tracebacks()
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        _reexec_local_python_if_available()
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
