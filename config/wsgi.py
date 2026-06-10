"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402
from django.db import connections  # noqa: E402
from django.db.migrations.executor import MigrationExecutor  # noqa: E402


def _raise_if_pending_migrations() -> None:
    """
    Fail fast on runserver/gunicorn when migrations aren't applied.
    This prevents confusing runtime DB errors like "no such column".

    Skip by setting SKIP_MIGRATION_GUARD=1 (useful for emergencies).
    """
    if os.getenv("SKIP_MIGRATION_GUARD", "").strip() in {"1", "true", "yes", "on"}:
        return

    connection = connections["default"]
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        return

    pending = sorted({migration for migration, backwards in plan if not backwards})
    if not pending:
        return

    pending_str = ", ".join(f"{m.app_label}.{m.name}" for m in pending)
    raise RuntimeError(
        "Pending Django migrations detected: "
        f"{pending_str}. "
        "Run: python manage.py migrate"
    )


_raise_if_pending_migrations()
application = get_wsgi_application()
