"""The admin: what it shows, what it refuses, and what it will not ask the database."""

import os
import subprocess
import sys
import textwrap

import pytest
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_redis_aiogram import admin as admin_module
from django_redis_aiogram.admin import (
    COUNT_LIMIT,
    MAX_STAGES,
    TelegramEventAdmin,
    register_event_log_admin,
)
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.models import TelegramEvent

ON = {'EVENT_LOG': True}
CHANGELIST = '/admin/django_redis_aiogram/telegramevent/'


def an_event(**kwargs):
    fields = {
        'kind': EventKind.OUTBOUND_SENT.value,
        'correlation_id': new_correlation_id(),
        'function': 'send_message',
        'chat_id': 42,
        'detail': {'text': 'hello'},
        'error': 'boom',
    }
    fields.update(kwargs)
    return TelegramEvent.objects.create(**fields)


def a_reader(username, *codenames):
    user = User.objects.create_user(username=username, password='x', is_staff=True)
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(codename=codename))
    return user


@pytest.fixture(autouse=True)
def _registered():
    register_event_log_admin()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_the_changelist_renders(client):
    an_event()
    client.force_login(a_reader('viewer', 'view_telegramevent'))

    assert client.get(CHANGELIST).status_code == 200


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_the_changelist_never_counts_past_the_cap(client):
    """Django's changelist runs COUNT(*) to build the page list. On a table
    sized by traffic that is a sequential scan on every page load, which is
    what show_full_result_count and the paginator are for.

    Every count it does run has to carry the cap — one row past it, so the
    page can tell "exactly ten thousand" from "more than we will count" — or it
    is back to scanning the table to tell someone a number they did not ask for.
    """
    an_event()
    client.force_login(a_reader('counter', 'view_telegramevent'))

    with CaptureQueriesContext(connection) as queries:
        client.get(CHANGELIST)

    counts = [query['sql'] for query in queries if 'COUNT(' in query['sql'].upper()]
    assert counts, 'the paginator stopped counting entirely, so the numbers are made up'
    assert all(f'LIMIT {COUNT_LIMIT + 1}' in sql for sql in counts), counts


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_count_that_stopped_at_the_cap_says_so(client, monkeypatch):
    """A page reporting exactly the cap reads as the whole answer.

    Silently, that is the defect the paginator exists to avoid, moved one step
    along: the number would be wrong and nothing would show it.

    The cap is lowered rather than ten thousand rows inserted — the behaviour
    under test is the comparison, not the number.
    """
    monkeypatch.setattr(admin_module, 'COUNT_LIMIT', 2)
    for _ in range(3):
        an_event()
    client.force_login(a_reader('capped', 'view_telegramevent'))

    body = client.get(CHANGELIST).content.decode()

    assert 'Narrow the filter' in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_count_inside_the_cap_says_nothing(client, monkeypatch):
    """The other half: a warning on every page is a warning nobody reads."""
    monkeypatch.setattr(admin_module, 'COUNT_LIMIT', 5)
    an_event()
    client.force_login(a_reader('uncapped', 'view_telegramevent'))

    body = client.get(CHANGELIST).content.decode()

    assert 'Narrow the filter' not in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_reader_without_the_payload_permission_sees_no_bodies(client):
    """The split that makes the two permissions worth having: support can see
    that a message went out without reading what it said."""
    event = an_event(detail={'text': 'a secret plan'}, error='a stack trace')
    client.force_login(a_reader('support', 'view_telegramevent'))

    body = client.get(f'{CHANGELIST}{event.pk}/change/').content.decode()

    assert 'a secret plan' not in body
    assert 'a stack trace' not in body

    # the error *code* is a class name, not payload, and the documented support
    # role is built on seeing it: it belongs in the list for this reader too
    listing = client.get(CHANGELIST).content.decode()
    assert 'error code' in listing.lower()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_reader_with_the_payload_permission_sees_them(client):
    event = an_event(detail={'text': 'a secret plan'}, error='a stack trace')
    client.force_login(a_reader('operator', 'view_telegramevent', 'view_telegramevent_payload'))

    body = client.get(f'{CHANGELIST}{event.pk}/change/').content.decode()

    assert 'a secret plan' in body
    assert 'a stack trace' in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_the_detail_is_escaped_not_marked_safe(client):
    """A detail holds whatever came off the wire, so mark_safe here would be
    stored XSS against everyone with admin access."""
    event = an_event(detail={'text': '<script>alert(1)</script>'})
    client.force_login(a_reader('escaper', 'view_telegramevent', 'view_telegramevent_payload'))

    body = client.get(f'{CHANGELIST}{event.pk}/change/').content.decode()

    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_adding_is_refused(client):
    client.force_login(a_reader('adder', 'view_telegramevent'))

    assert client.get(f'{CHANGELIST}add/').status_code == 403


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT={'EVENT_LOG': False})
def test_the_admin_is_hidden_while_the_log_is_off(client):
    an_event()
    client.force_login(a_reader('hidden', 'view_telegramevent'))

    assert client.get(CHANGELIST).status_code == 403


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_the_stages_of_one_message_are_shown_together(client):
    """The point of the correlation id, seen from the admin."""
    identifier = new_correlation_id()
    an_event(kind=EventKind.OUTBOUND_QUEUED.value, correlation_id=identifier)
    sent = an_event(kind=EventKind.OUTBOUND_SENT.value, correlation_id=identifier)
    an_event(kind=EventKind.OUTBOUND_SENT.value)  # a different message

    client.force_login(a_reader('stages', 'view_telegramevent'))
    body = client.get(f'{CHANGELIST}{sent.pk}/change/').content.decode()

    assert body.count(EventKind.OUTBOUND_QUEUED.value) >= 1
    # Django capitalises a field label when it renders it
    assert 'every stage of this message' in body.lower()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_search_the_columns_cannot_hold_is_refused_before_the_query(client):
    """Typed equality raises while the query is built — `ValidationError` for
    the uuid column, `ValueError` for the integer one, on every backend — so a
    term neither column can hold has to be answered before it is used.

    Asserted on the refusal itself rather than on a 200, which the changelist
    would return either way.
    """
    an_event()
    client.force_login(a_reader('searcher', 'view_telegramevent'))
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    narrowed, _ = admin_instance.get_search_results(None, TelegramEvent.objects.all(), 'hello')

    assert not narrowed.exists()
    assert narrowed.query.is_empty(), 'the term was handed to the database instead of refused'
    assert client.get(CHANGELIST, {'q': 'hello'}).status_code == 200


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_search_by_correlation_id_finds_the_row(client):
    identifier = new_correlation_id()
    an_event(correlation_id=identifier)
    an_event()
    client.force_login(a_reader('finder', 'view_telegramevent'))

    body = client.get(CHANGELIST, {'q': str(identifier)}).content.decode()

    assert '1 result' in body or str(identifier)[:8] in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_chain_longer_than_the_cap_says_it_was_cut(client):
    """A page that stops at exactly 200 rows without saying so reads as the
    whole history of the message, which is the wrong thing to believe about a
    message that retried thousands of times."""
    identifier = new_correlation_id()
    TelegramEvent.objects.bulk_create(
        TelegramEvent(kind=EventKind.OUTBOUND_RETRIED.value, correlation_id=identifier) for _ in range(MAX_STAGES + 2)
    )
    row = TelegramEvent.objects.filter(correlation_id=identifier).first()
    client.force_login(a_reader('long-chain', 'view_telegramevent'))

    body = client.get(f'{CHANGELIST}{row.pk}/change/').content.decode()

    assert f'only the first {MAX_STAGES} stages are shown' in body


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_search_asks_the_column_and_not_a_function_of_it(client):
    """The regression that made the search a sequential scan.

    Django builds `iexact` even from the `=` prefix, and that renders as
    `UPPER(correlation_id::text) = ...` on PostgreSQL and a `LIKE` on SQLite —
    either way something other than the column, so neither index applies. On a
    table sized by traffic that is the one query the page exists for, done the
    one way it must not be.
    """
    an_event()
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    by_id, _ = admin_instance.get_search_results(None, TelegramEvent.objects.all(), str(new_correlation_id()))
    by_chat, _ = admin_instance.get_search_results(None, TelegramEvent.objects.all(), '42')

    for sql in (str(by_id.query), str(by_chat.query)):
        where = sql.upper().split('WHERE', 1)[1]
        assert 'UPPER' not in where, sql
        assert '::TEXT' not in where, sql
        assert 'LIKE' not in where, sql


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_chat_id_too_large_for_the_column_is_refused(client):
    """int() accepts any number of digits and BIGINT does not; asking anyway is
    an error from the backend rather than an empty page."""
    an_event()
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    narrowed, _ = admin_instance.get_search_results(None, TelegramEvent.objects.all(), '9' * 40)

    assert narrowed.query.is_empty()


@pytest.mark.django_db
@override_settings(TELEGRAM_BOT=ON)
def test_a_search_by_chat_id_finds_the_rows(client):
    """The other half of what the help text promises, and the half a support
    reader actually types."""
    an_event(chat_id=42)
    an_event(chat_id=43)
    client.force_login(a_reader('by-chat', 'view_telegramevent'))
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    narrowed, _ = admin_instance.get_search_results(None, TelegramEvent.objects.all(), '42')

    assert [row.chat_id for row in narrowed] == [42]
    assert client.get(CHANGELIST, {'q': '42'}).status_code == 200


def test_the_admin_module_pulls_no_aiogram():
    """`admin.autodiscover` imports this module on every boot of any project
    with the admin installed, so what it imports is paid by processes that
    never talk to Telegram — including the migration container.

    A subprocess because the suite has aiogram loaded long before this runs.
    """
    script = textwrap.dedent("""
        import sys

        import django

        django.setup()

        from django.contrib import admin

        admin.autodiscover()

        assert 'django_redis_aiogram.admin' in sys.modules, 'the admin never loaded, so nothing was checked'
        assert 'aiogram' not in sys.modules, 'the admin pulled aiogram into a process that has no bot'

        from django_redis_aiogram.models import TelegramEvent

        # ready() is what registers it, and it runs during setup() — before the
        # autodiscover above. Without this the suite's own fixture registers the
        # model, and dropping the call from ready() would change nothing
        assert admin.site.is_registered(TelegramEvent), 'ready() did not register the admin'
        print('the admin stays cheap')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            'DJANGO_SETTINGS_MODULE': 'tests.db_settings',
            'DJANGO_REDIS_AIOGRAM_ENABLED': '0',
            'DJANGO_REDIS_AIOGRAM_EVENT_LOG': '1',
        },
    )

    assert result.returncode == 0, result.stderr
    assert 'the admin stays cheap' in result.stdout


@pytest.mark.django_db
def test_the_permissions_are_refusals_not_opinions():
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    assert admin_instance.has_add_permission(None) is False
    assert admin_instance.has_change_permission(None) is False
    assert admin_instance.has_delete_permission(None) is False
