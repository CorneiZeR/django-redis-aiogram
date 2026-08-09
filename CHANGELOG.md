# Changelog

## Unreleased

### Breaking

- **The `telegram_bot` package is gone.** 2.0 kept it as a deprecated shim and
  said it would be removed in 3.0; this is that. Put `django_redis_aiogram` in
  `INSTALLED_APPS` and import from it — `TelegramBot` is in
  `django_redis_aiogram.client`, the settings module is
  `django_redis_aiogram.settings`, and the management commands keep their names.
  A project that upgrades without touching `INSTALLED_APPS` fails at startup with
  `ModuleNotFoundError: No module named 'telegram_bot'`, which is the loudest
  this could reasonably be.
- **`keyspace` delivery is gone**, and with it `REDIS_EXP_KEY` and
  `REDIS_EXP_TIME`. It reproduced the 1.x mechanism — write a key with a TTL and
  react to its expiry event — and needed `CONFIG SET notify-keyspace-events`,
  which managed Redis providers refuse; it also could not deliver anything
  before the TTL elapsed. `blpop` has been the default since 2.0, needs no
  server configuration and delivers as the message arrives. Remove
  `'DELIVERY': 'keyspace'` from your settings: the value is now refused by check
  `E009` rather than silently ignored, so `manage.py check` tells you before the
  worker starts. Checks `E008` and `E013` are gone with the settings they
  guarded, and their ids are not reused — a `SILENCED_SYSTEM_CHECKS` entry
  naming one is now dead but harmless.
- `DeliveryKind` has one member, `BLPOP`. `DELIVERY` stays as a setting so a
  stale `'keyspace'` produces an error naming the legal value, rather than an
  unknown-key warning and a silently different delivery mode.
- **The 2.0-spelling string constants are gone.** `BLPOP_DELIVERY`,
  `KEYSPACE_DELIVERY`, `MEMORY_STORAGE`, `REDIS_STORAGE`, `JSON_SERIALIZER`,
  `PICKLE_SERIALIZER`, the `TAG_*` names, `OVERALL_PER_SECOND`,
  `PER_CHAT_PER_SECOND`, `GROUP_PER_MINUTE`, `POLLING` and `WEBHOOK` were
  aliases of the enum members carrying the same strings, kept because 2.0 had
  shipped them under those names. Import the member instead —
  `SerializerKind.JSON`, `SerializationTag.MODEL`, `UpdateMode.WEBHOOK` — and
  interpolate `.value`, never the member: a `(str, Enum)` member formats as its
  own qualified name on newer Pythons. The values are unchanged, so nothing in
  Redis or in your settings has to move.

### Added

- **An event log.** `TELEGRAM_BOT['EVENT_LOG'] = True` records what the package
  did — a message queued, delivered, failed or retried, an update received, an
  FSM transition, a payload refused — as rows in one append-only table. Rows for
  the same message share a `correlation_id`, so the row a web process wrote when
  it queued the send lines up with the row the bot container wrote when it
  delivered it, across two processes, with no coordination and no foreign key
  either way. See **Event log** in the wiki.
- Writes go through one background thread that batches with `bulk_create`, so no
  send ever waits on the database. A database that is slow or down costs dropped
  rows, never dropped messages; the drop is logged and, once the writer catches
  up, recorded as a `log.dropped` row so the gap is visible in the data too.
- `EVENT_LOG_KINDS` selects which kinds to keep, and `events.register_kind` adds
  your own. `kind` is an unconstrained column and the registry lives in Python,
  so adding a kind is not a migration.
- `EVENT_LOG_DATABASE` puts the log on another `DATABASES` alias, with
  `django_redis_aiogram.dbrouter.TelegramEventLogRouter` to move `migrate` with
  it. The writer names the alias explicitly, so the feature is correct with no
  router installed at all.
- Checks `E031`–`E042` and `W005`–`W009`, including the two that would otherwise
  only show up in production: an alias that is not in `DATABASES`, and a log
  switched on where the database has no engine. Both matter because the writer
  runs on a thread nobody is watching.
- **This package now ships a migration.** Run `manage.py migrate` after
  upgrading whether or not you turn the log on: the table is created either way,
  and creating it later on a live database is the more expensive order. It
  creates Django's four stock model permissions plus one,
  `view_telegramevent_payload`: there are no field-level permissions, so
  without it "saw that the message went out" and "read what it said" cannot be
  granted separately.

### Changed

- `ALLOW_PICKLE` is documented as what it is: the escape hatch for payloads JSON
  cannot describe, and not the 1.x upgrade window it was introduced as. Nothing
  about its behaviour changed — it is still off by default, the reader still
  refuses pickled payloads without it, and a refused payload is still left in
  flight rather than destroyed — on Redis 6.2 and newer. Without `LMOVE` there
  is no in-flight list, so a refused pickle is lost rather than held; the
  documentation now says so where it tells you the flag is safe to toggle. What changed is that the documentation no longer
  tells you to remove a setting you may need, and now says why the read path has
  a refusal branch at all.

### Documentation

- **Migrating from 1.x** is now **Upgrading**, covering each major release
  newest first. A 1.x to 3.0 jump has no shim to lean on, so it needed a page
  rather than a deleted one.

### Infrastructure

- The suite that needs no services is now two pytest invocations, alongside the
  Redis-backed one. `tests/settings.py` still configures no database — proving
  the package boots without one is part of what it tests — so database-backed
  tests live in `tests/db` under their own settings module and the default run
  ignores that directory. A new CI job runs it.

## 2.2.0 - 2026-08-04

### Changed

- **The redis floor is `>=6.2`**, up from `>=5.0`. The old floor promised
  support the package did not have: aiogram's `RedisStorage` calls `aclose()`,
  which redis-py added in 5.0.1, and aiogram's own extra asks for `>=6.2.0`. So
  `FSM_STORAGE: 'redis'` — the default — raised `AttributeError` on redis-py
  5.0.x, 6.0 and 6.1 while the metadata said those worked. Upgrading redis-py to
  6.2 or newer is the whole migration; `pip` does it on its own unless the
  version is pinned.

### Infrastructure

- The integration suite runs against a real Redis at **both ends** of the
  supported range, not only the newest. Running only the newest is what let a
  broken floor ship: `test-floors` installs the floors but the unit suite uses
  fakeredis, which has the `aclose()` redis-py 5.0 lacked, so the one
  combination that mattered — floors plus a real server — was never run.

## 2.1.1 - 2026-08-04

### Fixed

- `manage.py check` no longer warns about an untouched installation. 2.1.0
  shipped `REDIS_TIMEOUT` at 5 next to `BLPOP_TIMEOUT` at 5, so `W004` fired on
  every default configuration — and a warning nobody caused is what teaches
  people to stop reading the checks. The deadline defaults to 10, which also
  leaves the blocking pop at the 5 seconds it used before 2.1.0. A test now
  asserts the defaults report nothing at all.

## 2.1.0 - 2026-08-04

### Added

- `REDIS_TIMEOUT` (10 seconds) bounds how long any single Redis call may take,
  both connecting and waiting for an answer. Without it a server that accepts
  the connection and then stops responding holds the caller until the process
  is killed: redis-py only began applying a read deadline of its own in 8.0, and
  the supported floor is 5.0. Measured against a paused Redis 7 container —
  redis-py 5.0.0 never returned, 8.1.0 gave up after five seconds.
- Check `E030` for the new setting, and `W004` when `BLPOP_TIMEOUT` is at or
  above it.

### Fixed

- `BLPOP_TIMEOUT` is capped just below `REDIS_TIMEOUT`. A pop asked to wait
  longer than the socket will wait for an answer turns every idle round into a
  logged error — a consumer doing nothing wrong, complaining every few seconds
  — which is why the deadline could not simply be handed to the blocking read.
  The cap is reported by `W004` rather than applied silently.

## 2.0.1 - 2026-08-04

### Fixed

- The documentation links on the PyPI page. The README doubles as the long
  description, and PyPI serves it from `pypi.org` without rewriting links, so
  every `../../wiki/<page>` resolved to `pypi.org/wiki/<page>` — the whole
  documentation table, plus `LICENSE`, `CONTRIBUTING.md`, `AGENTS.md`,
  `CHANGELOG.md` and `SECURITY.md`, were dead there while working on GitHub.
  All of them are absolute now, and a test refuses any relative link in the
  README.
- `project.urls` declares `Documentation`, so the wiki appears in the PyPI
  sidebar rather than only inside the description.

## 2.0.0 - 2026-08-04

Upgrading the dependency needs no application-code changes: `telegram_bot`
still imports and still works in `INSTALLED_APPS`. Settings are a separate
matter — a 1.x queue needs `ALLOW_PICKLE` for the drain, and `parse_mode` moves
to `DEFAULT_BOT_PROPERTIES`. See the upgrade notes in the README.

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
- `ALLOW_PICKLE` is the temporary opt-in for *reading* 1.x pickled payloads.
  Turn it off once the queue has drained; it is off by default.
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
- `MODE` chooses where updates come from: `polling` (default) or `webhook`.
  Both are supported the same way; the choice can be made at startup through
  `DJANGO_REDIS_AIOGRAM_MODE`, or for one run with `start_tgbot --mode`.
- Webhook mode: `django_redis_aiogram.webhook.telegram_webhook` is a view you
  wire into your own `urls.py`, with `WEBHOOK_URL`, `WEBHOOK_SECRET` and
  `WEBHOOK_ALLOWED_UPDATES` to configure it, and `manage.py tgbot_webhook
  set|delete|info` to register it with Telegram. The secret is mandatory: the
  view refuses to serve without one and check `E027` says so before deployment.
- `manage.py tgbot_healthcheck` for container orchestration. The consumer
  publishes a heartbeat every `HEARTBEAT_INTERVAL` seconds with a TTL of three
  times that, so a dead consumer thread stops looking alive on its own; the
  command also fails when the queue grows past `HEALTHCHECK_MAX_QUEUE`.
- `django_redis_aiogram.enums` holds every value the settings accept —
  `DeliveryKind`, `SerializerKind`, `StorageKind`, `UpdateMode`,
  `SerializationTag`, `RateLimitKey` — so a project can import the enum instead
  of spelling a string. The values are frozen: queued payloads carry them.
- `django_redis_aiogram.exceptions` gives the package one error family.
  `DjangoRedisAiogramError` catches everything it raises;
  `SerializationError` and `UnknownApiMethodError` are the two a consumer is
  likely to name, and both keep their old import paths and base classes.
- Queued payloads may only name a Telegram API method aiogram exposes, and not
  `set_webhook`, `delete_webhook`, `log_out` or `close` — those administer the
  deployment rather than send. A payload naming anything else is refused when
  queued and dropped by the consumer, so whoever can write to Redis cannot
  reach `download_file` or the token.
- `import django_redis_aiogram` costs about a millisecond. Naming `bot` is what
  loads aiogram and the pydantic stack under it, and `ENABLED=0` keeps the
  package's own boot from naming it: no autodiscover, so no `tg_router` module
  is imported, and no checks are registered. A migration container or a CI run
  that imports nothing which sends never loads aiogram at all. The
  `telegram_bot` shim resolves its exports the same way, so a project still on
  the 1.x name pays for aiogram only where it sends.

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
- Delivery is crash-safe on Redis 6.2+: a message is parked in a per-worker
  processing list while being sent and reclaimed on the next start, so a worker
  killed mid-send no longer loses it. After a crash a message may be sent
  twice. `WORKER_NAME` names that list when several workers share a host.
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
- SIGTERM shuts the worker down in order and closes the aiogram session, and
  restores the handler it replaced.
- A pickle payload the configuration refuses is no longer acknowledged and
  deleted. It stays in the in-flight list with a log line naming the cure, so a
  missed `ALLOW_PICKLE` during the upgrade window cannot silently destroy the
  1.x queue it was meant to drain.
- `ALLOW_PICKLE` is read the same way everywhere. From the environment it
  arrives as a string, and `'false'` used to be truthy — for the reader, for
  the writer and for check `E022`.
- `reclaim()` only gives up crash-safe delivery when the server truly lacks
  `LMOVE`; `WRONGTYPE` or a permission error no longer disables it for the life
  of the container, and a Redis that is unreachable at startup is retried
  instead of ending the consumer thread.
- The keyspace consumer builds its subscription inside its retry loop, drains
  the backlog at startup, and keeps its heartbeat fresh even when
  `BLPOP_TIMEOUT` is longer than the heartbeat interval.
- Shutdown refuses a send rather than losing it: a call arriving once `close()`
  has started is reported instead of being scheduled onto a loop that will
  never run it, and teardown holds the loop lock so it cannot interleave with a
  send driving the same loop.
- The shared Redis connection is built at most once and handed out atomically;
  a `reset()` racing a reader could previously return `None`.
- A rate limiter is no longer cached per bot, so `override_settings` and a
  changed `RATE_LIMIT` reach a bot that already exists.
- Per-chat rate-limit buckets are capped: eviction used to stop at the first
  bucket still owing wait time, so one busy chat kept the map growing.
- `manage.py check` survives settings a project got wrong in unusual ways — a
  non-string key in `TELEGRAM_BOT`, an unhashable member of
  `WEBHOOK_ALLOWED_UPDATES`, an unreadable `ALLOW_PICKLE` — reporting them
  rather than raising.

### Infrastructure

- Test suite covering lazy import, the `ENABLED` flag, serialization
  round-trips, delivery, checks and the shim.
- CI across Python 3.10–3.14 and Django 5.2/6.0 with ruff, mypy and pytest,
  plus a job pinning the lowest supported dependency versions. Every version the
  package advertises has to pass before a merge.
- Releases publish to PyPI through Trusted Publishing.
- Dependabot, issue and pull request templates, `CONTRIBUTING.md`,
  `SECURITY.md`.
- An integration suite that runs against a real Redis — `LMOVE` support, the
  reclaim path, keyspace notifications enabled at startup, a mixed pickle/JSON
  backlog, FSM state across a restart — plus `scripts/smoke_install.sh`, which
  installs the built wheel into a throwaway Django project and checks it boots
  with no credentials. Both run in CI.
- `ruff` runs with every rule enabled; deliberate exceptions carry their reason
  on the line. `mypy` covers the package in strict mode.
- Documentation lives in the wiki, published from `docs/wiki` on push to
  `master`, with the README kept to a front page. Configuration examples and
  testing recipes on those pages are executed by the test suite.
- `AGENTS.md` and the **AI assistants** wiki page: briefs for coding agents
  working on the package and with it.

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