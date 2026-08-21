"""Watching what comes in, through the one hook both update sources pass.

Polling and webhook both feed the same ``Dispatcher``, so a single update
middleware sees every update exactly once. When the log is off nothing is
registered at all — the cost per update is zero rather than one branch.

FSM transitions come from wrapping the configured storage rather than diffing
state around the handler: ``set_state`` *is* the transition, so it costs no
extra round trip and misses none of the ones that happen inside a nested call,
a filter or a scene. Append-only makes the previous state unnecessary — the
previous row for the same key is it.
"""

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Dispatcher
from aiogram.dispatcher.event.bases import CancelHandler
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.types import TelegramObject, Update
from aiogram.types.update import UpdateTypeLookupError

from django_redis_aiogram.context import correlation_scope, current_correlation_id
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.payloads import describe
from django_redis_aiogram.recorder import Event, recorder

logger = logging.getLogger('django_redis_aiogram')


def state_name(state: StateType) -> str | None:
    """Name a state the way a reader recognizes it, whatever shape it arrives in."""
    if state is None:
        return None
    if isinstance(state, State):
        return state.state
    return str(state)


def event_type(update: Update) -> str:
    """Name the update's type, or nothing when this aiogram does not know it.

    `Update.event_type` raises rather than returning None, and a Bot API newer
    than the installed aiogram is exactly when it does. aiogram itself treats
    that as an update to skip, so the log must not be what turns it into an
    error.
    """
    try:
        return update.event_type
    except UpdateTypeLookupError:
        return ''


def describe_update(update: Update) -> dict[str, Any]:
    """Summarize an update, under the same payload policy a send obeys."""
    message = update.message or update.edited_message
    query = update.callback_query
    return describe(
        {
            'type': event_type(update) or None,
            'text': getattr(message, 'text', None),
            'data': getattr(query, 'data', None),
        }
    )


@dataclass(frozen=True)
class Inbound:
    """What every row about one update needs to name itself."""

    correlation_id: uuid.UUID
    update_id: int | None
    chat_id: int | None
    user_id: int | None
    started: float


class RecordingMiddleware(BaseMiddleware):
    """One row per update, and the correlation scope its replies inherit."""

    async def __call__(
        self,
        handler: Any,  # noqa: ANN401 - aiogram types this as a bare callable
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:  # noqa: ANN401 - whatever the handler chain returns
        """Record the update, then run the handlers inside its correlation scope."""
        if not isinstance(event, Update):
            return await handler(event, data)

        inbound = Inbound(
            correlation_id=new_correlation_id(),
            update_id=event.update_id,
            chat_id=getattr(data.get('event_chat'), 'id', None),
            user_id=getattr(data.get('event_from_user'), 'id', None),
            started=time.monotonic(),
        )
        identifier = inbound.correlation_id
        data['correlation_id'] = identifier

        recorder.record(
            Event(
                kind=EventKind.INBOUND_RECEIVED.value,
                correlation_id=identifier,
                update_id=inbound.update_id,
                function=event_type(event),
                chat_id=inbound.chat_id,
                user_id=inbound.user_id,
                # summarized only for the table: a receiver counting updates has no
                # use for the text, and this is the costly part of recording one
                detail=describe_update(event) if recorder.wants_payload else None,
            )
        )

        # everything a handler sends inherits this id, so a reply is joined to
        # the update that caused it without any project code passing it along
        with correlation_scope(identifier):
            try:
                result = await handler(event, data)
            except CancelHandler:
                raise
            except Exception as error:
                self._record(
                    EventKind.INBOUND_FAILED,
                    inbound,
                    error=error,
                    raw_state=data.get('raw_state'),
                )
                raise
            self._record(EventKind.INBOUND_HANDLED, inbound, raw_state=data.get('raw_state'))
            return result

    @staticmethod
    def _record(
        kind: EventKind,
        inbound: 'Inbound',
        *,
        error: BaseException | None = None,
        raw_state: object = None,
    ) -> None:
        """Record the outcome of one update, with how long the handlers took."""
        recorder.record(
            Event(
                kind=kind.value,
                correlation_id=inbound.correlation_id,
                update_id=inbound.update_id,
                chat_id=inbound.chat_id,
                user_id=inbound.user_id,
                duration_ms=int((time.monotonic() - inbound.started) * 1000),
                error_code=type(error).__name__ if error is not None else '',
                error=str(error) if error is not None else '',
                detail={'state_before': raw_state} if raw_state else {},
            )
        )


class RecordingStorage(BaseStorage):
    """Forwards to the configured storage, recording every state change.

    ``set_state`` is the transition, so this needs no extra read and misses
    none — including the ones a filter or a scene makes.
    """

    def __init__(self, inner: BaseStorage) -> None:
        """Hold the storage everything is forwarded to."""
        self.inner = inner

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Set the state, then record the transition it just made."""
        await self.inner.set_state(key, state)
        recorder.record(
            Event(
                kind=EventKind.FSM_TRANSITION.value,
                correlation_id=current_correlation_id() or new_correlation_id(),
                chat_id=key.chat_id,
                user_id=key.user_id,
                detail={'to': state_name(state), 'destiny': key.destiny, 'thread_id': key.thread_id},
            )
        )

    async def get_state(self, key: StorageKey) -> str | None:
        """Forward unchanged: reading a state is not a transition."""
        return await self.inner.get_state(key)

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        """Forward unchanged."""
        await self.inner.set_data(key, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """Forward unchanged."""
        return await self.inner.get_data(key)

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Forward rather than inherit: a storage may make this one round trip."""
        return await self.inner.update_data(key, data)

    async def close(self) -> None:
        """Forward: TelegramBot.close releases the storage through this."""
        await self.inner.close()


def install_instrumentation(dispatcher: Dispatcher) -> None:
    """Register the update middleware, unless nothing is reading events.

    Returning before anything is built is what makes the inactive cost zero:
    there is no middleware in the chain at all, not one that checks a flag.

    Read once, when the dispatcher is built — so a receiver connected after the
    first update arrives will not see updates in this process. Connect them while
    the apps load, which is where Django says signal receivers belong anyway.
    """
    if not recorder.active:
        return
    dispatcher.update.outer_middleware.register(RecordingMiddleware())


def instrumented(storage: BaseStorage) -> BaseStorage:
    """Wrap the storage when something reads events, and hand it back untouched when not.

    Same one-shot reading as :func:`install_instrumentation`, and for the same
    reason: the storage is built once.
    """
    if not recorder.active:
        return storage
    return RecordingStorage(storage)
