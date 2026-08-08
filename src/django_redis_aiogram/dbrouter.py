"""Send the event log to its own database, when one is configured.

Optional. The writer and the admin always name the alias explicitly, so the log
lands in the right database with no router installed at all. What the router
adds is ``migrate`` creating the table there, and third-party code reaching the
right alias through the plain manager.

Not ``routers.py``: that name already means aiogram router autodiscovery.
"""

from typing import Any

from django.db.models import Model

from django_redis_aiogram.settings import conf

APP_LABEL = 'django_redis_aiogram'


def event_log_database() -> str | None:
    """Return the configured alias for the log, or None when it lives in the default one."""
    return str(conf['EVENT_LOG_DATABASE'] or '').strip() or None


class TelegramEventLogRouter:
    """Routes this app's models to ``EVENT_LOG_DATABASE`` and nothing else anywhere."""

    def _alias_for(self, model: type[Model]) -> str | None:
        """Return the alias this app's models belong to, or None to express no opinion."""
        alias = event_log_database()
        # _meta is how Django itself asks a model which app it belongs to
        return alias if alias and model._meta.app_label == APP_LABEL else None  # noqa: SLF001

    def db_for_read(self, model: type[Model], **hints: Any) -> str | None:
        """Read this app's models from the log database."""
        return self._alias_for(model)

    def db_for_write(self, model: type[Model], **hints: Any) -> str | None:
        """Write this app's models to the log database."""
        return self._alias_for(model)

    def allow_relation(self, *objects: Model, **hints: Any) -> bool | None:
        """Express no opinion: this app owns no relations in either direction."""
        return None

    def allow_migrate(self, db: str, app_label: str, **hints: Any) -> bool | None:
        """Create the table on the log database only, and only when one is set.

        None rather than False for other apps: where somebody else's table
        belongs is not this router's decision to make.
        """
        alias = event_log_database()
        if alias is None or app_label != APP_LABEL:
            return None
        return db == alias
