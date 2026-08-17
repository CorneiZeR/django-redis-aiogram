"""Receive updates over HTTP instead of polling for them.

Long polling needs a process that runs forever. A webhook does not: Telegram
posts each update to a URL, so the update arrives in whichever process serves
that URL — normally the web one.

The view is deliberately synchronous. An async view would run on the server's
own loop under ASGI but on a throwaway loop per request under WSGI, and the
bot's HTTP session binds to the first loop that uses it. Driving the bot's own
loop works the same under both.
"""

import hmac
import json
import logging
from typing import Any

from aiogram.types import Update
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError

from django_redis_aiogram import bot
from django_redis_aiogram.enums import UpdateMode, choices
from django_redis_aiogram.exceptions import LoopUnavailableError
from django_redis_aiogram.settings import SETTINGS_NAME, conf

logger = logging.getLogger('django_redis_aiogram')

#: what Telegram sends the configured secret back in
SECRET_HEADER = 'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN'  # noqa: S105 - a header name, not the secret it carries
#: plain strings, so argparse choices and messages read as the settings do
MODES = choices(UpdateMode)


def current_mode() -> str:
    """Which of the two ways of receiving updates this deployment uses."""
    mode = str(conf['MODE'] or '').strip().lower()
    if mode not in MODES:
        msg = f"{SETTINGS_NAME}['MODE'] must be one of {sorted(MODES)}, got {mode!r}."
        raise ImproperlyConfigured(msg)
    return mode


def webhook_secret() -> str:
    """Return the shared secret, which the view refuses to run without."""
    secret = str(conf['WEBHOOK_SECRET'] or '').strip()
    if not secret:
        msg = (
            f"{SETTINGS_NAME}['WEBHOOK_SECRET'] is required to serve the webhook: without it "
            'anyone who finds the URL can feed your bot updates.'
        )
        raise ImproperlyConfigured(msg)
    return secret


@csrf_exempt
def telegram_webhook(request: HttpRequest) -> HttpResponse:  # noqa: PLR0911 - a guard-clause chain is the readable shape
    """Feed one update to the dispatcher.

    Answers 200 for anything Telegram should not retry, including a handler that
    raised — a non-2xx makes Telegram redeliver the same update, and a handler
    that fails once will fail again. A *refusal* is the other case: when nothing
    ran at all, because this process is shutting down, redelivery is exactly
    what should happen, so that answers 503.
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    if not bot.enabled:
        logger.warning('webhook received an update while the bot is disabled')
        return HttpResponse(status=503)

    if current_mode() != UpdateMode.WEBHOOK:
        # serving updates here while a worker polls for them would mean two
        # sources of updates and no way to tell which handled what
        logger.warning(
            'webhook received an update while this deployment polls',
            extra={'tg_mode': current_mode()},
        )
        return HttpResponse(status=503)

    given = request.META.get(SECRET_HEADER, '')
    if not hmac.compare_digest(given, webhook_secret()):
        logger.warning('webhook rejected an update with a wrong secret')
        return HttpResponse(status=403)

    try:
        telegram = bot.bot
    except ImproperlyConfigured:
        # a missing token is our problem, not a bad request
        logger.exception('webhook cannot build the bot')
        return HttpResponse(status=503)

    try:
        payload = json.loads(request.body)
        update = Update.model_validate(payload, context={'bot': telegram})
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as error:
        # the body is whoever posted it, so the type is all that goes in the log:
        # a traceback here would spread unvalidated input through the handlers
        logger.warning(
            'webhook could not read an update',
            extra={'tg_error': type(error).__name__},
        )
        return HttpResponse(status=400)

    try:
        bot.feed_update(update)
    except LoopUnavailableError:
        # nothing ran, so this update is still Telegram's to redeliver — which a
        # 2xx would tell it not to. The shutdown window is not a handler that
        # failed, and the two must not answer the same way
        logger.warning('webhook refused an update', extra={'tg_update': update.update_id})
        return HttpResponse(status=503)
    except Exception:
        logger.exception('webhook handler failed', extra={'tg_update': update.update_id})

    return HttpResponse(status=200)


def webhook_settings() -> dict[str, Any]:
    """Everything `setWebhook` needs, resolved from settings."""
    url = str(conf['WEBHOOK_URL'] or '').strip()
    if not url:
        msg = f"{SETTINGS_NAME}['WEBHOOK_URL'] is required to register a webhook."
        raise ImproperlyConfigured(msg)
    allowed = conf['WEBHOOK_ALLOWED_UPDATES']
    return {
        'url': url,
        'secret_token': webhook_secret(),
        'allowed_updates': list(allowed) if allowed else None,
        'drop_pending_updates': False,
    }
