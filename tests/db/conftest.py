"""Guards for the database suite.

Run it with its own settings module:

    python -m pytest --ds=tests.db_settings tests/db
"""

import pytest
from django.conf import settings


def pytest_configure(config):
    """Refuse to run under the no-database settings, rather than erroring later.

    The default invocation ignores `tests/db`, so getting here with the wrong
    module means someone pointed pytest at it by hand; a dozen confusing
    ImproperlyConfigured failures is a worse answer than one sentence.
    """
    if not settings.DATABASES:
        pytest.exit('tests/db needs a database: run it with --ds=tests.db_settings', returncode=4)
