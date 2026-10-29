# django-db-portability

Engine-agnostic data extraction and import for Django, as two management
commands: `extract_database` and `import_database`. No raw SQL, no
`pg_dump`/`mysqldump`/`COPY`, no engine-specific tooling — everything goes
through Django's own ORM, `dumpdata`/`loaddata`, serializers and
migrations machinery. A package extracted from PostgreSQL can be imported
straight into SQLite (or any other engine Django supports) with no manual
conversion step.

The app ships no models of its own and names no concrete app, model or
field anywhere in its code. Everything project-specific is read from
settings, all of them optional.

## Installation

Copy the `db_portability/` directory into your project alongside your
other apps, or install it as a package (this directory is a standalone
`pyproject.toml` project):

```bash
uv add /path/to/django-db-portability
# or
pip install /path/to/django-db-portability
```

Then add it to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...,
    "db_portability",
]
```

## Dependencies

**Django is the only runtime dependency** (`django>=6.0`, no upper bound —
the module only touches long-stable APIs: the ORM, serializers,
`dumpdata`/`loaddata`, and the migrations loader/executor). Nothing else
is imported anywhere in the package. The floor is conservative rather
than technical: nothing here needs Django 6 specifically, so lowering it
is mostly a matter of running the suite against the version you want.

Python **3.12+**, because `extract_bundle_to_workdir` passes
`filter="data"` to `tarfile.extractall()` — the extraction safety filter
from PEP 706. That argument also exists in later patch releases of older
Python lines, where it was backported as a security fix, but `>=3.12` is
the floor that does not depend on patch-level backports.

Anything an optional upload callback of yours needs (an object-storage
client, a cloud-drive SDK) is **your project's dependency, not this
app's**: the callback is resolved from a dotted path at call time and
never imported here.

## Usage

```bash
# Everything in the configured database, into a portable .tar.gz
manage.py extract_database

# Only one department/tenant (needs the settings below)
manage.py extract_database --department sales

# Also send the package through the configured upload callback.
# Off by default: an extraction writes locally and sends nothing anywhere
# unless this flag is passed.
manage.py extract_database --upload

# Import into whatever database this process points at -- which may be a
# different engine than the one the package came from. Pending migrations
# are applied first; nothing is written partially on conflict, corruption,
# or a migration mismatch.
manage.py import_database --input /path/to/db-extract-<timestamp>.tar.gz

# Validate and report without writing
manage.py import_database --input <package> --dry-run
```

### Flags

`extract_database`:

| Flag | Default | Meaning |
|---|---|---|
| `--department <label>` | none (full) | Limit the package to one department. Only accepts values from `DB_PORTABILITY_DEPARTMENTS`. |
| `--output-dir <path>` | `BASE_DIR/db_extracts` | Where the `.tar.gz` is written. |
| `--upload` | off | Also run the configured upload callback. Off means nothing leaves the machine. |
| `--upload-folder-id <id>` | none | Passed through to the callback as `folder_id` for this run. |

`import_database`:

| Flag | Default | Meaning |
|---|---|---|
| `--input <path>` | required | The `.tar.gz`, or an already-extracted directory. |
| `--dry-run` | off | Validate, check compatibility and conflicts, report, write nothing. |

There is deliberately **no** `--department` on import: the scope is
whatever the package contains, read from its manifest.

A package is a `.tar.gz` holding `manifest.json` plus one
`data/<app>.json` per included app. The local copy under
`BASE_DIR/db_extracts/` is always written first and never deleted — it is
the fallback, not a conditional extra — so a failing upload can never cost
you an extraction.

**Extraction never sends your data anywhere unless you ask.** Uploading is
opt-in per run via `--upload`; configuring
`DB_PORTABILITY_UPLOAD_CALLBACK` makes the option *available*, not
automatic. A database export is exactly the payload you do not want
leaving a machine as a side effect of a default.

### Scopes

Without `--department`, an extraction covers every installed app that has
models, minus framework bookkeeping tables whose primary keys the
destination regenerates for itself (`contenttypes`, `sessions`, `admin`,
`auth.Permission`, and the Celery result/beat tables).

With `--department <label>`, it covers that app plus the rows of a shared
app that belong to it, by referential closure — see the settings table.

`import_database` takes no department argument on purpose: the scope is
whatever the package physically contains, read from `manifest.scope`, so a
full package restores everything and a department package restores just
that department. Importing one department does not require, or disturb,
the others already present in the destination — the conflict check is per
individual primary key, not per table.

### Extracting from a database behind the code

The extraction reads the source database as it is, not as the models
declare it: tables and columns come from Django's introspection API
(`SourceSchema`). A model whose table the database does not have is
skipped, a field whose column it does not have is left out, and both are
listed in `manifest["schema_gaps"]`. `manifest["pending_migrations"]`
lists the migrations the extracting code knows but the database has not
applied. A snapshot can be rebuilt by migrating an empty database to
`manifest["migrations"]` and loading the package there.

Limitation: a related model's `natural_key()` still loads that model in
full, so a model *with* a natural key that gained a column the database
lacks still fails the extraction.

### Compatibility check on import

Each manifest records the latest migrations applied per included app at
extraction time (`manifest["migrations"]`) -- the applied frontier, so a
database whose leaf migrations are not applied yet is still stamped with
the schema it really has. On import, that is compared
against the migrations the **destination's current code** knows about —
not merely what is applied there — and the import aborts before writing
anything if the package names a migration this codebase does not
recognize. That catches restoring a newer backup onto older code, which
the static `format_version` alone cannot. Packages written before this
field existed carry no migration state and are treated as compatible.

## Settings

Every setting is optional. With none of them set, both commands still work
for **full** extraction and import; the first four are what unlock the
per-department scope.

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `DB_PORTABILITY_DEPARTMENTS` | `list[str]` | `[]` | App labels treated as departments (tenants). Unset, `--department` rejects every value. |
| `DB_PORTABILITY_SHARED_APP` | `str` | `None` | App label holding records shared across departments. Its filtered file is included alongside the department's own app on a scoped extraction. |
| `DB_PORTABILITY_PERSON_MODEL` | `str` — a model label like `"people.Person"` | `None` | The shared "person" model each department app subclasses through multi-table inheritance. Used to work out, by introspection, which shared-app rows belong to a department. |
| `DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS` | `dict[str, str]` — `{"app.Model": "field_name"}` | `{}` | Shared-app models scoped to a department by a plain field value rather than a foreign key to the person model — the one case introspection cannot infer. Anything reachable through a FK/O2O to the person model is discovered automatically and needs no entry. |
| `DB_PORTABILITY_UPLOAD_CALLBACK` | `str` — dotted path to a callable | `None` | Makes `--upload` available. Signature: `callback(path: str, folder_id=None) -> dict`. Configuring it does not cause uploads on its own — they stay opt-in per run. |
| `DB_PORTABILITY_ARCHIVE_PREFIX` | `str` | `"db-extract"` | Filename prefix for generated packages. Import accepts any filename, so changing this never invalidates existing packages. |

See `.env.example` for a worked example of each.

### How the department closure works

Given `DB_PORTABILITY_PERSON_MODEL` and a department app label, a
shared-app row belongs to that department when it is:

1. a person row whose primary key also exists in that department's
   multi-table-inheritance subclass of the person model — the subclass is
   located by introspecting the class hierarchy, not by a naming
   convention or a hand-maintained map;
2. a row referencing one of those person rows through a concrete FK/O2O —
   found by scanning model fields, so a reference added later is covered
   without touching this package;
3. a row of a model listed in `DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS`.

Nothing belonging to another department is ever included, which is what
makes a per-department package safe to hand over on its own.

## Tests

The app carries its own suite in `tests/`, in the normal Django layout —
so **a plain `manage.py test` collects and runs it** alongside your own
tests. It is self-contained: its own fixture apps (`people` shared,
`sales`/`support` as departments, `plain` as a misconfigured one) and its
own pinned `DB_PORTABILITY_*` values, so it asserts nothing about your
models or your configuration.

For it to actually run inside your project, add the fixture apps while
testing — ideally only then, so their tables never reach a real database:

```python
if TESTING:  # however your project detects a test run
    INSTALLED_APPS += [
        'db_portability.tests.people',
        'db_portability.tests.sales',
        'db_portability.tests.support',
        'db_portability.tests.plain',
    ]
```

```bash
manage.py test db_portability   # just this app
manage.py test                  # alongside everything else
```

Without those apps the suite is still collected, but skips itself with a
message naming what is missing — it never fails a project that has not
opted in.

From this package's own repository, where this directory is the root and
there is no host project at all:

```bash
uv sync --no-install-project --group dev
.venv/bin/python tests/runtests.py                # all of it
.venv/bin/python tests/runtests.py test_services  # one module
```

`--no-install-project` is required rather than preferred — see the
comment above `[dependency-groups]` in `pyproject.toml` for why an
editable install cannot work with this layout. The package does not need
installing anyway: `runtests.py` puts it on `sys.path` itself, which is
also why the suite runs from a bare copy of this directory anywhere on
disk.

Because the suite runs against fixture models the app was never written
for, a green run is also the portability check: it proves the discovery
rules work on a schema other than the one they were developed against.
Run it before shipping a change, and after bumping Django.

What it pins, in the order a future change is most likely to break it:

- **Discovery** — the department's multi-table-inheritance subclass is
  found by introspection; FK/O2O references to the person model (and to
  its subclasses) are found by field scan; the implicit MTI parent link is
  excluded; unrelated models are not picked up.
- **Package boundaries** — a department package contains that
  department's rows and nothing of the others; shared rows with no
  department never enter it; person rows precede the rows referencing
  them; the scoped-models setting is what pulls in a label-scoped model.
- **Settings contract** — every knob is optional, read at call time, and
  the app degrades sensibly when one is unset.
- **Refusals** — the destination is never half-written: corrupt package,
  unsupported format version, missing data file, per-PK conflict, and a
  migration stamp the destination's code does not know (checked *before*
  the conflict check, since it is the more fundamental mismatch).
- **Compatibility of old packages** — a package carrying no migration
  state still imports.
- **CLI contract** — flag names, uploading staying opt-in (a default run
  calls no callback even when one is configured), the callback receiving
  the folder id, a failed upload still leaving the local archive, and
  `--dry-run` writing nothing.

One case, the full-database round trip, skips inside a host project:
restoring a full package means first emptying the whole database, and a
test must not delete rows it does not own. It runs under
`tests/runtests.py`, where the only data present is the suite's own. The
per-department round trips touch fixture rows only and run everywhere.

If you want coverage of *your* project's wiring — your real models
against your own `DB_PORTABILITY_*` values — write that as a normal test
in your project; it complements this suite rather than duplicating it.

## License

BSD 3-Clause — see `LICENSE`.
