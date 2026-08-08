"""The only thing that bounds the table's growth."""

import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.models import TelegramEvent


def an_event(days_old=0, **kwargs):
    event = TelegramEvent.objects.create(
        kind=EventKind.OUTBOUND_SENT.value,
        correlation_id=new_correlation_id(),
        **kwargs,
    )
    # written after the insert, because created_at has a default rather than
    # being settable through the manager in a way that survives it
    TelegramEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - datetime.timedelta(days=days_old))
    return event


def prune(**options):
    out = StringIO()
    call_command('tgbot_prune_events', stdout=out, **options)
    return out.getvalue()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'EVENT_LOG_RETENTION_DAYS': 30})
def test_only_rows_past_the_window_go():
    old = an_event(days_old=40)
    recent = an_event(days_old=1)

    prune()

    remaining = set(TelegramEvent.objects.values_list('pk', flat=True))
    assert remaining == {recent.pk}, remaining
    assert not TelegramEvent.objects.filter(pk=old.pk).exists()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'EVENT_LOG_RETENTION_DAYS': 0})
def test_retention_unset_deletes_nothing():
    """0 means keep for ever, and W006 is what warns about it — a command that
    guessed a window instead would be a data-loss bug."""
    an_event(days_old=400)

    output = prune()

    assert TelegramEvent.objects.count() == 1
    assert 'Retention is not set' in output


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_dry_run_reports_without_deleting():
    an_event(days_old=40)

    output = prune(days=30, dry_run=True)

    assert TelegramEvent.objects.count() == 1
    assert 'would delete 1' in output


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_it_deletes_in_bounded_ranges_not_one_statement():
    """A single unbounded DELETE is the thing this exists to avoid: it holds a
    lock across the whole cold end of the table."""
    for _ in range(5):
        an_event(days_old=40)

    with CaptureQueriesContext(connection) as queries:
        prune(days=30, chunk=2, sleep=0)

    deletes = [q['sql'] for q in queries if q['sql'].strip().upper().startswith('DELETE')]
    assert len(deletes) >= 3, deletes
    # every one bounded on both sides, so none can reach the rows still arriving
    assert all('>=' in sql and '<=' in sql for sql in deletes), deletes
    assert TelegramEvent.objects.count() == 0


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_max_chunks_bounds_a_nightly_run():
    for _ in range(6):
        an_event(days_old=40)

    output = prune(days=30, chunk=2, sleep=0, max_chunks=1)

    assert TelegramEvent.objects.count() == 4
    assert 'Stopped after 1 chunks' in output


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_nothing_older_than_the_cutoff_is_said_plainly():
    an_event(days_old=1)

    output = prune(days=30)

    assert 'Nothing older than' in output
    assert TelegramEvent.objects.count() == 1


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_a_recent_row_inside_the_id_range_survives():
    """The id range is the access path, not the condition.

    Ids only approximate time order once several processes and a buffered
    writer are involved, so a recent row can sit below the watermark. Dropping
    created_at from the predicate would delete it.
    """
    recent = an_event(days_old=1)  # the lower id
    old = an_event(days_old=40)  # the watermark, above it

    prune(days=30, chunk=1000, sleep=0)

    assert TelegramEvent.objects.filter(pk=recent.pk).exists(), 'an id-only delete swept up a recent row'
    assert not TelegramEvent.objects.filter(pk=old.pk).exists()
