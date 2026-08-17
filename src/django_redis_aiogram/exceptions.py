"""The exceptions this package raises.

They live together, and build their own messages, so that call sites raise a
named domain error instead of a bare builtin with a formatted string.
"""


class DjangoRedisAiogramError(Exception):
    """Base class for every error this package raises."""


class LoopUnavailableError(DjangoRedisAiogramError, RuntimeError):
    """No event loop in this process can take an update right now.

    A refusal, not a handler failure: nothing has run, so the update is still
    Telegram's to redeliver. The webhook view answers a non-2xx for this and 200
    for everything else, which is the difference between a retry and a loop.

    Raised as one of the two below, so a caller can tell a shutdown from a fault.
    """


class ShuttingDownError(LoopUnavailableError):
    """The update arrived while this process was tearing the bot down."""

    def __init__(self) -> None:
        """Say which window this is, since it closes on its own."""
        super().__init__(
            'the bot is shutting down, so this update was not handled here. '
            'Telegram will redeliver it; another process, or this one after it '
            'restarts, will take it.',
        )


class LoopThreadNotStartedError(LoopUnavailableError):
    """The thread this process gives the loop did not start in time."""

    def __init__(self, timeout: float) -> None:
        """Name the deadline, which is the only number worth acting on."""
        super().__init__(
            f'the event loop thread did not start within {timeout}s, and driving '
            'the loop from this thread instead would put two threads on one loop. '
            'The update was not handled here.',
        )


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
