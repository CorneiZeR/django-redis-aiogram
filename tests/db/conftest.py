"""Guards for the database suite.

Run it with its own settings module:

    python -m pytest --ds=tests.db_settings tests/db
"""

import threading

import pytest
from django.conf import settings

from django_redis_aiogram.recorder import WRITER_THREAD


@pytest.fixture
def paused_writer(monkeypatch):
    """Let the recorder build its queue without anything draining it.

    `record()` starts the writer on the first event, and a live writer makes
    every assertion about the buffer a race: it may have taken the event before
    the test looks. Neutering start() leaves the production path intact —
    queue, bounds, drops — with the test in charge of when it is drained.

    join() goes with it, because joining a thread that never started raises.

    Keyed on the writer's name: patching every Thread would silently neuter any
    other thread a test starts, which is a trap rather than a fixture.
    """
    real_start, real_join = threading.Thread.start, threading.Thread.join

    def start(self):
        if self.name != WRITER_THREAD:
            real_start(self)

    def join(self, timeout=None):
        if self.name != WRITER_THREAD:
            real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(threading.Thread, 'join', join)


def pytest_configure(config):
    """Refuse to run under the no-database settings, rather than erroring later.

    The default invocation ignores `tests/db`, so getting here with the wrong
    module means someone pointed pytest at it by hand; a dozen confusing
    ImproperlyConfigured failures is a worse answer than one sentence.
    """
    if not settings.DATABASES:
        pytest.exit('tests/db needs a database: run it with --ds=tests.db_settings', returncode=4)
