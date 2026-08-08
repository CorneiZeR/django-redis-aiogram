"""Pace outgoing calls to stay under Telegram's published limits.

Retrying on ``TelegramRetryAfter`` is reactive: the message has already been
refused and the bot has already been told to back off. These limits are
documented, so the sane thing is not to exceed them in the first place.

Telegram enforces three at once:

* roughly 30 messages per second overall
* about one message per second to the same chat
* 20 messages per minute to the same group or channel

A limiter belongs to one bot. Limits are per token, so a second bot must not
share this budget — which is what makes the multi-bot case work unchanged.
"""

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import RateLimitKey, choices
from django_redis_aiogram.settings import SETTINGS_NAME, conf

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

KNOWN_RATE_LIMIT_KEYS = choices(RateLimitKey)

# the shipped limits live in defaults.py; duplicating them here would drift
RATE_LIMIT_DEFAULTS: dict[str, float] = dict(DEFAULTS['RATE_LIMIT'])

# chats a bot talks to at once; beyond this the idle ones are dropped
MAX_TRACKED_CHATS = 4096
#: how many of the oldest buckets to look at before evicting one regardless
EVICTION_CANDIDATES = 8

# refilling accumulates float error, so a full bucket can land on 0.9999999999
# instead of 1.0. Without this tolerance the wait shrinks to intervals too small
# to advance the clock at all, and acquire() spins forever.
TOKEN_EPSILON = 1e-9


class TokenBucket:
    """Classic token bucket, with the clock injectable so tests stay fast."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Build a bucket that refills at ``rate`` per second."""
        if rate <= 0:
            msg = 'rate must be positive'
            raise ValueError(msg)
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._updated = clock()
        self._guard = threading.Lock()

    def is_idle(self) -> bool:
        """Report whether the bucket is back to full, and so free to forget."""
        with self._guard:
            self._refill()
            return self._tokens >= self.capacity - TOKEN_EPSILON

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        """Block until one token is available, then spend it.

        The guard is a threading lock, not an asyncio one: a limiter is shared
        per token and may be reached from more than one loop or thread, and an
        asyncio primitive binds itself to the first loop that awaits it. It is
        held only across the read-modify-write, never across the sleep.
        """
        while True:
            with self._guard:
                self._refill()
                if self._tokens >= 1 - TOKEN_EPSILON:
                    self._tokens = max(0.0, self._tokens - 1)
                    return
                wait = (1 - self._tokens) / self.rate
            await self._sleep(wait)


class RateLimiter:
    """Holds the three buckets Telegram applies to a single bot."""

    def __init__(
        self,
        overall_per_second: float = RATE_LIMIT_DEFAULTS[RateLimitKey.OVERALL_PER_SECOND.value],
        per_chat_per_second: float = RATE_LIMIT_DEFAULTS[RateLimitKey.PER_CHAT_PER_SECOND.value],
        group_per_minute: float = RATE_LIMIT_DEFAULTS[RateLimitKey.GROUP_PER_MINUTE.value],
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Build the buckets for one bot; a rate of 0 switches that budget off.

        The parameter names are the ``RATE_LIMIT`` keys, so a settings mapping
        can be splatted straight in.
        """
        self._clock = clock
        self._sleep = sleep
        self._overall = self._bucket(overall_per_second)
        self._per_chat_rate = per_chat_per_second
        self._group_rate = group_per_minute / 60 if group_per_minute else 0
        self._group_capacity = group_per_minute or None
        self._chats: OrderedDict[int, TokenBucket] = OrderedDict()
        self._groups: OrderedDict[int, TokenBucket] = OrderedDict()
        # threading, not asyncio: see TokenBucket.acquire
        self._lock = threading.Lock()

    def _bucket(self, rate: float, capacity: float | None = None) -> TokenBucket | None:
        if not rate:
            return None
        return TokenBucket(rate, capacity, clock=self._clock, sleep=self._sleep)

    def _for(
        self,
        chats: OrderedDict[int, TokenBucket],
        key: int,
        rate: float,
        capacity: float | None = None,
    ) -> TokenBucket | None:
        bucket = chats.get(key)
        if bucket is None:
            created = self._bucket(rate, capacity)
            if created is None:
                return None
            bucket = chats[key] = created
            self._evict(chats)
        chats.move_to_end(key)
        return bucket

    @staticmethod
    def _evict(chats: OrderedDict[int, TokenBucket]) -> None:
        """Keep the map at the cap, preferring buckets that owe no wait time.

        When every candidate is still busy the least recently used one goes
        anyway, and its debt goes with it. That is a bounded loss rather than a
        way around the limit: a bucket is only evicted once MAX_TRACKED_CHATS
        other chats have been more recently active, which at the overall limit
        takes minutes, while per-chat debt clears in about a second. The
        alternative is an unbounded map, which is a leak.
        """
        while len(chats) > MAX_TRACKED_CHATS:
            # stopping at the first busy bucket left the map uncapped: one chat
            # that keeps sending pinned everything behind it
            candidates = [chats.popitem(last=False) for _ in range(min(EVICTION_CANDIDATES, len(chats)))]
            evict = next((index for index, (_, bucket) in enumerate(candidates) if bucket.is_idle()), 0)
            del candidates[evict]
            for key, bucket in reversed(candidates):
                chats[key] = bucket
                chats.move_to_end(key, last=False)

    @staticmethod
    def is_group(chat_id: int) -> bool:
        """Report whether ``chat_id`` is a group: those all carry a negative id.

        Supergroups and channels count as groups here, since Telegram gives the
        three of them the same per-minute budget.
        """
        return chat_id < 0

    async def acquire(self, chat_id: int | str | None = None) -> None:
        """Wait until sending to ``chat_id`` stays inside every limit."""
        buckets: list[TokenBucket] = []
        if self._overall is not None:
            buckets.append(self._overall)

        key = self._chat_key(chat_id)
        if key is not None:
            with self._lock:
                per_chat = self._for(self._chats, key, self._per_chat_rate)
                group = (
                    self._for(self._groups, key, self._group_rate, self._group_capacity) if self.is_group(key) else None
                )
            buckets.extend(bucket for bucket in (per_chat, group) if bucket is not None)

        for bucket in buckets:
            await bucket.acquire()

    @staticmethod
    def _chat_key(chat_id: int | str | None) -> int | None:
        """Return the bucket key for ``chat_id``, or None when it has none.

        Per-chat limits only apply to numeric ids: an ``@channel`` name cannot
        be keyed. The runtime check is wider than the annotation because the id
        comes from caller kwargs, where anything at all can turn up — including
        a bool, which int() would otherwise fold into chat 1.
        """
        if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
            return None
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return None


def build_rate_limiter() -> RateLimiter | None:
    """Build the limiter described by settings, or None when disabled."""
    limits = conf['RATE_LIMIT']
    if not limits:
        return None

    unknown = sorted(str(key) for key in limits if key not in KNOWN_RATE_LIMIT_KEYS)
    if unknown:
        msg = f"{SETTINGS_NAME}['RATE_LIMIT'] has unknown keys: {', '.join(unknown)}."
        raise ImproperlyConfigured(msg)
    return RateLimiter(**limits)


class _LimiterRegistry:
    """The limiters in use, one per bot token.

    Telegram applies its limits per bot, so two ``TelegramBot`` objects holding
    the same token must draw on one budget; separate limiters would let them
    send at twice the rate.
    """

    def __init__(self) -> None:
        """Start empty: a limiter is built on the first send with that token."""
        self._limiters: dict[str, RateLimiter] = {}
        # threading, not asyncio: see TokenBucket.acquire
        self._guard = threading.Lock()

    def get(self, token: str) -> RateLimiter | None:
        """Return the limiter for ``token``, building it if this is the first ask."""
        with self._guard:
            existing = self._limiters.get(token)
            if existing is not None:
                return existing
            limiter = build_rate_limiter()
            if limiter is not None:
                self._limiters[token] = limiter
            return limiter

    def clear(self) -> None:
        """Forget every limiter, so the next ask reads the settings again."""
        with self._guard:
            self._limiters.clear()


_registry = _LimiterRegistry()


def get_rate_limiter(token: str) -> RateLimiter | None:
    """Return the limiter for ``token``, shared across bot instances."""
    return _registry.get(token)


def reset_rate_limiters() -> None:
    """Forget the shared limiters, so changed settings take effect."""
    _registry.clear()


def _reset_on_setting_change(
    sender: object,  # noqa: ARG001 - Django sends this to every receiver, named
    setting: str,
    **kwargs: Any,
) -> None:
    if setting == SETTINGS_NAME:
        reset_rate_limiters()


setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.throttling')
