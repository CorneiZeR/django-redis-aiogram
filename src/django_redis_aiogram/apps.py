import logging

from django.apps import AppConfig
from django.core.checks import register

logger = logging.getLogger("django_redis_aiogram")


class TelegramBotAppConfig(AppConfig):
    name = "django_redis_aiogram"
    label = "django_redis_aiogram"
    verbose_name = "django-redis-aiogram"

    def ready(self) -> None:
        from django_redis_aiogram.checks import check_settings
        from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

        # parsed, not truthiness-tested: 'false' has to disable startup the same
        # way it disables sending, otherwise the two disagree
        if not coerce_bool(conf["ENABLED"], f"{SETTINGS_NAME}['ENABLED']"):
            logger.debug("django-redis-aiogram is disabled in this process")
            return

        register(check_settings)

        if coerce_bool(conf["AUTODISCOVER"], f"{SETTINGS_NAME}['AUTODISCOVER']"):
            from django_redis_aiogram.routers import autodiscover_tg_routers

            autodiscover_tg_routers()
