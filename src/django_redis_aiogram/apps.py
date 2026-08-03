from django.apps import AppConfig
from django.core.checks import register

from django_redis_aiogram.checks import check_settings


class TelegramBotAppConfig(AppConfig):
    name = 'django_redis_aiogram'
    label = 'django_redis_aiogram'
    verbose_name = 'django-redis-aiogram'

    def ready(self) -> None:
        from django_redis_aiogram.routers import autodiscover_tg_routers

        register(check_settings)
        autodiscover_tg_routers()
