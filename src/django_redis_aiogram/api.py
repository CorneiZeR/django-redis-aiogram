"""Which method names a queued payload is allowed to name.

A payload carries the name of the method to call, so without this list the queue
could reach anything public on ``Bot``: ``download_file`` writes to the
container's filesystem, ``token`` hands out the credential.

This lives apart from ``client`` so the delivery consumer can check a payload
before handing it anywhere, without importing the client.
"""

import re

import aiogram.methods
from aiogram import Bot

from django_redis_aiogram.exceptions import UnknownApiMethodError

#: API methods a queued payload must never reach. They are administrative, not
#: sends: set_webhook would point updates at someone else's URL, and log_out or
#: close ends the session for the whole deployment.
DENIED_METHODS = frozenset({'set_webhook', 'delete_webhook', 'log_out', 'close'})


def _api_methods() -> frozenset[str]:
    """Return the Bot attributes that correspond to a Telegram API method."""
    api = {re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower() for name in aiogram.methods.__all__}
    public = {name for name in dir(Bot) if not name.startswith('_')}
    return frozenset(api & public) - DENIED_METHODS


API_METHODS = _api_methods()


def check_function(function: str) -> str:
    """Return ``function`` if it names a Telegram API method, else raise."""
    if function not in API_METHODS:
        raise UnknownApiMethodError(function, len(API_METHODS))
    return function
