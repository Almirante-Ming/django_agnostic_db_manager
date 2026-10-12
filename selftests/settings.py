"""Standalone Django settings for this app's own test suite.

Deliberately independent of whatever project the app happens to be
installed in: SQLite in memory, the fixture apps from this directory, and
the DB_PORTABILITY_* settings pointed at those fixtures. Running the
suite therefore proves the app works against models it was not written
against, which is the whole point of it being a portable app.
"""

import tempfile
from pathlib import Path

BASE_DIR = Path(tempfile.mkdtemp(prefix='db-portability-selftest-'))

SECRET_KEY = 'selftests-only-not-a-secret'
DEBUG = True
USE_TZ = True

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'db_portability',
    'db_portability.selftests.people',
    'db_portability.selftests.sales',
    'db_portability.selftests.support',
    'db_portability.selftests.plain',
]

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# The app under test, wired to the fixture apps above. Mirrors the shape
# documented in README.md / .env.example.
DB_PORTABILITY_DEPARTMENTS = ['sales', 'support', 'plain']
DB_PORTABILITY_SHARED_APP = 'people'
DB_PORTABILITY_PERSON_MODEL = 'people.Person'
DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS = {
    'people.BrandingConfig': 'department',
}
# Left unset on purpose: the suite overrides it per test, and the default
# (no upload attempted) is itself one of the cases under test.
