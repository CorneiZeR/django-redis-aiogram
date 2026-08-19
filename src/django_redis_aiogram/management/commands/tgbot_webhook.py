"""Register, inspect or remove the Telegram webhook.

Telegram remembers the URL, not your settings file, so switching between polling
and webhooks means telling Telegram. `getUpdates` refuses to run while a webhook
is registered, which is why `delete` exists.
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand, CommandError

from django_redis_aiogram import bot
from django_redis_aiogram.enums import UpdateMode
from django_redis_aiogram.webhook import current_mode, webhook_settings


class Command(BaseCommand):
    """Tell Telegram where to post updates, or that it should stop."""

    help = 'Set, delete or show the Telegram webhook'

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the action and --drop-pending."""
        parser.add_argument('action', choices=['set', 'delete', 'info'])
        parser.add_argument(
            '--drop-pending',
            action='store_true',
            help='discard the updates Telegram queued while no webhook was registered',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the chosen action, and close the bot whichever way it ends."""
        if not bot.enabled:
            msg = (
                "the bot is disabled here (TELEGRAM_BOT['ENABLED'] or "
                'DJANGO_REDIS_AIOGRAM_ENABLED); nothing was changed'
            )
            raise CommandError(msg)

        action = options['action']
        try:
            getattr(self, f'_{action}')(options)
        finally:
            bot.close()

    def _set(self, options: dict[str, Any]) -> None:
        """Register the webhook Telegram should deliver to, warning if MODE disagrees.

        Setting one is what makes ``getUpdates`` start refusing, so a project still
        configured for polling is told plainly rather than left to read that from
        Telegram's error later.
        """
        if current_mode() != UpdateMode.WEBHOOK:
            self.stdout.write(
                self.style.WARNING(
                    f"TELEGRAM_BOT['MODE'] is '{current_mode()}': registering this webhook "
                    'stops getUpdates from working, so polling will fail until it is deleted'
                )
            )
        arguments = webhook_settings()
        arguments['drop_pending_updates'] = options['drop_pending']
        bot.loop.run_until_complete(bot.bot.set_webhook(**arguments))
        self.stdout.write(self.style.SUCCESS(f'webhook set to {arguments["url"]}'))
        self.stdout.write('polling will refuse to start until this is deleted')

    def _delete(self, options: dict[str, Any]) -> None:
        """Unregister the webhook, which is what polling needs before it can start.

        ``drop_pending_updates`` decides whether Telegram forgets what it queued while
        the webhook was down; the caller chooses, because that is a decision about
        duplicate work rather than about the webhook.
        """
        bot.loop.run_until_complete(bot.bot.delete_webhook(drop_pending_updates=options['drop_pending']))
        self.stdout.write(self.style.SUCCESS('webhook deleted; polling can start again'))

    def _info(self, _options: dict[str, Any]) -> None:
        """Report what Telegram thinks the webhook is, which is the only authority on it.

        ``last_error_message`` is the field worth the command: a webhook can be
        registered and rejected on every delivery, and nothing local shows that.
        """
        info = bot.loop.run_until_complete(bot.bot.get_webhook_info())
        if not info.url:
            self.stdout.write('no webhook registered; this bot is polled')
            return
        self.stdout.write(f'url: {info.url}')
        self.stdout.write(f'pending updates: {info.pending_update_count}')
        if info.last_error_message:
            self.stdout.write(self.style.WARNING(f'last error: {info.last_error_message} (at {info.last_error_date})'))
