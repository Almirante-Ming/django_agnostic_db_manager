"""Fixture app: configured as a department but deliberately WITHOUT any
subclass of the shared person model, so the "misconfigured project" error
path has something to assert against.
"""

from django.db import models


class PlainRecord(models.Model):
    label = models.CharField(max_length=100)
