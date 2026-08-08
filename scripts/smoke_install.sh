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

echo "--- building the wheel"
python -m build --wheel --outdir "$work/dist" "$root" >/dev/null
wheel="$(ls "$work"/dist/*.whl)"
echo "built $(basename "$wheel")"

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
from django_redis_aiogram import bot

def notify(chat_id: int) -> None:
    bot.send(chat_id=chat_id, text='hi')
PY
"$work/venv/bin/mypy" --strict uses_it.py 2>&1 | sed 's/^/    /'

echo
echo "smoke install passed"
