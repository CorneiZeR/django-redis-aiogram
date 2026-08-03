"""Stands in for a router with a typo in its imports.

Raising something else would not exercise the case: Django's autodiscover only
re-raises when the module exists, and ImportError is what a typo produces.
"""

from django.core.exceptions import ImproperlyConfigred  # noqa: F401
