"""Which method names a queued payload is allowed to name.

A payload carries the name of the method to call, so without this list the queue
could reach anything public on ``Bot``: ``download_file`` writes to the
container's filesystem, ``token`` hands out the credential.

This lives apart from ``client`` so the delivery consumer can check a payload
before handing it anywhere, without importing the client.
"""

import re

from aiogram import Bot


def _api_methods() -> frozenset[str]:
    """Bot attributes that correspond to a Telegram API method."""
    import aiogram.methods

    api = {re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower() for name in aiogram.methods.__all__}
    return frozenset(api & {name for name in dir(Bot) if not name.startswith('_')})


API_METHODS = _api_methods()


def check_function(function: str) -> str:
    """Return ``function`` if it names a Telegram API method, else raise."""
    if function not in API_METHODS:
        raise ValueError(
            f'{function!r} is not a Telegram API method. Queued payloads may only '
            f'name one of the {len(API_METHODS)} methods aiogram exposes for the '
            f'Bot API; see the Serialization page.'
        )
    return function
