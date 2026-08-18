"""Settings whose ``INSTALLED_APPS`` contains an app that marks its own `ready()`.

Used only by the healthcheck's registry test: it asserts that
``python -m django_redis_aiogram.healthcheck`` never populates the registry, and the
only honest way to show that is an app that would have left evidence if it had.
"""

SECRET_KEY = 'test-only'
USE_TZ = True
DATABASES: dict[str, dict[str, str]] = {}
INSTALLED_APPS = ['django_redis_aiogram', 'tests.marker_app.apps.MarkerConfig']

TELEGRAM_BOT: dict[str, object] = {}
