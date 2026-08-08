"""Settings for the half of the suite that needs a database.

`tests/settings.py` deliberately has no database at all, because proving the
package boots without one is part of what it tests. Adding sqlite there would
delete that invariant with nothing failing, so the database-backed tests get
their own module and their own pytest invocation.

Written out rather than star-imported from `tests.settings`: ruff runs over
`tests/` with `select = ["ALL"]`, and F403/F405 are not in the per-file ignores.
"""

SECRET_KEY = 'test-only'
USE_TZ = True

# the test runner replaces this with file:memorydb_default?mode=memory&cache=shared,
# which is what lets a background thread's own connection see the same rows
DATABASES = {
    'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
    # a second alias, so EVENT_LOG_DATABASE and the router are testable at all
    'logs': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
}

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.admin',
    'django_redis_aiogram',
    'tests.fake_app',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.request',
            ],
        },
    },
]

ROOT_URLCONF = 'tests.db_urls'
STATIC_URL = '/static/'

TELEGRAM_BOT: dict[str, object] = {}
