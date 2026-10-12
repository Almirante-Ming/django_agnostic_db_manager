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

# Write locally only, skipping the optional upload callback
manage.py extract_database --skip-upload

# Import into whatever database this process points at -- which may be a
# different engine than the one the package came from. Pending migrations
# are applied first; nothing is written partially on conflict, corruption,
# or a migration mismatch.
manage.py import_database --input /path/to/db-extract-<timestamp>.tar.gz

# Validate and report without writing
manage.py import_database --input <package> --dry-run
```

A package is a `.tar.gz` holding `manifest.json` plus one
`data/<app>.json` per included app. The local copy under
`BASE_DIR/db_extracts/` is always written first and never deleted — it is
the fallback, not a conditional extra — so a failing upload can never cost
you an extraction.

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

### Compatibility check on import

Each manifest records the leaf migrations applied per included app at
extraction time (`manifest["migrations"]`). On import, that is compared
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
| `DB_PORTABILITY_UPLOAD_CALLBACK` | `str` — dotted path to a callable | `None` | Optional upload step after a package is written. Signature: `callback(path: str, folder_id=None) -> dict`. Unset, no upload is attempted. |
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

The app carries its own suite, in `selftests/`. It is self-contained —
its own settings, its own SQLite-in-memory database, and its own fixture
apps (`people` shared, `sales`/`support` as departments, `plain` as a
misconfigured one) — so it needs no host project and asserts nothing
about yours:

```bash
python db_portability/selftests/runtests.py                 # all of it
python db_portability/selftests/runtests.py suite_services  # one module
```

It runs from a bare copy of this directory anywhere on disk; the only
requirement is that the parent of `db_portability/` is importable, which
`runtests.py` arranges itself.

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
- **CLI contract** — flag names, the upload callback receiving the folder
  id, `--skip-upload` not touching the callback, a failed upload still
  leaving the local archive, and `--dry-run` writing nothing.

The suite files are named `suite_*.py` rather than `test_*.py` on
purpose: a host project running `manage.py test` must not collect them,
since they need the fixture apps in `INSTALLED_APPS`. If you want
coverage of *your* project's wiring — your real models against these
settings — write that as a normal test in your project; it complements
this suite rather than duplicating it.

## License

BSD 3-Clause — see `LICENSE`.
