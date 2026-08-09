"""The wire format, and the compatibility it does and does not give.

The reader accepts the flat shape 2.x wrote, because a rolling upgrade leaves
those on the list. The reverse does not hold, and the test at the bottom is
there so nobody assumes it does.
"""

import uuid

import pytest

from django_redis_aiogram.context import correlation_scope, current_correlation_id
from django_redis_aiogram.envelope import (
    ENVELOPE_KEY,
    ENVELOPE_VERSION,
    MalformedEnvelopeError,
    UnknownEnvelopeVersionError,
    pack,
    unpack,
)
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.serializers import JsonSerializer, PickleSerializer


def test_an_envelope_round_trips():
    identifier = new_correlation_id()
    payload = pack('send_message', {'chat_id': 1, 'text': 'hi'}, identifier, 1700.0)

    call = unpack(payload)

    assert call.function == 'send_message'
    assert call.kwargs == {'chat_id': 1, 'text': 'hi'}
    assert call.correlation_id == identifier
    assert call.queued_at == 1700.0


@pytest.mark.parametrize('serializer', [JsonSerializer(), PickleSerializer()])
def test_the_envelope_survives_both_serializers(serializer):
    """`__envelope__` is not one of the codec tags, so the nesting decodes as a
    plain mapping and the kwargs inside it still become real objects."""
    identifier = new_correlation_id()
    payload = pack('send_message', {'chat_id': 1, 'text': 'hi'}, identifier, 1700.0)

    call = unpack(serializer.loads(serializer.dumps(payload)))

    assert call.function == 'send_message'
    assert call.kwargs == {'chat_id': 1, 'text': 'hi'}
    assert call.correlation_id == identifier


def test_a_2_x_flat_payload_is_still_read():
    """A rolling upgrade leaves these on the list; refusing them loses messages."""
    call = unpack({'function': 'send_message', 'chat_id': 1, 'text': 'hi'})

    assert call.function == 'send_message'
    assert call.kwargs == {'chat_id': 1, 'text': 'hi'}
    assert call.correlation_id is None
    assert call.queued_at == 0.0


def test_a_newer_envelope_is_refused_rather_than_misread():
    """Guessing at a shape this version does not know would deliver the wrong
    call; refusing leaves the message in flight for an upgrade to deliver."""
    payload = {ENVELOPE_KEY: ENVELOPE_VERSION + 1, 'function': 'send_message', 'kwargs': {}}

    with pytest.raises(UnknownEnvelopeVersionError, match='Upgrade the bot container first'):
        unpack(payload)


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param([{'function': 'send_message'}], id='a list'),
        pytest.param('send_message', id='a string'),
        pytest.param(42, id='a number'),
        pytest.param(None, id='null'),
    ],
)
def test_a_payload_that_is_not_a_mapping_is_refused_rather_than_raised_through(payload):
    """Redis is a trust boundary, and `.get` on a decoded list is an
    AttributeError that would leave the consumer thread dead."""
    with pytest.raises(MalformedEnvelopeError, match='not a mapping'):
        unpack(payload)


@pytest.mark.parametrize('version', [0, -1, 'one', [], {}, True, 1.0, 1.5])
def test_a_version_no_release_ever_wrote_is_refused_as_malformed(version):
    """Distinct from a newer version on purpose: a future shape is kept in
    flight for an upgraded consumer, and this one never becomes deliverable, so
    keeping it would mean reclaiming it for ever."""
    payload = {ENVELOPE_KEY: version, 'function': 'send_message', 'kwargs': {}}

    with pytest.raises(MalformedEnvelopeError) as refusal:
        unpack(payload)

    # the value came off an untrusted queue and this message reaches a log line
    assert repr(version) not in str(refusal.value) or isinstance(version, int)


@pytest.mark.parametrize(
    'broken',
    ['not-a-number', [], {}, object(), float('nan'), float('inf'), float('-inf')],
)
def test_an_unreadable_timestamp_costs_the_latency_not_the_message(broken):
    """`float()` either raises on it or, for nan and the infinities, accepts it
    and poisons every figure computed from it — nan is not even valid JSON to a
    strict reader. The call itself may be perfectly deliverable, so losing a
    real message over a metric would be the wrong trade."""
    payload = {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        'function': 'send_message',
        'kwargs': {'chat_id': 1},
        'queued_at': broken,
    }

    envelope = unpack(payload)

    assert envelope.queued_at == 0.0
    assert envelope.kwargs == {'chat_id': 1}


@pytest.mark.parametrize('broken', ['', 'not-a-uuid', None, 42])
def test_an_unreadable_correlation_id_does_not_lose_the_message(broken):
    """The id is for joining rows; a bad one must not cost the send."""
    payload = pack('send_message', {'chat_id': 1}, new_correlation_id(), 0.0)
    payload['correlation_id'] = broken

    call = unpack(payload)

    assert call.function == 'send_message'
    assert call.correlation_id is None


def test_missing_kwargs_decode_to_an_empty_mapping():
    call = unpack({ENVELOPE_KEY: ENVELOPE_VERSION, 'function': 'send_message'})

    assert call.kwargs == {}


def test_a_2_x_reader_would_choke_on_the_new_shape():
    """Why the bot container has to be deployed before the web tier.

    2.x did `self.handler(**payload)` into `send_raw`, which passed everything
    but `function` down to the aiogram method — and that method has a real
    signature. It raises there, the consumer logs and swallows it, and the
    message is gone with nothing to redeliver.
    """
    payload = pack('send_message', {'chat_id': 1}, new_correlation_id(), 0.0)

    def a_telegram_method(chat_id=None, text=None):  # what aiogram exposes
        return chat_id, text

    reached_the_method = {key: value for key, value in payload.items() if key != 'function'}
    with pytest.raises(TypeError, match='__envelope__'):
        a_telegram_method(**reached_the_method)


def test_the_correlation_scope_restores_what_it_replaced():
    outer = new_correlation_id()
    inner = new_correlation_id()

    assert current_correlation_id() is None
    with correlation_scope(outer):
        assert current_correlation_id() == outer
        with correlation_scope(inner):
            assert current_correlation_id() == inner
        assert current_correlation_id() == outer
    assert current_correlation_id() is None


def test_the_packed_id_is_a_string_the_serializers_can_carry():
    """A UUID has no JSON form, and adding a codec for one would change the
    tagged-JSON vocabulary that queued payloads depend on."""
    payload = pack('send_message', {}, uuid.UUID(int=1), 0.0)

    assert isinstance(payload['correlation_id'], str)
