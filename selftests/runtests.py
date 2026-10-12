#!/usr/bin/env python
"""Run this app's own test suite, standalone.

    python db_portability/selftests/runtests.py            # everything
    python db_portability/selftests/runtests.py suite_services   # one module

No host project, settings module or manage.py required -- the suite brings
its own settings and fixture models. The suite files are named
`suite_*.py` rather than `test_*.py` precisely so that a host project's
own `manage.py test` run does not collect them: they need the fixture apps
in INSTALLED_APPS, which only the settings module next to this file
provides.
"""

import os
import sys
from pathlib import Path


def main(argv):
    # The importable package is `db_portability`, so its PARENT has to be
    # on sys.path. Works both inside a host project and from a bare copy
    # of the app directory anywhere on disk.
    app_dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(app_dir.parent))

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'db_portability.selftests.settings')

    import django
    from django.test.utils import get_runner
    from django.conf import settings

    django.setup()

    requested = argv[1:] or ['suite_services', 'suite_commands']
    labels = [
        name if name.startswith('db_portability.') else f'db_portability.selftests.{name}'
        for name in requested
    ]

    runner = get_runner(settings)(verbosity=2, interactive=False)
    return runner.run_tests(labels)


if __name__ == '__main__':
    sys.exit(bool(main(sys.argv)))
