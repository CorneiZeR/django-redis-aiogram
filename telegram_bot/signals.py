from importlib import import_module

from django.apps import apps

from telegram_bot import conf


def autodiscover_tg_routers():
    """Automatic search and initialization of 'tg_router' functions (by default)"""
    for app_config in apps.get_app_configs():
        try:
            # Name of the module containing 'tg_router', e.g. 'myapp.tg_router'
            routers_module_name = f"{app_config.name}.{conf.MODULE_NAME}"
            import_module(routers_module_name)

        except ImportError:  # tg_router not exists in that app
            pass
