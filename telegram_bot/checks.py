from typing import Tuple, Any, Optional

from django.core.checks import CheckMessage, Error

from telegram_bot import conf


def check_settings(**kwargs: Any) -> Tuple[CheckMessage]:
    return (  # noqa
        *check_int('REDIS_EXP_TIME', 1, _min=0),
        *check_str('REDIS_EXP_KEY', 2),
        *check_str('REDIS_MESSAGES_KEY', 3),
        *check_str('TOKEN', 4),
        *check_str('REDIS_URL', 5),
        *check_str('MODULE_NAME', 6)
    )


def check_int(key: str, error: int, _min: Optional[int] = None, _max: Optional[int] = None) -> Tuple[Error]:
    """
    Check if value in settings is integer and in range.

    :param key: key in settings
    :param error: error code
    :param _min: minimum value
    :param _max: maximum value
    :return: tuple of errors
    """
    error_msg = '{key} should be an integer{greater}{less}.'.format(
        key=key,
        greater=f', greater than {_min}' if _min is not None else '',
        less=f', less than {_max}' if _max is not None else ''
    )

    value = conf.get(key)
    if isinstance(value, int):
        if _min is not None and value > _min:
            return tuple()
        if _max is not None and value < _max:
            return tuple()

    return (Error(error_msg, id=f'telegram_bot.E{str(error).zfill(3)}'),)


def check_str(key: str, error: int, _not_equal: Optional[str] = None) -> Tuple[Error]:
    """
    Check if value in settings is string and not empty.

    :param key: key in settings
    :param error: error code
    :param _not_equal: value to compare
    :return: tuple of errors
    """
    error_msg = '{key} should be a string {not_equal}.'.format(
        key=key,
        not_equal=f', not not_equal "{_not_equal}"' if _not_equal else ', not empty'
    )

    value = conf.get(key)
    if isinstance(value, str):
        if value != (_not_equal or ''):
            return tuple()

    return (Error(error_msg, id=f'telegram_bot.E{str(error).zfill(3)}'),)
