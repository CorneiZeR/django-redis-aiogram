# Delivery

`send_redis` pushes a serialized call onto a Redis list. The bot container
consumes that list and makes the call.

This page is about outbound messages: how a queued `bot.send()` reaches
Telegram. Which way *updates* arrive — polling or webhook — is a separate
choice, described in **[[Webhook]]**; the queue works the same under both.

## blpop

The consumer blocks on `BLPOP`, so a message is picked up the moment it is
queued. No server configuration, any database index, and a backlog simply
waits until the worker comes back.

`BLPOP_TIMEOUT` is only how often the block is interrupted to check whether the
worker is shutting down. It does not delay delivery.

It is also capped just below `REDIS_TIMEOUT`, the deadline on any single Redis
call. A pop asked to wait longer than the socket will wait for an answer turns
every idle round into an error, so raising `BLPOP_TIMEOUT` above the deadline
would break a consumer that is doing nothing wrong. Check `W004` says so before
deployment; raise `REDIS_TIMEOUT` too if you want longer blocks.

`DELIVERY` names the consumer and `'blpop'` is its only value. The `keyspace`
consumer 1.x used — write a key with a TTL, react to its expiry event — was
removed in 3.0: it needed `CONFIG SET notify-keyspace-events`, which managed
providers refuse, and nothing could be delivered before the TTL elapsed. If your
settings still say `'keyspace'`, check `E009` fails `manage.py check` and names
the value to use.

## Running more than one worker

The consumer takes each message once — `BLMOVE` and `BLPOP` are atomic. Running
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

`Delivery.dispatch()` is what decides that, and its return value is the whole
contract: `True` means the consumer acknowledges the message — removing it from
the in-flight list — and `False` means it does not, leaving the message there
for a later run to reclaim. Withholding the acknowledgement only saves the
message where there *is* an in-flight list: without `LMOVE` the message was
already popped before it was refused, so `False` and `True` come to the same
thing and it is gone. Exactly one case returns `False` today: a pickled
payload refused because `ALLOW_PICKLE` is off, which is the one failure a
change of configuration can undo. Everything else — undecodable bytes, a method
that is not Telegram API, a handler that raised — returns `True`, because
redelivering it would only fail again.

## What happens to a broken message

A payload that cannot be decoded is logged and dropped; the consumer moves on.
A handler that raises is logged the same way. Neither stops the worker.

## Shutting down

`SIGTERM` — what `docker stop` sends — unwinds polling, stops the consumer,
closes the aiogram session and the FSM storage. Messages already in the list
stay there for the next start.
