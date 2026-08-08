"""The table, the migration and the permission surface it creates."""

import io
import time

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connection

from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.models import TelegramEvent


@pytest.mark.django_db
def test_the_migration_creates_the_table():
    assert 'django_redis_aiogram_event' in connection.introspection.table_names()


@pytest.mark.django_db
def test_the_migrations_match_the_models():
    """A field added without a migration is a table nobody's `migrate` creates.

    `--check` exits non-zero only when something is missing, so drift is the
    SystemExit and agreement is the quiet return.
    """
    out = io.StringIO()
    try:
        call_command('makemigrations', 'django_redis_aiogram', check=True, dry_run=True, stdout=out)
    except SystemExit:
        pytest.fail(f'the models have drifted from the migrations:\n{out.getvalue()}')


def test_the_model_holds_no_relations():
    """A foreign key here would break Django's fast-delete path, and every prune
    would have to fetch primary keys first."""
    relations = [field.name for field in TelegramEvent._meta.get_fields() if field.is_relation]

    assert relations == [], relations


def test_the_model_declares_no_constraints():
    """CheckConstraint is silently skipped on older MySQL and partial unique
    constraints are PostgreSQL-only, so an IntegrityError from a *log* write
    would be both unportable and untraceable."""
    assert TelegramEvent._meta.constraints == []


def test_the_index_names_fit_oracle():
    """Django raises models.E034 above 30 characters, but only when a backend is
    passed to the model checks — which a plain `manage.py check` does not do."""
    too_long = [index.name for index in TelegramEvent._meta.indexes if len(index.name) > 30]

    assert too_long == [], too_long


@pytest.mark.django_db
def test_the_permissions_are_the_stock_four_plus_one():
    """Only one custom permission, and only because Django has no equivalent:
    there are no field-level permissions, so 'saw that it went out' and 'read
    what it said' cannot otherwise be granted separately."""
    codenames = set(
        Permission.objects.filter(content_type__app_label='django_redis_aiogram').values_list('codename', flat=True)
    )

    assert codenames == {
        'add_telegramevent',
        'change_telegramevent',
        'delete_telegramevent',
        'view_telegramevent',
        'view_telegramevent_payload',
    }, codenames


@pytest.mark.django_db
def test_a_row_round_trips():
    identifier = new_correlation_id()
    TelegramEvent.objects.create(
        kind='outbound.sent',
        correlation_id=identifier,
        function='send_message',
        chat_id=42,
        detail={'attempts': 1},
    )

    stored = TelegramEvent.objects.get(correlation_id=identifier)
    assert stored.detail == {'attempts': 1}
    assert stored.chat_id == 42
    assert str(stored) == 'outbound.sent send_message'


def test_the_correlation_id_leads_with_the_time():
    """What keeps the correlation index appending rather than scattering.

    Ordering is to the millisecond, and no further: plain version 7 leaves the
    rest random, and only Python 3.14's own uuid7 adds a counter for ties. That
    is enough — inserts inside one millisecond land on the same page anyway.
    uuid4 would pass every other test in this file and fail this one.
    """
    identifier = new_correlation_id()

    assert identifier.version == 7
    milliseconds = int.from_bytes(identifier.bytes[:6], 'big')
    assert abs(milliseconds - time.time() * 1000) < 5000, 'the leading 48 bits are not a timestamp'


def test_correlation_ids_sort_across_milliseconds():
    earlier = new_correlation_id()
    time.sleep(0.005)
    later = new_correlation_id()

    assert earlier < later, 'the correlation id is not time-ordered'
