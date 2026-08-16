# Contributing

Thanks for taking the time. Bug reports with a reproduction are as useful as
patches.

`AGENTS.md` covers the same ground in the form coding agents read: the layout,
the commands, and the invariants that must stay covered.

## Getting set up

```shell
git clone git@github.com:CorneiZeR/django-redis-aiogram.git
cd django-redis-aiogram
python -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e . --group dev
```

Python 3.10–3.14 is supported.

`--group` is [PEP 735](https://peps.python.org/pep-0735/), which pip understands
from **25.1**. A fresh virtual environment inherits whatever pip your Python
shipped with — on 3.10 that is old enough to reject the flag as unknown — so the
upgrade above is not ceremony. `pip --version` if it fails.

## Before opening a pull request

```shell
ruff check .
ruff format --check .
mypy
python -m pytest
python -m pytest --ds=tests.db_settings tests/db
```

`tests/settings.py` configures no database at all, because proving the package
boots without one is part of what the suite tests. Anything that needs a
database therefore lives in `tests/db` with its own settings module, and the
default run ignores that directory — so it takes a second invocation.

A third suite needs a real Redis and is skipped without one:

```shell
docker run -d --name drai-redis -p 6399:6379 redis:7-alpine
until docker exec drai-redis redis-cli ping >/dev/null 2>&1; do sleep 0.3; done
DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL=redis://localhost:6399/0 python -m pytest -m integration
```

`docker run` returns before the server accepts connections, hence the wait. Give
it a **throwaway** server or at least a database nothing else uses: the fixture
runs `FLUSHDB` before and after every test.

It covers what fakeredis cannot: whether `LMOVE` exists and the consumer picks
the crash-safe path, whether a reclaim takes back only its own worker's message,
a mixed pickle/JSON backlog draining, and FSM state surviving a restart.
`scripts/smoke_install.sh` is the other half — it builds the wheel and the sdist,
checks what each of them carries, installs the wheel into a throwaway project and
checks that Django boots with no credentials at all.

CI splits those up: `ruff`, `ruff format` and `mypy` run once on Python 3.13,
while `pytest` runs across Python 3.10–3.14 × Django 5.2/6.0/6.1, plus a job
pinning the lowest supported versions of every dependency. Every version the package
advertises has to pass before a merge.

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
  that boundary. A third exists whenever the event log is on — the writer —
  and it owns its own database connection; nothing else closes it. With the log
  off it is never started.
- **A schema change ships with its migration**, in the same pull request. CI
  runs `makemigrations --check`, and `smoke_install.sh` applies the migration
  against the built wheel, so a `migrations/` directory left out of packaging
  fails there rather than in someone's deployment.
- **A new setting goes in three places**: `DEFAULTS`, a check in `checks.py`,
  and `docs/wiki/Settings.md`. Miss the first and `W003` warns about it; miss
  the third and the check-id test fails.
- **A new event kind is registered in `events.py`** and documented on the Event
  log page. It is not a schema change, which is the point of the registry.

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

## Documentation

Wiki pages live in `docs/wiki/`. Edit them there, in the same pull request as
the code they describe; a push to `master` publishes them to the wiki. Links
between pages use `[[Page-Name]]`, or `[[Page-Name|Link text]]` when the label
differs from the page name — the page always comes first — and tests
check that they all resolve and that none is written the other way round.

## Commits

Explain why the change is needed, not just what changed. If it fixes a bug,
describe the failure it produces.

## Releases

Publishing runs on [Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so there is no API token anywhere. Every action is pinned to a commit, and the
build job installs its tools by hash from `.github/release-requirements.txt`,
with `.github/release-constraints.txt` pinning the build backend that
`python -m build` resolves in an isolated environment of its own.

Refresh both before cutting a release — the command that generates each is in
its header — and open the refresh as its own pull request, so the diff is
reviewed rather than landing alongside the release commit. A pin that has gone
stale is only discovered when the release workflow runs, which is the worst
moment to find out.
