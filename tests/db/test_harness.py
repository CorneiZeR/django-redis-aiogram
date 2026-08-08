"""The database suite's own wiring, asserted before anything relies on it.

Every test below fails if `--ds=tests.db_settings` is not in effect, which is
the point: the harness is what the rest of this directory is built on.
"""

import threading

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, connections


def test_the_database_suite_has_a_database():
    assert settings.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3'


@pytest.mark.django_db
def test_a_query_reaches_the_database():
    """Configured is not connected; this proves migrations ran and it answers."""
    assert get_user_model().objects.count() == 0


@pytest.mark.django_db
def test_the_package_owns_no_tables_yet():
    """Pins the state this suite starts from, so the first migration is visible
    as a change rather than arriving unnoticed."""
    owned = [name for name in connection.introspection.table_names() if name.startswith('django_redis_aiogram')]

    assert owned == [], owned


@pytest.mark.django_db
def test_the_admin_is_reachable(client):
    """Driven rather than described.

    Comparing INSTALLED_APPS and ROOT_URLCONF would still pass with the admin
    route deleted from tests/db_urls.py, which is the regression this exists to
    catch. Rendering the login page also proves the templates, the context
    processors and the session and message middleware are all wired up.
    """
    response = client.get('/admin/login/')

    assert response.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_a_background_thread_sees_the_same_test_database():
    """Anything writing from its own thread depends on this.

    Django names the sqlite test database `file:memorydb_default?mode=memory&
    cache=shared` rather than `:memory:`, which is the difference between a
    second connection seeing the same rows and seeing an empty database. A
    plain `:memory:` here would make a threaded writer silently untestable.
    """
    get_user_model().objects.create_user(username='probe')
    seen: list[object] = []

    def worker():
        try:
            seen.append(get_user_model().objects.filter(username='probe').count())
        except Exception as error:
            seen.append(repr(error))
        finally:
            connections.close_all()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert seen == [1], seen
