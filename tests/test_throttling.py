"""Proactive rate limiting.

Retrying on TelegramRetryAfter is reactive — the message was already refused.
These tests drive the limiter with a fake clock, so they assert the pacing
arithmetic rather than sleeping through it.
"""

import asyncio

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import RateLimitKey, choices
from django_redis_aiogram.throttling import (
    KNOWN_RATE_LIMIT_KEYS,
    MAX_TRACKED_CHATS,
    RateLimiter,
    TokenBucket,
    build_rate_limiter,
    get_rate_limiter,
    reset_rate_limiters,
)


class FakeClock:
    """A clock that only moves when the limiter asks to sleep."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    @property
    def total_slept(self):
        return sum(self.slept)


def run(coroutine):
    return asyncio.run(coroutine)


def test_bucket_allows_a_burst_up_to_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate=10, capacity=10, clock=clock.time, sleep=clock.sleep)

    async def scenario():
        for _ in range(10):
            await bucket.acquire()

    run(scenario())
    assert clock.slept == []


def test_bucket_paces_once_the_burst_is_spent():
    clock = FakeClock()
    bucket = TokenBucket(rate=10, capacity=10, clock=clock.time, sleep=clock.sleep)

    async def scenario():
        for _ in range(15):
            await bucket.acquire()

    run(scenario())
    # five extra messages at ten per second
    assert clock.total_slept == pytest.approx(0.5, abs=1e-6)


def test_an_idle_bucket_cannot_bank_the_silence_as_credit():
    """A minute of quiet must not become a minute's worth of sends at one instant.

    The claim starts no further back than the burst allows. Without that clamp the
    slot walks backwards with the clock, so a bucket at Telegram's own 30/s, idle
    for a minute, admits **1831** calls before it pauses — measured, not reasoned —
    against the 31 the burst permits. Both existing burst tests start from a fresh
    bucket, where the clamp and its absence agree, so neither could see it.
    """
    clock = FakeClock()
    bucket = TokenBucket(rate=30, capacity=30, clock=clock.time, sleep=clock.sleep)
    clock.now += 60  # what a quiet bot looks like between two conversations

    async def scenario():
        attempts = 0
        # bounded well above the 1831 an unclamped bucket reaches: a regression that
        # stopped sleeping at all would otherwise spin here until CI's own timeout, where
        # a hang says far less than a failure
        while not clock.slept and attempts < 3000:
            # counted before the await, because the call that finally sleeps is counted
            # too: what this measures is which call is the first to wait, not how many
            # went through without waiting
            attempts += 1
            await bucket.acquire()
        assert clock.slept, f'the bucket admitted {attempts} calls without ever pacing'
        return attempts

    attempts = run(scenario())

    assert attempts == 31, f'the silence was banked as credit: the first wait came at call {attempts}'


def test_bucket_rejects_a_nonpositive_rate():
    with pytest.raises(ValueError, match='rate must be positive'):
        TokenBucket(rate=0)


def test_overall_limit_paces_across_chats():
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=30,
        per_chat_per_second=0,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for index in range(60):
            await limiter.acquire(chat_id=index)

    run(scenario())
    # 30 free, then 30 more at 30/s
    assert clock.total_slept == pytest.approx(1.0, abs=1e-3)


def test_per_chat_limit_paces_one_conversation():
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for _ in range(4):
            await limiter.acquire(chat_id=777)

    run(scenario())
    assert clock.total_slept == pytest.approx(3.0, abs=1e-3)


def test_separate_chats_do_not_block_each_other():
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for chat in range(4):
            await limiter.acquire(chat_id=chat + 1)

    run(scenario())
    assert clock.slept == []


def test_groups_get_the_slower_per_minute_budget():
    """A negative id is a group, supergroup or channel: 20 per minute."""
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=0,
        group_per_minute=20,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for _ in range(21):
            await limiter.acquire(chat_id=-100500)

    run(scenario())
    # capacity is one minute's worth, so only the 21st waits three seconds
    assert clock.total_slept == pytest.approx(3.0, abs=1e-3)


def test_private_chats_are_not_treated_as_groups():
    assert RateLimiter.is_group(-100500) is True
    assert RateLimiter.is_group(100500) is False


def test_string_chat_ids_are_keyed_when_numeric():
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        await limiter.acquire(chat_id='42')
        await limiter.acquire(chat_id=42)

    run(scenario())
    assert clock.total_slept == pytest.approx(1.0, abs=1e-3)


def test_channel_usernames_skip_the_per_chat_bucket():
    """'@channel' cannot be keyed, so only the overall limit applies."""
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for _ in range(5):
            await limiter.acquire(chat_id='@some_channel')

    run(scenario())
    assert clock.slept == []


def test_missing_chat_id_still_respects_the_overall_limit():
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=2,
        per_chat_per_second=0,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for _ in range(4):
            await limiter.acquire()

    run(scenario())
    assert clock.total_slept == pytest.approx(1.0, abs=1e-3)


def test_idle_chats_are_evicted():
    """A long-running bot must not accumulate a bucket per chat forever."""
    clock = FakeClock()
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def scenario():
        for chat in range(MAX_TRACKED_CHATS + 50):
            await limiter.acquire(chat_id=chat + 1)
            clock.now += 5  # every bucket refills before the next chat

    run(scenario())
    assert len(limiter._chats) <= MAX_TRACKED_CHATS


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': None})
def test_rate_limiting_can_be_disabled():
    assert build_rate_limiter() is None
    assert TelegramBot().rate_limiter is None


@override_settings(TELEGRAM_BOT={})
def test_enabled_by_default_with_telegrams_numbers():
    limiter = build_rate_limiter()
    assert isinstance(limiter, RateLimiter)
    assert limiter._overall.rate == DEFAULTS['RATE_LIMIT']['overall_per_second']


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': {'overall_per_second': 5}})
def test_partial_settings_keep_the_other_defaults():
    limiter = build_rate_limiter()
    assert limiter._overall.rate == 5
    assert limiter._per_chat_rate == DEFAULTS['RATE_LIMIT']['per_chat_per_second']


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': {'per_second': 5}})
def test_unknown_key_is_reported():
    with pytest.raises(ImproperlyConfigured, match='unknown keys'):
        build_rate_limiter()


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': {'per_second': 5}, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_check_catches_an_unknown_key():
    assert 'django_redis_aiogram.E020' in {message.id for message in check_settings()}


@override_settings(
    TELEGRAM_BOT={
        'RATE_LIMIT': {'overall_per_second': -1},
        'TOKEN': '42:x',
        'REDIS_URL': 'r://x',
    }
)
def test_check_catches_a_negative_rate():
    assert 'django_redis_aiogram.E020' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': 'fast', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_check_catches_a_non_mapping():
    assert 'django_redis_aiogram.E020' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x'})
def test_bots_sharing_a_token_share_the_budget():
    """Telegram meters per token: separate limiters would double the rate."""
    first, second = TelegramBot(), TelegramBot()
    assert first.rate_limiter is second.rate_limiter


def test_a_different_token_gets_its_own_budget():
    with override_settings(TELEGRAM_BOT={'TOKEN': '42:one'}):
        first = TelegramBot().rate_limiter
        second = TelegramBot()
        second._rate_limiter_built = False
        with override_settings(TELEGRAM_BOT={'TOKEN': '42:two'}):
            assert second.rate_limiter is not first


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': {'overall_per_second': 7}})
def test_defaults_come_from_the_settings_defaults():
    """Duplicated literals in RateLimiter would drift from defaults.py."""
    limiter = build_rate_limiter()
    assert limiter._overall.rate == 7
    assert limiter._per_chat_rate == DEFAULTS['RATE_LIMIT']['per_chat_per_second']
    assert limiter._group_capacity == DEFAULTS['RATE_LIMIT']['group_per_minute']


@override_settings(
    TELEGRAM_BOT={
        'SERIALIZER': 'pickle',
        'ALLOW_PICKLE': False,
        'TOKEN': '42:x',
        'REDIS_URL': 'r://x',
    }
)
def test_writing_pickle_while_refusing_to_read_it_is_rejected():
    """Otherwise every queued message is written and then silently discarded."""
    assert 'django_redis_aiogram.E022' in {message.id for message in check_settings()}


def test_the_known_keys_are_the_rate_limit_enum():
    """A second hand-written list of the same names is a list that drifts."""
    assert choices(RateLimitKey) == KNOWN_RATE_LIMIT_KEYS
    assert frozenset(DEFAULTS['RATE_LIMIT']) == KNOWN_RATE_LIMIT_KEYS


@override_settings(TELEGRAM_BOT={})
def test_one_token_gets_one_limiter():
    first = get_rate_limiter('42:one')
    assert get_rate_limiter('42:one') is first
    assert get_rate_limiter('42:two') is not first


@override_settings(TELEGRAM_BOT={})
def test_resetting_forgets_the_shared_limiters():
    """override_settings fires this, which is how a changed budget takes effect."""
    before = get_rate_limiter('42:one')
    reset_rate_limiters()
    assert get_rate_limiter('42:one') is not before


def test_eviction_caps_the_map_even_when_every_bucket_is_busy():
    """Stopping at the first busy bucket left the map growing without limit."""
    clock = FakeClock()  # frozen, so nothing refills while the test runs
    limiter = RateLimiter(
        overall_per_second=0,
        per_chat_per_second=1,
        group_per_minute=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    for chat_id in range(1, MAX_TRACKED_CHATS + 51):
        asyncio.run(limiter.acquire(chat_id))  # one token each, so none is idle

    assert clock.slept == [], 'the limiter waited, so the buckets were not all busy'

    assert len(limiter._chats) <= MAX_TRACKED_CHATS, len(limiter._chats)
    assert not [bucket for bucket in limiter._chats.values() if bucket.is_idle()], (
        'the buckets were idle, so this did not exercise the busy path'
    )


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'RATE_LIMIT': {'overall_per_second': 5, 'per_chat_per_second': 0, 'group_per_minute': 0},
    }
)
def test_a_living_bot_picks_up_changed_rate_limits():
    """The instance used to cache its limiter, so setting_changed cleared the
    registry and the bot kept pacing to the old numbers anyway."""
    instance = TelegramBot()
    limiter = instance.rate_limiter
    assert limiter is not None
    assert limiter._overall is not None
    assert limiter._overall.rate == 5

    with override_settings(
        TELEGRAM_BOT={
            'TOKEN': '42:x',
            'RATE_LIMIT': {'overall_per_second': 9, 'per_chat_per_second': 0, 'group_per_minute': 0},
        }
    ):
        changed = instance.rate_limiter
        assert changed is not None
        assert changed._overall is not None
        assert changed._overall.rate == 9, 'the bot kept the limiter built for the old settings'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'SERIALIZER': 'pickle', 'ALLOW_PICKLE': 'false'})
def test_a_textual_false_still_trips_the_pickle_check():
    """From the environment ALLOW_PICKLE is a string, and 'false' is truthy —
    the check has to coerce it exactly the way the reader does."""
    assert 'django_redis_aiogram.E022' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'SERIALIZER': 'pickle', 'ALLOW_PICKLE': 'maybe'})
def test_unreadable_allow_pickle_is_reported_not_raised():
    """A check reports; E017 owns the type complaint, E022 stays silent."""
    reported = {message.id for message in check_settings()}

    assert 'django_redis_aiogram.E017' in reported
    assert 'django_redis_aiogram.E022' not in reported


def test_admission_is_strict_fifo():
    """A herd re-racing for the same token admits in whatever order the loop
    happens to resume, so the message that waited longest had no claim on going
    first. Claiming a slot up front makes arrival order the admission order.

    On a real loop, like the wakeup count below: under `FakeClock` each waiter
    that wakes finds the bucket already refilled, so no herd forms and the old
    design comes out ordered too. Against a real one it admits
    `[0, 14, 26, 13, 28, 2, ...]`.
    """
    admitted = []

    async def scenario():
        bucket = TokenBucket(rate=400, capacity=1)

        async def caller(index):
            await bucket.acquire()
            admitted.append(index)

        await asyncio.gather(*(caller(index) for index in range(30)))

    run(scenario())

    assert admitted == list(range(30))


def test_a_backlog_wakes_once_per_admitted_call():
    """Counting tokens made every waiter recompute the same wait from the same
    state, so N waiters woke together and N-1 went back to sleep — about N²/2
    wakeups for N sends.

    On a real loop, not `FakeClock`: its `sleep` advances the clock by the whole
    wait, so the herd never forms and the count comes out right on either design.

    What this pins is the wakeup count, which is what the reservation design buys.
    Falsified by giving `acquire` a recompute-and-re-sleep tail, which is what the
    token counter did: 120 251 wakeups for these forty sends, against 35.

    It does not pin the *order* admissions come out in — the
    changelog's "admission becomes strict FIFO" follows from claiming under the lock
    in call order, and no edit to this module makes it observably false, so a test
    asserting it would be one that cannot fail.
    """
    waits = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0)

    async def scenario():
        bucket = TokenBucket(rate=200, capacity=5, sleep=counting_sleep)
        await asyncio.gather(*(bucket.acquire() for _ in range(40)))

    run(scenario())

    # forty calls, five of them inside the burst
    assert len(waits) == 35


def test_the_wait_is_measured_from_when_it_starts():
    """The slot is claimed under the lock; the wait is not.

    A clock sampled while claiming is already stale by the time the sleep is
    computed, and sleeping `slot - stale` overshoots by exactly that gap. The
    scripted clock puts the gap where it would be in production — between
    releasing the lock and starting to wait.
    """
    reads = iter([0.0, 0.0, 0.0, 0.0, 0.05])
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    async def scenario():
        bucket = TokenBucket(rate=10, capacity=1, clock=lambda: next(reads), sleep=sleep)
        await bucket.acquire()
        await bucket.acquire()

    run(scenario())

    # the slot is at 0.1 and the wait starts at 0.05, so half of it has passed
    assert slept == [pytest.approx(0.05)]
