"""The exceptions this package raises.

They live together, and build their own messages, so that call sites raise a
named domain error instead of a bare builtin with a formatted string.
"""


class DjangoRedisAiogramError(Exception):
    """Base class for every error this package raises."""


class SerializationError(DjangoRedisAiogramError):
    """A payload could not be encoded or decoded."""


class UnknownApiMethodError(DjangoRedisAiogramError, ValueError):
    """A queued payload named something that is not a Telegram API method."""

    def __init__(self, function: str, method_count: int) -> None:
        """Name the rejected method and how many the Bot API actually has."""
        super().__init__(
            f'{function!r} is not a Telegram API method. Queued payloads may only '
            f'name one of the {method_count} methods aiogram exposes for the '
            f'Bot API; see the Serialization page.',
        )
