"""The seam a project gets metrics out of.

Deliberately a ``django.dispatch.Signal`` rather than a setting naming a dotted
path. A setting would need an entry in ``Settings.md``, a check id, an
``import_string``, a lazy cache to keep the import off the hot path, and a
decision about what to do when the path is wrong. A signal needs none of that,
``send_robust`` contains most of what a receiver can do wrong, and connecting one is
the thing every Django developer already knows how to do. *Most*: see
:meth:`~django_redis_aiogram.recorder.EventRecorder._publish` for the receiver shape
Django's own containment misses, which is why this package does not rely on it
alone.

This module imports ``django.dispatch`` and nothing else — not aiogram, not the
ORM, not the rest of this package. A metrics module can import it at settings
time without dragging anything in.
"""

from django.dispatch import Signal

#: Fired once per batch of recorded events, on whichever thread flushed that batch —
#: normally the event writer's own, and three other threads can be it.
#:
#: Receivers get ``events``: a tuple of :class:`~django_redis_aiogram.recorder.Event`,
#: whose field names are pinned by ``tests/test_public_surface.py`` and are
#: therefore public API. ``sender`` is the recorder instance.
#:
#: Four things about it are load-bearing, and three of them are surprising:
#:
#: * **It fires whether or not the event log is on.** The table and the metrics
#:   are separate decisions: connect a receiver and the events flow, with
#:   ``EVENT_LOG`` left off and no migration in sight.
#: * **Payload summaries are the only part of ``detail`` the log gates.** With the
#:   log off, ``detail`` still carries what the recording seam measured itself —
#:   a send's ``duration_ms``, a retry's ``retry_after``, a queueing failure's
#:   ``stage``, a gap's ``dropped`` count. What is missing is the summarised
#:   arguments, because redacting and bounding a payload is the expensive part of
#:   recording and no part of counting. A receiver that needs message bodies needs
#:   the log on as well, and then ``EVENT_LOG_PAYLOAD`` decides what is in there.
#: * **``EVENT_LOG_KINDS`` filters this too, with one exemption.** It is one answer
#:   to "which events does this deployment care about", not two — so a receiver
#:   sees exactly the kinds the table would have kept. ``log.dropped`` is the
#:   exception, in both directions: it is the record that recording itself fell
#:   behind, and a deployment that filtered it out would read the hole as quiet
#:   traffic. The table has always been exempt from the filter for that row, and
#:   receivers are exempt with it.
#: * **The write is attempted before receivers see the batch.** So nothing a
#:   receiver does can change a row that was written, which is why they get real
#:   ``Event`` objects rather than copies — the ``detail`` dict inside one is an
#:   ordinary mutable dict shared with the other receivers, so treat it as
#:   read-only. It is *attempted*, not guaranteed: a write that failed still
#:   publishes, because a database being down is exactly when someone is watching a
#:   dashboard. Receiving a batch is therefore not evidence that a row exists for
#:   it, and with ``EVENT_LOG`` off there is no row by design.
#:
#: The rule is **whichever thread flushed the batch publishes it**, and normally that
#: is the event writer's, once the batch's own write has been attempted — so a slow
#: receiver delays neither a send nor that write, only later batches and the writer's
#: shutdown. Never delaying a send is the point of the design; the rest follows from
#: the rule rather than being promised separately.
#:
#: Three other threads can flush a batch, so three other threads can publish one:
#:
#: * whatever calls ``EventRecorder.drain_once()``, which exists so a test can drive
#:   the real flush path on its own thread
#: * at shutdown, whichever thread called ``stop()``, for whatever the writer had not
#:   drained. Those events are published rather than dropped because they are the last
#:   ones before the process goes, and there is by then no writer left to hand them to
#: * under ``EVENT_LOG_SYNC``, the thread that recorded the event, which is the one
#:   case with no batch and no writer involved at all
#:
#: ``EVENT_LOG_SYNC`` only takes effect with the log on — there is nothing to insert
#: synchronously otherwise — so the write is always attempted there, and may still
#: fail. It is a testing setting, and receivers running inside the send path is one
#: more reason to keep it one.
#:
#: A dispatch that fails can leave later receivers without the batch: ``send_robust``
#: stops its own loop when Django's failure logging raises, which it does for a
#: callable instance. This package catches that and logs
#: ``publishing recorded events failed``, but the receivers after the offending one
#: were never called.
#:
#: The write happens before either, and only when ``EVENT_LOG`` is on: with the log
#: off nothing is written at all, and a receiver is the only thing the batch reaches.
events_recorded: Signal = Signal()
