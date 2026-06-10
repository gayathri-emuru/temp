"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application  # noqa: E402
from django.db import connections  # noqa: E402
from django.db.migrations.executor import MigrationExecutor  # noqa: E402


def _raise_if_pending_migrations() -> None:
    """
    Fail fast when starting ASGI servers with unapplied migrations.
    Skip by setting SKIP_MIGRATION_GUARD=1.
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
application = get_asgi_application()
