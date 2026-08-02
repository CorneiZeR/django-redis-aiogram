from django.utils.module_loading import autodiscover_modules

from django_redis_aiogram.settings import conf


def autodiscover_tg_routers() -> None:
    """Import <app>.<MODULE_NAME> for every installed app.

    Django's helper tells "the app has no such module" apart from "the module
    exists but raised on import", so a broken router surfaces instead of being
    silently swallowed.
    """
    autodiscover_modules(conf['MODULE_NAME'])
