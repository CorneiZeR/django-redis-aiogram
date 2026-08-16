"""The backend that moves queued messages from Redis to Telegram.

``blpop`` is the only consumer: a blocking pop needs no server configuration,
works on any database index, delivers immediately and leaves messages in the
list while the worker is down. The keyspace consumer 1.x used was removed in
3.0 — it needed ``CONFIG SET notify-keyspace-events``, which managed Redis
providers usually refuse, and it could not deliver before the TTL elapsed.

It consumes crash-safely where the server allows it: a message is moved to a
processing list while it is being sent and removed afterwards, so a worker
killed mid-send leaves it behind to be reclaimed on the next start. That makes
delivery at-least-once — after a crash a message may be sent twice. Servers
older than Redis 6.2 lack ``LMOVE``; there the consumer falls back to plain
pops, which is the 1.x at-most-once behaviour, and says so in the log.
"""

import hashlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from redis.exceptions import ResponseError

from django_redis_aiogram.api import check_function
from django_redis_aiogram.enums import DeliveryKind, EventKind
from django_redis_aiogram.envelope import Envelope, UnknownEnvelopeVersionError, unpack
from django_redis_aiogram.events import new_correlation_id, worker_identity
from django_redis_aiogram.recorder import Event, as_identifier, recorder
from django_redis_aiogram.redis import as_bytes, get_redis, read_timeout
from django_redis_aiogram.serializers import PickleReadRefusedError, SerializationError, loads
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')

Handler = Callable[..., Any]


class Delivery(ABC):
    """Consumes the Redis queue until stopped."""

    def __init__(self, handler: Handler) -> None:
        """Take what each decoded message is handed to once it arrives."""
        self.handler = handler
        self._stop = threading.Event()
        self._reliable = True
        self._beat_at = 0.0

    @property
    def queue_key(self) -> str:
        """The list queued messages are written to and read from."""
        return str(conf['REDIS_MESSAGES_KEY'])

    @property
    def processing_key(self) -> str:
        """Per-worker, so a restarting worker reclaims only its own messages.

        A shared list would let a starting worker pull a message back out from
        under another worker that is still sending it.
        """
        return f'{self.queue_key}:processing:{worker_identity()}'

    @abstractmethod
    def run(self) -> None:
        """Block, consuming messages, until :meth:`stop` is called."""

    def stop(self) -> None:
        """Ask :meth:`run` to return after its current read."""
        self._stop.set()

    def start_thread(self) -> threading.Thread:
        """Run the consumer on a daemon thread and return it."""
        thread = threading.Thread(target=self.run, name='tgbot-delivery', daemon=True)
        thread.start()
        return thread

    def reclaim(self) -> bool:
        """Requeue messages a crashed worker left in the processing list.

        Also the probe for crash-safe mode: on a server without LMOVE the very
        first call fails, and the consumer downgrades to plain pops.

        Returns whether the list is settled; False means the caller should try
        again, because a Redis that was unreachable at startup left messages
        stranded there.
        """
        connection = get_redis()
        count = 0
        try:
            # RIGHT->LEFT keeps the original order at the front of the queue
            while connection.lmove(self.processing_key, self.queue_key, 'RIGHT', 'LEFT'):
                count += 1
        except ResponseError as error:
            if not self._downgrade_without_lmove(error):
                # WRONGTYPE, NOPERM and friends say nothing about LMOVE support
                logger.exception(
                    'could not reclaim previous messages, will retry',
                    extra={'tg_key': self.processing_key},
                )
                return False
            return True
        except Exception:
            # run() is the thread target, so anything escaping here — a Redis
            # that is not up yet, for one — would end the consumer for good
            logger.exception(
                'could not reclaim previous messages, will retry',
                extra={'tg_key': self.processing_key},
            )
            return False
        if count:
            logger.info(
                'reclaimed messages from a previous run',
                extra={'tg_key': self.queue_key, 'tg_count': count},
            )
        return True

    def _downgrade_without_lmove(self, error: ResponseError) -> bool:
        """Fall back to plain pops when the server has no LMOVE, and say whether it did.

        Shared by the two places that reach for LMOVE first, so a caller draining by
        hand gets the same downgrade the consumer loop gets rather than the raw error.
        """
        if 'unknown command' not in str(error).lower():
            return False
        if self._reliable:
            self._reliable = False
            logger.warning(
                'crash-safe delivery unavailable: this Redis predates LMOVE (6.2); '
                'a worker killed mid-send may lose that one message',
                extra={'tg_key': self.queue_key},
            )
        return True

    @property
    def heartbeat_key(self) -> str:
        """Per worker, like the in-flight list: each one answers for itself."""
        return f'{self.queue_key}:heartbeat:{worker_identity()}'

    def heartbeat(self) -> None:
        """Say the loop is still turning, at most once per HEARTBEAT_INTERVAL.

        A container cannot see a thread in another process. This key is what
        ``tgbot_healthcheck`` reads, and refreshing it per message would be a
        write per message, so it is paced.
        """
        interval = max(1, int(conf['HEARTBEAT_INTERVAL']))
        now = time.monotonic()
        if now - self._beat_at < interval:
            return
        self._beat_at = now
        try:
            get_redis().set(self.heartbeat_key, str(int(time.time())), ex=interval * 3)
        except Exception:
            # the loop must keep consuming even when it cannot say so
            logger.exception('could not write the heartbeat', extra={'tg_key': self.heartbeat_key})

    def acknowledge(self, raw: bytes | str) -> None:
        """Drop a delivered message from the processing list."""
        if not self._reliable:
            return
        try:
            # redis-py's stubs say str, but bytes round-trip identically
            get_redis().lrem(self.processing_key, 1, raw)  # type: ignore[arg-type]
        except Exception:
            # worst case the message is redelivered on the next start
            logger.exception(
                'failed to acknowledge a delivered message',
                extra={'tg_key': self.processing_key},
            )

    def _read(self, raw: bytes) -> tuple['Envelope | None', bool]:
        """Turn one message off the queue into an envelope, or into a verdict.

        Everything here is untrusted input, so no failure may escape: what comes
        back is either the envelope or `None` plus whether to acknowledge the
        message that never became one.
        """
        try:
            payload = loads(raw)
        except PickleReadRefusedError:
            logger.exception(
                'leaving a refused pickle message in flight; set ALLOW_PICKLE to deliver it',
                extra={'tg_key': self.processing_key},
            )
            return None, False
        except SerializationError:
            self._record_undecodable(raw, 'serialization')
            logger.exception('dropping undecodable queued message')
            return None, True
        except Exception:
            self._record_undecodable(raw, 'unknown')
            logger.exception('dropping queued message that failed to decode')
            return None, True
        try:
            return unpack(payload), True
        except UnknownEnvelopeVersionError:
            # written by a newer producer than this consumer understands, so
            # leaving it in flight is what lets an upgrade deliver it
            logger.exception('leaving a message from a newer version in flight')
            return None, False
        except Exception:
            # MalformedEnvelopeError and whatever else a hostile payload can
            # provoke: nothing will ever make sense of it, so it is
            # acknowledged rather than left to come back for ever — and this
            # reader is on the far side of a trust boundary, where an escaping
            # exception would end the consumer for the life of the container
            self._record_undecodable(raw, 'envelope')
            logger.exception('dropping a queued message whose envelope cannot be read')
            return None, True

    def dispatch(self, raw: bytes) -> bool:
        """Decode one message and hand it to the handler.

        A bad payload is one message's problem, so everything short of a kill is
        logged and dropped: the consumer has to survive it to deliver the rest.

        Returns whether the message should be acknowledged. Two cases say no: a
        pickle the configuration refuses, and an envelope from a newer version.
        Both are valid payloads somebody else can deliver, so they stay in
        flight — acknowledging would destroy them over a setting or a deploy
        order.
        """
        envelope, acknowledge = self._read(raw)
        if envelope is None:
            return acknowledge
        try:
            check_function(envelope.function)
        except ValueError:
            self._record(
                EventKind.QUEUE_REJECTED,
                envelope,
                error='not a Telegram API method',
            )
            logger.exception(
                'dropping queued message naming a method that is not Telegram API',
                extra={'tg_function': envelope.function},
            )
            return True
        self._record(EventKind.OUTBOUND_CONSUMED, envelope)
        try:
            # by keyword, the way 2.x splatted it: a handler taking **kwargs
            # only — which every documented recipe does — refuses a positional
            self.handler(
                function=envelope.function,
                correlation_id=envelope.correlation_id,
                queued_at=envelope.queued_at,
                **envelope.kwargs,
            )
        except Exception:
            logger.exception(
                'handler failed for queued message',
                extra={'tg_function': envelope.function},
            )
        return True

    def _record(self, kind: EventKind, envelope: Envelope, error: str = '') -> None:
        """Record what the consumer did with one message."""
        chat_id = envelope.kwargs.get('chat_id')
        recorder.record(
            Event(
                kind=kind.value,
                correlation_id=envelope.correlation_id or new_correlation_id(),
                function=envelope.function,
                chat_id=as_identifier(chat_id),
                worker=worker_identity(),
                error=error,
                detail=self._queue_latency(envelope),
            )
        )

    @staticmethod
    def _queue_latency(envelope: Envelope) -> dict[str, Any]:
        """How long the message waited, when the producer said when it was queued."""
        if not envelope.queued_at:
            return {}
        return {'queue_ms': int((time.time() - envelope.queued_at) * 1000)}

    def _record_undecodable(self, raw: bytes, reason: str) -> None:
        """Record a payload nothing could read.

        A fingerprint, never the bytes: an undecodable payload is by definition
        untrusted input and may be a pickle, so putting it in a JSON column
        would spread it into every log shipper and admin page downstream.
        """
        recorder.record(
            Event(
                kind=EventKind.QUEUE_UNDECODABLE.value,
                worker=worker_identity(),
                error=reason,
                detail={'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()[:16]},
            )
        )

    def consume_pending(self) -> None:
        """Drain the queue without blocking, acknowledging each message."""
        connection = get_redis()
        raw: bytes | str | None
        while not self._stop.is_set():
            if self._reliable:
                try:
                    raw = connection.lmove(self.queue_key, self.processing_key, 'LEFT', 'RIGHT')
                except ResponseError as error:
                    # run() learns this from reclaim(); nothing probes for a caller
                    # draining by hand, so without this the first pop against a
                    # pre-6.2 server raises out of a documented helper
                    if not self._downgrade_without_lmove(error):
                        raise
                    continue
            else:
                # lpop only widens to a list when given a count
                raw = connection.lpop(self.queue_key)  # type: ignore[assignment]
            if raw is None:
                return
            if self.dispatch(as_bytes(raw)):
                self.acknowledge(raw)


class BlpopDelivery(Delivery):
    """Blocks on the queue itself, so a message is delivered as it arrives."""

    def run(self) -> None:
        """Block on the queue until :meth:`stop` is called."""
        # 0 means "block for ever" in Redis, which would swallow stop(). The
        # heartbeat is written between reads, so a read longer than its interval
        # would let the key expire under a consumer that is doing fine
        interval = max(1, int(conf['HEARTBEAT_INTERVAL']))
        # and the read deadline caps it too: asking BLPOP to wait longer than
        # the socket will wait for an answer turns an idle round into an error
        timeout = max(1, min(int(conf['BLPOP_TIMEOUT']), interval, read_timeout() - 1))
        connection = get_redis()
        reclaimed = self.reclaim()
        logger.info(
            'delivery started',
            extra={
                'tg_delivery': DeliveryKind.BLPOP.value,
                'tg_key': self.queue_key,
                'tg_timeout': timeout,
                'tg_crash_safe': self._reliable,
            },
        )
        raw: bytes | str | None
        while not self._stop.is_set():
            self.heartbeat()
            if not reclaimed:
                reclaimed = self.reclaim()
            try:
                if self._reliable:
                    raw = connection.blmove(self.queue_key, self.processing_key, timeout, 'LEFT', 'RIGHT')
                else:
                    item = connection.blpop([self.queue_key], timeout=timeout)
                    raw = None if item is None else item[1]
            except Exception:
                # a dropped connection must not kill the worker thread
                logger.exception('blocking pop failed, retrying', extra={'tg_key': self.queue_key})
                self._stop.wait(timeout)
                continue
            if raw is None:
                continue
            if self.dispatch(as_bytes(raw)):
                self.acknowledge(raw)


# keyed by the enum's value, so the keys are the plain strings the setting holds
DELIVERIES: dict[str, type[Delivery]] = {
    DeliveryKind.BLPOP.value: BlpopDelivery,
}


def get_delivery(handler: Handler) -> Delivery:
    """Build the consumer the DELIVERY setting names."""
    name = conf['DELIVERY']
    try:
        return DELIVERIES[name](handler)
    except KeyError:
        msg = f'Unknown delivery {name!r}, expected one of {sorted(DELIVERIES)}.'
        raise ValueError(msg) from None
