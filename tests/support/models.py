"""Fixture app: a second department, so cross-department isolation can be
asserted rather than assumed.
"""

from django.db import models

from db_portability.tests.people.models import Person


class SupportPerson(Person):
    shift = models.CharField(max_length=20, blank=True)


class SupportTicket(models.Model):
    assignee = models.ForeignKey(SupportPerson, on_delete=models.CASCADE, related_name='tickets')
    subject = models.CharField(max_length=200)
