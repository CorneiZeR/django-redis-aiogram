"""The Django app that hooks this package into a project's startup.

Importing this module has to stay free of side effects: Django imports it while
the app registry is still being populated, before settings are safe to read.
Everything that needs configuration happens in ``ready()``.
"""

import logging

from django.apps import AppConfig
from django.core.checks import register

logger = logging.getLogger('django_redis_aiogram')


class TelegramBotAppConfig(AppConfig):
    """Registers the system checks and imports every app's router module."""

    name = 'django_redis_aiogram'
    label = 'django_redis_aiogram'
    verbose_name = 'django-redis-aiogram'
    # app-local, so it does not touch the project's DEFAULT_AUTO_FIELD
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        """Register the checks and autodiscover routers, unless disabled here."""
        # deferred: apps.py is imported while the app registry is still loading
        from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf  # noqa: PLC0415 - as above

        # parsed, not truthiness-tested: 'false' has to disable startup the same
        # way it disables sending, otherwise the two disagree
        if not coerce_bool(conf['ENABLED'], f"{SETTINGS_NAME}['ENABLED']"):
            logger.debug('django-redis-aiogram is disabled in this process')
            return

        # after the gate: checks are the only reason a disabled boot would pay
        # for anything beyond the settings module
        from django_redis_aiogram.checks import check_settings  # noqa: PLC0415 - only when enabled

        register(check_settings)

        if coerce_bool(conf['AUTODISCOVER'], f"{SETTINGS_NAME}['AUTODISCOVER']"):
            from django_redis_aiogram.routers import autodiscover_tg_routers  # noqa: PLC0415 - only when enabled

            autodiscover_tg_routers()
