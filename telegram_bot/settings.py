from typing import Dict, Any

from django.conf import settings

from telegram_bot.defaults import DEFAULTS


class Settings:
    """Shadow Django's settings with a little logic"""
    @property
    def settings(self) -> Dict[str, Any]:
        return {**DEFAULTS, **getattr(settings, 'TELEGRAM_BOT', {})}

    @property
    def REDIS_EXP_TIME(self) -> int:  # noqa
        return self.settings['REDIS_EXP_TIME']

    @property
    def REDIS_EXP_KEY(self) -> str:  # noqa
        return self.settings['REDIS_EXP_KEY']

    @property
    def REDIS_MESSAGES_KEY(self) -> str:  # noqa
        return self.settings['REDIS_MESSAGES_KEY']

    @property
    def TOKEN(self) -> str:  # noqa
        return self.settings['TOKEN']

    @property
    def REDIS_URL(self) -> str:  # noqa
        return self.settings['REDIS_URL']

    @property
    def MODULE_NAME(self) -> str:  # noqa
        return self.settings['MODULE_NAME']


conf = Settings()
