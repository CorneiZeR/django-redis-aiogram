SECRET_KEY = 'test-only'
USE_TZ = True
DATABASES: dict[str, dict[str, str]] = {}
INSTALLED_APPS = ['django_redis_aiogram', 'tests.fake_app']

# deliberately no TOKEN and no REDIS_URL: the suite asserts the package stays
# importable and Django stays bootable without them
TELEGRAM_BOT: dict[str, object] = {}
