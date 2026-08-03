# Delivery

`send_redis` pushes a serialized call onto a Redis list. The bot container
consumes that list and makes the call.

Two consumers are available.

| | `blpop` (default) | `keyspace` |
| --- | --- | --- |
| Server configuration | none | `CONFIG SET notify-keyspace-events` |
| Managed Redis | works | usually refused |
| Latency | immediate | `REDIS_EXP_TIME` at the earliest |
| Worker was down | messages wait in the list | the list is drained at the next start |
| Several workers | safe, one takes each message | safe, but pointless duplication of effort |

Use `blpop` unless you have a reason not to.

## blpop

The consumer blocks on `BLPOP`, so a message is picked up the moment it is
queued. No server configuration, any database index, and a backlog simply
waits until the worker comes back.

`BLPOP_TIMEOUT` is only how often the block is interrupted to check whether the
worker is shutting down. It does not delay delivery.

## keyspace

This reproduces the 1.x mechanism: `send_redis` also writes a key with a TTL,
and the consumer subscribes to the expiry event for that key.

```python
TELEGRAM_BOT = {'DELIVERY': 'keyspace'}
```

Two things to know:

- it needs `notify-keyspace-events` to include `Ex`. The worker tries to set it
  at startup; managed providers refuse `CONFIG SET`, in which case you get a
  warning and have to enable it server-side
- nothing is delivered until the TTL elapses, and Redis emits the expiry event
  when it gets to it, so `REDIS_EXP_TIME` is a floor on latency, not a bound
- expiry events are not replayed, so anything queued while the worker was down
  would sit there unseen. The consumer drains the list once at startup to cover
  that; between restarts a missed event still means waiting for the next one

The channel is derived from the database index in `REDIS_URL`. In 1.x it was
hardcoded to database 0, so any other index silently delivered nothing.

## Running more than one worker

Both consumers take each message once — `blpop` and `LPOP` are atomic. Running
several bot containers is safe, though a single one handles a lot: the limits
in **[[Rate limits]]** bind long before the consumer does.

## Crash safety

On Redis 6.2+ a message is moved to `<queue>:processing` while it is being
sent and removed once the handler returns. A worker killed mid-send leaves it
there, and the next start reclaims it — delivery is **at-least-once**, so a
crash can cause a duplicate send.

Older servers lack `LMOVE`; the consumer says so in the log and falls back to
plain pops, which is the 1.x at-most-once behaviour: a kill between the pop
and the send loses that one message.

The in-flight list is **per worker**: `<REDIS_MESSAGES_KEY>:processing:<name>`,
where `<name>` is `WORKER_NAME` when it is set and the hostname (`HOSTNAME`, or
what the host reports) otherwise. A restarted container keeps its name, which is
what lets it reclaim its own interrupted messages and never pull one out from
under a worker that is still sending it. If several workers share a host, give
each its own `WORKER_NAME` — otherwise they share a list and can duplicate each
other's sends.

Handler errors are not crashes: a message whose send *failed* is acknowledged
and logged, not redelivered forever.

## What happens to a broken message

A payload that cannot be decoded is logged and dropped; the consumer moves on.
A handler that raises is logged the same way. Neither stops the worker.

## Shutting down

`SIGTERM` — what `docker stop` sends — unwinds polling, stops the consumer,
closes the aiogram session and the FSM storage. Messages already in the list
stay there for the next start.
