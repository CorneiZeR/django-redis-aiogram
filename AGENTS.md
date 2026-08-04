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
    delivery.py     BlpopDelivery and KeyspaceDelivery consumers
    serializers.py  tagged JSON, and pickle behind ALLOW_PICKLE
    throttling.py   token buckets, one budget per token
    checks.py       system checks E001-E022, W001-W003
    settings.py     lazy settings with an environment fallback
    redis.py        lazy connection
    routers.py      autodiscover
src/telegram_bot/   deprecated shim for the 1.x package name, removed in 3.0
docs/wiki/          the wiki, published from master
tests/              pytest, fakeredis, no network
```

## Commands

```shell
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy && python -m pytest -q
```

Those four are exactly what CI gates on. `pytest` needs no Redis and no token.

Two more suites exist and are not part of that loop:

```shell
DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL=redis://localhost:6399/0 python -m pytest -m integration
bash scripts/smoke_install.sh
```

The first needs a real server; run it when you touch delivery, serialization,
FSM persistence, keyspace notifications or connection cleanup. It flushes the
database it is pointed at, so point it at a throwaway one.

The second builds and installs the wheel; run it when you touch packaging,
Django startup or the shim. Packaging-only work does not need the Redis suite,
and vice versa.

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
- **The shim stays working.** `telegram_bot` must remain importable and usable in
  `INSTALLED_APPS` until 3.0.

## Style

- Code, comments, docstrings and documentation are in English.
- Comment what is not obvious from the code, one line by default. Explain *why*,
  not *what*.
- Public API is annotated; the package ships `py.typed` and mypy runs on it.
- No new runtime dependencies without a reason that survives being questioned.

## Documentation

Wiki pages live in `docs/wiki/` and are edited in the same pull request as the
code they describe. Links are `[[Page-Name|Link text]]`, page first;
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
