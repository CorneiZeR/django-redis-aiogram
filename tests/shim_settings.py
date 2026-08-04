"""Settings for the subprocess that boots a 1.x project through the shim."""

SECRET_KEY = 'shim'
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth', 'telegram_bot']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
USE_TZ = True
