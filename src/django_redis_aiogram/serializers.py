"""Serialization of queued aiogram calls.

JSON is the format. Pickle stays behind ``ALLOW_PICKLE`` as the escape hatch for
payloads JSON cannot describe, off by default because unpickling the queue makes
anything able to write to the Redis list able to execute code in the container.

Two aiogram details make plain ``model_dump(mode='json')`` insufficient:

* aiogram fills unset fields with a ``Default`` sentinel that pydantic cannot
  serialize. Dropping those fields via ``exclude_unset`` looks like a fix but
  also drops discriminators, and an ``InputMediaPhoto`` then silently comes back
  as an ``InputMediaAudio``. Sentinels are therefore tagged and restored.
* Every model is tagged with its concrete class name, so decoding never has to
  infer the type from a union.

Each type that needs a tag is a :class:`TypeCodec` in ``_CODECS``: it recognizes
its own values, writes them as a tagged JSON object and reads them back.
:func:`encode` walks the registry before falling back to the structural cases
(enums, mappings, sequences); :func:`decode` matches a decoded object on the tag.
"""

import base64
import datetime
import json
import pickle
from abc import ABC, abstractmethod
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from aiogram import types
from aiogram.client.default import Default
from aiogram.types.base import TelegramObject
from aiogram.types.input_file import (
    BufferedInputFile,
    FSInputFile,
    InputFile,
    URLInputFile,
)
from django.core.exceptions import ImproperlyConfigured

from django_redis_aiogram.enums import SerializationTag, SerializerKind
from django_redis_aiogram.exceptions import DjangoRedisAiogramError, SerializationError
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

#: the module's public surface; listing SerializationError keeps its 2.0 import path alive
__all__ = [
    'SERIALIZERS',
    'BytesCodec',
    'DateCodec',
    'DatetimeCodec',
    'DecimalCodec',
    'DefaultSentinelCodec',
    'InputFileCodec',
    'JsonSerializer',
    'ModelCodec',
    'NonMappingPayloadError',
    'PickleReadRefusedError',
    'PickleSerializer',
    'PickleWriteRefusedError',
    'SerializationError',
    'Serializer',
    'TypeCodec',
    'UnknownInputFileKindError',
    'UnknownModelError',
    'UnknownSerializerError',
    'UnsupportedInputFileError',
    'decode',
    'encode',
    'get_serializer',
    'loads',
    'looks_like_json',
]

# Frozen like the tags: these name the input file kind inside a queued payload
FS_INPUT_FILE = 'FSInputFile'
URL_INPUT_FILE = 'URLInputFile'
BUFFERED_INPUT_FILE = 'BufferedInputFile'


class UnsupportedInputFileError(SerializationError):
    """An input file was queued that has no JSON representation."""

    def __init__(self, value: InputFile) -> None:
        """Name the refused type and the two ways to send the file anyway."""
        super().__init__(
            f'{type(value).__name__} cannot be queued. Send a file_id or a URL instead, '
            "or set TELEGRAM_BOT['SERIALIZER'] to 'pickle' together with "
            'ALLOW_PICKLE = True, or the reader will refuse what it writes.',
        )


class UnknownInputFileKindError(SerializationError):
    """A payload named an input file kind this version cannot rebuild."""

    def __init__(self, kind: object) -> None:
        """Name the refused kind."""
        super().__init__(f'Unknown input file type {kind!r}.')


class UnknownModelError(SerializationError):
    """A payload named a class that is not an aiogram type."""

    def __init__(self, name: str) -> None:
        """Name the refused class."""
        super().__init__(f'{name!r} is not an aiogram type.')


class NonMappingPayloadError(SerializationError):
    """A payload decoded to something other than a mapping of call arguments."""

    def __init__(self) -> None:
        """State the shape every queued payload must have."""
        super().__init__('Queued payload must be a mapping.')


class UnknownSerializerError(SerializationError):
    """The ``SERIALIZER`` setting named a format this package cannot build."""

    def __init__(self, name: object) -> None:
        """Name the refused serializer and the ones that exist."""
        known = sorted(SERIALIZERS)
        super().__init__(f'Unknown serializer {name!r}, expected one of {known}.')


class PickleReadRefusedError(SerializationError):
    """A pickled payload was read while ``ALLOW_PICKLE`` was off."""

    def __init__(self) -> None:
        """Explain the refusal and the setting that lifts it."""
        super().__init__(
            'Refusing to unpickle a queued payload, because unpickling queue data '
            "is code execution. Set TELEGRAM_BOT['ALLOW_PICKLE'] = True to accept "
            'it, and only on a queue nothing untrusted can write to.',
        )


class PickleWriteRefusedError(DjangoRedisAiogramError, ImproperlyConfigured):
    """Pickle was configured for writes that the reader would refuse."""

    def __init__(self, settings_name: str) -> None:
        """Say what the two settings together would do to every message."""
        super().__init__(
            f"{settings_name}['SERIALIZER'] is 'pickle' while ALLOW_PICKLE is False, so "
            'every queued message would be written and then refused on read. Set '
            "ALLOW_PICKLE to True, or use the 'json' serializer.",
        )


CodecValue = TypeVar('CodecValue')


class TypeCodec(ABC, Generic[CodecValue]):
    """One tagged type: how to recognize it, how to write it, how to read it."""

    tag: ClassVar[SerializationTag]

    @abstractmethod
    def matches(self, value: object) -> bool:
        """Report whether this codec owns ``value``."""

    @abstractmethod
    def encode(self, value: CodecValue) -> dict[str, Any]:
        """Return the tagged JSON object that stands for ``value``."""

    @abstractmethod
    def decode(self, payload: dict[str, Any]) -> CodecValue:
        """Rebuild the value that a tagged JSON object stands for."""


class DefaultSentinelCodec(TypeCodec[Default]):
    """aiogram's marker for an unset argument, stored by the name it carries."""

    tag = SerializationTag.DEFAULT

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is a ``Default`` sentinel."""
        return isinstance(value, Default)

    def encode(self, value: Default) -> dict[str, Any]:
        """Tag the sentinel's name, which is all it holds."""
        return {self.tag: value.name}

    def decode(self, payload: dict[str, Any]) -> Default:
        """Rebuild the sentinel from its name."""
        return Default(payload[self.tag])


class ModelCodec(TypeCodec[TelegramObject]):
    """Any aiogram model, tagged with its concrete class name."""

    tag = SerializationTag.MODEL

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is an aiogram model."""
        return isinstance(value, TelegramObject)

    def encode(self, value: TelegramObject) -> dict[str, Any]:
        """Tag the class name and encode the fields, sentinels included."""
        return {self.tag: type(value).__name__, 'data': {key: encode(item) for key, item in dict(value).items()}}

    def decode(self, payload: dict[str, Any]) -> TelegramObject:
        """Look the tagged class up and validate the decoded fields into it."""
        model = _resolve_model(payload[self.tag])
        return model.model_validate({key: decode(item) for key, item in payload['data'].items()})


class InputFileCodec(TypeCodec[InputFile]):
    """The three input files that can be described in JSON.

    ``FSInputFile`` keeps only its path, so the bot container has to be able to
    read it; that is a documented caveat rather than something checkable here.
    """

    tag = SerializationTag.INPUT_FILE

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is an input file."""
        return isinstance(value, InputFile)

    def encode(self, value: InputFile) -> dict[str, Any]:
        """Tag the kind and whatever that kind needs to be rebuilt."""
        common = {'filename': value.filename, 'chunk_size': value.chunk_size}
        if isinstance(value, FSInputFile):
            return {self.tag: FS_INPUT_FILE, 'path': str(value.path), **common}
        if isinstance(value, URLInputFile):
            return {
                self.tag: URL_INPUT_FILE,
                'url': value.url,
                'headers': value.headers,
                'timeout': value.timeout,
                **common,
            }
        if isinstance(value, BufferedInputFile):
            return {
                self.tag: BUFFERED_INPUT_FILE,
                'data': base64.b64encode(value.data).decode('ascii'),
                **common,
            }
        raise UnsupportedInputFileError(value)

    def decode(self, payload: dict[str, Any]) -> InputFile:
        """Rebuild the input file the tagged kind names."""
        kind = payload[self.tag]
        common = {'filename': payload['filename'], 'chunk_size': payload['chunk_size']}
        if kind == FS_INPUT_FILE:
            return FSInputFile(payload['path'], **common)
        if kind == URL_INPUT_FILE:
            return URLInputFile(payload['url'], headers=payload['headers'], timeout=payload['timeout'], **common)
        if kind == BUFFERED_INPUT_FILE:
            return BufferedInputFile(base64.b64decode(payload['data']), **common)
        raise UnknownInputFileKindError(kind)


class DatetimeCodec(TypeCodec[datetime.datetime]):
    """A datetime, as an ISO 8601 string that keeps its offset."""

    tag = SerializationTag.DATETIME

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is a datetime."""
        return isinstance(value, datetime.datetime)

    def encode(self, value: datetime.datetime) -> dict[str, Any]:
        """Tag the ISO form."""
        return {self.tag: value.isoformat()}

    def decode(self, payload: dict[str, Any]) -> datetime.datetime:
        """Parse the ISO form back."""
        return datetime.datetime.fromisoformat(payload[self.tag])


class DateCodec(TypeCodec[datetime.date]):
    """A date, as an ISO 8601 string."""

    tag = SerializationTag.DATE

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is a date."""
        return isinstance(value, datetime.date)

    def encode(self, value: datetime.date) -> dict[str, Any]:
        """Tag the ISO form."""
        return {self.tag: value.isoformat()}

    def decode(self, payload: dict[str, Any]) -> datetime.date:
        """Parse the ISO form back."""
        return datetime.date.fromisoformat(payload[self.tag])


class DecimalCodec(TypeCodec[Decimal]):
    """A Decimal, as its exact string form rather than a lossy float."""

    tag = SerializationTag.DECIMAL

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is a Decimal."""
        return isinstance(value, Decimal)

    def encode(self, value: Decimal) -> dict[str, Any]:
        """Tag the exact string form."""
        return {self.tag: str(value)}

    def decode(self, payload: dict[str, Any]) -> Decimal:
        """Rebuild the Decimal from its string form."""
        return Decimal(payload[self.tag])


class BytesCodec(TypeCodec[bytes]):
    """Raw bytes, base64 encoded."""

    tag = SerializationTag.BYTES

    def matches(self, value: object) -> bool:
        """Report whether ``value`` is bytes."""
        return isinstance(value, bytes)

    def encode(self, value: bytes) -> dict[str, Any]:
        """Tag the base64 form."""
        return {self.tag: base64.b64encode(value).decode('ascii')}

    def decode(self, payload: dict[str, Any]) -> bytes:
        """Decode the base64 form back to bytes."""
        return base64.b64decode(payload[self.tag])


# Order matters: the whole registry runs before encode() falls back to treating a
# value as a plain mapping, which is what keeps an aiogram model from decaying
# into one, and DatetimeCodec has to precede DateCodec because a datetime is a date.
_CODECS: tuple[TypeCodec[Any], ...] = (
    DefaultSentinelCodec(),
    ModelCodec(),
    InputFileCodec(),
    DatetimeCodec(),
    DateCodec(),
    DecimalCodec(),
    BytesCodec(),
)


def encode(value: Any) -> Any:  # noqa: ANN401 - a queued call argument is arbitrary by nature
    """Turn an arbitrary aiogram call argument into JSON-safe data."""
    for codec in _CODECS:
        if codec.matches(value):
            return codec.encode(value)
    if isinstance(value, Enum):
        return encode(value.value)
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode(value: Any) -> Any:  # noqa: ANN401 - the mirror image of encode
    """Reverse :func:`encode`."""
    if isinstance(value, dict):
        for codec in _CODECS:
            if codec.tag in value:
                return codec.decode(value)
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def _resolve_model(name: str) -> type[TelegramObject]:
    """Look a tagged class up in ``aiogram.types``, refusing anything else."""
    model = getattr(types, name, None)
    if not (isinstance(model, type) and issubclass(model, TelegramObject)):
        raise UnknownModelError(name)
    return model


class Serializer(Protocol):
    """What the queue writer and the queue reader need from a format."""

    name: str

    def dumps(self, payload: dict[str, Any]) -> bytes:
        """Turn a queued call into bytes."""
        ...

    def loads(self, raw: bytes) -> dict[str, Any]:
        """Turn bytes back into a queued call."""
        ...


class _TaggedEncoder(json.JSONEncoder):
    """Writes the tagged forms while walking, rather than before it.

    :func:`encode` rebuilds every container and then ``json.dumps`` walks the
    copy, so a payload is traversed twice and duplicated once. ``default`` is
    called only for values JSON cannot write natively, which is exactly the set
    the codecs are for, so the same bytes come out of one pass.

    A model payload is the exception, and stays about as expensive as it was:
    :meth:`ModelCodec.encode` recurses through :func:`encode` per field, and it
    has to — ``encode`` is exported, and a codec that returned half-tagged data
    would break every caller that uses the two functions on their own.
    """

    def default(self, o: Any) -> Any:  # noqa: ANN401 - a queued call argument is arbitrary by nature
        """Tag what JSON cannot write, exactly as :func:`encode` would."""
        for codec in _CODECS:
            if codec.matches(o):
                return codec.encode(o)
        if isinstance(o, Enum):
            return o.value
        return super().default(o)


_ENCODER = _TaggedEncoder()

#: above this many containers the payload is re-encoded the slow way to find out
#: whether it is too deep to read back. A cheap over-estimate of nesting depth:
#: depth can never exceed the number of containers, and braces inside strings
#: only make it fire early, which costs a walk rather than a message
NESTING_GUARD = 900


class JsonSerializer:
    """The default format: tagged JSON, readable and not executable."""

    # .value, not the member: this name gets logged and compared, and a member formats as its class
    name: str = SerializerKind.JSON.value

    def dumps(self, payload: dict[str, Any]) -> bytes:
        """Encode a queued call as JSON bytes."""
        try:
            text = _ENCODER.encode(payload)
        except SerializationError:
            raise
        except (TypeError, ValueError, RecursionError) as error:
            msg = f'Cannot encode payload as JSON: {error}'
            raise SerializationError(msg) from error
        if text.count('{') + text.count('[') >= NESTING_GUARD:
            self._refuse_if_unreadable(payload)
        return text.encode('utf-8')

    @staticmethod
    def _refuse_if_unreadable(payload: dict[str, Any]) -> None:
        """Refuse a payload this writer can produce and no reader can get back.

        The C encoder does not respect Python's recursion limit, but :func:`decode`
        does — so without this a deeply nested call would be queued happily and
        then be undecodable for ever on the consumer, which drops it. Encoding it
        the old way is what raises, and it is only ever reached by payloads far
        larger than any real message.
        """
        try:
            encode(payload)
        except SerializationError:
            raise
        except (TypeError, ValueError, RecursionError) as error:
            msg = f'Cannot encode payload as JSON: {error}'
            raise SerializationError(msg) from error

    def loads(self, raw: bytes) -> dict[str, Any]:
        """Decode JSON bytes back into a queued call."""
        try:
            decoded = decode(json.loads(raw))
        except SerializationError:
            raise
        except (TypeError, ValueError, KeyError, RecursionError) as error:
            msg = f'Cannot decode JSON payload: {error}'
            raise SerializationError(msg) from error
        if not isinstance(decoded, dict):
            raise NonMappingPayloadError
        return decoded


class PickleSerializer:
    """The 1.x format, kept for objects JSON cannot describe."""

    name: str = SerializerKind.PICKLE.value

    def dumps(self, payload: dict[str, Any]) -> bytes:
        """Encode a queued call as pickle bytes."""
        try:
            return pickle.dumps(payload)
        except Exception as error:
            msg = f'Cannot pickle payload: {error}'
            raise SerializationError(msg) from error

    def loads(self, raw: bytes) -> dict[str, Any]:
        """Decode pickle bytes back into a queued call."""
        try:
            decoded = pickle.loads(raw)  # noqa: S301 - gated by ALLOW_PICKLE, a documented trust boundary
        except Exception as error:
            msg = f'Cannot unpickle payload: {error}'
            raise SerializationError(msg) from error
        if not isinstance(decoded, dict):
            raise NonMappingPayloadError
        return decoded


SERIALIZERS: dict[str, type[Serializer]] = {
    SerializerKind.JSON.value: JsonSerializer,
    SerializerKind.PICKLE.value: PickleSerializer,
}


def get_serializer() -> Serializer:
    """Build the serializer that the ``SERIALIZER`` setting names."""
    name = conf['SERIALIZER']
    if name == SerializerKind.PICKLE and not coerce_bool(conf['ALLOW_PICKLE'], f"{SETTINGS_NAME}['ALLOW_PICKLE']"):
        # check E022 reports this, but a WSGI process never runs the checks
        raise PickleWriteRefusedError(SETTINGS_NAME)
    try:
        return SERIALIZERS[name]()
    except KeyError:
        raise UnknownSerializerError(name) from None


def looks_like_json(raw: bytes) -> bool:
    """Report whether the JSON serializer wrote ``raw``."""
    # pickle never starts with whitespace or '{', so leading blanks are safe to skip
    return raw.lstrip()[:1] == b'{'


def loads(raw: bytes) -> dict[str, Any]:
    """Decode a queued payload, detecting which serializer wrote it.

    Detection is what lets a running deployment switch to JSON without draining
    the queue first.
    """
    if looks_like_json(raw):
        return JsonSerializer().loads(raw)
    # from the environment this arrives as a string, and 'false' is truthy
    if not coerce_bool(conf['ALLOW_PICKLE'], f"{SETTINGS_NAME}['ALLOW_PICKLE']"):
        raise PickleReadRefusedError
    return PickleSerializer().loads(raw)
