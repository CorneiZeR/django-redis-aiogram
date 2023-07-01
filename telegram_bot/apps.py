from django.apps import AppConfig
from django.core.checks import register, Tags

from telegram_bot.checks import check_settings


class TelegramBotAppConfig(AppConfig):
    name = "telegram_bot"
    verbose_name = "django-redis-aiogram"

    def ready(self) -> None:
        from telegram_bot.signals import autodiscover_tg_routers
        register(Tags.security)(check_settings)
        autodiscover_tg_routers()
