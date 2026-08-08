# Serialization

A queued call is stored as `{'function': ..., **kwargs}`. The default format is
JSON.

## Why not pickle

Whatever can write to the Redis list decides what the bot container executes.
With pickle that means arbitrary code. JSON is narrower, but not merely "a
different Telegram call": a payload naming an `FSInputFile` picks a path, so a
queue writer can make the bot upload any file its container can read. Treat the
queue as a trust boundary either way — see
[SECURITY.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/SECURITY.md).

1.0.4 moved *to* pickle because keyboards would not survive as plain dicts.
That is no longer true: aiogram 3 models are pydantic v2 and round-trip
cleanly.

## What survives the queue

| Type | Notes |
| ---- | ----- |
| Keyboards | all four `reply_markup` types, nested buttons intact |
| aiogram models | `InputMedia*`, `MessageEntity`, `LinkPreviewOptions`, `ReplyParameters`, … |
| `datetime`, `date` | ISO format |
| `Decimal` | exact, as a string |
| `bytes` | base64 |
| Enums | by value |
| `URLInputFile`, `FSInputFile`, `BufferedInputFile` | see below |
| Plain data | strings, numbers, booleans, lists, dicts, `None` |

## Files

`URLInputFile` and `BufferedInputFile` carry everything they need. `FSInputFile`
carries only a path, so the file must also exist in the bot container — share a
volume, or send the bytes instead.

Anything else that is not JSON-representable raises `SerializationError` when
queued, naming the alternative:

```text
FooInputFile cannot be queued. Send a file_id or a URL instead,
or set TELEGRAM_BOT['SERIALIZER'] to 'pickle' together with
ALLOW_PICKLE = True, or the reader will refuse what it writes.
```

Falling back to pickle takes both keys, and a queue nothing untrusted can write
to — the reader refuses pickled payloads unless told otherwise:

```python
TELEGRAM_BOT = {
    'SERIALIZER': 'pickle',
    'ALLOW_PICKLE': True,
}
```

## The method name is checked against an allowlist

A payload names the method to call, so that name is validated before anything is
looked up on the bot. Only the Telegram API methods aiogram exposes are
accepted — the ones matching `aiogram.methods`, 185 of them at the time of
writing. Anything else is refused with a `ValueError`.

That closes off the other public attributes a `Bot` carries: `download_file`
would write to the container's filesystem, `token` would hand out the
credential. Neither is reachable from the queue.

This narrows what a queue writer can do; it does not make the queue safe. Redis
remains a trust boundary — see
[SECURITY.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/SECURITY.md).

## Two details that make it work

**Every model is tagged with its class name.** Decoding looks the class up
rather than inferring it from a union. Without this, `InputMediaPhoto` comes
back as `InputMediaAudio` whenever the discriminator is missing.

**`Default` sentinels are tagged too.** aiogram fills unset fields with a
`Default` marker that pydantic cannot serialize. The obvious fix —
`exclude_unset=True` — also strips discriminators, which is exactly how the
`InputMediaPhoto` corruption happens. So the sentinels are preserved by name
and rebuilt on the way out.

Class lookup is limited to `aiogram.types` members that subclass
`TelegramObject`, so a payload cannot name an arbitrary import path.

## Pickle, the escape hatch

JSON is the format. Pickle is what is left when a payload has no JSON form at
all — `UnsupportedInputFileError` names it for exactly that reason when you try
to queue an open file. It is not a migration aid: 3.0 removed the shim and the
1.x queue is long drained, and the setting is still here because the escape
hatch is still needed.

It is off by default because **unpickling queue data is code execution**.
Whoever can write to the Redis list can run code in the bot container, so this
is a trust boundary, not a preference.

Reading pickled payloads takes one key:

```python
TELEGRAM_BOT = {
    'ALLOW_PICKLE': True,
}
```

Writing them takes both — writing a format the reader refuses would discard
every message, which is what `E022` reports before deployment:

```python
TELEGRAM_BOT = {
    'SERIALIZER': 'pickle',
    'ALLOW_PICKLE': True,
}
```

Two behaviours make the mixed case work:

- **Reads sniff the format per message.** A queue holding both formats drains
  without being stopped, so switching `SERIALIZER` needs no downtime.
- **A refused pickle stays in flight**, rather than being acknowledged — *on
  Redis 6.2 and newer*. It is the only case where `dispatch()` returns `False`,
  which is what withholds the acknowledgement; see **[[Delivery]]**. Turning `ALLOW_PICKLE` off while a producer is still
  writing pickled payloads leaves them in the worker's processing list with a
  log line saying so; set it back, restart the worker, and they are delivered.

> **Turning `ALLOW_PICKLE` off is only recoverable where `LMOVE` exists.**
> Without it there is no in-flight list — the consumer has already popped the
> message when it refuses it, so a refused pickle is **gone**. The consumer says
> which mode it is in at startup, as `tg_crash_safe` on the `delivery started`
> line. On such a server, stop or upgrade every pickle producer *before* turning
> the flag off. See **[[Delivery]]**.

Only worth it if you must queue objects JSON cannot represent, and only with a
Redis nothing untrusted can write to.

## Failures

Both serializers raise `SerializationError` — never a bare `TypeError`,
`ValueError` or `RecursionError` — so callers have one exception to catch. On
the consumer side an undecodable message is logged and dropped rather than
stopping the worker.
