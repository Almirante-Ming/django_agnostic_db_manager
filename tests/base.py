"""Shared base for this app's tests.

These tests are collected by a plain `manage.py test` like any other
Django app's, which means they run inside whatever project installed this
app. Two things make that safe:

1. **They do not read the host project's configuration.** Every test runs
   with the DB_PORTABILITY_* settings pinned to the fixture apps below, so
   a host that points them at its own models does not change the outcome.
   Tests that need a different value override it on top as usual.

2. **They skip, rather than fail, when the fixture apps are absent.** The
   suite needs its fixture apps in INSTALLED_APPS to have tables to work
   against. A project that has not added them gets a clear skip message
   instead of errors. To actually run them, add to INSTALLED_APPS --
   ideally only while testing, so the tables never reach a real database:

       if IS_TESTING:  # or however your project detects a test run
           INSTALLED_APPS += [
               'db_portability.tests.people',
               'db_portability.tests.sales',
               'db_portability.tests.support',
               'db_portability.tests.plain',
           ]

   Or run the suite standalone, with no host project at all:

       python db_portability/tests/runtests.py
"""

import unittest

from django.apps import apps
from django.test import TestCase, override_settings

#: Apps the suite needs in INSTALLED_APPS to have tables to work against.
FIXTURE_APP_LABELS = ('people', 'sales', 'support', 'plain')

#: The app's configuration, pinned to the fixture apps so the suite asserts
#: against a known shape rather than the host project's.
FIXTURE_SETTINGS = {
    'DB_PORTABILITY_DEPARTMENTS': ['sales', 'support', 'plain'],
    'DB_PORTABILITY_SHARED_APP': 'people',
    'DB_PORTABILITY_PERSON_MODEL': 'people.Person',
    'DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS': {'people.BrandingConfig': 'department'},
    # Both left at "not configured" so the defaults are what gets asserted;
    # individual tests override them where that is the point.
    'DB_PORTABILITY_UPLOAD_CALLBACK': None,
    'DB_PORTABILITY_ARCHIVE_PREFIX': None,
}


def missing_fixture_apps():
    return [label for label in FIXTURE_APP_LABELS if not apps.is_installed(f'db_portability.tests.{label}')]


def host_apps_outside_fixtures():
    """App labels a full extraction would pull in beyond this suite's own
    fixtures. Empty when running standalone; non-empty inside a real
    project, where a test must not delete data it does not own.
    """
    from db_portability import services as svc

    return [label for label in svc.resolve_included_apps() if label not in FIXTURE_APP_LABELS]


def require_fixture_apps():
    """Call at module import. Skips the whole module, with an actionable
    message, in a project that has not installed the fixture apps.
    """
    missing = missing_fixture_apps()
    if missing:
        raise unittest.SkipTest(
            "db_portability's own tests need their fixture apps installed; missing: "
            + ', '.join(f'db_portability.tests.{label}' for label in missing)
            + '. See db_portability/tests/base.py for how to enable them, or run '
              'python db_portability/tests/runtests.py to run them standalone.'
        )


@override_settings(**FIXTURE_SETTINGS)
class PortabilityTestCase(TestCase):
    """Base for every test here. Subclasses inherit the pinned settings."""

    pass
