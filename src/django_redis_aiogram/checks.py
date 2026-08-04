"""Django system checks for the package settings.

Every check is a row in :data:`CHECKS`: an id, the setting it guards and a rule.
The id is spelled out in the row, so grepping ``E019`` finds both the check and
the ``docs/wiki/Settings.md`` entry that explains it.

Check ids moved from ``telegram_bot.EXXX`` to ``django_redis_aiogram.EXXX`` in
2.0; update ``SILENCED_SYSTEM_CHECKS`` if you silenced any of them.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, fields
from functools import partial
from typing import Any

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import UpdateType
from aiogram.fsm.storage.base import BaseStorage
from django.core.checks import CheckMessage, Error
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import DeliveryKind, SerializerKind, StorageKind, UpdateMode, choices
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf
from django_redis_aiogram.throttling import KNOWN_RATE_LIMIT_KEYS

DELIVERY_CHOICES = choices(DeliveryKind)
MODE_CHOICES = choices(UpdateMode)
SERIALIZER_CHOICES = choices(SerializerKind)

_STORAGE_CHOICES = choices(StorageKind)
_ID_PREFIX = 'django_redis_aiogram'


@dataclass(frozen=True)
class Problem:
    """What a rule found: the tail of the message, and where it belongs.

    ``key`` names the setting to blame when it is not the one the check guards —
    one webhook rule reports against both WEBHOOK_URL and WEBHOOK_SECRET.
    """

    message: str
    key: str | None = None
    hint: str | None = None


Validator = Callable[[str], list[Problem]]


@dataclass(frozen=True)
class Check:
    """One row of the registry: the id it reports under, the setting, the rule.

    An id starting with ``W`` reports as a warning, so it cannot fail
    ``manage.py check``; anything else reports as an error.
    """

    code: str
    key: str
    validate: Validator

    def run(self) -> list[CheckMessage]:
        """Turn everything the rule found into Django check messages."""
        return [self._message(problem) for problem in self.validate(self.key)]

    def _message(self, problem: Problem) -> CheckMessage:
        """Label one problem with the setting it is about and this row's id."""
        key = self.key if problem.key is None else problem.key
        # an empty key means the check is about the settings dict as a whole
        label = f"{SETTINGS_NAME}['{key}']" if key else SETTINGS_NAME
        report = CheckWarning if self.code.startswith('W') else Error
        return report(f'{label} {problem.message}', hint=problem.hint, id=f'{_ID_PREFIX}.{self.code}')


def _a_boolean(key: str) -> list[Problem]:
    """Require a real bool: a non-empty string would enable whatever it names."""
    value = conf.get(key)
    if isinstance(value, bool):
        return []
    return [Problem(f'must be a boolean, got {type(value).__name__}.')]


def _an_integer(key: str, *, minimum: int | None = None) -> list[Problem]:
    """Require an integer, at or above ``minimum`` when one is given."""
    value = conf.get(key)
    # bool is a subclass of int, so it has to be rejected explicitly
    if isinstance(value, bool) or not isinstance(value, int):
        return [Problem(f'must be an integer, got {type(value).__name__}.')]
    if minimum is not None and value < minimum:
        return [Problem(f'must be >= {minimum}, got {value}.')]
    return []


def _a_string(key: str, *, allowed: Collection[str] | None = None) -> list[Problem]:
    """Require a string, one of ``allowed`` when the setting is an enumeration."""
    value = conf.get(key)
    if not isinstance(value, str):
        return [Problem(f'must be a string, got {type(value).__name__}.')]
    if allowed is not None and value not in allowed:
        return [Problem(f'must be one of {sorted(allowed)}, got {value!r}.')]
    return []


def _a_callable(key: str) -> list[Problem]:
    """Require something callable."""
    value = conf.get(key)
    if callable(value):
        return []
    return [Problem(f'must be callable, got {type(value).__name__}.')]


def _a_mapping(key: str) -> list[Problem]:
    """Require a mapping."""
    value = conf.get(key)
    if isinstance(value, Mapping):
        return []
    return [Problem(f'must be a mapping, got {type(value).__name__}.')]


def _known_bot_properties(key: str) -> list[Problem]:
    """Reject names ``DefaultBotProperties`` does not have, which it would drop."""
    value = conf.get(key)
    if not isinstance(value, Mapping):
        return []
    known = {field.name for field in fields(DefaultBotProperties)}
    # keys may be anything a project typed into settings, so stringify before joining
    unknown = sorted(str(name) for name in value if name not in known)
    if not unknown:
        return []
    return [Problem(f'has unknown properties: {", ".join(unknown)}. Known: {", ".join(sorted(known))}.')]


def _importable_storage(key: str) -> list[Problem]:
    """Resolve a dotted path here, so a typo fails before the first message."""
    value = conf.get(key)
    if not isinstance(value, str):
        return []
    if value in _STORAGE_CHOICES:
        return []
    if '.' not in value:
        return [Problem(f"must be 'redis', 'memory', or a dotted path, got {value!r}.")]
    try:
        storage = import_string(value)
    except ImportError as error:
        return [Problem(f'cannot be imported: {error}')]
    if not (isinstance(storage, type) and issubclass(storage, BaseStorage)):
        return [Problem(f'must point to a BaseStorage subclass, got {value!r}.')]
    return []


def _sane_rate_limits(key: str) -> list[Problem]:
    """Require known budget names holding non-negative numbers."""
    value = conf.get(key)
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [Problem(f'must be a mapping or None, got {type(value).__name__}.')]
    unknown = sorted(str(name) for name in value if name not in KNOWN_RATE_LIMIT_KEYS)
    if unknown:
        known = ', '.join(sorted(KNOWN_RATE_LIMIT_KEYS))
        return [Problem(f'has unknown keys: {", ".join(unknown)}. Known: {known}.')]
    for name, rate in value.items():
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            return [Problem(f'{name} must be a non-negative number, got {rate!r}.')]
    return []


def _readable_serializer(key: str) -> list[Problem]:
    """Refuse to write pickle the reader would throw away: sends would vanish."""
    # coerced like the reader coerces it: from the environment this is a string
    if conf.get(key) != SerializerKind.PICKLE:
        return []
    try:
        # coerced like the reader coerces it: from the environment this is a string
        allowed = coerce_bool(conf.get('ALLOW_PICKLE'), f"{SETTINGS_NAME}['ALLOW_PICKLE']")
    except ImproperlyConfigured:
        # unreadable is E017's finding; this check cannot say anything about it
        return []
    if allowed:
        return []
    return [
        Problem(
            "is 'pickle' while ALLOW_PICKLE is False, so queued messages would be "
            'written and then refused on read. Set ALLOW_PICKLE to True, or use '
            "the 'json' serializer.",
        )
    ]


def _serviceable_webhook(key: str) -> list[Problem]:
    """Reject a webhook Telegram cannot reach, or one anybody could post to."""
    url = str(conf.get(key) or '').strip()
    webhook_mode = str(conf.get('MODE') or '').strip().lower() == UpdateMode.WEBHOOK
    if not url:
        if webhook_mode:
            return [
                Problem(
                    "is required when MODE is 'webhook': Telegram has to be told where to "
                    "post updates. Switch MODE back to 'polling' if you cannot serve one.",
                )
            ]
        return []

    problems: list[Problem] = []
    if not str(conf.get('WEBHOOK_SECRET') or '').strip():
        problems.append(
            Problem(
                'is required when WEBHOOK_URL is set: the view compares it with the header '
                'Telegram echoes back, and without it anyone who finds the URL can feed '
                'your bot updates.',
                key='WEBHOOK_SECRET',
            )
        )
    if not url.startswith('https://'):
        problems.append(Problem(f'must be https, got {url!r} — Telegram refuses anything else.'))
    return problems


def _known_update_types(key: str) -> list[Problem]:
    """Require a real collection: a string would reach Telegram as single characters."""
    allowed = conf.get(key)
    if not allowed:
        return []
    if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Collection):
        return [Problem(f'must be a list or tuple of update types, got {type(allowed).__name__}.')]

    known = {member.value for member in UpdateType}
    # anything unhashable would raise out of the membership test below, so the
    # type is settled first and reported by repr rather than by value
    invalid = [repr(name) for name in allowed if not isinstance(name, str)]
    invalid += [repr(name) for name in allowed if isinstance(name, str) and name not in known]
    if invalid:
        return [
            Problem(f'contains update types Telegram does not have: {sorted(invalid)}. Valid ones are {sorted(known)}.')
        ]
    return []


def _known_keys(_key: str) -> list[Problem]:
    """Warn about keys nothing reads: settings keeps them, so a typo is silent."""
    # a non-string key would raise out of join and sorting mixed types raises
    # too, so everything unknown is rendered through repr's eyes first
    unknown = sorted(repr(key) for key in set(conf) - set(DEFAULTS))
    if not unknown:
        return []
    return [
        Problem(
            f'contains unknown keys: {", ".join(unknown)}.',
            hint=f'Known keys are: {", ".join(sorted(DEFAULTS))}.',
        )
    ]


def _filled_in_when_enabled(key: str, *, hint: str) -> list[Problem]:
    """Warn, never error, when an enabled bot has nothing to connect with.

    A project may legitimately boot without credentials — during migrations or
    image builds — so this must not be able to fail ``manage.py check``.
    """
    if not conf['ENABLED'] or str(conf.get(key) or '').strip():
        return []
    return [Problem('is empty while the bot is enabled.', hint=hint)]


CHECKS: tuple[Check, ...] = (
    Check('E001', 'ENABLED', _a_boolean),
    Check('E002', 'AUTODISCOVER', _a_boolean),
    Check('E003', 'RAISE_EXCEPTION', _a_boolean),
    Check('E017', 'ALLOW_PICKLE', _a_boolean),
    Check('E004', 'TOKEN', _a_string),
    Check('E005', 'REDIS_URL', _a_string),
    Check('E006', 'MODULE_NAME', _a_string),
    Check('E007', 'REDIS_MESSAGES_KEY', _a_string),
    Check('E021', 'WORKER_NAME', _a_string),
    Check('E008', 'REDIS_EXP_KEY', _a_string),
    Check('E009', 'DELIVERY', partial(_a_string, allowed=DELIVERY_CHOICES)),
    Check('E010', 'SERIALIZER', partial(_a_string, allowed=SERIALIZER_CHOICES)),
    Check('E011', 'FSM_STORAGE', _a_string),
    Check('E012', 'MAX_RETRIES', partial(_an_integer, minimum=1)),
    Check('E013', 'REDIS_EXP_TIME', partial(_an_integer, minimum=1)),
    Check('E014', 'BLPOP_TIMEOUT', partial(_an_integer, minimum=1)),
    Check('E023', 'HEARTBEAT_INTERVAL', partial(_an_integer, minimum=1)),
    Check('E024', 'HEALTHCHECK_MAX_QUEUE', partial(_an_integer, minimum=0)),
    Check('E028', 'MODE', partial(_a_string, allowed=MODE_CHOICES)),
    Check('E025', 'WEBHOOK_URL', _a_string),
    Check('E026', 'WEBHOOK_SECRET', _a_string),
    Check('E027', 'WEBHOOK_URL', _serviceable_webhook),
    Check('E029', 'WEBHOOK_ALLOWED_UPDATES', _known_update_types),
    Check('E015', 'DEFAULT_KWARGS', _a_callable),
    Check('E016', 'DEFAULT_BOT_PROPERTIES', _a_mapping),
    Check('E018', 'DEFAULT_BOT_PROPERTIES', _known_bot_properties),
    Check('E020', 'RATE_LIMIT', _sane_rate_limits),
    Check('E022', 'SERIALIZER', _readable_serializer),
    Check('E019', 'FSM_STORAGE', _importable_storage),
    Check('W003', '', _known_keys),
    Check(
        'W001',
        'TOKEN',
        partial(
            _filled_in_when_enabled,
            hint='Set it, or set ENABLED to False in processes that never reach Telegram.',
        ),
    ),
    Check(
        'W002',
        'REDIS_URL',
        partial(
            _filled_in_when_enabled,
            hint='Set it, or set ENABLED to False in processes that never reach Redis.',
        ),
    ),
)


def check_settings(**kwargs: Any) -> list[CheckMessage]:
    """Run every registered check and return everything it reported."""
    return [message for check in CHECKS for message in check.run()]
