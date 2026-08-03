# Serialization

A queued call is stored as `{'function': ..., **kwargs}`. The default format is
JSON.

## Why not pickle

Whatever can write to the Redis list decides what the bot container executes.
With pickle that means arbitrary code; with JSON it means, at worst, a
different Telegram call. Treat the queue as a trust boundary either way — see
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

```
FooInputFile cannot be queued. Send a file_id or a URL instead,
or set TELEGRAM_BOT['SERIALIZER'] to 'pickle'.
```

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

## Switching formats

Reads detect the format per message, but pickled payloads are **refused by
default** — unpickling queue data is code execution. If the queue still holds
1.x messages when you deploy, open the door for the upgrade window only:

```python
TELEGRAM_BOT = {
    'ALLOW_PICKLE': True,  # remove once the queue has drained
}
```

`'SERIALIZER': 'pickle'` goes back to writing pickled payloads. Only worth it
if you must queue objects JSON cannot represent, and only with a Redis nothing
else can write to.

## Failures

Both serializers raise `SerializationError` — never a bare `TypeError`,
`ValueError` or `RecursionError` — so callers have one exception to catch. On
the consumer side an undecodable message is logged and dropped rather than
stopping the worker.
