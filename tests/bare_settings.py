"""Settings with nothing installed but the package itself.

`tests/settings.py` also installs `tests.fake_app`, whose `tg_router` imports aiogram — so
autodiscover pulls it at `django.setup()` and any assertion about what the *checks* import
would pass whether or not they behave. This module exists so that question can be asked.
"""

SECRET_KEY = 'test-only'
USE_TZ = True
DATABASES: dict[str, dict[str, str]] = {}
INSTALLED_APPS = ['django_redis_aiogram']

# set, so the checks report nothing and the assertion is about imports alone; neither
# value is ever connected to, because running the checks reaches no network
TELEGRAM_BOT: dict[str, object] = {
    'TOKEN': '1234567890:test-token-that-is-never-used-for-anything',
    'REDIS_URL': 'redis://localhost:6379/0',
}
