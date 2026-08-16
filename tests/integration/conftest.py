"""Fixtures for the suite that needs a real Redis.

fakeredis agrees with everything, which is exactly why it cannot answer the
questions that broke 1.x deployments: whether `LMOVE` exists, what a server
without it says when asked, and whether FSM state written by one process is
readable by the next.
"""

import os

import pytest
from redis import Redis

from django_redis_aiogram.redis import reset_redis
from django_redis_aiogram.throttling import reset_rate_limiters

# the marker is registered in pyproject.toml, and each module carries it itself
REDIS_URL = os.environ.get('DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL', '')


@pytest.fixture(scope='session')
def redis_url():
    if not REDIS_URL:
        pytest.skip('set DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL to run the integration suite')
    return REDIS_URL


@pytest.fixture
def server(redis_url):
    """A real client, flushed before each test so nothing leaks between them.

    **This erases the whole selected database**, before and after every test.
    Point `DJANGO_REDIS_AIOGRAM_TEST_REDIS_URL` at a throwaway server or at
    least a database nothing else uses.
    """
    client = Redis.from_url(redis_url)
    client.flushdb()
    reset_redis()
    reset_rate_limiters()
    try:
        yield client
    finally:
        client.flushdb()
        reset_redis()
        reset_rate_limiters()
        client.close()


@pytest.fixture
def version(server):
    """The server's Redis version as a tuple, for skipping what it cannot do."""
    raw = str(server.info('server')['redis_version'])
    return tuple(int(part) for part in raw.split('.')[:2])
