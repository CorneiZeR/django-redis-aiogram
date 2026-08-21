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
    # part of turning it on, not an afterthought: nothing on the write path deletes
    # anything, so `W006` warns while this is 0, which is also the default — and a project running
    # `manage.py check --fail-level WARNING` in CI, which this documentation recommends,
    # gets a red build for a log that grows for ever. See Pruning below
    'EVENT_LOG_RETENTION_DAYS': 30,
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
| `outbound.dropped` | it never got sent: queueing refused it, retries were exhausted, or shutdown canceled it. `detail.stage` says which |
| `inbound.received` | an update arrived, by polling or webhook |
| `inbound.handled` | the handlers finished |
| `inbound.failed` | a handler raised |
| `fsm.transition` | a chat's state changed |
| `queue.undecodable` | a payload could not be decoded |
| `queue.rejected` | a payload named something that is not a Telegram API method |
| `log.dropped` | the writer fell behind and lost events — the gap, recorded |

`outbound.dropped` is the one worth reading twice. Four different things end up
under that one kind, they are not equally recoverable, and `detail` together with
`error_code` is what separates them:

| Row says | What happened | Send it again? |
| --- | --- | --- |
| `detail.stage: serialising` | the payload could not be encoded, so it never left the process | yes — Redis never saw it |
| `detail.stage: queueing` | the write to Redis raised | **not certainly** — an `RPUSH` that raised may have been applied and only its reply lost |
| `detail.max_retries: N` | Telegram refused it with a rate limit N times over | it was delivered nowhere and nothing will retry it, so yes — but expect the same refusal |
| `error_code: NotScheduled` | it was never scheduled, or was canceled at shutdown; `error` says which | **usually not** — see below |

`NotScheduled` is the one that will bite an operator. A send that came off the
queue is *deliberately* left in the worker's in-flight list when shutdown cancels
it, so a restart under the same worker identity reclaims and delivers it —
re-sending by hand duplicates. A send made directly with `send_raw`, from your own
code rather than from the queue, was never in that list, and nothing will ever
retry it. The rows tell them apart: a queued message has `outbound.queued` and
`outbound.consumed` rows under the same `correlation_id`, and a direct `send_raw`
has neither. See **[[Delivery]]** for the guarantee that makes the first case work.

The stages matter most after a broadcast: `send_many` loses the ids of the failing
chunk with the exception, so these rows are the only list of which messages went
missing, and the stage is the only thing that says whether re-sending them would
duplicate.

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

A row the database refuses on its own — a constraint, a column too small — is counted
the same way, so a batch that lands 39 of 40 rows leaves a `log.dropped` behind it
rather than a silent hole. The count survives a gap row that itself cannot be written: it
is taken off the counter before the row is written and given back if that row does not
land — raised or refused alike — so the next successful flush reports it. Taking it off
first is also what stops two flushes reporting the same hole, since a worker draining by
hand and the writer thread can both be mid-flush at once.

`EVENT_LOG_BUFFER_SIZE`, `EVENT_LOG_BATCH_SIZE` and `EVENT_LOG_FLUSH_INTERVAL`
size it. A batch larger than the buffer can never fill, so `W007` says so.

**A broadcast is where this bites.** `send_many` records one `outbound.queued` row
per message, the same as sending them one at a time — but it removes the pacing
the sequential round trips used to give the writer, so fifty thousand chats arrive
as fifty thousand events in a few seconds rather than spread over minutes. Raise
`EVENT_LOG_BUFFER_SIZE`, or narrow `EVENT_LOG_KINDS`, before the first large one.
The messages are never at risk; the rows about them are.

What is lost: on `SIGKILL`, a worker timeout or `os._exit()`, whatever is in the
queue and in the current batch. At the defaults that is under a second of events
plus up to 200 rows. A clean `SIGTERM` loses nothing. This is an event feed, not
a ledger — if you need durability across a kill, the thing that already gives it
to you is the Redis queue.

## Metrics, without the table

The same events reach a `django.dispatch.Signal`, so a project can count what the
bot does without keeping a row for any of it:

```python
# metrics.py, imported from your AppConfig.ready()
from prometheus_client import Counter

from django_redis_aiogram.signals import events_recorded

SENDS = Counter('telegram_events', 'django-redis-aiogram events', ['kind', 'function'])


def count(sender, events, **kwargs):
    for event in events:
        SENDS.labels(kind=event.kind, function=event.function or 'none').inc()


events_recorded.connect(count, dispatch_uid='metrics.telegram')
```

Receivers get `events`: a tuple of `Event`, whose field names are the same ones the
table's columns carry and are pinned as public API. A signal rather than a setting
naming a dotted path, because there is then no path to get wrong, no check id for
it, and no question about what happens when the import fails.

With `EVENT_LOG` on, the write is attempted before a receiver sees the batch, so
nothing a receiver does can change a row that was written — which is why they get
the real objects rather than copies. The `detail` dict inside one is an ordinary
dict, shared with the other receivers, so treat it as read-only.

*Attempted*, not guaranteed, and only when the log is on at all. A write that failed
still publishes, because a database being down is exactly when someone is watching a
dashboard; with the log off there is no write to attempt and a receiver is the only
thing the batch reaches. Either way, a batch arriving is not evidence that a row
exists for it.

Four things about it are worth knowing before you rely on it, and three of them
surprise people:

**It fires with `EVENT_LOG` off.** The table and the metrics are separate
decisions. Connect a receiver, leave the log off, run no migration for it: the
events still arrive. Turn the log on as well and both happen.

**Payload summaries are the only part of `detail` the log gates.** With the log
off, `detail` still carries whatever the recording seam measured itself: a send's
`duration_ms`, a retry's `retry_after`, a queueing failure's `stage`, a gap's
`dropped` count. What is missing is the *summarized arguments* — redacting
credentials out of a payload, walking it and bounding it costs tens of
microseconds, and a counter keyed on `kind` and `function` needs none of it. If
your receiver needs message bodies, it needs the log on too, and then
`EVENT_LOG_PAYLOAD` decides what is in there.

**`EVENT_LOG_KINDS` filters this as well, with one exemption.** It is one answer to
"which events does this deployment care about", not two — so a receiver sees
exactly the kinds the table would have kept. `log.dropped` is exempt in both
directions: it is the record that recording itself fell behind, and a deployment
that filtered it out would read the hole as quiet traffic rather than as a gap.
The table has always been exempt for that row; receivers are exempt with it.

**Connect during app loading.** The update middleware and the FSM storage wrapper
are built once, and whether to build them is decided then. A receiver connected
after the first update arrives will not see updates in that process. An
`AppConfig.ready()` is where Django says signal receivers belong, and it is early
enough.

### Where it runs, and what that costs

The rule is **whichever thread flushed the batch publishes it**, and normally that is
the event writer's own — with `EVENT_LOG` on, after that batch's own write has been
attempted. So a slow receiver delays neither a send nor the rows it just saw, only
later batches and how long the writer takes to stop. Never delaying a send is the
whole reason this is not a settings hook calling into your code from the send path.

Three other threads can flush a batch, so three other threads can publish one:

* whatever calls `recorder.drain_once()`, which exists so a test can drive the real
  flush path on its own thread

* under `EVENT_LOG_SYNC`, on the thread that recorded the event, after its write
  attempt. That flag only takes effect with the log on — there is nothing to insert
  synchronously otherwise — so the write is always attempted there, and may still
  fail. It is a testing setting, and receivers running inside the send path is one
  more reason to keep it one
* at shutdown, on whichever thread called `stop()`, for whatever the writer had not
  drained by then. Those are published rather than dropped because they are the last
  events before the process goes, and there is no writer left to hand them to

A receiver that raises is logged as `an events_recorded receiver raised` and costs
neither the other receivers their batch nor the database its rows. `send_robust` is
most of that: Django hands the exception back instead of letting it end the writer.

Not all of it, though, and the gap is worth knowing if you write a receiver as a
class. Django's own failure logging reads `receiver.__qualname__`, which a *callable
instance* does not have — so for that shape `send_robust` raises rather than
containing anything, measured on Django 6.1. This package catches that too and logs
`publishing recorded events failed`; without it the exception would be counted as a
failed database write, which is the one story in the log that would send you to the
wrong place entirely.

One consequence survives the catch: `send_robust` stops its own receiver loop when it
raises, so **receivers after the offending one never see that batch**. The rows are
unaffected — they were written first — and the next batch starts the loop again. If
that matters to you, do not write a receiver as a callable class, or keep the risky
one last.

Two honest notes about `prometheus_client` in particular. Its `labels()` and
`inc()` both take locks, and in multiprocess mode an increment is an mmap write —
cheap, but not free, and it happens once per event in the batch. And `outbound.sent`
is recorded inside the `start_tgbot` worker, which serves no HTTP at all: the
exporter has to be stood up **in that container**, or the numbers you scrape from
the web tier will only ever cover queueing.

No new dependency comes with this. `django.dispatch` is Django, and nothing here
imports `prometheus_client` or knows it exists.

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
exactly, so both use their index. Sorting is limited to the three indexed columns:
`created_at`, `kind` and `chat_id`. The other headers are not links, and an `?o=`
naming one of them — from a bookmark, or a link shared before this restriction — is
dropped rather than honoured, because ordering the whole table by `worker` is a
sequential scan and a sort on every page.

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
schedule the command; `W006` warns while it is `0`, which is its default, because the
feature is not finished without it:

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
that database afterward. Django's migrations have no representation for
`PARTITION BY`, and both PostgreSQL and MySQL require every unique key to
contain the partition column, which an auto-incrementing primary key cannot
express.
