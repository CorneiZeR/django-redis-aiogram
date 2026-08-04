"""Django system checks for the package settings.

Check ids moved from ``telegram_bot.EXXX`` to ``django_redis_aiogram.EXXX`` in
2.0; update ``SILENCED_SYSTEM_CHECKS`` if you silenced any of them.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import fields
from typing import Any

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import UpdateType
from aiogram.fsm.storage.base import BaseStorage
from django.core.checks import CheckMessage, Error, Warning
from django.utils.module_loading import import_string

from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.settings import SETTINGS_NAME, conf
from django_redis_aiogram.throttling import KNOWN_RATE_LIMIT_KEYS

DELIVERY_CHOICES = frozenset({'blpop', 'keyspace'})
MODE_CHOICES = frozenset({'polling', 'webhook'})
SERIALIZER_CHOICES = frozenset({'json', 'pickle'})


def _label(key: str) -> str:
    return f"{SETTINGS_NAME}['{key}']"


def _error(key: str, message: str, code: int) -> Error:
    return Error(f'{_label(key)} {message}', id=f'django_redis_aiogram.E{code:03d}')


def _warning(key: str, message: str, hint: str, code: int) -> Warning:
    return Warning(f'{_label(key)} {message}', hint=hint, id=f'django_redis_aiogram.W{code:03d}')


def check_bool(key: str, code: int) -> list[CheckMessage]:
    value = conf.get(key)
    if isinstance(value, bool):
        return []
    return [_error(key, f'must be a boolean, got {type(value).__name__}.', code)]


def check_int(key: str, code: int, minimum: int | None = None) -> list[CheckMessage]:
    value = conf.get(key)
    # bool is a subclass of int, so it has to be rejected explicitly
    if isinstance(value, bool) or not isinstance(value, int):
        return [_error(key, f'must be an integer, got {type(value).__name__}.', code)]
    if minimum is not None and value < minimum:
        return [_error(key, f'must be >= {minimum}, got {value}.', code)]
    return []


def check_str(key: str, code: int, choices: Collection[str] | None = None) -> list[CheckMessage]:
    value = conf.get(key)
    if not isinstance(value, str):
        return [_error(key, f'must be a string, got {type(value).__name__}.', code)]
    if choices is not None and value not in choices:
        return [_error(key, f'must be one of {sorted(choices)}, got {value!r}.', code)]
    return []


def check_callable(key: str, code: int) -> list[CheckMessage]:
    value = conf.get(key)
    if callable(value):
        return []
    return [_error(key, f'must be callable, got {type(value).__name__}.', code)]


def check_mapping(key: str, code: int) -> list[CheckMessage]:
    value = conf.get(key)
    if isinstance(value, Mapping):
        return []
    return [_error(key, f'must be a mapping, got {type(value).__name__}.', code)]


def check_bot_properties(code: int) -> list[CheckMessage]:
    value = conf.get('DEFAULT_BOT_PROPERTIES')
    if not isinstance(value, Mapping):
        return []
    known = {field.name for field in fields(DefaultBotProperties)}
    # keys may be anything a project typed into settings, so stringify before joining
    unknown = sorted(str(key) for key in value if key not in known)
    if not unknown:
        return []
    return [
        _error(
            'DEFAULT_BOT_PROPERTIES',
            f'has unknown properties: {", ".join(unknown)}. Known: {", ".join(sorted(known))}.',
            code,
        )
    ]


def check_fsm_storage(code: int) -> list[CheckMessage]:
    """Resolve a dotted path here, so a typo fails before the first message."""
    value = conf.get('FSM_STORAGE')
    if not isinstance(value, str):
        return []
    if value in {'redis', 'memory'}:
        return []
    if '.' not in value:
        return [
            _error(
                'FSM_STORAGE',
                f"must be 'redis', 'memory', or a dotted path, got {value!r}.",
                code,
            )
        ]
    try:
        storage = import_string(value)
    except ImportError as error:
        return [_error('FSM_STORAGE', f'cannot be imported: {error}', code)]
    if not (isinstance(storage, type) and issubclass(storage, BaseStorage)):
        return [
            _error('FSM_STORAGE', f'must point to a BaseStorage subclass, got {value!r}.', code)
        ]
    return []


def check_rate_limit(code: int) -> list[CheckMessage]:
    value = conf.get('RATE_LIMIT')
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [
            _error('RATE_LIMIT', f'must be a mapping or None, got {type(value).__name__}.', code)
        ]
    unknown = sorted(str(key) for key in value if key not in KNOWN_RATE_LIMIT_KEYS)
    if unknown:
        return [
            _error(
                'RATE_LIMIT',
                f'has unknown keys: {", ".join(unknown)}. '
                f'Known: {", ".join(sorted(KNOWN_RATE_LIMIT_KEYS))}.',
                code,
            )
        ]
    for key, rate in value.items():
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            return [
                _error('RATE_LIMIT', f'{key} must be a non-negative number, got {rate!r}.', code)
            ]
    return []


def check_serializer_agrees_with_reads(code: int) -> list[CheckMessage]:
    """Writing pickle while refusing to read it silently discards every message."""
    if conf.get('SERIALIZER') != 'pickle' or conf.get('ALLOW_PICKLE'):
        return []
    return [
        _error(
            'SERIALIZER',
            "is 'pickle' while ALLOW_PICKLE is False, so queued messages would be "
            'written and then refused on read. Set ALLOW_PICKLE to True, or use '
            "the 'json' serializer.",
            code,
        )
    ]


def check_webhook(code: int) -> list[CheckMessage]:
    """A webhook URL without a secret is an open door to whoever finds it."""
    url = str(conf.get('WEBHOOK_URL') or '').strip()
    webhook_mode = str(conf.get('MODE') or '').strip().lower() == 'webhook'
    if not url:
        if webhook_mode:
            return [
                _error(
                    'WEBHOOK_URL',
                    "is required when MODE is 'webhook': Telegram has to be told where to "
                    "post updates. Switch MODE back to 'polling' if you cannot serve one.",
                    code,
                )
            ]
        return []

    messages: list[CheckMessage] = []
    if not str(conf.get('WEBHOOK_SECRET') or '').strip():
        messages.append(
            _error(
                'WEBHOOK_SECRET',
                'is required when WEBHOOK_URL is set: the view compares it with the header '
                'Telegram echoes back, and without it anyone who finds the URL can feed '
                'your bot updates.',
                code,
            )
        )
    if not url.startswith('https://'):
        messages.append(
            _error(
                'WEBHOOK_URL', f'must be https, got {url!r} — Telegram refuses anything else.', code
            )
        )
    return messages


def check_allowed_updates(code: int) -> list[CheckMessage]:
    """A string here would reach Telegram as a list of single characters."""
    allowed = conf.get('WEBHOOK_ALLOWED_UPDATES')
    if not allowed:
        return []
    if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Collection):
        return [
            _error(
                'WEBHOOK_ALLOWED_UPDATES',
                f'must be a list or tuple of update types, got {type(allowed).__name__}.',
                code,
            )
        ]

    known = {member.value for member in UpdateType}
    unknown = [name for name in allowed if name not in known]
    if unknown:
        return [
            _error(
                'WEBHOOK_ALLOWED_UPDATES',
                f'contains update types Telegram does not have: {sorted(unknown)}. '
                f'Valid ones are {sorted(known)}.',
                code,
            )
        ]
    return []


def check_unknown_keys(code: int) -> list[CheckMessage]:
    unknown = sorted(set(conf) - set(DEFAULTS))
    if not unknown:
        return []
    return [
        Warning(
            f'{SETTINGS_NAME} contains unknown keys: {", ".join(unknown)}.',
            hint=f'Known keys are: {", ".join(sorted(DEFAULTS))}.',
            id=f'django_redis_aiogram.W{code:03d}',
        )
    ]


def check_credentials() -> list[CheckMessage]:
    """Warn, not error, when an enabled bot has nothing to connect with.

    A project may legitimately boot without credentials — during migrations or
    image builds — so this must not be able to fail ``manage.py check``.
    """
    if not conf['ENABLED']:
        return []
    messages: list[CheckMessage] = []
    if not str(conf.get('TOKEN') or '').strip():
        messages.append(
            _warning(
                'TOKEN',
                'is empty while the bot is enabled.',
                'Set it, or set ENABLED to False in processes that never reach Telegram.',
                1,
            )
        )
    if not str(conf.get('REDIS_URL') or '').strip():
        messages.append(
            _warning(
                'REDIS_URL',
                'is empty while the bot is enabled.',
                'Set it, or set ENABLED to False in processes that never reach Redis.',
                2,
            )
        )
    return messages


def check_settings(**kwargs: Any) -> list[CheckMessage]:
    checks: list[Callable[[], list[CheckMessage]]] = [
        lambda: check_bool('ENABLED', 1),
        lambda: check_bool('AUTODISCOVER', 2),
        lambda: check_bool('RAISE_EXCEPTION', 3),
        lambda: check_bool('ALLOW_PICKLE', 17),
        lambda: check_str('TOKEN', 4),
        lambda: check_str('REDIS_URL', 5),
        lambda: check_str('MODULE_NAME', 6),
        lambda: check_str('REDIS_MESSAGES_KEY', 7),
        lambda: check_str('WORKER_NAME', 21),
        lambda: check_str('REDIS_EXP_KEY', 8),
        lambda: check_str('DELIVERY', 9, DELIVERY_CHOICES),
        lambda: check_str('SERIALIZER', 10, SERIALIZER_CHOICES),
        lambda: check_str('FSM_STORAGE', 11),
        lambda: check_int('MAX_RETRIES', 12, minimum=1),
        lambda: check_int('REDIS_EXP_TIME', 13, minimum=1),
        lambda: check_int('BLPOP_TIMEOUT', 14, minimum=1),
        lambda: check_int('HEARTBEAT_INTERVAL', 23, minimum=1),
        lambda: check_int('HEALTHCHECK_MAX_QUEUE', 24, minimum=0),
        lambda: check_str('MODE', 28, MODE_CHOICES),
        lambda: check_str('WEBHOOK_URL', 25),
        lambda: check_str('WEBHOOK_SECRET', 26),
        lambda: check_webhook(27),
        lambda: check_allowed_updates(29),
        lambda: check_callable('DEFAULT_KWARGS', 15),
        lambda: check_mapping('DEFAULT_BOT_PROPERTIES', 16),
        lambda: check_bot_properties(18),
        lambda: check_rate_limit(20),
        lambda: check_serializer_agrees_with_reads(22),
        lambda: check_fsm_storage(19),
        lambda: check_unknown_keys(3),
        check_credentials,
    ]
    return [message for check in checks for message in check()]
