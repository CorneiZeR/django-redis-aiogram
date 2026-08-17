# Event log

An optional table recording what the bot did: a message queued, delivered,
retried or dropped, an update received, an FSM transition, a payload refused.
It exists to answer the question the structured log cannot once it has rotated —
*this user says they never got the message; did we send it?*

**It is not a replacement for [[Logging]].** Above roughly ten thousand events a
second a database is the wrong tool and a log shipper is the right one. Below
that, a table you can query and join against your own models is worth the write.

```python
TELEGRAM_BOT = {
    'EVENT_LOG': True,
}
```

Then `python manage.py migrate`. The table is created by `migrate` whether or
not the flag is on; nothing reads or writes it until you turn it on.

## One row is one thing that happened

| Column | What it holds |
| ------ | ------------- |
| `created_at` | when the event happened, stamped by whoever recorded it |
| `correlation_id` | ties the stages of one message together |
| `kind` | which of the kinds below |
| `function` | the aiogram method, when there is one |
| `chat_id`, `user_id`, `message_id`, `update_id` | the identifiers Telegram issued |
| `worker` | which container recorded it |
| `attempt`, `duration_ms` | how many tries, and how long |
| `error_code`, `error` | why it failed |
| `detail` | everything kind-specific, as JSON |

Rows are **inserted and never updated**. That is what lets two processes write
the same message's history with no coordination: the web process writes
`outbound.queued`, the bot container writes `outbound.sent`, and they line up
through `correlation_id` without either holding a foreign key.

It also means enabling the log in the bot container but not in the web processes
gives you `sent` rows with no `queued` rows to match. That is not a bug.

## The kinds

| Kind | When |
| ---- | ---- |
| `outbound.queued` | a send was written to the Redis list |
| `outbound.consumed` | the bot container took it off the list |
| `outbound.sent` | Telegram accepted it |
| `outbound.retried` | Telegram refused it with a rate limit; backing off |
| `outbound.failed` | the call raised |
| `outbound.dropped` | retries were exhausted, or shutdown cancelled it |
| `inbound.received` | an update arrived, by polling or webhook |
| `inbound.handled` | the handlers finished |
| `inbound.failed` | a handler raised |
| `fsm.transition` | a chat's state changed |
| `queue.undecodable` | a payload could not be decoded |
| `queue.rejected` | a payload named something that is not a Telegram API method |
| `log.dropped` | the writer fell behind and lost events — the gap, recorded |

Register your own:

```python
from django_redis_aiogram.events import register_kind

ORDER_NOTIFIED = register_kind('shop.order.notified', 'Order notified')
```

Adding a kind is **not** a schema change — the column is an unconstrained
`CharField` and the registry lives in Python. Register at module scope in
something your `tg_router` imports, or the kind will be missing from that
container's admin filter. Namespace as `<app>.<noun>.<verb>` and keep the total
in the tens: `kind` leads an index, and that index is only worth having while
its cardinality stays low.

## Nothing waits for the database

Recording hands the event to a bounded in-memory queue; one background thread
drains it in batches. A send never waits on the database, and a database that is
slow or down costs dropped rows, never dropped messages.

When the queue is full the event is dropped, counted, and reported at most once
a minute as `the event log is falling behind`. When the writer catches up it
records a `log.dropped` row, so the gap is visible in the data and not only in
the log.

`EVENT_LOG_BUFFER_SIZE`, `EVENT_LOG_BATCH_SIZE` and `EVENT_LOG_FLUSH_INTERVAL`
size it. A batch larger than the buffer can never fill, so `W007` says so.

What is lost: on `SIGKILL`, a worker timeout or `os._exit()`, whatever is in the
queue and in the current batch. At the defaults that is under a second of events
plus up to 200 rows. A clean `SIGTERM` loses nothing. This is an event feed, not
a ledger — if you need durability across a kill, the thing that already gives it
to you is the Redis queue.

## Message bodies are not stored by default

`EVENT_LOG_PAYLOAD` is `'summary'`: argument names and text lengths, not the
text. Set it to `'full'` to store bodies, and treat that as the personal-data
decision it is. `'none'` stores no payload at all.

Credentials are stripped from `detail` and `error` either way. That matters more
than it sounds: the bot token is in the API URL, aiogram puts the URL in its
exception messages, and those messages are what an `error` column holds.

## The admin, and who may see what

With the flag on and `django.contrib.admin` installed, the feed appears in the
admin as a read-only list. Add, change and delete are refused outright — rows
leave through `tgbot_prune_events`, not one at a time.

Two permissions decide what a reader sees, and each gates something real:

Django's four stock permissions are created as usual. One is added, because
Django has no equivalent for it:

| Permission | Grants |
| ---------- | ------ |
| `view_telegramevent` | the list and the detail page: when, which kind, which chat, which method, the error code |
| `view_telegramevent_payload` | the `detail` and `error` columns — message bodies under `EVENT_LOG_PAYLOAD: 'full'`, and exception text |

That split is the point: support needs to see that a message went out and when,
without reading what it said. There are no field-level permissions in Django, so
there is no way to express it with the stock four.

`add`, `change` and `delete` exist but the admin refuses all three, since the
feed is append-only and rows leave through `tgbot_prune_events`.

```python
from django.contrib.auth.models import Group, Permission

support = Group.objects.create(name='Telegram support')
support.permissions.add(Permission.objects.get(codename='view_telegramevent'))

operators = Group.objects.create(name='Telegram operators')
operators.permissions.set(
    Permission.objects.filter(
        content_type__app_label='django_redis_aiogram',
        codename__in=['view_telegramevent', 'view_telegramevent_payload'],
    )
)
```

Permissions live wherever `django.contrib.auth` does. If the log is on another
alias, leave `auth` where it is — the router in this package moves only its own
app, and dragging `auth` along would move your users with it.

What the admin deliberately does not do, because the table is sized by traffic:
no full result count, no date drilldown (its truncation is a scan no index can
serve), and no substring search — the two searchable columns are matched
exactly, so both use their index.

Paging counts at most **10 000 rows**, inside a `LIMIT`. The number is exact for
the filtered views people actually read and stops growing past the cap, so the
deepest pages are unreachable; at that depth the answer is a filter, not another
page. Django would otherwise run `COUNT(*)` over the whole filtered queryset on
every page load.

A `LIMIT` only stops early if the rows arrive already ordered, which is why the
kind index is on `(kind, -id)` — the same `-id` the changelist orders by. Before
3.1.0 it was `(kind, -created_at)`, so every filtered page sorted in a temporary
b-tree first and the bound above was not true. The list also leaves `error` and
`detail` in the table: it renders neither, and between them they are most of what
a row weighs. The detail page asks for them back.

## Growth, and the job that bounds it

Budget roughly **0.3 kB per event** including indexes. A million events a day is
about 0.3 GB a day, so thirty days of retention is around 9 GB. Storing message
bodies pushes the per-event figure up, so measure rather than trust it once
`EVENT_LOG_PAYLOAD` is `'full'`.

Nothing on the write path deletes anything. Set `EVENT_LOG_RETENTION_DAYS` and
schedule the command; `W006` warns while it is unset, because the feature is not
finished without it:

```shell
python manage.py tgbot_prune_events
```

It deletes by primary-key range in bounded chunks, one transaction each, so it
never holds a long lock and never competes with the inserts arriving at the
other end of the table. `--sleep` paces it for replicas, `--max-chunks` bounds a
nightly run, `--dry-run` reports without deleting.

On PostgreSQL the space returns via autovacuum rather than immediately; after a
large first prune, a plain `VACUUM` (never `FULL`, which takes an exclusive
lock) is the follow-up.

**Do not put a `ForeignKey` on `TelegramEvent`.** It breaks Django's fast-delete
path, and every prune then has to fetch primary keys first.

## A separate database

```python
TELEGRAM_BOT = {'EVENT_LOG_DATABASE': 'logs'}
DATABASE_ROUTERS = ['django_redis_aiogram.dbrouter.TelegramEventLogRouter', ...]
```

The writer and the admin name the alias explicitly, so the log lands in the
right database even with no router installed. The router is what makes `migrate`
create the table there. Put it **first** in `DATABASE_ROUTERS` — Django takes
the first non-`None` answer — and run `migrate --database=logs`.

`W005` fires when the log is on and its database has no engine, and `E041` when
the alias is not in `DATABASES` at all. Both matter because the writer runs on a
thread nobody is watching: without them the failure is a log line in a container
nobody reads.

## Partitioning, and what this package will not do

The package will not shard or partition the table. What it does is stay
partitionable, which is a real property and not a consolation: no foreign keys
in or out, no constraints, no unique index other than the primary key, and only
inserts, selects and range deletes.

That is exactly the set of properties an operator needs to take the table over
out of band. The sketch below is for an **empty** table — do it right after
`migrate`, before the log is switched on:

```sql
ALTER TABLE django_redis_aiogram_event RENAME TO django_redis_aiogram_event_old;

CREATE TABLE django_redis_aiogram_event (
    LIKE django_redis_aiogram_event_old INCLUDING DEFAULTS INCLUDING IDENTITY
) PARTITION BY RANGE (created_at);            -- no primary key on the parent

CREATE TABLE django_redis_aiogram_event_2026_08
    PARTITION OF django_redis_aiogram_event FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE INDEX ON django_redis_aiogram_event_2026_08 (id);   -- per partition, per index

DROP TABLE django_redis_aiogram_event_old;
```

`INCLUDING IDENTITY` is not decoration: Django creates the primary key as
`GENERATED BY DEFAULT AS IDENTITY` rather than a `serial` with a default, and
`INCLUDING DEFAULTS` alone copies nothing for it — every insert that omits `id`
then fails on the not-null constraint.

Three things that sketch does **not** do, and that a table with rows in it
needs: `LIKE` copies neither the indexes nor the primary key, so each partition
needs its own; a partition has to exist for every date
that will be written, past and future, or the insert fails outright; and the
existing rows have to be moved with an `INSERT ... SELECT` from the old table
before it is dropped. Work all three out for your data before running anything.

Retention then becomes `DROP TABLE` on a whole partition — instant, no dead
tuples. All of this is **unsupported**: `migrate` must not touch this app on
that database afterwards. Django's migrations have no representation for
`PARTITION BY`, and both PostgreSQL and MySQL require every unique key to
contain the partition column, which an auto-incrementing primary key cannot
express.
