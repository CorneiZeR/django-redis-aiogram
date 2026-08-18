"""Answer whether the bot container is doing its job.

`docker ps` says the process is up, which is not the same thing: the consumer
thread can be dead while polling continues, or Redis can be unreachable, and the
container stays "healthy" either way.

A wrapper, since 3.1.0, over :mod:`django_redis_aiogram.healthcheck`. The decision
lives there so that ``python -m django_redis_aiogram.healthcheck`` can make it without
``django.setup()`` — a management command populates the app registry and runs every
``AppConfig.ready()`` in the host project first, which is 17.89s in one measured
consumer against 0.01s of actual probing, and more than any Docker ``timeout`` can
honestly allow. This command is unchanged for anyone who has it in a compose file
today, and **Deployment** says why the other form belongs in a healthcheck.
"""

from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand, CommandError

from django_redis_aiogram.healthcheck import add_limit_flags, check


class Command(BaseCommand):
    """Check Redis, the consumer's heartbeat and the queue length, in that order."""

    help = 'Exit 0 when the bot container is healthy, non-zero with a reason otherwise'

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the two limits, both of which default to a setting.

        Taken from the module that acts on them rather than restated here: a second copy
        of a flag is how one form ends up with a default the other does not have.
        """
        add_limit_flags(parser)

    def handle(self, *args: Any, **options: Any) -> None:
        """Report the first thing that is wrong, or that everything is fine.

        ``stranded`` and ``guarantee`` are asked for explicitly, because this command's
        output must not change for anyone who has it in a compose file — while the
        container-facing entry point leaves both off, since neither can alter the
        verdict and both are the expensive part of the probe.
        """
        report = check(
            max_queue=options['max_queue'],
            max_age=options['max_age'],
            stranded=True,
            guarantee=True,
        )
        if not report.ok:
            raise CommandError(report.message)
        # plain when nothing was examined: a disabled process is not a healthy bot, and
        # this command has never coloured that line green
        self.stdout.write(self.style.SUCCESS(report.message) if report.checked else report.message)
        for warning in report.warnings:
            self.stdout.write(self.style.WARNING(warning))
