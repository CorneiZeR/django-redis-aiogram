"""The correlation identifier in scope, so a reply inherits the update's id.

A handler that answers an update should produce rows joinable to the update that
caused it, without every project threading an argument through its own code. A
context variable does that: aiogram runs each update's chain in its own task,
and a task copies the context at creation, so one update cannot leak into
another.

Read it **before** scheduling, never inside the coroutine. ``_hand_off`` creates
its task from a ``call_soon_threadsafe`` callback whose context belongs to the
loop rather than to the handler, so a read from in there would come back empty.
"""

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

_current: ContextVar[uuid.UUID | None] = ContextVar('django_redis_aiogram_correlation_id', default=None)


def current_correlation_id() -> uuid.UUID | None:
    """Return the identifier of whatever is being handled here, if anything is."""
    return _current.get()


@contextlib.contextmanager
def correlation_scope(correlation_id: uuid.UUID) -> Iterator[None]:
    """Run a block with this identifier in scope, restoring the previous one."""
    token = _current.set(correlation_id)
    try:
        yield
    finally:
        _current.reset(token)
