from django_redis_aiogram import apps as base


class TelegramBotAppConfig(base.TelegramBotAppConfig):
    """Lets `INSTALLED_APPS = ['telegram_bot']` keep working.

    The base class is reached through the module rather than imported by name:
    two AppConfig subclasses in one apps.py leave Django unable to choose, and
    it falls back to the plain AppConfig, whose ready() does nothing — so
    autodiscover and the system checks would silently never run.
    """

    name = "telegram_bot"
    label = "telegram_bot"
    default = True
