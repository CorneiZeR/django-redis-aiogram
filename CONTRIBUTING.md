# Contributing

Thanks for taking the time. Bug reports with a reproduction are as useful as
patches.

## Getting set up

```shell
git clone git@github.com:CorneiZeR/django-redis-aiogram.git
cd django-redis-aiogram
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

Python 3.10–3.14 is supported.

## Before opening a pull request

```shell
ruff check .
ruff format --check .
mypy
python -m pytest
```

CI splits those up: `ruff`, `ruff format` and `mypy` run once on Python 3.13,
while `pytest` runs across Python 3.10–3.13 × Django 5.2/6.0, plus a job pinning
the lowest supported versions of every dependency. Python 3.14 runs too but
cannot block a merge, since its dependency wheels still lag.

## What the tests care about

A few invariants are easy to break by accident, so they have dedicated tests.
If you touch these areas, keep them covered:

- **Nothing happens at import time.** The package must import, and Django must
  boot, with no token and no reachable Redis. Anything that connects or
  validates credentials belongs behind a property or a function.
- **Serialization round-trips exactly.** Queued payloads must come back as the
  same concrete types. Discriminated unions such as `InputMediaPhoto` are the
  trap: dropping unset fields silently turns one into an `InputMediaAudio`.
- **Thread boundaries.** The delivery consumer runs in its own thread while the
  event loop belongs to the polling thread. Never call `create_task` across
  that boundary.
- **The `telegram_bot` shim keeps working.** It must stay importable and usable
  in `INSTALLED_APPS` until 3.0.

## Style

- Code, comments and docstrings are written in English.
- Comment only what is genuinely non-obvious, and prefer a single line.
- Log events with a constant message and values in `extra`, so structured log
  backends can index them:

  ```python
  logger.warning('rate limited by telegram', extra={'tg_function': function})
  ```

  Prefix the keys — unprefixed names can collide with `LogRecord` attributes.

## Reviews

Pull requests are reviewed automatically by
[CodeRabbit](https://github.com/apps/coderabbitai), which is free for public
repositories. `.coderabbit.yaml` points it at the invariants above.

## Commits

Explain why the change is needed, not just what changed. If it fixes a bug,
describe the failure it produces.
