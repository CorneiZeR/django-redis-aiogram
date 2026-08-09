"""Every stage of an outbound message, and the ones that used to leave no trace.

Four of these cover sends that were dropped with nothing but a log line before
3.0: refused because the bot was shutting down, refused because the loop was
closed, dropped in the hand-off, and cancelled at shutdown.
"""

import asyncio
import uuid

import pytest
from aiogram import exceptions
from aiogram.methods import SendMessage
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.client import resolve_correlation_id, task_correlation_id
from django_redis_aiogram.context import correlation_scope
from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.recorder import as_identifier, recorder
from django_redis_aiogram.serializers import JsonSerializer

QUEUE = 'TELEGRAM_BOT_MESSAGE'
SETTINGS = {
    'EVENT_LOG': True,
    'TOKEN': '42:x',
    'FSM_STORAGE': 'memory',
    'RATE_LIMIT': None,
    'MAX_RETRIES': 1,
}


class Sent:
    """What aiogram hands back, carrying the only id Telegram gives."""

    message_id = 4321


def a_bot(behaviour):
    """A stand-in for the aiogram Bot, with a session close() that does nothing."""

    class Fake:
        async def send_message(self, **kwargs):
            return behaviour(kwargs)

        class session:
            @staticmethod
            async def close():
                return None

    return Fake()


def kinds():
    return list(TelegramEvent.objects.order_by('id').values_list('kind', flat=True))


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_delivered_send_records_the_id_telegram_gave():
    instance = TelegramBot()
    instance._bot = a_bot(lambda _kwargs: Sent())
    try:
        identifier = instance.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        instance._bot = None
        instance.close()

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_SENT.value)
    assert row.correlation_id == identifier
    assert row.message_id == Sent.message_id, 'the aiogram return value was thrown away again'
    assert row.chat_id == 7
    assert row.duration_ms is not None
    assert row.worker, 'the row does not say which container sent it'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failed_send_records_why():
    def explode(_kwargs):
        msg = 'telegram said no'
        raise RuntimeError(msg)

    instance = TelegramBot()
    instance._bot = a_bot(explode)
    try:
        instance.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        instance._bot = None
        instance.close()

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_FAILED.value)
    assert row.error_code == 'RuntimeError'
    assert 'telegram said no' in row.error


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_rate_limit_records_the_retry_and_then_the_giving_up():
    attempts = []

    def refuse(_kwargs):
        attempts.append(True)
        raise exceptions.TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='x'),
            message='Too Many Requests',
            retry_after=0,
        )

    instance = TelegramBot()
    instance._bot = a_bot(refuse)
    try:
        instance.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        instance._bot = None
        instance.close()

    recorded = kinds()
    assert len(attempts) == instance.max_retries + 1, attempts
    assert EventKind.OUTBOUND_RETRIED.value in recorded, recorded
    assert recorded[-1] == EventKind.OUTBOUND_DROPPED.value, recorded


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_send_refused_at_shutdown_leaves_a_row():
    """Before 3.0 this was a log line and nothing else, so a message lost on
    `docker stop` was invisible to anything but a log search."""
    instance = TelegramBot()
    instance._closing = True
    try:
        identifier = instance.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        instance._closing = False

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_DROPPED.value)
    assert row.correlation_id == identifier
    assert 'shutting down' in row.error
    # a direct send_raw was never queued, so this row is the only one that will
    # ever exist for the message: an id alone cannot say what was lost
    assert row.function == 'send_message'
    assert row.chat_id == 7


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_pacing_figure_measures_the_attempt_that_sent_it():
    """`paced_ms` answers "how long did the rate limiter hold this back".

    Measured from the first attempt it would fold in every earlier attempt and
    the sleeps Telegram itself asked for, which is a different question and a
    much larger number — and the figure is what someone reads when they ask why
    a message was slow.
    """
    refusals = []

    def refuse_once(_kwargs):
        if not refusals:
            refusals.append(True)
            raise exceptions.TelegramRetryAfter(
                method=SendMessage(chat_id=1, text='x'),
                message='Too Many Requests',
                retry_after=1,
            )
        return Sent()

    instance = TelegramBot()
    instance._bot = a_bot(refuse_once)
    try:
        instance.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=10)
    finally:
        instance._bot = None
        instance.close()

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_SENT.value)
    # the retry slept a second; the successful attempt waited on nothing
    assert row.detail['paced_ms'] < 500, row.detail
    assert row.duration_ms >= 1000, 'the whole send did take the sleep'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_queueing_records_the_id_the_caller_was_given(redis_server):
    instance = TelegramBot()
    identifier = instance.send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_QUEUED.value)
    assert row.correlation_id == identifier
    assert row.chat_id == 7
    # the default payload level keeps the body out
    assert row.detail['text'] == {'__omitted__': 'text', 'length': 2}


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_the_consumer_records_what_it_took_off_the_queue(redis_server):
    """The row that makes queue latency measurable, and the one that ties the
    two processes together."""
    instance = TelegramBot()
    identifier = instance.send_redis(chat_id=7, text='hi')

    handled = []
    BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs)).consume_pending()
    recorder.flush(timeout=5)

    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_CONSUMED.value)
    assert row.correlation_id == identifier, 'the consumed row did not join the queued one'
    assert row.worker, 'the row does not say which container took it'
    assert 'queue_ms' in row.detail


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_an_undecodable_payload_is_recorded_by_fingerprint_not_by_content(redis_server):
    """An undecodable payload is untrusted input and may be a pickle, so the
    row holds a hash and a size rather than the bytes."""
    redis_server.rpush(QUEUE, b'{not json at all')

    BlpopDelivery(handler=lambda **kwargs: None).consume_pending()
    recorder.flush(timeout=5)

    row = TelegramEvent.objects.get(kind=EventKind.QUEUE_UNDECODABLE.value)
    assert set(row.detail) == {'bytes', 'sha256'}
    assert 'not json at all' not in str(row.detail)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_a_payload_naming_something_that_is_not_an_api_method_is_recorded(redis_server):
    redis_server.rpush(QUEUE, JsonSerializer().dumps({'function': 'download_file', 'kwargs': {}}))

    BlpopDelivery(handler=lambda **kwargs: None).consume_pending()
    recorder.flush(timeout=5)

    row = TelegramEvent.objects.get(kind=EventKind.QUEUE_REJECTED.value)
    assert row.function == 'download_file'
    assert 'not a Telegram API method' in row.error


def test_an_explicit_id_wins_over_the_scope():
    chosen = new_correlation_id()
    with correlation_scope(new_correlation_id()):
        assert resolve_correlation_id(chosen) == chosen


def test_the_scope_is_used_when_nothing_is_passed():
    """What makes a reply join the update that caused it with no plumbing."""
    inbound = new_correlation_id()
    with correlation_scope(inbound):
        assert resolve_correlation_id(None) == inbound


def test_a_fresh_id_when_there_is_no_scope():
    assert resolve_correlation_id(None) != resolve_correlation_id(None)


def test_a_string_id_is_accepted_and_nonsense_is_refused():
    identifier = new_correlation_id()

    assert resolve_correlation_id(str(identifier)) == identifier
    with pytest.raises(ValueError, match='must be a UUID'):
        resolve_correlation_id('not-a-uuid')


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (42, 42),
        ('@channel', None),
        (None, None),
        (True, None),
        (1.5, None),
        (2**63 - 1, 2**63 - 1),
        (-(2**63), -(2**63)),
        # a Python integer has no width; the column does, and an insert that
        # overflows costs the row rather than reporting the value
        (2**63, None),
        (-(2**63) - 1, None),
        (10**40, None),
    ],
)
def test_only_a_real_integer_chat_id_is_stored(value, expected):
    """A @username is a valid chat_id for Telegram and not one for a BIGINT."""
    assert as_identifier(value) == expected


def test_the_id_survives_a_round_trip_through_a_task_name():
    """How shutdown says which message it cancelled, without threading an
    argument through asyncio."""
    identifier = new_correlation_id()

    async def build():
        task = asyncio.create_task(asyncio.sleep(0), name=f'tgbot:{identifier.hex}')
        recovered = task_correlation_id(task)
        await task
        return recovered

    assert asyncio.run(build()) == identifier


def test_an_unnamed_task_gets_an_id_rather_than_an_error():
    """asyncio names its own tasks 'Task-3' and the like, so the fallback has to
    produce something a UUID column can hold rather than refuse."""

    async def build():
        task = asyncio.create_task(asyncio.sleep(0))
        recovered = task_correlation_id(task)
        await task
        return recovered

    assert isinstance(asyncio.run(build()), uuid.UUID)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_an_envelope_the_reader_cannot_make_sense_of_is_dropped_not_raised(redis_server):
    """A payload that decodes but declares a version no release ever wrote.

    Distinct from a newer version, which stays in flight for an upgraded
    consumer: this one never becomes deliverable, so keeping it would mean
    reclaiming it for ever. Recorded by fingerprint, because a payload the
    reader refused is untrusted input.
    """
    redis_server.rpush(QUEUE, JsonSerializer().dumps({'__envelope__': 0, 'function': 'send_message'}))

    handled = []
    BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs)).consume_pending()
    recorder.flush(timeout=5)

    assert handled == [], 'a payload that is not an envelope reached the handler'
    row = TelegramEvent.objects.get(kind=EventKind.QUEUE_UNDECODABLE.value)
    assert set(row.detail) == {'bytes', 'sha256'}
    assert redis_server.llen(QUEUE) == 0, 'the message was left to come back for ever'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'redis://localhost:6379/0'})
def test_a_queue_that_refuses_the_message_records_the_drop_and_raises(redis_server, monkeypatch):
    """A Redis that refuses the push means the message was never queued, and a
    `queued` row would say the opposite — the one row that must not be written
    optimistically, because nothing downstream will ever contradict it.

    The exception still reaches the caller: queueing is the caller's operation,
    and swallowing it would leave them believing the message is on its way.
    """

    def refuse(*_args, **_kwargs):
        msg = 'redis is gone'
        raise ConnectionError(msg)

    monkeypatch.setattr(type(redis_server), 'rpush', refuse)
    instance = TelegramBot()

    with pytest.raises(ConnectionError):
        instance.send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)

    assert not TelegramEvent.objects.filter(kind=EventKind.OUTBOUND_QUEUED.value).exists()
    row = TelegramEvent.objects.get(kind=EventKind.OUTBOUND_DROPPED.value)
    assert row.error_code == 'ConnectionError'
    assert row.function == 'send_message'
    assert row.chat_id == 7
    assert row.detail['stage'] == 'queueing'
