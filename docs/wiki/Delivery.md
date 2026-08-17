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
in **[[Rate-limits|Rate limits]]** bind long before the consumer does.

## Crash safety

On Redis 6.2+ a message is moved to `<queue>:processing` while it is being
sent and removed once the send has actually finished. A worker killed mid-send
leaves it there, and the next start reclaims it — delivery is **at-least-once**,
so a crash can cause a duplicate send.

Before 3.1.0 it was removed when the *handler returned*, and `send_raw` returns
as soon as the coroutine is scheduled. In polling mode that meant the message
left the in-flight list before Telegram had seen it, and this guarantee was not
true: a `docker stop` with a backlog delivered what the drain had time for and
lost the rest, with nothing left to redeliver. Webhook mode always had it,
because there the consumer drives the send to completion itself.

Duplicates after a kill are therefore real now where they were not before.
**Build idempotency on your own business key**, not on `correlation_id`: a
handler's replies inherit the id of the update that caused them, so it is not
one per message.

### It follows the handler, not the queue

Waiting for the send is something the *handler* opts into, by taking an
`on_complete` keyword. `bot.send_raw` does, and `manage.py start_tgbot` uses it,
so a normal worker has the guarantee above.

A handler of your own that takes only `**kwargs` — the shape every recipe on
**[[Testing]]** uses — keeps the pre-3.1.0 semantics exactly: it is acknowledged
the moment it returns, which is at-most-once if it goes on to do the sending
somewhere else. That is deliberate, so existing handlers are not silently made to
hold messages they never release. Take `on_complete` and call it when your send
finishes if you want the message held until then.

`MAX_IN_FLIGHT` bounds how many sends the consumer will leave outstanding before
it stops taking messages. The default, `0`, is no bound. It is worth setting on a
worker that sees large backlogs: acknowledging is an `LREM`, which scans the
in-flight list, so an unbounded one turns draining a backlog into quadratic work.

`REQUIRE_CRASH_SAFE` refuses to start at all where `LMOVE` is missing, rather
than running at-most-once and only saying so in a log line. The check happens
before the consumer thread starts — a failure inside it would kill the thread and
leave the process polling updates with nothing draining the queue.

Older servers lack `LMOVE`; the consumer says so in the log and falls back to
plain pops, which is the 1.x at-most-once behaviour: a kill between the pop
and the send loses that one message.

The in-flight list is **per worker**: `<REDIS_MESSAGES_KEY>:processing:<name>`,
where `<name>` is `WORKER_NAME` when it is set and the hostname (`HOSTNAME`, or
what the host reports) otherwise.

**The name has to survive the container.** That is what lets a worker reclaim its
own interrupted messages, and never pull one out from under a worker still
sending. A container started without `hostname:` does *not* keep its name —
Docker invents a fresh twelve-character one for each container it creates — so
whenever one is replaced rather than restarted in place, whatever the last one
was sending is stranded in a list nothing will look at again. `docker compose up`
after a change, a rescheduled pod, a redeploy: each is a new container. Set
`WORKER_NAME`, or give the container a fixed `hostname:`; check `W010` reports
the case it can detect.

The list is keyed on the resolved name, so the other half is the collision: two
workers that resolve to the *same* name share one in-flight list, and each will
reclaim what the other is still sending. Sharing a host is the common way to
arrive there, but so is copying a `WORKER_NAME` or a fixed `hostname:` between
services. Give each worker its own.

`manage.py tgbot_reclaim --worker <name>` is the way back from a list that is
already stranded. It is deliberately manual: naming a worker is a human saying it
is gone, and nothing here probes for liveness, because a slow worker looks
exactly like a dead one and taking its message back sends it twice.

Two flags exist because that judgement can be wrong. `--dry-run` reports what is
there and moves nothing, so you can name a worker before committing to the claim
that it is gone — it applies `--limit` to its report, so what it says it would
move is what a real run moves. `--limit <n>` bounds a single run, which keeps the
blast radius of a mistaken name to `n` messages rather than a whole list.

`manage.py tgbot_healthcheck` reports how many messages sit under other worker
names, so a stranded pile stops being invisible. That count is a floor, not a
total: the sweep behind it is bounded, because `SCAN` walks the whole keyspace
and the probe runs on a timer. It says so when it stopped early.

Handler errors are not crashes: a message whose send *failed* is acknowledged
and logged, not redelivered forever.

`Delivery.dispatch()` is what decides that, and its return value is the whole
contract: `True` means the consumer acknowledges the message — removing it from
the in-flight list — and `False` means it does not, leaving the message there
for a later run to reclaim. Withholding the acknowledgement only saves the
message where there *is* an in-flight list: without `LMOVE` the message was
already popped before it was refused, so `False` and `True` come to the same
thing and it is gone. Three cases return `False` today. A pickled payload
refused because `ALLOW_PICKLE` is off, which is the one failure a change of
configuration can undo; a handler that accepted `on_complete`, where the
acknowledgement is not withheld but deferred to the send; and a send cancelled
rather than failed, which reached nothing and is left for a reclaim. Everything
else — undecodable bytes, a method that is not Telegram API, a handler that
raised before it scheduled anything — returns `True`, because redelivering it
would only fail again.

## What happens to a broken message

A payload that cannot be decoded is logged and dropped; the consumer moves on.
A handler that raises is logged the same way. Neither stops the worker.

## Shutting down

`SIGTERM` — what `docker stop` sends — unwinds polling, stops the consumer,
closes the aiogram session and the FSM storage. Messages already in the list
stay there for the next start.
