"""Resolve the package's settings, lazily, from three sources.

A project configures the package through one ``TELEGRAM_BOT`` dict in its Django
settings. Anything it leaves out is looked for in the environment, and anything
the environment leaves out comes from :mod:`django_redis_aiogram.defaults`.

Nothing here reads Django settings at import time: before 2.0 it did, which took
the whole project down — its test suite included — whenever the token or Redis
was absent.
"""

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

from django_redis_aiogram.defaults import DEFAULTS

SETTINGS_NAME = 'TELEGRAM_BOT'
ENV_PREFIX = 'DJANGO_REDIS_AIOGRAM_'

_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})
_FALSY = frozenset({'0', 'false', 'no', 'off'})

_MISSING = object()


def parse_bool(value: str, source: str) -> bool:
    """Parse a human-written boolean, rejecting anything ambiguous.

    ``source`` names the setting or variable in the error, so a typo is
    traceable to the place that holds it.
    """
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    msg = f'{source} must be one of {sorted(_TRUTHY | _FALSY)}, got {value!r}.'
    raise ImproperlyConfigured(msg)


def coerce_bool(value: object, source: str) -> bool:
    """Accept the shapes a settings file realistically holds.

    Plain bool(value) would read the string 'false' as True and quietly enable
    a bot the project meant to switch off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return parse_bool(value, source)
    msg = f'{source} must be a boolean, got {type(value).__name__}.'
    raise ImproperlyConfigured(msg)


def _from_env(key: str, default: object) -> object:
    """Read a setting from the environment, coercing it to the default's type.

    Returns the ``_MISSING`` sentinel when the variable is unset, which is what
    lets an empty string through as a deliberate value.

    Only scalars are supported: callables and containers have no sane textual
    form, so they stay settings-only.
    """
    name = ENV_PREFIX + key
    raw = os.environ.get(name)
    if raw is None:
        return _MISSING
    if isinstance(default, bool):
        return parse_bool(raw, name)
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            msg = f'{name} must be an integer, got {raw!r}.'
            raise ImproperlyConfigured(msg) from None
    if isinstance(default, str):
        return raw
    return _MISSING


class Settings(Mapping[str, Any]):
    """Package settings resolved on first access.

    Resolution order is Django settings, then environment, then defaults.
    Reading Django settings lazily is what keeps importing this package free of
    side effects, so nothing here may run at import time.
    """

    def __init__(self) -> None:
        """Start with nothing resolved; the first lookup does the work."""
        self._cache: dict[str, Any] | None = None

    def _resolve(self) -> dict[str, Any]:
        overrides = getattr(django_settings, SETTINGS_NAME, None) or {}
        if not isinstance(overrides, Mapping):
            msg = f'{SETTINGS_NAME} must be a mapping, got {type(overrides).__name__}.'
            raise ImproperlyConfigured(msg)
        resolved = dict(DEFAULTS)
        for key, default in DEFAULTS.items():
            if key in overrides:
                resolved[key] = overrides[key]
                continue
            value = _from_env(key, default)
            if value is not _MISSING:
                resolved[key] = value
        # unknown keys are kept rather than dropped; checks.py warns about them
        for key, value in overrides.items():
            resolved.setdefault(key, value)
        return resolved

    @property
    def resolved(self) -> dict[str, Any]:
        """Every setting, resolved once and then cached until reset."""
        # one read, kept local: a reset() between two reads would return None
        cache = self._cache
        if cache is None:
            cache = self._cache = self._resolve()
        return cache

    def reset(self) -> None:
        """Drop the cache, so the next read picks up changed settings."""
        self._cache = None

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401 - a setting holds whatever the project put there
        """Return one resolved setting, resolving them all on the first ask."""
        return self.resolved[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the resolved setting names."""
        return iter(self.resolved)

    def __len__(self) -> int:
        """Return how many settings are resolved."""
        return len(self.resolved)

    def __repr__(self) -> str:
        """Describe the cache without resolving it: repr() must stay cheap."""
        state = 'unresolved' if self._cache is None else f'{len(self._cache)} keys'
        return f'<{type(self).__name__} {state}>'


conf = Settings()


@dataclass(frozen=True)
class PopCeiling:
    """How long a blocking pop may actually wait, and which settings decided that.

    ``bound_by`` is a tuple because the limits can tie: ``HEARTBEAT_INTERVAL`` at 9
    beside ``REDIS_TIMEOUT`` at 10 both produce 9, and naming one of them sends an
    operator to raise it and meet the same warning again, unchanged.
    """

    seconds: int
    bound_by: tuple[str, ...]


def blpop_ceiling() -> PopCeiling:
    """Return the real cap on a blocking pop, which is not ``BLPOP_TIMEOUT`` alone.

    Three bounds meet at the consumer's pop and the smallest wins: the configured
    ``BLPOP_TIMEOUT``, the ``HEARTBEAT_INTERVAL`` — a worker that popped for longer
    than that would let its own heartbeat key expire and look dead — and one second
    inside ``REDIS_TIMEOUT``, so the pop returns before the read deadline fires.

    Lives here rather than beside the consumer because ``checks.py`` needs it too, and
    importing :mod:`django_redis_aiogram.delivery` would pull in aiogram through
    :mod:`django_redis_aiogram.api` — which is the whole reason ``manage.py check``
    costs nothing.

    ``bound_by`` is what makes a hint actionable: told only that the pop is capped, an
    operator raises ``REDIS_TIMEOUT`` when it was the heartbeat that bound it — and
    when the two tie, raising either one alone changes nothing at all.
    """
    limits = {
        'HEARTBEAT_INTERVAL': max(1, int(conf['HEARTBEAT_INTERVAL'])),
        'REDIS_TIMEOUT': max(1, max(1, int(conf['REDIS_TIMEOUT'])) - 1),
    }
    seconds = min(limits.values())
    # every setting sitting at the minimum, not the first one found: a tie means both
    # have to move, and a hint naming one of them is a round trip that achieves nothing
    return PopCeiling(seconds=seconds, bound_by=tuple(key for key, value in limits.items() if value == seconds))


def _reset_on_setting_change(
    sender: object,  # noqa: ARG001 - Django sends this to every receiver, named
    setting: str,
    **kwargs: Any,
) -> None:
    if setting == SETTINGS_NAME:
        conf.reset()


# dispatch_uid keeps autoreload from stacking duplicate receivers
setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.settings')
