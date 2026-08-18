"""The marker itself: an ``AppConfig`` whose ``ready()`` writes a file."""

import os
import tempfile
from pathlib import Path

from django.apps import AppConfig

#: where the mark lands, overridable so a test can point it at a temporary path
MARKER_ENV = 'DJANGO_REDIS_AIOGRAM_TEST_MARKER'


def marker_path() -> Path:
    """Where this app records that its ``ready()`` ran."""
    return Path(os.environ.get(MARKER_ENV) or Path(tempfile.gettempdir()) / 'drai-registry-marker')


class MarkerConfig(AppConfig):
    """Writes to :func:`marker_path` from ``ready()``, and nothing else."""

    name = 'tests.marker_app'
    label = 'drai_marker_app'

    def ready(self) -> None:
        """Record that the app registry was populated in this process."""
        marker_path().write_text('ready() ran', encoding='utf-8')
