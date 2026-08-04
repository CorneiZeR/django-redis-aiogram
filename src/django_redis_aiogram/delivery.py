"""Backends that move queued messages from Redis to Telegram.

``blpop`` is the default: a blocking pop needs no server configuration, works
on any database index, delivers immediately and leaves messages in the list
while the worker is down.

``keyspace`` reproduces the 1.x mechanism — write a key with a TTL and react to
its expiry event. It needs ``CONFIG SET notify-keyspace-events``, which managed
Redis providers usually refuse, and it only delivers once the TTL elapses.

Both consume crash-safely where the server allows it: a message is moved to a
processing list while it is being sent and removed afterwards, so a worker
killed mid-send leaves it behind to be reclaimed on the next start. That makes
delivery at-least-once — after a crash a message may be sent twice. Servers
older than Redis 6.2 lack ``LMOVE``; there the consumers fall back to plain
pops, which is the 1.x at-most-once behaviour, and say so in the log.
"""

import contextlib
import logging
import os
import socket
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from redis.client import PubSub
from redis.exceptions import ResponseError

from django_redis_aiogram.api import check_function
from django_redis_aiogram.redis import as_bytes, get_db_index, get_redis
from django_redis_aiogram.serializers import SerializationError, loads
from django_redis_aiogram.settings import conf

logger = logging.getLogger("django_redis_aiogram")

Handler = Callable[..., Any]

BLPOP_DELIVERY = "blpop"
KEYSPACE_DELIVERY = "keyspace"


def worker_identity() -> str:
    """Name this worker's processing list.

    Defaults to the hostname, which a container keeps across restarts — that is
    what lets a restarted worker find its own interrupted messages. Set
    WORKER_NAME when several workers share a host.
    """
    configured = conf.get("WORKER_NAME")
    if configured:
        return str(configured)
    return os.environ.get("HOSTNAME") or socket.gethostname()


class Delivery(ABC):
    """Consumes the Redis queue until stopped."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self._stop = threading.Event()
        self._reliable = True
        self._beat_at = 0.0

    @property
    def queue_key(self) -> str:
        return str(conf["REDIS_MESSAGES_KEY"])

    @property
    def processing_key(self) -> str:
        """Per-worker, so a restarting worker reclaims only its own messages.

        A shared list would let a starting worker pull a message back out from
        under another worker that is still sending it.
        """
        return f"{self.queue_key}:processing:{worker_identity()}"

    @abstractmethod
    def run(self) -> None:
        """Block, consuming messages, until :meth:`stop` is called."""

    def stop(self) -> None:
        self._stop.set()

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="tgbot-delivery", daemon=True)
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
            while connection.lmove(self.processing_key, self.queue_key, "RIGHT", "LEFT"):
                count += 1
        except ResponseError as error:
            if "unknown command" not in str(error).lower():
                # WRONGTYPE, NOPERM and friends say nothing about LMOVE support
                logger.exception(
                    "could not reclaim previous messages, will retry",
                    extra={"tg_key": self.processing_key},
                )
                return False
            self._reliable = False
            logger.warning(
                "crash-safe delivery unavailable: this Redis predates LMOVE (6.2); "
                "a worker killed mid-send may lose that one message",
                extra={"tg_key": self.queue_key},
            )
            return True
        except Exception:
            # run() is the thread target, so anything escaping here — a Redis
            # that is not up yet, for one — would end the consumer for good
            logger.exception(
                "could not reclaim previous messages, will retry",
                extra={"tg_key": self.processing_key},
            )
            return False
        if count:
            logger.info(
                "reclaimed messages from a previous run",
                extra={"tg_key": self.queue_key, "tg_count": count},
            )
        return True

    @property
    def heartbeat_key(self) -> str:
        """Per worker, like the in-flight list: each one answers for itself."""
        return f"{self.queue_key}:heartbeat:{worker_identity()}"

    def heartbeat(self) -> None:
        """Say the loop is still turning, at most once per HEARTBEAT_INTERVAL.

        A container cannot see a thread in another process. This key is what
        ``tgbot_healthcheck`` reads, and refreshing it per message would be a
        write per message, so it is paced.
        """
        interval = max(1, int(conf["HEARTBEAT_INTERVAL"]))
        now = time.monotonic()
        if now - self._beat_at < interval:
            return
        self._beat_at = now
        try:
            get_redis().set(self.heartbeat_key, str(int(time.time())), ex=interval * 3)
        except Exception:
            # the loop must keep consuming even when it cannot say so
            logger.exception("could not write the heartbeat", extra={"tg_key": self.heartbeat_key})

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
                "failed to acknowledge a delivered message",
                extra={"tg_key": self.processing_key},
            )

    def dispatch(self, raw: bytes) -> None:
        try:
            payload = loads(raw)
        except SerializationError:
            logger.exception("dropping undecodable queued message")
            return
        except Exception:
            logger.exception("dropping queued message that failed to decode")
            return
        try:
            check_function(str(payload.get("function", "")))
        except ValueError:
            logger.exception(
                "dropping queued message naming a method that is not Telegram API",
                extra={"tg_function": payload.get("function")},
            )
            return
        try:
            self.handler(**payload)
        except Exception:
            logger.exception(
                "handler failed for queued message",
                extra={"tg_function": payload.get("function")},
            )

    def consume_pending(self) -> None:
        """Drain the queue without blocking, acknowledging each message."""
        connection = get_redis()
        raw: bytes | str | None
        while not self._stop.is_set():
            if self._reliable:
                raw = connection.lmove(self.queue_key, self.processing_key, "LEFT", "RIGHT")
            else:
                # lpop only widens to a list when given a count
                raw = connection.lpop(self.queue_key)  # type: ignore[assignment]
            if raw is None:
                return
            self.dispatch(as_bytes(raw))
            self.acknowledge(raw)


class BlpopDelivery(Delivery):
    def run(self) -> None:
        connection = get_redis()
        # 0 means "block for ever" in Redis, which would swallow stop(). The
        # heartbeat is written between reads, so a read longer than its interval
        # would let the key expire under a consumer that is doing fine
        interval = max(1, int(conf["HEARTBEAT_INTERVAL"]))
        timeout = max(1, min(int(conf["BLPOP_TIMEOUT"]), interval))
        reclaimed = self.reclaim()
        logger.info(
            "delivery started",
            extra={
                "tg_delivery": BLPOP_DELIVERY,
                "tg_key": self.queue_key,
                "tg_timeout": timeout,
                "tg_crash_safe": self._reliable,
            },
        )
        raw: bytes | str | None
        while not self._stop.is_set():
            self.heartbeat()
            if not reclaimed:
                reclaimed = self.reclaim()
            try:
                if self._reliable:
                    raw = connection.blmove(self.queue_key, self.processing_key, timeout, "LEFT", "RIGHT")
                else:
                    item = connection.blpop([self.queue_key], timeout=timeout)
                    raw = None if item is None else item[1]
            except Exception:
                # a dropped connection must not kill the worker thread
                logger.exception("blocking pop failed, retrying", extra={"tg_key": self.queue_key})
                self._stop.wait(timeout)
                continue
            if raw is None:
                continue
            self.dispatch(as_bytes(raw))
            self.acknowledge(raw)


class KeyspaceDelivery(Delivery):
    def run(self) -> None:
        channel = f"__keyevent@{get_db_index()}__:expired"
        pubsub: PubSub | None = None
        reclaimed = False
        try:
            while not self._stop.is_set():
                try:
                    self.heartbeat()
                    if pubsub is None:
                        pubsub = self._subscribe(channel)
                    if not reclaimed:
                        reclaimed = self.reclaim()
                        if reclaimed:
                            # no expiry event announces what the retry put back
                            self.consume_pending()
                    pubsub.get_message(timeout=1.0)
                except Exception:
                    # setting up is as much a network call as reading: a Redis
                    # that is not up yet must not end the consumer thread
                    logger.exception("keyspace consumer error, retrying")
                    pubsub = self._close(pubsub)
                    self._stop.wait(1.0)
        finally:
            self._close(pubsub)

    def _subscribe(self, channel: str) -> PubSub:
        self._enable_notifications()
        pubsub: PubSub = get_redis().pubsub(  # type: ignore[no-untyped-call]
            ignore_subscribe_messages=True
        )
        pubsub.subscribe(**{channel: self._on_expired})
        logger.info(
            "delivery started",
            extra={
                "tg_delivery": KEYSPACE_DELIVERY,
                "tg_channel": channel,
                "tg_crash_safe": self._reliable,
            },
        )
        return pubsub

    @staticmethod
    def _close(pubsub: PubSub | None) -> None:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                pubsub.close()

    def _enable_notifications(self) -> None:
        connection = get_redis()
        try:
            current = connection.config_get("notify-keyspace-events")
            flags = str(current.get("notify-keyspace-events", ""))
            if "E" in flags and ("x" in flags or "A" in flags):
                return
            connection.config_set("notify-keyspace-events", f"{flags}Ex")
        except ResponseError as error:
            logger.warning(
                "cannot enable keyspace notifications; enable them server-side or "
                "switch TELEGRAM_BOT['DELIVERY'] to 'blpop'",
                extra={"tg_error": str(error)},
            )
        except Exception:
            # a refusal is normal; anything else is retried by the loop above,
            # but it must be logged where it happened
            logger.exception("could not probe keyspace notifications, continuing")

    def _on_expired(self, message: dict[str, Any]) -> None:
        data = message["data"]
        # pubsub hands back str when the URL enables decode_responses
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        if text != conf["REDIS_EXP_KEY"]:
            return
        self.consume_pending()


DELIVERIES: dict[str, type[Delivery]] = {
    BLPOP_DELIVERY: BlpopDelivery,
    KEYSPACE_DELIVERY: KeyspaceDelivery,
}


def get_delivery(handler: Handler) -> Delivery:
    name = conf["DELIVERY"]
    try:
        return DELIVERIES[name](handler)
    except KeyError:
        raise ValueError(f"Unknown delivery {name!r}, expected one of {sorted(DELIVERIES)}.") from None
