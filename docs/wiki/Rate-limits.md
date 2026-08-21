# Rate limits

Telegram publishes its limits, so the bot paces itself against them rather
than sending too fast and being refused.

| Limit | Default | Setting |
| ----- | ------- | ------- |
| Overall | 30 messages/second | `overall_per_second` |
| Same chat | 1 message/second | `per_chat_per_second` |
| Same group or channel | 20 messages/minute | `group_per_minute` |

```python
TELEGRAM_BOT = {
    'RATE_LIMIT': {
        'overall_per_second': 30,
        'per_chat_per_second': 1,
        'group_per_minute': 20,
    },
}
```

Set an entry to `0` to drop that one limit, or `RATE_LIMIT` to `None` to switch
pacing off entirely.

## How it behaves

Each limit is a token bucket, so a burst up to the bucket's size goes straight
out and the rest is spaced. A group may send its whole 20 in one go, then waits
three seconds per message.

A negative `chat_id` is what identifies a group, supergroup or channel — that
is how the per-minute limit is applied only where it belongs. A `@username`
cannot be keyed to a bucket, so only the overall limit applies to it.

Per-chat buckets are forgotten once they are idle again, so a bot talking to
many chats does not accumulate them forever.

## Several bots

Telegram meters per token, so the budget belongs to a bot instance rather than
the process:

```python
first = TelegramBot()  # its own budget
second = TelegramBot()  # a separate one
```

Nothing extra is needed when a second token arrives.

## Retries still exist

`MAX_RETRIES` and the `TelegramRetryAfter` handling have not gone away. Pacing
means they should rarely be reached — Telegram can still refuse a message for
reasons that have nothing to do with your rate.

Exhausting the retries logs an error with `tg_function` and `tg_max_retries`,
and re-raises the last refusal when `RAISE_EXCEPTION` reads as true — `'false'`
from the environment reads as false, like every boolean here. See **[[Logging]]**.

**Where the raise reaches you is not everywhere.** It reaches a caller that waited
for the answer, which since 3.1.0 excludes a process that serves the webhook: there
the send is handed to the loop thread and the caller returns before Telegram has
been asked, so the failure lands in the log and not in your `except`. Queued sends
were always like that — the worker raises where the worker is.
**[[Sending-messages|Sending messages]]** has the whole picture.

## Tuning

The defaults are Telegram's documented numbers, and they apply to a bot
messaging many different users. Broadcasting to one large group is bound by
`group_per_minute` instead. If you are being refused anyway, lower
`overall_per_second` before raising `MAX_RETRIES` — the limits are not
contractual and are applied more tightly to some accounts than others.

## Memory, and what happens beyond it

Per-chat buckets are kept in memory, so the limiter tracks at most 4096 chats
and groups at a time. Once past that, the least recently used bucket is dropped
when a new chat needs one — and if every candidate still owes wait time, one of
them is dropped anyway.

That is a deliberate bounded loss, not a way around the limit. A bucket is only
evicted after 4096 *other* chats have been more recently active, which at
`overall_per_second: 30` takes over two minutes; per-chat debt clears in about a
second, so what is dropped is stale in practice. The overall bucket is never
evicted, so the bot-wide rate holds regardless.

If you genuinely message tens of thousands of distinct chats inside a couple of
minutes, treat per-chat pacing as best-effort and keep `overall_per_second` as
the limit you rely on.
