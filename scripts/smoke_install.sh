#!/usr/bin/env bash
# Install the built wheel into a throwaway Django project and check that a
# project boots with no credentials at all.
#
# The unit suite imports from the source tree, so it cannot catch a packaging
# mistake: a missing py.typed, a module that only resolves because `src/` is on
# the path. This does.
set -euo pipefail

# a PYTHONPATH pointing at src/ would let imports resolve from the source tree,
# so a wheel missing a module would still pass — the one thing this must catch
unset PYTHONPATH PYTHONHOME MYPYPATH

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# a directory of already-built artifacts, when the caller has one. The release workflow
# does: it builds once, uploads that wheel, and this must check *that* file rather than
# one built again here — same source, but not the same bytes, and the bytes are what
# PyPI keeps for ever
if [ -n "${1:-}" ]; then
    echo "--- using the artifacts already built in $1"
    wheel="$(ls "$1"/*.whl)"
    sdist="$(ls "$1"/*.tar.gz)"
else
    echo "--- building the wheel and the sdist"
    python -m build --sdist --wheel --outdir "$work/dist" "$root" >/dev/null
    wheel="$(ls "$work"/dist/*.whl)"
    sdist="$(ls "$work"/dist/*.tar.gz)"
fi
echo "checking $(basename "$wheel") and $(basename "$sdist")"

echo "--- the sdist must carry what a rebuild and a review need"
python - "$sdist" <<'PY'
import sys, tarfile

with tarfile.open(sys.argv[1]) as archive:
    # the leading directory is <name>-<version>/, which no expectation should
    # depend on, so compare on what follows it
    names = {name.split('/', 1)[1] for name in archive.getnames() if '/' in name}
for expected in (
    'src/django_redis_aiogram/__init__.py',
    'tests/test_public_surface.py',
    'docs/wiki/Delivery.md',
    'scripts/smoke_install.sh',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'AGENTS.md',
):
    assert expected in names, f'{expected} missing from the sdist'
print('sources, tests, docs and the contributor files all travel with the sdist')
PY

echo "--- the wheel must carry what a consumer needs"
python - "$wheel" <<'PY'
import sys, zipfile

names = set(zipfile.ZipFile(sys.argv[1]).namelist())
for expected in (
    'django_redis_aiogram/py.typed',
    'django_redis_aiogram/api.py',
    'django_redis_aiogram/management/commands/start_tgbot.py',
    'django_redis_aiogram/migrations/__init__.py',
    'django_redis_aiogram/migrations/0001_initial.py',
    # every migration, not just the first: 3.1.0 ships 0002 and the Upgrading page
    # tells operators to run it, so a wheel that dropped it would break the upgrade
    # it documents
    'django_redis_aiogram/migrations/0002_kind_id_index.py',
):
    assert expected in names, f'{expected} missing from the wheel'
# 3.0 removed the 1.x package name; packaging it again would silently recreate
# the site-packages collision the rename was made to fix
shim = [name for name in names if name == 'telegram_bot.py' or name.startswith('telegram_bot/')]
assert not shim, f'the removed telegram_bot package is back in the wheel: {shim}'
print('py.typed and the management command are present; the 1.x name is absent')
PY

echo "--- installing into a fresh environment"
python -m venv "$work/venv"
"$work/venv/bin/pip" install -q --upgrade pip
"$work/venv/bin/pip" install -q "$wheel"

echo "--- a project with neither TOKEN nor REDIS_URL"
mkdir -p "$work/project"
cat > "$work/project/settings.py" <<'PY'
SECRET_KEY = 'smoke'
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth', 'django_redis_aiogram']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'smoke.sqlite3'}}
USE_TZ = True
PY
cat > "$work/project/manage.py" <<'PY'
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
PY

cd "$work/project"
echo "check:"
"$work/venv/bin/python" manage.py check 2>&1 | sed 's/^/    /'

echo "--- the shipped migration applies"
"$work/venv/bin/python" manage.py migrate --noinput 2>&1 | sed 's/^/    /'
"$work/venv/bin/python" manage.py makemigrations --check --dry-run 2>&1 | sed 's/^/    /'

echo "--- the healthcheck runs from an install, without django.setup()"
# the Deployment page tells readers to put this exact command in a compose file, and
# an install is the only place a packaging-level break in it shows up: a module missing
# from the wheel, or one that has grown an import needing the app registry
DJANGO_SETTINGS_MODULE=settings DJANGO_REDIS_AIOGRAM_REDIS_URL=redis://127.0.0.1:1/0 \
    "$work/venv/bin/python" -m django_redis_aiogram.healthcheck > "$work/probe.out" 2> "$work/probe.err" \
    && probe_status=0 || probe_status=$?
grep -q 'redis is unreachable' "$work/probe.err" \
    || { echo "    the probe did not reach Redis at all:"; sed 's/^/      /' "$work/probe.err"; exit 1; }
[ "$probe_status" = 1 ] || { echo "    expected exit 1 from an unreachable Redis, got $probe_status"; exit 1; }
echo "    python -m django_redis_aiogram.healthcheck refused an unreachable Redis with exit 1"

# and the path a healthy container takes, which is the one compose depends on: a probe
# that only ever answered "unreachable" would pass the check above while returning
# non-zero for every healthy container, and nothing here would notice
if [ -n "${DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL:-}" ]; then
    # a reachable server is not a healthy bot: the probe also wants a heartbeat, which is
    # what a running consumer writes. Written here by hand, because starting a consumer
    # would need a Telegram token — and what is under test is the probe's success path,
    # not the consumer's
    DJANGO_REDIS_AIOGRAM_REDIS_URL="$DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL" \
        "$work/venv/bin/python" - <<PROBE
import time

import redis

# the same write the consumer makes: an epoch second as text, with a TTL. The probe
# reads it as a timestamp, so a placeholder value reads as a worker last seen in 1970
client = redis.Redis.from_url("$DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL")
client.set('TELEGRAM_BOT_MESSAGE:heartbeat:smoke', str(int(time.time())), ex=60)
PROBE
    DJANGO_SETTINGS_MODULE=settings DJANGO_REDIS_AIOGRAM_REDIS_URL="$DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL" \
        DJANGO_REDIS_AIOGRAM_WORKER_NAME=smoke \
        "$work/venv/bin/python" -m django_redis_aiogram.healthcheck > "$work/ok.out" 2> "$work/ok.err" \
        && healthy_status=0 || healthy_status=$?
    [ "$healthy_status" = 0 ] || {
        echo "    a reachable Redis still gave exit $healthy_status:"
        sed 's/^/      /' "$work/ok.err"
        exit 1
    }
    grep -q . "$work/ok.out" || { echo '    the healthy probe printed nothing on stdout'; exit 1; }
    echo "    and answered a reachable Redis with exit 0 and a line on stdout"
else
    echo "    (set DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL to check the success path too)"
fi

echo "--- the 1.x package name is gone from the installed environment"
"$work/venv/bin/python" - <<'PY'
import importlib.util

assert importlib.util.find_spec('telegram_bot') is None, 'telegram_bot is still importable'
print('    telegram_bot no longer resolves')
PY

echo "--- disabled, the command exits cleanly"
disabled_output="$(DJANGO_REDIS_AIOGRAM_ENABLED=0 "$work/venv/bin/python" manage.py start_tgbot 2>&1)"
echo "$disabled_output" | sed 's/^/    /'
case "$disabled_output" in
  *disabled*) ;;
  *) echo "the command exited 0 but said nothing about being disabled" >&2; exit 1 ;;
esac

echo "--- types are visible to a consumer"
"$work/venv/bin/pip" install -q mypy
cat > uses_it.py <<'PY'
# typing.assert_type is 3.11 and up, and this script runs on whatever python a
# contributor has. mypy installs typing_extensions itself, so this adds nothing
import uuid

from typing_extensions import assert_type

from redis import Redis

from django_redis_aiogram import bot, redis_conn

def notify(chat_id: int) -> None:
    bot.send(chat_id=chat_id, text='hi')

async def notify_from_async(chat_id: int) -> None:
    # 3.1.0's own surface, checked against the installed package rather than the
    # checkout: `asend` returns the correlation id, `aqueue_depth` an int, and
    # `close` takes the drain budget as a float
    identifier = await bot.asend(chat_id=chat_id, text='hi')
    assert_type(identifier, uuid.UUID)
    assert_type(await bot.aqueue_depth(), int)
    bot.close(drain_timeout=0.5)

# redis_conn forwards through __getattr__, so it resolved to Any while
# get_redis() did not. Nothing is called here: this is the installed package's
# typing, checked without a server
assert_type(redis_conn, Redis)
PY
"$work/venv/bin/mypy" --strict uses_it.py 2>&1 | sed 's/^/    /'

echo "--- the metadata a consumer resolves against"
"$work/venv/bin/python" - <<'META'
from importlib.metadata import metadata

fields = metadata('django-redis-aiogram')
classifiers = fields.get_all('Classifier') or []
for expected in ('Framework :: AsyncIO', 'Framework :: Django :: 6.1', 'Typing :: Typed'):
    assert expected in classifiers, f'{expected} is missing from the wheel metadata'
extras = fields.get_all('Provides-Extra') or []
assert 'hiredis' in extras, extras
# dev is for this repository, not for anyone installing the package
assert 'dev' not in extras, f'the dev extra shipped in the wheel: {extras}'
print('    classifiers, extras and the absence of dev all check out')
META

echo
echo "smoke install passed"
