"""Round-trip tests for the queue payload format.

1.0.4 switched from JSON to pickle because keyboards would not survive the
trip. They do now; what actually breaks plain pydantic dumping is aiogram's
``Default`` sentinel, and the obvious workaround for it corrupts discriminated
unions. Both cases are pinned here.
"""

import datetime
import json
import sys
from decimal import Decimal

import pytest
from aiogram import types
from aiogram.client.default import Default
from aiogram.methods import SendMediaGroup, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types.input_file import BufferedInputFile, FSInputFile, URLInputFile
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_redis_aiogram.enums import PayloadDetail, SerializationTag, SerializerKind
from django_redis_aiogram.serializers import (
    _CODECS,
    JsonSerializer,
    PickleSerializer,
    SerializationError,
    encode,
    get_serializer,
    loads,
)


def roundtrip(value):
    serializer = JsonSerializer()
    return serializer.loads(serializer.dumps({'value': value}))['value']


@pytest.mark.parametrize(
    'markup',
    [
        types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text='b', callback_data='c')]]),
        types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text='b')]]),
        types.ReplyKeyboardRemove(),
        types.ForceReply(force_reply=True),
    ],
)
def test_every_reply_markup_survives(markup):
    restored = roundtrip(markup)
    assert type(restored) is type(markup)
    assert restored == markup


def test_inline_keyboard_keeps_its_payload():
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='press', callback_data='data')]]
    )
    restored = roundtrip(markup)
    assert restored.inline_keyboard[0][0].callback_data == 'data'
    assert SendMessage(chat_id=1, text='x', reply_markup=restored).reply_markup == markup


def test_discriminator_is_preserved():
    """exclude_unset would drop type='photo' and decode this as InputMediaAudio."""
    media = types.InputMediaPhoto(media='https://example.test/a.png', caption='cap')
    restored = roundtrip(media)
    assert type(restored) is types.InputMediaPhoto
    assert restored.type == 'photo'
    group = SendMediaGroup(chat_id=1, media=[restored])
    assert type(group.media[0]) is types.InputMediaPhoto


@pytest.mark.parametrize(
    'value',
    [
        types.LinkPreviewOptions(is_disabled=True),
        types.ReplyParameters(message_id=5),
    ],
)
def test_models_holding_default_sentinels(value):
    """These are exactly the models that raise PydanticSerializationError."""
    assert [item for item in dict(value).values() if isinstance(item, Default)], (
        'fixture no longer carries a Default sentinel, so it tests nothing'
    )
    restored = roundtrip(value)
    assert type(restored) is type(value)
    assert restored == value
    assert [item for item in dict(restored).values() if isinstance(item, Default)]


def test_default_sentinel_keeps_its_name():
    restored = roundtrip(Default('parse_mode'))
    assert isinstance(restored, Default)
    assert restored.name == 'parse_mode'


def test_message_entities_survive():
    entities = [types.MessageEntity(type='bold', offset=0, length=2)]
    restored = roundtrip(entities)
    assert restored[0].type == 'bold'
    assert SendMessage(chat_id=1, text='xx', entities=restored).entities == entities


@pytest.mark.parametrize(
    'value',
    [
        datetime.datetime(2026, 8, 3, 12, 30, tzinfo=datetime.timezone.utc),
        datetime.date(2026, 8, 3),
        Decimal('1.50'),
        b'\x89PNG',
        'plain',
        42,
        None,
        True,
        [1, 'two', None],
        {'nested': {'deep': [1, 2]}},
    ],
)
def test_scalars_and_containers(value):
    assert roundtrip(value) == value


def test_tuples_come_back_as_lists():
    assert roundtrip((1, 2)) == [1, 2]


def test_fs_input_file(tmp_path):
    path = tmp_path / 'a.png'
    restored = roundtrip(FSInputFile(path, filename='a.png'))
    assert isinstance(restored, FSInputFile)
    assert str(restored.path) == str(path)
    assert restored.filename == 'a.png'


def test_url_input_file():
    restored = roundtrip(URLInputFile('https://example.test/a.png', filename='a.png'))
    assert isinstance(restored, URLInputFile)
    assert restored.url == 'https://example.test/a.png'


def test_buffered_input_file():
    restored = roundtrip(BufferedInputFile(b'\x89PNG', filename='a.png'))
    assert isinstance(restored, BufferedInputFile)
    assert restored.data == b'\x89PNG'
    assert restored.filename == 'a.png'


def test_full_call_payload():
    serializer = JsonSerializer()
    payload = {
        'function': 'send_photo',
        'chat_id': 100,
        'caption': 'hi',
        'reply_markup': types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='b', url='https://example.test')]]
        ),
    }
    restored = serializer.loads(serializer.dumps(payload))
    assert restored['function'] == 'send_photo'
    assert restored['chat_id'] == 100
    assert restored['reply_markup'] == payload['reply_markup']


def test_the_enum_values_are_frozen():
    """Queued payloads carry these tags and settings carry these names, so a
    member may be renamed but never revalued."""
    assert tuple(tag.value for tag in SerializationTag) == (
        '__model__',
        '__default__',
        '__datetime__',
        '__date__',
        '__decimal__',
        '__bytes__',
        '__input_file__',
    )
    assert tuple(kind.value for kind in SerializerKind) == ('json', 'pickle')


def test_a_tag_lands_in_the_payload_as_a_plain_key():
    """An enum member used as a dict key must serialize to its value, not its name."""
    raw = JsonSerializer().dumps({'when': datetime.date(2026, 8, 3)})
    assert json.loads(raw) == {'when': {'__date__': '2026-08-03'}}


def test_every_tag_has_exactly_one_codec():
    assert sorted(codec.tag for codec in _CODECS) == sorted(SerializationTag)


def test_unknown_input_file_kind_is_rejected():
    payload = b'{"__input_file__": "SmtpInputFile", "filename": "a.png", "chunk_size": 65536}'
    with pytest.raises(SerializationError, match='Unknown input file type'):
        JsonSerializer().loads(payload)


def test_unknown_model_name_is_rejected():
    with pytest.raises(SerializationError, match='not an aiogram type'):
        JsonSerializer().loads(b'{"__model__": "os", "data": {}}')


def test_non_mapping_payload_is_rejected():
    with pytest.raises(SerializationError, match='must be a mapping'):
        JsonSerializer().loads(b'[1, 2]')


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'json'})
def test_get_serializer_json():
    assert isinstance(get_serializer(), JsonSerializer)


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'pickle', 'ALLOW_PICKLE': True})
def test_get_serializer_pickle():
    assert isinstance(get_serializer(), PickleSerializer)


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'yaml'})
def test_get_serializer_rejects_unknown():
    with pytest.raises(SerializationError, match='Unknown serializer'):
        get_serializer()


def test_reader_detects_json():
    raw = JsonSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    assert loads(raw)['chat_id'] == 1


@override_settings(TELEGRAM_BOT={'ALLOW_PICKLE': True})
def test_legacy_pickle_drains_during_the_upgrade_window():
    """A queue written by 1.x drains once the operator opts in."""
    raw = PickleSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    assert loads(raw)['chat_id'] == 1


def test_pickle_reads_are_refused_by_default():
    """Unpickling queue data is code execution; it must be an explicit opt-in."""
    raw = PickleSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    with pytest.raises(SerializationError, match='ALLOW_PICKLE'):
        loads(raw)


def test_unsupported_input_file_reports_how_to_proceed():
    class Weird(types.input_file.InputFile):
        async def read(self, bot):  # pragma: no cover - never called
            yield b''

    with pytest.raises(SerializationError, match='file_id or a URL'):
        JsonSerializer().dumps({'photo': Weird()})


def test_unencodable_object_raises_serialization_error():
    """Callers catch SerializationError; a raw TypeError would escape them."""

    class Opaque:
        pass

    with pytest.raises(SerializationError, match='Cannot encode payload as JSON'):
        JsonSerializer().dumps({'thing': Opaque()})


def test_malformed_json_raises_serialization_error():
    with pytest.raises(SerializationError, match='Cannot decode JSON payload'):
        JsonSerializer().loads(b'{not json')


def test_corrupt_pickle_raises_serialization_error():
    with pytest.raises(SerializationError, match='Cannot unpickle payload'):
        PickleSerializer().loads(b'\x80\x04corrupt')


def test_json_detection_tolerates_leading_whitespace():
    raw = b'  \n' + JsonSerializer().dumps({'function': 'send_message', 'chat_id': 5})
    assert loads(raw)['chat_id'] == 5


def test_deeply_nested_payload_raises_serialization_error():
    """encode/decode recurse, so depth must surface as SerializationError."""
    payload: dict = {'function': 'send_message'}
    node = payload
    for _ in range(sys.getrecursionlimit() + 100):
        node['next'] = {}
        node = node['next']

    with pytest.raises(SerializationError):
        JsonSerializer().dumps(payload)


def test_a_queue_holding_both_formats_is_read_per_message():
    """Format detection is per payload, which is what allows a live switch."""
    json_payload = JsonSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    pickle_payload = PickleSerializer().dumps({'function': 'send_message', 'chat_id': 2})

    with override_settings(TELEGRAM_BOT={'ALLOW_PICKLE': True}):
        assert loads(json_payload)['chat_id'] == 1
        assert loads(pickle_payload)['chat_id'] == 2

    # ALLOW_PICKLE applies only to the non-JSON one
    with override_settings(TELEGRAM_BOT={'ALLOW_PICKLE': False}):
        assert loads(json_payload)['chat_id'] == 1
        with pytest.raises(SerializationError, match='ALLOW_PICKLE'):
            loads(pickle_payload)


@override_settings(TELEGRAM_BOT={'ALLOW_PICKLE': 'false'})
def test_a_textual_allow_pickle_still_refuses():
    """From the environment the flag is a string, and 'false' is truthy."""
    raw = PickleSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    with pytest.raises(SerializationError, match='ALLOW_PICKLE'):
        loads(raw)


@override_settings(TELEGRAM_BOT={'ALLOW_PICKLE': 'yes'})
def test_a_textual_allow_pickle_still_permits():
    raw = PickleSerializer().dumps({'function': 'send_message', 'chat_id': 2})
    assert loads(raw)['chat_id'] == 2


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'pickle', 'ALLOW_PICKLE': False})
def test_writing_pickle_the_reader_refuses_is_rejected_at_runtime():
    """E022 reports this, but a WSGI process never runs the system checks."""
    with pytest.raises(ImproperlyConfigured, match='ALLOW_PICKLE'):
        get_serializer()


def test_the_one_pass_encoder_writes_the_same_bytes():
    """The encoder replaces a walk that built a tagged copy first, so the only
    thing that may change is the cost."""
    corpus = [
        {'function': 'send_message', 'chat_id': 1, 'text': 'hello'},
        {'tag': SerializationTag.MODEL, 'mode': PayloadDetail.FULL},
        {SerializationTag.MODEL: 'as a key', PayloadDetail.SUMMARY: 'and this one'},
        {'when': datetime.datetime(2026, 8, 16, 3, 4, 5, tzinfo=datetime.timezone.utc)},
        {'day': datetime.date(2026, 8, 16), 'money': Decimal('1.10'), 'blob': b'\x00\xff'},
        {'nested': [{'deep': ({'tuple': 1}, [2, 3])}]},
        {'sentinel': Default('parse_mode')},
        {'markup': InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='b', callback_data='c')]])},
    ]

    for payload in corpus:
        assert JsonSerializer().dumps(payload) == json.dumps(encode(payload)).encode('utf-8'), payload


def test_a_payload_too_deep_to_read_back_is_refused():
    """The C encoder ignores Python's recursion limit; `decode` does not.

    So without the guard a deeply nested call would be written happily and then
    be undecodable for ever on the consumer, which drops it — a message destroyed
    by an optimisation.
    """
    payload: dict = {'function': 'send_message'}
    node = payload
    for _ in range(sys.getrecursionlimit() + 100):
        node['next'] = {}
        node = node['next']

    with pytest.raises(SerializationError):
        JsonSerializer().dumps(payload)


def test_an_ordinary_payload_never_pays_for_the_guard(monkeypatch):
    """The guard re-encodes the slow way, so it must not fire on real messages."""
    from django_redis_aiogram import serializers

    calls = []
    original = serializers.encode
    monkeypatch.setattr(serializers, 'encode', lambda value: calls.append(value) or original(value))
    keyboard = {
        'chat_id': 1,
        'reply_markup': {'inline_keyboard': [[{'text': f'b{i}', 'callback_data': f'c{i}'}] for i in range(30)]},
    }

    JsonSerializer().dumps(keyboard)

    assert calls == []
