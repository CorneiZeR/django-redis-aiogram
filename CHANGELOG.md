# Changelog

## 2.0.0 - unreleased

Nothing is required to keep a 1.x project working: `telegram_bot` still
imports and still works in `INSTALLED_APPS`. See the upgrade notes in the
README.

### Breaking

- Package renamed to `django_redis_aiogram`. `telegram_bot` remains as a
  deprecated shim and is removed in 3.0.
- Requires Python 3.10–3.14, Django 5.2+, aiogram 3.30+. Django 4.2 reached
  end of life, and aiogram 3.30 needs Python 3.10.
- Queue payloads are serialized as JSON by default instead of pickle, and
  pickled payloads are **refused** by default — unpickling queue data is code
  execution. If the queue holds 1.x messages when you deploy, set
  `ALLOW_PICKLE: True` for the upgrade window and remove it once drained.
- Delivery defaults to `blpop` instead of keyspace expiry events. Set
  `DELIVERY: 'keyspace'` for the old behaviour.
- System check ids moved from `telegram_bot.EXXX` to `django_redis_aiogram.EXXX`.
- `TelegramBot` moved from `telegram_bot.telegram_bot` to
  `django_redis_aiogram.client`, and the settings module is
  `django_redis_aiogram.settings`. The package exports the `bot` and `conf`
  objects, which would otherwise shadow same-named submodules.
- `ENABLED` is parsed rather than coerced with `bool()`. The string `'false'`
  now disables the bot instead of enabling it, integers are accepted, and
  anything else raises `ImproperlyConfigured`.
- Packaging moved to `pyproject.toml` with a `src` layout.

### Added

- `ENABLED` lets a process opt out of the bot entirely: no autodiscover, no
  system checks, `send_raw` and `send_redis` become no-ops, and no credentials
  are required. Reads `TELEGRAM_BOT['ENABLED']` or the environment variable
  `DJANGO_REDIS_AIOGRAM_ENABLED`.
- Every scalar setting can come from `DJANGO_REDIS_AIOGRAM_<NAME>`; Django
  settings take precedence.
- `DEFAULT_BOT_PROPERTIES` maps onto aiogram's `DefaultBotProperties`, so
  `parse_mode` is configured once on the bot rather than injected into every
  call.
- `FSM_STORAGE` selects `redis` (default), `memory`, or a dotted path.
- `ALLOW_PICKLE` refuses pickled payloads once a 1.x queue has drained.
- `AUTODISCOVER` can be turned off on its own.
- `start_tgbot --idle` keeps a disabled container parked instead of exiting,
  for restart policies that treat a clean exit as a crash loop.
- Public `bot.router`, `bot.dispatcher` and `bot.enabled`.
- `py.typed`: the package ships type information.
- `bot.send()` picks the route for you: direct inside the bot container,
  queued anywhere else.
- `RATE_LIMIT` paces outgoing calls under Telegram's published limits instead
  of waiting to be refused. Budgets are per bot, so a second token gets its own.
- `close()` releases the FSM storage as well as the bot session and the loop.

### Fixed

- Importing the package no longer builds a bot, opens a Redis connection or
  creates an event loop. A missing token or Redis URL used to take the whole
  Django project down, including its test suite, in every process.
- System checks now actually validate. The old ones could not fail: the
  validation flag was only ever set inside an `isinstance` branch that a wrong
  type never entered.
- FSM state is no longer lost on restart — the dispatcher was built without a
  storage, so it defaulted to memory even with Redis configured.
- The delivery consumer no longer calls `create_task` on an event loop owned by
  another thread.
- Exhausting `MAX_RETRIES` now logs and honours `RAISE_EXCEPTION` instead of
  returning silently.
- Keyspace delivery reads the database index from `REDIS_URL` instead of
  assuming 0, and degrades to a warning when the server refuses `CONFIG SET`
  rather than crashing — managed Redis providers routinely refuse it.
- `send_redis` writes its expiry key with a real TTL; 1.x relied on positional
  arguments lining up.
- Keyspace delivery drains the queue with atomic pops. The 1.x lrange+ltrim
  pair let a second worker read the same messages and deliver them twice.
- Delivery is crash-safe on Redis 6.2+: a message is parked in a processing
  list while being sent and reclaimed on the next start, so a worker killed
  mid-send no longer loses it. After a crash a message may be sent twice.
- The keyspace consumer no longer dies on `decode_responses` connections, and
  survives errors raised while handling a single event.
- Concurrent `send_raw` calls from a multi-threaded web server are serialized
  instead of failing with "this event loop is already running".
- Payloads are decoded correctly when `REDIS_URL` sets `decode_responses`.
- The `setting_changed` receivers use a `dispatch_uid`, so autoreload no longer
  stacks duplicates.
- Serializer failures surface as `SerializationError` instead of raw
  `TypeError` / `ValueError` escaping to the caller.
- Autodiscover no longer swallows `ImportError` raised inside a router module,
  so a broken router surfaces.
- Logging goes to the `django_redis_aiogram` logger instead of the root logger,
  with values in `extra` rather than interpolated into the message.
- `override_settings(TELEGRAM_BOT=...)` now takes effect; settings used to be
  frozen at import.
- SIGTERM shuts the worker down in order and closes the aiogram session.

### Infrastructure

- Test suite covering lazy import, the `ENABLED` flag, serialization
  round-trips, delivery, checks and the shim.
- CI across Python 3.10–3.13 and Django 5.2/6.0 with ruff, mypy and pytest,
  plus a job pinning the lowest supported dependency versions and a
  non-blocking one on Python 3.14.
- Releases publish to PyPI through Trusted Publishing.
- Dependabot, issue and pull request templates, `CONTRIBUTING.md`,
  `SECURITY.md`.

## 1.0.0 - 2023-07-01
- Initial release

## 1.0.1 - 2023-07-01
- edit README.md

## 1.0.2 - 2023-07-01
- edit README.md

## 1.0.3 - 2023-07-01
- fix clearing messages from redis

## 1.0.4 - 2023-10-13
- update aiogram version
- change json to pickle, now supports more types of data

## 1.0.5 - 2023-10-19
- correcting README
- update settings to TypedDict
- add sending raw aiogram functions
- add possibility to send message from django

## 1.0.6 - 2023-10-21
- rm parse_mode by default
- add the ability to flexibly configure default kwargs for different aiogram functions
- edit min aiogram version

## 1.0.7 - 2023-10-21
- edit README

## 1.0.8 - 2024-02-20
- add max retries for sending message to settings (`MAX_RETRIES`)
- add reraise exception to `send_raw` (`RAISE_EXCEPTION`)