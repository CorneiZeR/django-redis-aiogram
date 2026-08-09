"""A read-only view of the event feed, sized for a table nobody wants to count.

Nothing here reads a setting and nothing registers itself: ``admin.autodiscover``
imports this module while the app registry is still loading, and reading
settings at import time is the defect 2.0 existed to remove. Registration
happens in :meth:`~django_redis_aiogram.apps.TelegramBotAppConfig.ready`, where
settings are already safe to read.
"""

import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from django.contrib import admin, messages
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.functional import cached_property
from django.utils.html import format_html, format_html_join

from django_redis_aiogram.eventlog import log_alias
from django_redis_aiogram.events import failure_kinds, kind_choices
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

if TYPE_CHECKING:
    # django-stubs parameterises these; at runtime neither is subscriptable
    ModelAdminBase = admin.ModelAdmin[TelegramEvent]
    AnyModelAdmin = admin.ModelAdmin[Any]
else:
    ModelAdminBase = admin.ModelAdmin
    AnyModelAdmin = admin.ModelAdmin

#: how many stages of one message the detail page will render
MAX_STAGES = 200
#: rows the changelist will count before it stops asking
COUNT_LIMIT = 10_000


def log_is_on() -> bool:
    """Report whether the feed is recorded, read per request so a test can override it."""
    try:
        return coerce_bool(conf['EVENT_LOG'], f"{SETTINGS_NAME}['EVENT_LOG']")
    except ImproperlyConfigured:
        # a misconfigured flag is E031's finding; the admin's answer is to hide
        return False


def may_see_payloads(request: HttpRequest) -> bool:
    """Whether this user may read message bodies and exception text.

    Split from plain view access on purpose: support needs to see that a message
    went out and when, without reading what it said.
    """
    checker = getattr(request.user, 'has_perm', None)
    return bool(checker and checker('django_redis_aiogram.view_telegramevent_payload'))


class KindFilter(admin.SimpleListFilter):
    """Filters by kind, from the registry rather than from the table.

    A plain ``list_filter`` on the column would build its dropdown with
    ``SELECT DISTINCT``, which is a full scan every time the changelist loads.
    """

    title = 'kind'
    parameter_name = 'kind'

    def lookups(self, _request: HttpRequest, _model_admin: AnyModelAdmin) -> list[tuple[str, str]]:
        """Every registered kind, in the order they were registered."""
        return kind_choices()

    def queryset(self, _request: HttpRequest, queryset: QuerySet[TelegramEvent]) -> QuerySet[TelegramEvent]:
        """Narrow to the chosen kind, which leads the index it uses."""
        value = self.value()
        return queryset.filter(kind=value) if value else queryset


class OutcomeFilter(admin.SimpleListFilter):
    """Everything that went wrong, as one question."""

    title = 'outcome'
    parameter_name = 'outcome'

    def lookups(self, _request: HttpRequest, _model_admin: AnyModelAdmin) -> list[tuple[str, str]]:
        """Two answers: the failure kinds, or everything else."""
        return [('failed', 'Something went wrong'), ('ok', 'Went fine')]

    def queryset(self, _request: HttpRequest, queryset: QuerySet[TelegramEvent]) -> QuerySet[TelegramEvent]:
        """Narrow to the outcome chosen, as an IN list on the kind index."""
        failures = failure_kinds()
        if self.value() == 'failed':
            return queryset.filter(kind__in=failures)
        if self.value() == 'ok':
            return queryset.exclude(kind__in=failures)
        return queryset


class BoundedPaginator(Paginator):  # type: ignore[type-arg]
    """Counts, but never past ``COUNT_LIMIT`` rows.

    Django's changelist runs ``COUNT(*)`` over the filtered queryset to build
    the page list. On a table sized by traffic that is a sequential scan on
    every page load, and the number it produces is stale by the time it renders.

    Counting inside a ``LIMIT`` keeps the answer honest for the filtered views
    people actually read, and turns the unfiltered one into a bounded index
    scan rather than the whole table. Past the limit the count stops growing,
    so the deepest pages are unreachable — by then the answer is a filter, not
    another page.
    """

    #: whether the count stopped at the cap, so the page can say it did
    truncated = False

    @cached_property
    def count(self) -> int:
        """Count what fits inside the cap, in one query the index can serve."""
        # the changelist always paginates a queryset; the base class is typed
        # for anything sliceable, which has no count()
        rows = cast('QuerySet[TelegramEvent]', self.object_list)
        # one row past the cap, so the difference between "exactly ten thousand"
        # and "more than we will count" is knowable rather than assumed
        found = int(rows[: COUNT_LIMIT + 1].count())
        self.truncated = found > COUNT_LIMIT
        return min(found, COUNT_LIMIT)


class TelegramEventAdmin(ModelAdminBase):
    """Read-only, and deliberately narrow about what it will ask the database."""

    list_display = ('created_at', 'kind', 'function', 'chat_id', 'thread', 'worker', 'error_code')
    list_filter = (KindFilter, OutcomeFilter)
    # what makes the box appear; the lookup itself is get_search_results below
    search_fields = ('correlation_id', 'chat_id')
    search_help_text = 'An exact correlation id, or an exact chat id.'
    show_full_result_count = False
    paginator = BoundedPaginator
    list_per_page = 50
    # nothing to join: the model holds no foreign key, which is what keeps an
    # insert from becoming a constraint check
    list_select_related = False
    ordering = ('-id',)
    # no date_hierarchy: its drilldown truncates created_at for every row, which
    # is a full scan no index can serve

    def get_queryset(self, request: HttpRequest) -> QuerySet[TelegramEvent]:
        """Read from the alias the writer writes to, router installed or not."""
        return super().get_queryset(request).using(log_alias())

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:  # noqa: ANN401 - Django types this as a bare response
        """Render the list, saying so when the count stopped at the cap.

        A page that reports exactly ten thousand results reads as the whole
        answer. Silently, it would be the same defect the paginator exists to
        avoid, moved one step along.
        """
        response = super().changelist_view(request, extra_context)
        changelist = getattr(response, 'context_data', {}).get('cl')
        paginator = getattr(changelist, 'paginator', None)
        if paginator is not None and paginator.count and getattr(paginator, 'truncated', False):
            self.message_user(
                request,
                f'More than {COUNT_LIMIT:,} events match. Narrow the filter or search for an '
                f'exact id; counting further would scan the table.',
                messages.WARNING,
            )
        return response

    def get_fields(self, request: HttpRequest, _obj: TelegramEvent | None = None) -> list[Any]:
        """Hide the two columns that can hold a message body or a stack trace."""
        fields = [
            'created_at',
            'correlation_id',
            'kind',
            'function',
            'chat_id',
            'user_id',
            'message_id',
            'update_id',
            'worker',
            'attempt',
            'duration_ms',
            'error_code',
            'stages',
        ]
        if may_see_payloads(request):
            fields[-1:-1] = ['pretty_detail', 'error']
        return fields

    def get_readonly_fields(self, request: HttpRequest, obj: TelegramEvent | None = None) -> list[Any]:
        """Everything: the feed records what happened, and that is not editable."""
        return self.get_fields(request, obj)

    def get_search_results(
        self,
        _request: HttpRequest,
        queryset: QuerySet[TelegramEvent],
        search_term: str,
    ) -> tuple[QuerySet[TelegramEvent], bool]:
        """Match the two typed columns exactly, each on its own index.

        Django's own search cannot: even the `=` prefix builds `iexact`, which
        renders as `UPPER(correlation_id::text) = ...` — a function on the
        column, so no index applies and the search becomes a sequential scan of
        a table sized by traffic. Typed equality is what the indexes are for.

        The cost of typed equality is that a term the column cannot hold raises
        while the query is built, which is why anything neither column can hold
        is answered with nothing rather than handed to the database.
        """
        term = search_term.strip()
        if not term:
            return queryset, False
        if term.lstrip('-').isdigit():
            number = int(term)
            # a chat_id is a BIGINT; a longer number is not one, and asking
            # would be an error from the backend rather than an empty page
            if -(2**63) <= number < 2**63:
                return queryset.filter(chat_id=number), False
            return queryset.none(), False
        try:
            identifier = uuid.UUID(term)
        except ValueError:
            return queryset.none(), False
        return queryset.filter(correlation_id=identifier), False

    @admin.display(description='detail')
    def pretty_detail(self, obj: TelegramEvent) -> str:
        """Render the JSON readably, and escaped.

        format_html escapes; mark_safe here would be stored XSS, because a
        detail holds whatever came off the wire.
        """
        return format_html('<pre>{}</pre>', json.dumps(obj.detail, indent=2, ensure_ascii=False, default=str))

    @admin.display(description='every stage of this message')
    def stages(self, obj: TelegramEvent) -> str:
        """Render the whole correlated chain, in order, from one indexed query.

        Bounded on purpose: a message that retried ten thousand times is a bug,
        and rendering all of it would make this page a second one. One row more
        than the cap is read so the page can say it stopped rather than end at
        a number that looks like the whole story.
        """
        rows = list(
            TelegramEvent.objects.using(log_alias())
            .filter(correlation_id=obj.correlation_id)
            .order_by('id')
            .values_list('created_at', 'kind', 'worker')[: MAX_STAGES + 1]
        )
        body = format_html_join('', '<tr><td>{}</td><td>{}</td><td>{}</td></tr>', rows[:MAX_STAGES])
        if len(rows) > MAX_STAGES:
            body += format_html(
                '<tr><td colspan="3">and more — only the first {} stages are shown</td></tr>',
                MAX_STAGES,
            )
        return format_html('<table>{}</table>', body)

    @admin.display(description='thread', ordering='correlation_id')
    def thread(self, obj: TelegramEvent) -> str:
        """Link a row to the rest of its message, through the exact search."""
        return format_html('<a href="?q={}">{}</a>', obj.correlation_id, str(obj.correlation_id)[:8])

    def has_add_permission(self, _request: HttpRequest) -> bool:
        """Refuse: the feed is append-only, and only this package appends."""
        return False

    def has_change_permission(self, _request: HttpRequest, _obj: TelegramEvent | None = None) -> bool:
        """Refuse: a record of what happened is not something to edit."""
        return False

    def has_delete_permission(self, _request: HttpRequest, _obj: TelegramEvent | None = None) -> bool:
        """Refuse: a table this size is pruned in ranges, not a row at a time.

        `manage.py tgbot_prune_events` is what does it.
        """
        return False

    def has_view_permission(self, request: HttpRequest, obj: TelegramEvent | None = None) -> bool:
        """Read the flag per request, so override_settings works in a test."""
        return log_is_on() and bool(super().has_view_permission(request, obj))

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Keep the app off the admin index entirely while the log is off."""
        return log_is_on() and bool(super().has_module_permission(request))


def register_event_log_admin(site: admin.AdminSite | None = None) -> None:
    """Register the read-only admin. Called from ready(), behind the flag."""
    target = site or admin.site
    if not target.is_registered(TelegramEvent):
        target.register(TelegramEvent, TelegramEventAdmin)
