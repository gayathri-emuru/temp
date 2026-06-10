import json

from django.apps import AppConfig
from django.db import connections
from django.db.backends.signals import connection_created


def _sqlite_json_valid(value):
    if value is None:
        return 0
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            return 0
    try:
        json.loads(str(value))
    except Exception:
        return 0
    return 1


def _register_sqlite_json_functions(sender, connection, **kwargs):
    if connection.vendor != "sqlite" or connection.connection is None:
        return
    try:
        connection.connection.create_function("JSON_VALID", 1, _sqlite_json_valid)
    except Exception:
        # If the runtime already provides JSON_VALID or refuses registration,
        # keep startup alive and let SQLite/Django surface any real DB error.
        pass


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        connection_created.connect(_register_sqlite_json_functions, dispatch_uid="core.sqlite_json_functions")
        for connection in connections.all():
            if connection.connection is not None:
                _register_sqlite_json_functions(sender=None, connection=connection)
