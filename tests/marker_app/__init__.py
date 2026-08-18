"""An app that leaves a mark when the registry populates it.

Exists so a test can tell the difference between "Django's settings were read" and
"``django.setup()`` ran" — which is the difference between 0.07s and however long the
host project's ``AppConfig.ready()`` methods take.
"""
