"""The seam a project gets metrics out of.

Deliberately a ``django.dispatch.Signal`` rather than a setting naming a dotted
path. A setting would need an entry in ``Settings.md``, a check id, an
``import_string``, a lazy cache to keep the import off the hot path, and a
decision about what to do when the path is wrong. A signal needs none of that,
Django already contains receiver exceptions for us through ``send_robust``, and
connecting one is the thing every Django developer already knows how to do.

This module imports ``django.dispatch`` and nothing else — not aiogram, not the
ORM, not the rest of this package. A metrics module can import it at settings
time without dragging anything in.
"""

from django.dispatch import Signal

#: Fired once per batch of recorded events, from the event writer's own thread.
#:
#: Receivers get ``events``: a list of :class:`~django_redis_aiogram.recorder.Event`,
#: whose field names are pinned by ``tests/test_public_surface.py`` and are
#: therefore public API. ``sender`` is the recorder instance.
#:
#: Three things about it are load-bearing, and two of them are surprising:
#:
#: * **It fires whether or not the event log is on.** The table and the metrics
#:   are separate decisions: connect a receiver and the events flow, with
#:   ``EVENT_LOG`` left off and no migration in sight.
#: * **``detail`` is only filled when the event log is on.** Summarising a
#:   payload means redacting and bounding it, which is the expensive part of
#:   recording and no part of counting. A receiver that needs message bodies
#:   needs the log on as well.
#: * **``EVENT_LOG_KINDS`` filters this too.** It is one answer to "which events
#:   does this deployment care about", not two.
#:
#: It runs on the writer thread, so a slow receiver delays rows reaching the
#: database but never delays a send. Under ``EVENT_LOG_SYNC`` there is no writer
#: thread and receivers run on the thread that recorded the event — that flag is
#: for tests, and this is one more reason it is.
events_recorded = Signal()
