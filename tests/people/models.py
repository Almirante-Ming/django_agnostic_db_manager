"""Fixture app: the "shared" app, holding records several departments use.

Shaped to exercise all three ways a shared-app row can belong to a
department (see services.resolve_department_closure):

    Person          -- the configured person model, subclassed per
                       department through multi-table inheritance
    Account         -- reaches a Person through a O2O, so it must be
                       discovered by field introspection alone
    BrandingConfig  -- scoped by a plain label field, so it needs an
                       explicit DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS entry
    Announcement    -- belongs to no department at all, so it must never
                       be swept into a scoped package
"""

from django.db import models


class Person(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.name


class Account(models.Model):
    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name='account')
    username = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.username


class BrandingConfig(models.Model):
    department = models.CharField(max_length=30, unique=True)
    headline = models.CharField(max_length=200)

    def __str__(self):
        return self.department


class Announcement(models.Model):
    """Shared-app rows with no department association whatsoever."""

    title = models.CharField(max_length=200)

    def __str__(self):
        return self.title
