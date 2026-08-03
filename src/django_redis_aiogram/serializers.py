"""Serialization of queued aiogram calls.

1.x pickled the queue payload, which makes anything able to write to the Redis
list able to execute code in the bot container. JSON is the default now.

Two aiogram details make plain ``model_dump(mode='json')`` insufficient:

* aiogram fills unset fields with a ``Default`` sentinel that pydantic cannot
  serialize. Dropping those fields via ``exclude_unset`` looks like a fix but
  also drops discriminators, and an ``InputMediaPhoto`` then silently comes back
  as an ``InputMediaAudio``. Sentinels are therefore tagged and restored.
* Every model is tagged with its concrete class name, so decoding never has to
  infer the type from a union.
"""

import base64
import datetime
import json
import pickle
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from aiogram import types
from aiogram.client.default import Default
from aiogram.types.base import TelegramObject
from aiogram.types.input_file import (
    BufferedInputFile,
    FSInputFile,
    InputFile,
    URLInputFile,
)

TAG_MODEL = '__model__'
TAG_DEFAULT = '__default__'
TAG_DATETIME = '__datetime__'
TAG_DATE = '__date__'
TAG_DECIMAL = '__decimal__'
TAG_BYTES = '__bytes__'
TAG_INPUT_FILE = '__input_file__'

JSON_SERIALIZER = 'json'
PICKLE_SERIALIZER = 'pickle'


class SerializationError(Exception):
    """A payload could not be encoded or decoded."""


def _encode_input_file(value: InputFile) -> dict[str, Any]:
    common = {'filename': value.filename, 'chunk_size': value.chunk_size}
    if isinstance(value, FSInputFile):
        return {TAG_INPUT_FILE: 'FSInputFile', 'path': str(value.path), **common}
    if isinstance(value, URLInputFile):
        return {
            TAG_INPUT_FILE: 'URLInputFile',
            'url': value.url,
            'headers': value.headers,
            'timeout': value.timeout,
            **common,
        }
    if isinstance(value, BufferedInputFile):
        return {
            TAG_INPUT_FILE: 'BufferedInputFile',
            'data': base64.b64encode(value.data).decode('ascii'),
            **common,
        }
    raise SerializationError(
        f'{type(value).__name__} cannot be queued. Send a file_id or a URL instead, '
        f"or set TELEGRAM_BOT['SERIALIZER'] to 'pickle'."
    )


def encode(value: Any) -> Any:
    """Turn an arbitrary aiogram call argument into JSON-safe data."""
    if isinstance(value, Default):
        return {TAG_DEFAULT: value.name}
    if isinstance(value, TelegramObject):
        return {
            TAG_MODEL: type(value).__name__,
            'data': {key: encode(item) for key, item in dict(value).items()},
        }
    if isinstance(value, InputFile):
        return _encode_input_file(value)
    if isinstance(value, Enum):
        return encode(value.value)
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, datetime.datetime):
        return {TAG_DATETIME: value.isoformat()}
    if isinstance(value, datetime.date):
        return {TAG_DATE: value.isoformat()}
    if isinstance(value, Decimal):
        return {TAG_DECIMAL: str(value)}
    if isinstance(value, bytes):
        return {TAG_BYTES: base64.b64encode(value).decode('ascii')}
    return value


def _resolve_model(name: str) -> type[TelegramObject]:
    model = getattr(types, name, None)
    if not (isinstance(model, type) and issubclass(model, TelegramObject)):
        raise SerializationError(f'{name!r} is not an aiogram type.')
    return model


def _decode_input_file(payload: dict[str, Any]) -> InputFile:
    kind = payload[TAG_INPUT_FILE]
    common = {'filename': payload['filename'], 'chunk_size': payload['chunk_size']}
    if kind == 'FSInputFile':
        return FSInputFile(payload['path'], **common)
    if kind == 'URLInputFile':
        return URLInputFile(
            payload['url'], headers=payload['headers'], timeout=payload['timeout'], **common
        )
    if kind == 'BufferedInputFile':
        return BufferedInputFile(base64.b64decode(payload['data']), **common)
    raise SerializationError(f'Unknown input file type {kind!r}.')


def decode(value: Any) -> Any:
    """Reverse :func:`encode`."""
    if isinstance(value, dict):
        if TAG_DEFAULT in value:
            return Default(value[TAG_DEFAULT])
        if TAG_DATETIME in value:
            return datetime.datetime.fromisoformat(value[TAG_DATETIME])
        if TAG_DATE in value:
            return datetime.date.fromisoformat(value[TAG_DATE])
        if TAG_DECIMAL in value:
            return Decimal(value[TAG_DECIMAL])
        if TAG_BYTES in value:
            return base64.b64decode(value[TAG_BYTES])
        if TAG_INPUT_FILE in value:
            return _decode_input_file(value)
        if TAG_MODEL in value:
            model = _resolve_model(value[TAG_MODEL])
            return model.model_validate({key: decode(item) for key, item in value['data'].items()})
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


class Serializer(Protocol):
    name: str

    def dumps(self, payload: dict[str, Any]) -> bytes: ...

    def loads(self, raw: bytes) -> dict[str, Any]: ...


class JsonSerializer:
    name = JSON_SERIALIZER

    def dumps(self, payload: dict[str, Any]) -> bytes:
        try:
            return json.dumps(encode(payload)).encode('utf-8')
        except SerializationError:
            raise
        except (TypeError, ValueError, RecursionError) as error:
            raise SerializationError(f'Cannot encode payload as JSON: {error}') from error

    def loads(self, raw: bytes) -> dict[str, Any]:
        try:
            decoded = decode(json.loads(raw))
        except SerializationError:
            raise
        except (TypeError, ValueError, KeyError, RecursionError) as error:
            raise SerializationError(f'Cannot decode JSON payload: {error}') from error
        if not isinstance(decoded, dict):
            raise SerializationError('Queued payload must be a mapping.')
        return decoded


class PickleSerializer:
    name = PICKLE_SERIALIZER

    def dumps(self, payload: dict[str, Any]) -> bytes:
        try:
            return pickle.dumps(payload)
        except Exception as error:
            raise SerializationError(f'Cannot pickle payload: {error}') from error

    def loads(self, raw: bytes) -> dict[str, Any]:
        try:
            decoded = pickle.loads(raw)
        except Exception as error:
            raise SerializationError(f'Cannot unpickle payload: {error}') from error
        if not isinstance(decoded, dict):
            raise SerializationError('Queued payload must be a mapping.')
        return decoded


SERIALIZERS: dict[str, type[Serializer]] = {
    JSON_SERIALIZER: JsonSerializer,
    PICKLE_SERIALIZER: PickleSerializer,
}


def get_serializer() -> Serializer:
    from django_redis_aiogram.settings import conf

    name = conf['SERIALIZER']
    try:
        return SERIALIZERS[name]()
    except KeyError:
        raise SerializationError(
            f'Unknown serializer {name!r}, expected one of {sorted(SERIALIZERS)}.'
        ) from None


def looks_like_json(raw: bytes) -> bool:
    # pickle never starts with whitespace or '{', so leading blanks are safe to skip
    return raw.lstrip()[:1] == b'{'


def loads(raw: bytes) -> dict[str, Any]:
    """Decode a queued payload, detecting which serializer wrote it.

    Detection is what lets a running deployment switch to JSON without draining
    the queue first.
    """
    from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

    if looks_like_json(raw):
        return JsonSerializer().loads(raw)
    # from the environment this arrives as a string, and 'false' is truthy
    if not coerce_bool(conf['ALLOW_PICKLE'], f"{SETTINGS_NAME}['ALLOW_PICKLE']"):
        raise SerializationError(
            'Refusing to unpickle a queued payload. If this queue still holds '
            "messages written by 1.x, set TELEGRAM_BOT['ALLOW_PICKLE'] = True "
            'for the upgrade window and remove it once the queue has drained.'
        )
    return PickleSerializer().loads(raw)
