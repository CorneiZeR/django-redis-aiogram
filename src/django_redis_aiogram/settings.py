import os
from collections.abc import Iterator, Mapping
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

from django_redis_aiogram.defaults import DEFAULTS

SETTINGS_NAME = 'TELEGRAM_BOT'
ENV_PREFIX = 'DJANGO_REDIS_AIOGRAM_'

_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})
_FALSY = frozenset({'0', 'false', 'no', 'off'})

_MISSING = object()


def parse_bool(value: str, source: str) -> bool:
    """Parse a human-written boolean, rejecting anything ambiguous."""
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ImproperlyConfigured(
        f'{source} must be one of {sorted(_TRUTHY | _FALSY)}, got {value!r}.'
    )


def coerce_bool(value: Any, source: str) -> bool:
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
    raise ImproperlyConfigured(f'{source} must be a boolean, got {type(value).__name__}.')


def _from_env(key: str, default: Any) -> Any:
    """Read a setting from the environment, coercing it to the default's type.

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
            raise ImproperlyConfigured(f'{name} must be an integer, got {raw!r}.') from None
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
        self._cache: dict[str, Any] | None = None

    def _resolve(self) -> dict[str, Any]:
        from django.conf import settings

        overrides = getattr(settings, SETTINGS_NAME, None) or {}
        if not isinstance(overrides, Mapping):
            raise ImproperlyConfigured(
                f'{SETTINGS_NAME} must be a mapping, got {type(overrides).__name__}.'
            )
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
        if self._cache is None:
            self._cache = self._resolve()
        return self._cache

    def reset(self) -> None:
        self._cache = None

    def __getitem__(self, key: str) -> Any:
        return self.resolved[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.resolved)

    def __len__(self) -> int:
        return len(self.resolved)

    def __repr__(self) -> str:
        state = 'unresolved' if self._cache is None else f'{len(self._cache)} keys'
        return f'<{type(self).__name__} {state}>'


conf = Settings()


def _reset_on_setting_change(sender: Any, setting: str, **kwargs: Any) -> None:
    if setting == SETTINGS_NAME:
        conf.reset()


# dispatch_uid keeps autoreload from stacking duplicate receivers
setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.settings')
