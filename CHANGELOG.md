# Changelog

## 3.1.0 - unreleased

**At-least-once delivery is true now, and was not before.** A message is
acknowledged when its send finishes rather than when it is scheduled, so
duplicates after a crash are real where they used to be impossible — and losses
were. **Make your handlers idempotent on your own business key**, not on
`correlation_id`: a handler's replies inherit the id of the update that caused
them, so it is not one per message.

### Fixed

- **Updates in one web process are handled concurrently.** A process serving the
  webhook drove nothing, so every `feed_update` took `run_until_complete` *under
  the loop lock* and updates handled strictly one at a time. Measured on four
  concurrent updates with a 200 ms handler: 0.81 s serialized against 0.21 s with
  the loop running. It also means a send scheduled from inside a handler now runs
  when it is scheduled — before, nothing stepped it until the next update
  arrived, or `close()`, or never.
- **`send_raw` no longer waits in a web process that serves the webhook.** It
  hands work to a running loop rather than driving one, and from the first update
  such a process handles there is a loop running. Measured: 0.30 s and Telegram
  called before it returns, against 0.00 s and Telegram not called yet. The
  practical difference is the exception — a send that fails after its retries
  raised into the view under `RAISE_EXCEPTION` and is now logged instead.
  A process that never serves the webhook is unaffected, and `send()`, which
  queues, never had this behaviour. The Sending messages page says what to do
  when you need the answer.
- In webhook mode `start_tgbot` runs the loop instead of blocking on an event, so
  a send the consumer schedules runs when it is scheduled rather than waiting for
  whatever happens next. Both modes now start the consumer from the loop, which
  is what keeps a backlog from reaching `send_raw` while the loop is not running
  yet.
- `close()` waits for the updates a webhook process is still answering before it
  stops that loop, and cancels what outlasts `DRAIN_TIMEOUT`. A request thread
  waits on its update with no deadline of its own, so stopping the loop under one
  would hold that worker for the life of the process. A cancelled update is
  answered 503, the same as one refused on arrival — nothing handled it either
  way, so Telegram should redeliver it rather than be told to forget it.
- **The webhook view answers 503 to an update it refused**, rather than 200. It
  answers 200 to a handler that raised, because retrying one that failed once is
  a loop — but an update refused mid-shutdown was never handled, so redelivery is
  the point. During a rolling restart that is the difference between the update
  moving to the next instance and disappearing.

- **The consumer acknowledged a message before Telegram had seen it.** In polling
  mode `send_raw` returns as soon as the coroutine is scheduled, and the consumer
  treated that as delivery: the message left the in-flight list at pop speed while
  Telegram is fed at `overall_per_second`. A `docker stop` with a backlog sent
  what the drain had time for — roughly 180 messages at the defaults — and lost
  the rest, with nothing left to redeliver, while the module docstring,
  **Delivery**, **Deployment** and **Troubleshooting** all promised
  at-least-once. Webhook mode always had the guarantee, because there the
  consumer drives the send to completion itself. A handler that accepts an
  `on_complete` keyword is now handed one and the message waits for it; one that
  does not — which is every documented recipe — keeps the old semantics exactly,
  so nothing outside this package changes behaviour.
- **`manage.py check` no longer imports aiogram.** `DEFAULT_BOT_PROPERTIES`
  defaults to `{}`, and the check that validates it reached for
  `DefaultBotProperties` before noticing there was nothing to validate — so
  every `check`, and with it every `migrate`, `runserver` and `shell`, paid for
  aiogram in every project that installs this package. Measured on an
  installation that configures nothing: 966 ms and 174 MiB before, 7 ms and
  47 MiB after. A non-empty `DEFAULT_BOT_PROPERTIES` is still validated exactly
  as before.
- **The FSM storage is bounded by `REDIS_TIMEOUT` like everything else.** aiogram
  builds its own Redis client and was handed no connection settings, so it had no
  read deadline at all on the declared 6.2 floor, and redis-py 8's own five
  seconds rather than yours above it. Every update reads FSM state, so a Redis
  that accepts the connection and then stops answering wedged the whole bot
  instead of one send. A `?socket_timeout=` on `REDIS_URL` still wins, as it
  always did — redis-py resolves the URL after keyword arguments.
- `manage.py tgbot_*` drains by hand no longer fail on a Redis older than 6.2.
  `consume_pending()` had nothing to probe `LMOVE` support with — the consumer
  loop learns it from `reclaim()` — so the first pop raised `ResponseError`
  straight out of a documented helper instead of falling back to plain pops.
- **A send handed to the loop just before shutdown is no longer destroyed.** A
  hand-off is a `call_soon_threadsafe` callback until the loop steps, and a
  callback is not a task — so the drain could not see it, and `close()` had
  already set the flag its callback refuses on. The message was gone from the
  queue's in-flight list by then, so nothing would ever redeliver it. `close()`
  now runs one turn of the loop before draining, which is what turns those
  callbacks into tasks it can wait for.
- **The consumer's join deadline is derived from the bound that governs it.** It
  was `BLPOP_TIMEOUT + 1` — six seconds at the defaults — while every call the
  consumer makes is bounded by `REDIS_TIMEOUT`, ten. A consumer that outlived the
  join went on to acknowledge a message `close()` had already refused, destroying
  one message per shutdown. It is now `REDIS_TIMEOUT + 1`, and a thread still
  alive after it says so in the log instead of leaving silence that reads as a
  clean stop.
- **Recording an event no longer rolls back the transaction it was recorded in.**
  The writer discarded stale connections before every batch, and inside an
  `atomic()` block Django closes the connection on the first check — which sets
  `needs_rollback`. Under `EVENT_LOG_SYNC`, with `ATOMIC_REQUESTS` or a plain
  `atomic()`, that destroyed the caller's own writes on PostgreSQL and MySQL.
  The suite could not see it: sqlite `:memory:` refuses to close at all. Stale
  connections are still discarded, just never while a transaction is open — and
  only the log's own alias, never every connection in the process. With
  `EVENT_LOG_DATABASE` pointing somewhere of its own, the sweep reached past the
  log's connection, which is not in a transaction, to the caller's `default` one,
  which is.
- **A database that refuses everything is now reported as a failure.** The
  bisecting write caught `DatabaseError` at every rung and returned normally, so
  the writer counted a total refusal as a written batch: the suspension after
  repeated failures was unreachable, no `log.dropped` row was ever written, and
  a forgotten `migrate` meant a full batch of statements and tracebacks once per
  flush interval, for ever.
- **Events are no longer lost when the writer thread dies or when `stop()`
  races a `record()`.** Both left a queue nobody would ever drain again, and its
  contents disappeared with no row, no counter and no gap marker. Both now drain
  it: what can be written is written, and what cannot is counted, so the next
  successful flush records the gap.
- The writer reads its own batch size, buffer size and flush interval with a
  fallback to their defaults. They are read on the writer thread, in a loop,
  outside the net that contains a failed flush — so a value that could not be
  parsed ended the writer and took the whole buffer with it. Checks `E036`–`E038`
  still report it at boot.
- **The changelist's bounded count is now actually bounded.** The kind index was
  `(kind, -created_at)` while the changelist orders by `-id`, so every filtered
  page sorted in a temporary b-tree — the page query and the count alike. A
  `LIMIT` can only stop early if the rows arrive ordered, so the documented
  "counts at most 10 000 rows" was not true for any filtered view. The index is
  `(kind, -id)` now. **This release ships migration `0002`; run `manage.py
  migrate`.** The new index is added before the old one is dropped, so the table
  is never without one on `kind`, and the name is new because the columns differ.
  `AddIndex` issues a plain `CREATE INDEX`, so on a table large enough for the
  lock to matter, run the migration in a window. What it costs: a per-kind *time
  window* query loses its range column. `drai_event_recent` still covers a time
  window without a kind.
- The changelist stops fetching `error` and `detail`. It renders neither, and
  between them they are most of what a row weighs — about 1.4 MB per fifty-row
  page, fetched even for a reader the payload permission withholds them from.
  The detail page asks for them back, and only for a reader allowed to see them.
- Only indexed columns are sortable in the admin. One click on the `function`,
  `worker` or `error_code` header was a full sort of a table sized by traffic.
- **`tgbot_prune_events` makes forward progress past a recent low-id row.** The
  walk's lower bound was the table's lowest id while its upper bound was filtered
  by the cutoff, so one surviving row down there pinned every run to restart from
  it — and a bounded `--max-chunks` run spent its budget crossing rows it could
  not delete. The watermark is also read with `ORDER BY id DESC LIMIT 1` instead
  of an aggregate, and the pause between chunks is skipped when a chunk deleted
  nothing and under `--dry-run` entirely.

### Added

- **`await bot.asend(...)`**, and `asend_redis`, for code already on an event
  loop. `send()` writes to a socket on the calling thread, which under ASGI is
  the thread serving requests, and on a first call that includes a TCP connect
  bounded by `REDIS_TIMEOUT`. Native `redis.asyncio` rather than a thread wrapper:
  `sync_to_async` measured the same for one send, but its default
  `thread_sensitive=True` runs every call in one shared thread — twenty
  concurrent sends took 0.49 s against 0.02 s — and even threaded it draws on a
  pool shared with every other `sync_to_async` in the process, the ORM's
  included. Each loop gets its own client, because these connections are
  loop-affine, and `await bot.aclose()` is the only way to close one on the loop
  that owns it — the only loop permitted to. A process that runs a loop per unit of
  work should call it; the registry drops clients whose loop has closed, so nothing
  accumulates without it, but the sockets wait for the collector. **Deployment**
  has the recipe.
- **`bot.send_many(chat_ids, ...)`** and **`await bot.asend_many(...)`** queue one
  message per chat, a chunk of them per variadic `RPUSH`, returning an id per
  message in the order given. It speeds up *queueing*, not delivery — the rate
  limits still pace what leaves, so fifty thousand chats is about half an hour at
  the default thirty a second — and it makes event-log overflow worse by removing
  the pacing sequential round trips gave the writer. A chunk that fails records a
  drop for its own messages and raises; the earlier chunks are already queued.
- **`bot.queue_depth()`** and **`bot.inflight_depth(worker=None)`**, with async
  twins, so a monitor stops reproducing `<REDIS_MESSAGES_KEY>:processing:<worker>`
  by hand — a scheme that is this package's to change. Troubleshooting used to
  send people to `redis-cli` for it.
- Both producers now share one write body: serialisation, the key and both event
  rows live in a single context manager, and each transport is the one line that
  writes. The `await` is the only thing the two cannot share, so it is the only
  thing they do not.

- **`manage.py tgbot_reclaim --worker <name>`** puts a dead worker's in-flight
  messages back on the queue. Crash safety rests on a restarted worker
  recognising its own list, and a container started without `hostname:` gets a
  fresh name from Docker for each container it creates — so every replacement,
  which is what a redeploy does, stranded whatever the last one was sending where
  nothing would look again. The command is deliberately
  manual: naming a worker is a human saying it is gone, and one that is merely
  slow looks exactly like one that is dead. `--dry-run` reports without moving
  anything, through the same `--limit` a real run applies, and `--limit` bounds
  what one run can move.
- Check `W010` reports the case it can detect: `WORKER_NAME` empty while the
  hostname is one Docker generated. Narrow on purpose — an unset `WORKER_NAME` is
  the documented default and correct almost everywhere, so warning about it as
  such would fire on every untouched installation and teach people to stop
  reading warnings.
- `manage.py tgbot_healthcheck` says which guarantee is in force — established
  by asking, not assumed from a default — and how many messages sit in flight
  under other worker names, so a stranded pile stops being invisible. It reports
  rather than acts: one of those may be a message another worker is sending this
  second. The count is a floor and says so when it is one: `SCAN` walks the whole
  keyspace, and this runs on a healthcheck timer, so the sweep is bounded.
- `MAX_IN_FLIGHT` bounds how many sends the consumer will leave outstanding
  before it stops taking messages, with `0` — the default — meaning no bound.
  Worth setting on a worker that sees large backlogs: acknowledging is an `LREM`,
  which scans the in-flight list, so an unbounded one turns draining a backlog
  into quadratic work.
- `REQUIRE_CRASH_SAFE` refuses to start where `LMOVE` is missing, rather than
  running at-most-once and only saying so in a log line. Checked before the
  consumer thread starts: a failure raised inside it would kill the thread and
  leave the process polling updates with nothing draining the queue. An
  unreachable Redis is not mistaken for an old server.
- `Delivery.crash_safe` reports which guarantee is actually in force.
- Checks `E045` and `E046` for the two settings above.
- `DRAIN_TIMEOUT` sets how long `close()` gives in-flight sends before cancelling
  them. It was hardcoded at five seconds and `start_tgbot` called `close()` bare,
  so a deployment could raise `stop_grace_period` all it liked and never buy the
  drain a second more. The Deployment page now has the arithmetic for sizing the
  grace period against all three waits.
- Check `E044` refuses a `DRAIN_TIMEOUT` that is not a finite number, or is
  negative. `close()` reads it while shutting down, between stopping the consumer
  and flushing the event log, so raising there would cost the rows describing what
  the drain just did — it falls back to the default rather than refusing, and the
  check is what tells you at boot.
- Check `E043` refuses a `REDIS_URL` that sets `decode_responses` while
  `ALLOW_PICKLE` is on. Decoding is otherwise supported and stays supported — one
  URL is often shared with a cache backend — but a pickled payload is not valid
  text, and redis-py decodes inside its own parser: the consumer raises *after*
  the server has moved the message to the in-flight list, and every later reclaim
  trips over the same message for ever. No restart recovers from it, which is why
  this is an error and not a warning.
- **A metrics seam that is not the event log.** `django_redis_aiogram.signals`
  carries `events_recorded`, a `django.dispatch.Signal` fired once per batch on the
  event writer's own thread, with the `Event` objects that batch holds — except in the
  two cases where there is no writer thread to run on: under `EVENT_LOG_SYNC`, which
  only takes effect with the log on, and at shutdown for whatever the writer had not
  drained, on the thread that called `recorder.stop()`. A signal
  rather than a setting naming a dotted path: no path to get wrong, no check id for
  it, no lazy import cache, and no question about what a failing import means.
  A receiver that raises costs neither the other receivers their batch nor the
  database its rows, and is logged as `an events_recorded receiver raised`.
  `send_robust` is most of that — and only most: Django's own failure logging reads
  `receiver.__qualname__`, which a *callable instance* does not have, so for that
  shape `send_robust` raises instead of containing anything, measured on 6.1. That is
  caught here as well and logged as `publishing recorded events failed`, because
  otherwise it reached the writer's failure counter and was reported as a database
  refusing a batch it never saw.

  It fires with `EVENT_LOG` **off** — the table and the metrics are separate
  decisions, and gating them together is how an advertised metric comes out
  silently empty. So the one gate became three: `enabled` still means "this process
  writes rows", `active` means "the table or a receiver is reading" and is what
  every producing seam now sits behind, and `wants_payload` guards only the
  summarising, which is the expensive part and no part of counting — so with the log
  off a receiver gets `Event` objects without the *summarised arguments*, while still
  getting what the seam measured itself: a send's `duration_ms`, a retry's
  `retry_after`, a queueing failure's `stage`, a gap's `dropped` count. Rows are what
  the table gets, and with the log off there are none. `EVENT_LOG_KINDS` filters
  receivers as well, because it is one answer to "which events does this deployment
  care about" and not two — except for `log.dropped`, which is the record that
  recording itself fell behind and is exempt in both directions, since a deployment
  that filtered it out would read the hole as quiet traffic.

  Receivers see the batch **after** its write has been attempted — and only attempted:
  with the log off there is nothing to write, and a failed write publishes anyway. They
  get it as a tuple.

  Both are containment rather than convenience. Publishing came *first* originally, and
  receivers were handed the same list and the same `Event` objects the ORM was about to
  read — and a frozen dataclass does not freeze the `detail` dict inside it, so a
  receiver clearing the list or editing a `detail` changed what got persisted. It
  cannot now: by the time a receiver runs, the write is done. The tuple covers what
  ordering does not — `send_robust` hands every receiver the same argument, so one of
  them could otherwise decide what the next one sees. Each `detail` dict is still
  shared between receivers, so treat it as read-only.

  `Event`'s field names are pinned in `tests/test_public_surface.py`, which makes
  them public API. Importing the seam pulls neither aiogram nor the ORM: 0.356 ms
  on top of a process that has already imported Django, of which `django.dispatch`
  is 0.150 ms. And a process that has receivers but no table no longer imports
  `eventlog` — and so `django.db` — to close a connection it never opened.
  **Event log** has the recipe, including the two honest notes about
  `prometheus_client` and about which container has to run the exporter.

### Changed

- **Encoding a queued call takes one pass instead of two.** `encode()` rebuilt
  every container and `json.dumps` then walked the copy. A `JSONEncoder` that
  tags as it writes produces the same bytes from one walk: a plain send 2.17 →
  0.58 µs, an envelope 4.12 → 0.84 µs, a thirty-button keyboard **53.3 → 6.1 µs**.
  A payload built from aiogram model objects is unchanged at 1.0x — `ModelCodec`
  still recurses per field, and it has to, because `encode` is exported and a
  codec returning half-tagged data would break every caller that uses it alone.
  A payload too deeply nested to read back is still refused: the C encoder
  ignores Python's recursion limit while `decode` does not, so without a guard
  such a call would be queued happily and then be undecodable for ever.
- **Redaction reads the settings once per payload, not once per string.**
  `redact_text` resolved `TOKEN` and `WEBHOOK_SECRET` for every string at every
  depth of every event, and `to_row` rebuilt the redaction key set for every row
  of a two-hundred-row batch. Both are hoisted. A string with no colon also skips
  the token regex, which is exact — every token Telegram issues contains one —
  and covered by its own test, because what it guards is the token reaching a row.
- **The rate limiter no longer spins.** It paced correctly, but by counting
  tokens: every waiter recomputed the same wait from the same shared state, so N
  waiters woke together, one won and the rest went back to sleep. Measured at 40
  queued sends it woke **113,652** times; it now wakes 35. At 500 sends the old
  design burned 0.387 s of pure spinning. Admission also becomes strict FIFO —
  before, a herd re-racing for the same token admitted in whatever order the loop
  happened to resume, so the message that had waited longest had no claim on
  going first. The limits themselves are unchanged, and every existing pacing
  test passes untouched.
- Redis commands are documented as deliberately un-retried. `Redis.from_url`
  builds the pool before the client, so redis-py's client-level retry default
  never reached the connection and every command already ran with zero retries;
  that was an accident, and it is now a decision with a test behind it. Neither
  `RPUSH` nor `BLMOVE` is idempotent, and redis-py retries the whole
  send-and-read — a connection dropped after the server applied an `RPUSH` would
  queue the message twice and a real person would receive two of them.

### Infrastructure

- **`pip install -e '.[dev]'` becomes `pip install -e . --group dev`.** The dev
  requirements were an extra, so `Provides-Extra: dev` shipped in the wheel and
  every consumer resolving the package saw a group of linters they have no use
  for. They are a PEP 735 dependency group now, which needs pip 25.1 or newer —
  `AGENTS.md`, `CONTRIBUTING.md` and CI all say the new command.
- An optional `hiredis` extra: `pip install django-redis-aiogram[hiredis]` makes
  redis-py parse the protocol in C. Worth it on a busy consumer, pointless on a
  web tier that only ever pushes, which is why it is opt-in rather than a
  dependency.
- The sdist carries `docs/`, `scripts/`, `CONTRIBUTING.md`, `SECURITY.md` and
  `AGENTS.md`, and the wheel gains the `Framework :: AsyncIO`,
  `Framework :: Django :: 6.1` and `Topic :: Communications :: Chat`
  classifiers, plus `Repository` and `Funding` URLs.
- `redis_conn` is annotated for consumers. It forwards through `__getattr__`, so
  `redis_conn.ping()` typed as `Any` while `get_redis().ping()` did not; the
  smoke install now `assert_type`s it, which is the only place a packaging-level
  typing regression is catchable.
- CI gains a Django 6.1 leg and a `valkey/valkey:8` integration leg — the fork
  most managed providers actually run. The integration suite also asserts that an
  unknown command's error text contains `unknown command`, which is the single
  assumption the crash-safety downgrade rests on and which fakeredis can never
  answer.
- `publish.yml` checks that the release tag and `__version__` agree before
  building, and pins every action it runs to a commit — it is the one workflow
  holding `id-token: write`. The tag reaches the shell through `env` rather than
  template interpolation, since a tag may legally contain a quote, and the build
  tools are installed by hash from `.github/release-requirements.txt`, with
  `.github/release-constraints.txt` pinning the backend that `python -m build`
  resolves in an isolated environment of its own. That job is where third-party
  code last touches the artefact PyPI receives. `hatchling>=1.27` in
  `pyproject.toml` is unchanged: only what CI installs is pinned, not what
  consumers build against.
- Deprecation warnings fail the suite. Deliberately not a bare `error`: that
  escalates `ResourceWarning` into `PytestUnraisableExceptionWarning`, whose
  attribution follows GC timing and differs across the 3.10-3.14 legs.
- The lazy-boot tests assert what they were meant to. The first now compares a
  `sys.modules` delta against `sys.stdlib_module_names`, so it catches any
  third-party import the package pulls rather than aiogram alone, and a third
  test covers the checks. They run under `tests/bare_settings.py`, because the
  suite's usual settings install an app whose router imports aiogram anyway.
- `django-stubs` is held below 6.1. 6.1.0 resolves every `Self`-returning
  `QuerySet` method to `Any`, which fails `mypy --strict` on code that has not
  changed since 3.0.0.

## 3.0.0 - 2026-08-09

Two kinds of change at once: the compatibility 2.0 shipped for 1.x is gone —
the `telegram_bot` package name, `keyspace` delivery, and the string constants
that aliased enum members — and the package can now record what it did to a
table. The removals are mechanical and `manage.py check` names each one. The
event log is opt-in and off by default.

One piece of compatibility is **added** rather than removed: the consumer reads
both the new envelope and the flat payload 2.x wrote, so a backlog drains
across the upgrade.

**Upgrade the bot container before the web tier.** Queued payloads now carry an
envelope, and a 2.x consumer handed one loses the message.

**Run `manage.py migrate`.** The package ships a table for the first time,
created whether or not you turn the log on.

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
- **Queued payloads carry an envelope.** 2.x wrote
  `{'function': name, **kwargs}` and the consumer splatted it straight back into
  aiogram, so there was nowhere to put a correlation id without it arriving at
  Telegram as an unexpected argument. 3.0 nests the arguments under
  `__envelope__` instead. The reader accepts the old flat shape, so a backlog
  drains — but the reverse does not hold: a 2.x consumer handed a new payload
  calls the Telegram method with `__envelope__` as a keyword, raises, logs it
  and swallows it, and the message is lost. **Deploy the bot container before
  the web tier.** A test reproduces the failure so the constraint is not
  folklore.
- `bot.send()`, `send_redis()` and `send_raw()` return the correlation id
  instead of `None`, and take one as a keyword argument. Source-compatible at
  every existing call site. A handler replying to an update inherits that
  update's id through a context variable, so a reply is joined to its cause
  without any project code passing it along.
- **Inbound updates and FSM transitions are recorded too.** One outer
  middleware on the dispatcher sees every update exactly once, whether it
  arrived by polling or by webhook, and records when it arrived, how long the
  handlers took and whether they raised. FSM transitions come from wrapping the
  configured storage: `set_state` *is* the transition, so it costs no extra
  round trip and misses none of the ones a filter or a scene makes.
- With the log off, no middleware is registered and the storage is handed back
  unwrapped — the cost per update is zero rather than one branch.
- `EVENT_LOG_SYNC` falls back to the writer thread inside a running loop. The
  ORM is unusable there, so writing on the calling thread would turn the update
  middleware from a recorder into a source of `SynchronousOnlyOperation`.
- Asserting on the queue in your own tests now goes through
  `envelope.unpack` — see **Testing** in the wiki, whose recipes are executed
  by the suite and were updated with the format.
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
- **A read-only admin for the feed**, registered only when the flag is on and
  only when `django.contrib.admin` is installed. It is built for a table nobody
  wants to count: paging counts at most ten thousand rows inside a `LIMIT` and
  says when it stopped rather than reporting the cap as the answer, no date
  drilldown, exact-match search on the two indexed columns, and a filter built
  from the kind registry rather than from a `SELECT DISTINCT` over the table.
- Access splits in two: `view_telegramevent` for the list and the detail page,
  and `view_telegramevent_payload` for message bodies and exception text, so
  support can see that a message went out without reading what it said.
- `manage.py tgbot_prune_events` is what bounds the table. It deletes by
  primary-key range in bounded chunks, one transaction each, so it never holds
  a long lock and a range at the cold end cannot conflict with the inserts
  still arriving at the hot end. `--sleep` paces it for replicas, `--max-chunks`
  bounds a nightly run, `--dry-run` reports without deleting. With
  `EVENT_LOG_RETENTION_DAYS` unset it deletes nothing and says so — guessing a
  window would be a data-loss bug — and `W006` warns while it is unset. Two
  things the wiki page spells out and that bite after the fact: on PostgreSQL
  the space returns through autovacuum rather than immediately, so a large first
  prune wants a plain `VACUUM` afterwards (never `FULL`, which takes an
  exclusive lock); and attaching a `ForeignKey` to `TelegramEvent` disables
  Django's fast-delete path, so every prune then has to fetch primary keys
  first.
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