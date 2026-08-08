"""Guards for the database suite.

Run it with its own settings module:

    python -m pytest --ds=tests.db_settings tests/db
"""

import threading

import pytest
from django.conf import settings


@pytest.fixture
def paused_writer(monkeypatch):
    """Let the recorder build its queue without anything draining it.

    `record()` starts the writer on the first event, and a live writer makes
    every assertion about the buffer a race: it may have taken the event before
    the test looks. Neutering start() leaves the production path intact —
    queue, bounds, drops — with the test in charge of when it is drained.

    join() goes with it, because joining a thread that never started raises.
    """
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    monkeypatch.setattr(threading.Thread, 'join', lambda self, timeout=None: None)


def pytest_configure(config):
    """Refuse to run under the no-database settings, rather than erroring later.

    The default invocation ignores `tests/db`, so getting here with the wrong
    module means someone pointed pytest at it by hand; a dozen confusing
    ImproperlyConfigured failures is a worse answer than one sentence.
    """
    if not settings.DATABASES:
        pytest.exit('tests/db needs a database: run it with --ds=tests.db_settings', returncode=4)
