"""The only thing that bounds the table's growth."""

import datetime
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.management.commands import tgbot_prune_events as prune_command
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
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'EVENT_LOG_RETENTION_DAYS': 30})
def test_an_unknown_database_alias_is_refused_by_name():
    """`E041` guards the setting; `--database` goes around it.

    This is the one command that runs from cron, so the failure has to read as a
    configuration mistake and say what the alternatives are — a Django traceback about a
    missing connection is the least useful thing to be woken by. Asserted on the aliases
    too: a message that refuses without naming what exists leaves the operator guessing.
    """
    with pytest.raises(CommandError, match='no database is configured under the alias') as refused:
        call_command('tgbot_prune_events', database='nowhere')

    assert 'nowhere' in str(refused.value)
    assert 'default' in str(refused.value), 'the refusal has to name the aliases that do exist'


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
def test_the_pause_happens_between_chunks_and_not_after_the_last(monkeypatch):
    """`--sleep` is the valve for replica lag, and a valve nothing turns is a
    flag that lies. Patched rather than waited on, so this stays a test about
    the command and not about the clock."""
    slept = []
    monkeypatch.setattr(prune_command.time, 'sleep', slept.append)
    for _ in range(4):
        an_event(days_old=40)

    prune(days=30, chunk=1, sleep=0.25)

    assert slept == [0.25, 0.25, 0.25], slept


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
    below = an_event(days_old=40)  # the lowest id, and expired
    recent = an_event(days_old=1)  # a survivor in the middle of the range
    old = an_event(days_old=40)  # the watermark, above it

    prune(days=30, chunk=1000, sleep=0)

    assert TelegramEvent.objects.filter(pk=recent.pk).exists(), 'an id-only delete swept up a recent row'
    assert not TelegramEvent.objects.filter(pk=old.pk).exists()
    # old/recent/old, so the created_at predicate cannot go dead when the walk's
    # lower bound moves: with the survivor at the bottom, a range that started
    # above it would pass while deleting nothing it should not
    assert not TelegramEvent.objects.filter(pk=below.pk).exists()


@pytest.mark.django_db(databases=['default', 'logs'])
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'EVENT_LOG_DATABASE': 'logs'})
def test_it_prunes_the_configured_alias_and_leaves_the_other_alone():
    """Every other test in this file runs against one alias, so a handle() that
    pruned whatever `default` happens to be would pass them all."""
    on_logs = TelegramEvent.objects.using('logs').create(
        kind=EventKind.OUTBOUND_SENT.value, correlation_id=new_correlation_id()
    )
    on_default = TelegramEvent.objects.using('default').create(
        kind=EventKind.OUTBOUND_SENT.value, correlation_id=new_correlation_id()
    )
    stale = timezone.now() - datetime.timedelta(days=40)
    TelegramEvent.objects.using('logs').filter(pk=on_logs.pk).update(created_at=stale)
    TelegramEvent.objects.using('default').filter(pk=on_default.pk).update(created_at=stale)

    prune(days=30, sleep=0)

    assert not TelegramEvent.objects.using('logs').exists(), 'the configured alias was not pruned'
    assert TelegramEvent.objects.using('default').exists(), 'it pruned an alias it was not pointed at'


@pytest.mark.django_db(databases=['default', 'logs'])
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'EVENT_LOG_DATABASE': 'logs'})
def test_the_database_flag_wins_over_the_configured_alias():
    """The two put in conflict, which is the only arrangement that pins the
    precedence: with the setting unset, either order picks the same alias."""
    stale = timezone.now() - datetime.timedelta(days=40)
    for alias in ('logs', 'default'):
        row = TelegramEvent.objects.using(alias).create(
            kind=EventKind.OUTBOUND_SENT.value, correlation_id=new_correlation_id()
        )
        TelegramEvent.objects.using(alias).filter(pk=row.pk).update(created_at=stale)

    prune(days=30, sleep=0, database='default')

    assert not TelegramEvent.objects.using('default').exists(), 'the flag was ignored'
    assert TelegramEvent.objects.using('logs').exists(), 'it pruned the configured alias instead'


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_each_chunk_gets_its_own_transaction(monkeypatch):
    """Counting the DELETEs is not enough: moving atomic() outside the loop
    would leave the statement count identical and the lock held throughout."""
    for _ in range(5):
        an_event(days_old=40)

    entered = []
    original = prune_command.transaction.atomic

    def counting_atomic(*args, **kwargs):
        # Django's own Collector.delete() opens one too, with savepoint=False.
        # Counting both would report two per chunk and say nothing about ours
        if 'savepoint' not in kwargs:
            entered.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(prune_command.transaction, 'atomic', counting_atomic)
    prune(days=30, chunk=2, sleep=0)

    assert len(entered) == 3, f'expected one transaction per chunk, got {len(entered)}'
    assert TelegramEvent.objects.count() == 0


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_a_surviving_low_id_row_does_not_pin_the_walk():
    """`low` was the table's lowest id while the watermark was cutoff-filtered.

    One recent row down there and every run restarted from it, so a bounded
    `--max-chunks` run spent its whole budget crossing rows it could not delete
    and never reached the expired ones.
    """
    survivor = an_event(days_old=1)  # the lowest id, and not expired
    for _ in range(5):
        an_event(days_old=40)

    prune(days=30, chunk=1, sleep=0, max_chunks=2)

    assert TelegramEvent.objects.filter(pk=survivor.pk).exists()
    # exact, not "fewer than before": with chunk=1 the old walk spent its first
    # chunk on the survivor's own id and deleted nothing there, so it got through
    # one expired row where this gets through two
    assert TelegramEvent.objects.count() == 4, 'a chunk was spent on the surviving row'


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_a_dry_run_does_not_pace_itself(monkeypatch):
    """The pause is for replicas and autovacuum. A dry run deletes nothing, so
    there is nothing to pace, and a nightly `--dry-run` over a large table slept
    once per chunk for no reason."""
    slept = []
    monkeypatch.setattr('django_redis_aiogram.management.commands.tgbot_prune_events.time.sleep', slept.append)
    for _ in range(4):
        an_event(days_old=40)

    prune(days=30, chunk=1, sleep=0.1, dry_run=True)

    assert slept == []
