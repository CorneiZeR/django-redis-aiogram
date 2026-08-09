# AGENTS.md

Instructions for coding agents working on **this repository**. For agents
integrating the package into a project, see the wiki page
[AI assistants](https://github.com/CorneiZeR/django-redis-aiogram/wiki/AI-assistants).

## What this is

A Django app that runs aiogram in a neighbouring container and queues Telegram
messages through Redis. The Django processes never poll; they push a payload
onto a Redis list, and the bot container consumes it.

```text
src/django_redis_aiogram/
    __init__.py     lazy exports: bot, conf, redis_conn, get_redis, __version__
    apps.py         AppConfig.ready(): checks and autodiscover, both behind ENABLED
    client.py       TelegramBot: bot/dispatcher/loop, send, send_raw, send_redis
    api.py          the allowlist of Telegram API method names a payload may use
    delivery.py     BlpopDelivery, the one consumer
    serializers.py  tagged JSON, and pickle behind ALLOW_PICKLE
    throttling.py   token buckets, one budget per token
    checks.py       system checks E001-E042, W001-W009
    settings.py     lazy settings with an environment fallback
    redis.py        lazy connection
    routers.py      autodiscover
    models.py       TelegramEvent, the append-only feed; migrations/ beside it
    events.py       the event-kind registry and the correlation id
    recorder.py     the bounded queue and the writer thread; no django.db here
    eventlog.py     the only module that touches the ORM
    dbrouter.py     optional routing of the log to its own database
docs/wiki/          the wiki, published from master
tests/              pytest, fakeredis, no network
```

## Commands

```shell
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy && python -m pytest -q
python -m pytest -q --ds=tests.db_settings tests/db
```

Those gate every pull request. `pytest` needs no Redis and no token.

The second invocation is the database-backed half. `tests/settings.py` has
`DATABASES = {}` on purpose — proving the package boots without one is part of
what the suite tests — so anything needing a database lives in `tests/db` under
`tests/db_settings.py`, and the default run ignores that directory.

CI also runs the two below — integration against a real Redis service, and the
smoke install — so a change that only passes the loop above can still fail the
build. Run them locally when you touch delivery, packaging or the public
surface:

```shell
DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL=redis://localhost:6399/0 python -m pytest -m integration
bash scripts/smoke_install.sh
```

The first needs a real server; run it when you touch delivery, serialization,
FSM persistence or connection cleanup. It flushes the database it is pointed at,
so point it at a throwaway one.

The second builds and installs the wheel; run it when you touch packaging,
Django startup or the public surface — it type-checks a consumer file against
the installed package, so a moved export fails there and nowhere else.
Packaging-only work does not need the Redis suite, and vice versa.

## Rules that are not negotiable

- **Nothing happens at import time.** The package must import, and Django must
  boot, with no token and no reachable Redis. Anything that connects or
  validates credentials goes behind a property or a function. This is the defect
  2.0 existed to fix; re-introducing it breaks every consumer's test suite.
- **Importing the package stays cheap.** `__init__` resolves its exports lazily
  (PEP 562) so `import django_redis_aiogram` costs ~1 ms, and a disabled Django
  boot never loads aiogram (~900 ms). `tests/test_lazy_init.py` pins both in
  subprocesses; an eager import anywhere on the boot path fails them.
- **Every change carries a test, and the test must fail without the change.**
  Revert your fix, watch the test fail, put it back. A test that passes either
  way is worse than none, because it reads as coverage.
- **Values go in `extra`, not in the message.** `logger.warning('rate limited',
  extra={'tg_function': name})`, never an f-string. Keys are `tg_`-prefixed so
  they cannot collide with `LogRecord` attributes.
- **Never log through the root logger.** `logging.getLogger('django_redis_aiogram')`
  only; `tests/test_logging_discipline.py` enforces it, `logging.basicConfig()`
  included.
- **Thread boundaries are real.** The delivery consumer runs in its own thread
  while the event loop belongs to the polling thread. `create_task` across that
  boundary corrupts the loop; go through `TelegramBot._schedule`.
- **Anything reaching the network can fail.** `run()` is a thread target: an
  exception escaping it ends the consumer for the life of the container. Log and
  continue, or retry.
- **`tests/test_public_surface.py` is a contract.** It pins the shape of
  `TelegramBot` that predates 2.0 — attributes, methods and the observer
  decorators. Adding to that surface is fine; moving or removing anything on it
  is a breaking change and needs the changelog entry to say so.
- **`models.py` imports no aiogram.** Django imports it on every
  `django.setup()`, before `ready()` and regardless of `ENABLED`, so a migration
  container pays for whatever it pulls. `django.db.models` and
  `django_redis_aiogram.enums`/`events` only — never `client`, `serializers` or
  `api`. `tests/test_event_log_off.py` boots a subprocess to prove it.
- **`recorder.py` imports no `django.db`.** Only `eventlog.py` does, and the
  writer thread imports it on its first flush. That is what makes a disabled log
  cost nothing and what makes `record()` legal from a coroutine — `put_nowait`
  touches no I/O, so there is no `SynchronousOnlyOperation` to avoid.
- **The feed is append-only.** No updates, no foreign keys, no
  `Meta.constraints`, no index on the JSON column. Fast pruning, shardability
  and two processes writing one message's history without coordination all rest
  on it; a foreign key alone breaks Django's fast-delete path.
- **`record()` may neither raise nor wait.** A log that can break delivery is
  worse than no log, so everything — the settings read included — is wrapped.
- **The fact table stores identifiers, never descriptors.** A chat title belongs
  in `detail` as a snapshot of the event, not in a column.
- **The token must not reach a row.** It is in the API URL, aiogram puts the URL
  in its exception messages, and those messages are what an `error` column holds.
- **Interpolate `.value`, never a `(str, Enum)` member.** On newer Pythons a
  member formats as its own qualified name.

## Style

- Code, comments, docstrings and documentation are in English.
- Comment what is not obvious from the code, one line by default. Explain *why*,
  not *what*.
- Public API is annotated; the package ships `py.typed` and mypy runs on it.
- No new runtime dependencies without a reason that survives being questioned.

## Documentation

Wiki pages live in `docs/wiki/` and are edited in the same pull request as the
code they describe. Links are `[[Page-Name]]`, or `[[Page-Name|Link text]]`
with the page first when the label differs;
`tests/test_wiki.py` checks that every link resolves, that the sidebar lists
every page, and that the README's wiki links are not stale. Configuration
examples in the docs are executed by `tests/test_docs_examples.py` and
`tests/test_documented_recipes.py`, so a snippet that cannot run fails the build.

## Pull requests

One reviewable change per pull request, green on `ruff`, `ruff format`, `mypy`
and `pytest`. Say why the change is needed and what failure it produces.
[CodeRabbit](https://github.com/apps/coderabbitai) reviews automatically;
answer its findings, fix what is still valid and say plainly what you skipped
and why.
