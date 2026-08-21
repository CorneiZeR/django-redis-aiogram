"""FSM state has to outlive the container.

1.x built the dispatcher with no storage, so aiogram fell back to MemoryStorage
and every dialogue reset on deploy. The claim that `FSM_STORAGE: 'redis'` fixes
it is a claim about a real server, so it is tested against one.
"""

import time

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from django.test import override_settings

from django_redis_aiogram import TelegramBot

pytestmark = pytest.mark.integration

KEY = StorageKey(bot_id=42, chat_id=1, user_id=1)


def test_state_written_before_a_restart_is_there_after(server, redis_url):
    with override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': redis_url}):
        before = TelegramBot()
        storage = before.dispatcher.storage
        assert isinstance(storage, RedisStorage), 'redis is the documented default'

        async def write() -> None:
            await storage.set_state(KEY, 'awaiting_photo')
            await storage.set_data(KEY, {'order': 7})

        before.loop.run_until_complete(write())
        before.close()  # the whole container goes away

        after = TelegramBot()

        async def read() -> tuple[str | None, dict]:
            return (
                await after.dispatcher.storage.get_state(KEY),
                await after.dispatcher.storage.get_data(KEY),
            )

        state, data = after.loop.run_until_complete(read())
        after.close()

    assert state == 'awaiting_photo', 'the dialogue was reset by the restart'
    assert data == {'order': 7}


def test_memory_storage_loses_it_which_is_why_redis_is_the_default(server, redis_url):
    """The 1.x behavior, kept as an option and shown to be the wrong default."""
    with override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': redis_url, 'FSM_STORAGE': 'memory'}):
        before = TelegramBot()
        assert isinstance(before.dispatcher.storage, MemoryStorage)
        before.loop.run_until_complete(before.dispatcher.storage.set_state(KEY, 'awaiting_photo'))
        before.close()

        after = TelegramBot()
        state = after.loop.run_until_complete(after.dispatcher.storage.get_state(KEY))
        after.close()

    assert state is None, 'memory storage kept state across a restart, which it cannot'


def test_closing_releases_the_storage_client(server, redis_url):
    """RedisStorage owns a second connection, and nothing else closes it.

    Asserting on a later call would prove nothing: redis-py reconnects
    transparently. Counting clients would be brittle, since anything else on the
    server shifts the number. So the connection is identified: whichever address
    appears while the storage is in use is the one that has to be gone after
    close().
    """

    def addresses() -> set[str]:
        return {str(client['addr']) for client in server.client_list()}

    with override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': redis_url}):
        before = addresses()
        instance = TelegramBot()
        instance.loop.run_until_complete(instance.dispatcher.storage.set_state(KEY, 'x'))

        opened = addresses() - before
        assert opened, 'the storage never opened a connection of its own'

        instance.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and opened & addresses():
            time.sleep(0.05)

    left_open = opened & addresses()
    assert not left_open, f'close() left the storage connection open: {left_open}'
