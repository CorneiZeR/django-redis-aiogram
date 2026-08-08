"""The admin: what it shows, what it refuses, and what it will not ask the database."""

import pytest
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_redis_aiogram.admin import COUNT_LIMIT, TelegramEventAdmin, register_event_log_admin
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

    Every count it does run has to carry the cap, or the page is back to
    scanning the table to tell someone a number they did not ask for.
    """
    an_event()
    client.force_login(a_reader('counter', 'view_telegramevent'))

    with CaptureQueriesContext(connection) as queries:
        client.get(CHANGELIST)

    counts = [query['sql'] for query in queries if 'COUNT(' in query['sql'].upper()]
    assert counts, 'the paginator stopped counting entirely, so the numbers are made up'
    assert all(f'LIMIT {COUNT_LIMIT}' in sql for sql in counts), counts


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
    """Both searchable columns are typed, and PostgreSQL raises out of the
    changelist rather than matching nothing when a term cannot be cast.

    Asserted on the refusal itself rather than on a 200: SQLite matches nothing
    quietly, so a status code would pass here with the guard deleted and fail
    for a project running the backend the guard is for.
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


@pytest.mark.django_db
def test_the_permissions_are_refusals_not_opinions():
    admin_instance = TelegramEventAdmin(TelegramEvent, None)

    assert admin_instance.has_add_permission(None) is False
    assert admin_instance.has_change_permission(None) is False
    assert admin_instance.has_delete_permission(None) is False
