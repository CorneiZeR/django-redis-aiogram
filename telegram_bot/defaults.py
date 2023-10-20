from typing import Dict, Union, Callable

DEFAULTS: Dict[str, Union[int, str, Callable]] = {
    # event expiration time in redis
    'REDIS_EXP_TIME': 5,
    # redis key for handling expired event
    'REDIS_EXP_KEY': 'TELEGRAM_BOT_EXP',
    # redis key for collecting messages
    'REDIS_MESSAGES_KEY': 'TELEGRAM_BOT_MESSAGE',
    # name of the module to find
    'MODULE_NAME': 'tg_router',
    # default kwargs for telegram bot
    'DEFAULT_KWARGS': lambda _: {},
    # telegram bot token
    'TOKEN': '',
    # url for redis connection
    'REDIS_URL': ''
}
