"""Fixture app: a department, owning its own tables plus a
multi-table-inheritance subclass of the shared person model.
"""

from django.db import models

from db_portability.tests.people.models import Person


class SalesPerson(Person):
    quota = models.IntegerField(default=0)


class SalesNote(models.Model):
    author = models.ForeignKey(SalesPerson, on_delete=models.CASCADE, related_name='notes')
    body = models.TextField(blank=True)
