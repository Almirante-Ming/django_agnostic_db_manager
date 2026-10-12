"""Engine-agnostic database extraction and import.

Used by the `extract_database` / `import_database` management commands in
this app. All cross-engine portability comes from Django's own
serialization layer (`dumpdata` / `loaddata` / `serializers`) -- no raw
SQL and no vendor-specific tooling (`pg_dump`, `mysqldump`, `COPY`) is
used anywhere, so a package produced against one engine can be loaded
into another.

Nothing in this module names a concrete app, model or field. Whatever
varies per project is read from settings at call time:

    DB_PORTABILITY_DEPARTMENTS              list[str]       default []
    DB_PORTABILITY_SHARED_APP               str             default None
    DB_PORTABILITY_PERSON_MODEL             str             default None
    DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS dict[str, str]  default {}
    DB_PORTABILITY_UPLOAD_CALLBACK          str             default None
    DB_PORTABILITY_ARCHIVE_PREFIX           str             default 'db-extract'

Every one of them is optional: with none set, full-database extraction
and import still work, and the per-department (per-tenant) scope is
simply unavailable. See README.md for the full description of each.
"""

import json
import logging
import tarfile
import tempfile
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

BUNDLE_FORMAT_VERSION = "1"

DEFAULT_ARCHIVE_PREFIX = 'db-extract'

# Framework bookkeeping tables. Their primary keys are autoincrements
# assigned at migrate/runtime and are recreated automatically in the
# destination (post_migrate), so carrying them across would conflict with
# rows the destination generated for itself.
DEFAULT_EXCLUDED_APPS = {
    'contenttypes',
    'sessions',
    'admin',
    'django_celery_beat',
    'django_celery_results',
}
DEFAULT_EXCLUDED_MODELS = {'auth.permission'}


def valid_departments():
    """App labels treated as departments (tenants), from
    settings.DB_PORTABILITY_DEPARTMENTS. Public so the management commands
    can build their `--department` choices without a hardcoded list.
    """
    return list(getattr(settings, 'DB_PORTABILITY_DEPARTMENTS', None) or [])


def _shared_app_label():
    """App label holding records shared across departments, from
    settings.DB_PORTABILITY_SHARED_APP. None means a department package
    carries only that department's own app, with no shared-app closure.
    """
    return getattr(settings, 'DB_PORTABILITY_SHARED_APP', None)


def _person_model():
    """The shared "person" model each department app subclasses through
    multi-table inheritance, from settings.DB_PORTABILITY_PERSON_MODEL.
    None disables the person-based part of the closure; models listed in
    DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS still apply on their own.
    """
    label = getattr(settings, 'DB_PORTABILITY_PERSON_MODEL', None)
    return apps.get_model(label) if label else None


def default_output_dir():
    """Local directory for packages and import logs, created on demand."""
    output_dir = Path(settings.BASE_DIR) / 'db_extracts'
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def resolve_included_apps(department=None):
    """App labels to include in an extraction: everything with models
    except the default exclusions, or -- when `department` is given -- the
    configured shared app plus that department's own app.
    """
    if department is not None:
        if department not in valid_departments():
            raise ValueError(f"Invalid department: {department!r}")
        shared_app = _shared_app_label()
        return ([shared_app] if shared_app else []) + [department]

    included = []
    for app_config in apps.get_app_configs():
        if app_config.label in DEFAULT_EXCLUDED_APPS:
            continue
        if not list(app_config.get_models()):
            continue
        included.append(app_config.label)
    return included


def _department_person_model(department, person_model):
    """Find, by introspection rather than by a hand-maintained name map,
    the model in app `department` that subclasses `person_model` through
    multi-table inheritance.
    """
    for model in apps.get_app_config(department).get_models():
        if model is not person_model and issubclass(model, person_model):
            return model
    return None


def _person_referencing_fields(person_model):
    """(model, field_name) for every concrete FK/O2O whose related model is
    `person_model` or a subclass of it. Discovered by introspection, not
    from a fixed list, so a reference added later is covered without
    editing this module.
    """
    mti_pk_field = f'{person_model._meta.model_name}_ptr'
    pairs = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if not getattr(field, 'concrete', False):
                continue
            related = getattr(field, 'related_model', None)
            if not (isinstance(related, type) and issubclass(related, person_model)):
                continue
            if field.name == mti_pk_field:
                continue  # implicit multi-table-inheritance pk, never in `fields`
            pairs.append((model, field.name))
    return pairs


def resolve_department_closure(department):
    """Querysets over the shared app holding exactly the rows that belong
    to one department (referential closure). A shared-app row belongs to a
    department when it is:

    a) a row of the configured person model that has a counterpart in that
       department's multi-table-inheritance subclass -- found by
       introspection (`_department_person_model`);
    b) a row referencing one of those through a FK/O2O -- found by
       introspection (`_person_referencing_fields`), so no model name is
       hardcoded here;
    c) a row of a model listed in DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS,
       for models scoped by a plain department-label field instead of a
       foreign key -- the one case introspection cannot infer.

    Return order matters for load order within the resulting file (a
    person row must precede whatever references it) and follows the order
    of the steps above.
    """
    if department not in valid_departments():
        raise ValueError(f"Invalid department: {department!r}")

    shared_app = _shared_app_label()
    person_model = _person_model()
    querysets = []

    if person_model is not None:
        tenant_person_model = _department_person_model(department, person_model)
        if tenant_person_model is None:
            raise ValueError(
                f"No subclass of {person_model._meta.label} found in app '{department}'."
            )
        person_ids = list(tenant_person_model.objects.values_list('pk', flat=True))
        querysets.append(person_model.objects.filter(pk__in=person_ids))

        for model, field_name in _person_referencing_fields(person_model):
            if shared_app and model._meta.app_label != shared_app:
                continue
            querysets.append(model._default_manager.filter(**{f'{field_name}_id__in': person_ids}))

    scoped_models = getattr(settings, 'DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS', None) or {}
    for model_label, field_name in scoped_models.items():
        model = apps.get_model(model_label)
        querysets.append(model._default_manager.filter(**{field_name: department}))

    return querysets


def capture_migration_state(included_apps):
    """Leaf migrations already applied in the source database, per included
    app, at extraction time. Recorded in the manifest so an import can tell
    whether the package came from a schema the destination's code does not
    know -- a real compatibility signal, unlike the static
    BUNDLE_FORMAT_VERSION on its own.
    """
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    state = {}
    for app_label in included_apps:
        leaves = sorted(
            name for (app_name, name) in loader.graph.leaf_nodes()
            if app_name == app_label and (app_name, name) in loader.applied_migrations
        )
        if leaves:
            state[app_label] = leaves
    return state


def check_migration_compatibility(manifest):
    """Compare the manifest's recorded migration state against the
    migration graph the destination's CURRENT CODE knows about -- not
    against what is applied there, which `ensure_migrations_applied`
    already handles. A migration named in the package but unknown to the
    destination means the package came from a newer (or forked) schema
    than this code understands; the unknown "app.migration" entries are
    returned, and an empty list means compatible.

    Packages written before this field existed carry no migration state
    and are treated as compatible: there is nothing to compare.
    """
    recorded = manifest.get('migrations') or {}
    if not recorded:
        return []

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    known_by_app = {}
    for app_name, name in loader.graph.nodes:
        known_by_app.setdefault(app_name, set()).add(name)

    unknown = []
    for app_label, migration_names in recorded.items():
        known_names = known_by_app.get(app_label, set())
        for name in migration_names:
            if name not in known_names:
                unknown.append(f'{app_label}.{name}')
    return unknown


def build_manifest(scope, included_apps, counts, source_engine=None, migrations=None):
    return {
        "format_version": BUNDLE_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_engine": source_engine or connection.vendor,
        "scope": scope,
        "apps": list(included_apps),
        "excluded_apps": sorted(DEFAULT_EXCLUDED_APPS),
        "counts": counts,
        "migrations": migrations or {},
    }


def _dumpdata_app_to_file(app_label, out_path, exclude=None):
    # natural_primary=True would omit 'pk' for models that define a
    # natural_key() (Django's own user model, among others), which the
    # per-(model, pk) conflict check below depends on. natural_foreign
    # stays on so FKs into apps excluded from the package (contenttypes)
    # still resolve.
    with out_path.open('w', encoding='utf-8') as fh:
        call_command(
            'dumpdata', app_label,
            natural_foreign=True, natural_primary=False,
            indent=2, format='json',
            exclude=exclude or [],
            stdout=fh,
        )


def _model_counts_for_app(app_label, excluded_models):
    counts = {}
    for model in apps.get_app_config(app_label).get_models():
        model_key = f'{app_label}.{model.__name__}'
        if model_key.lower() in excluded_models:
            continue
        counts[model_key] = model._default_manager.count()
    return counts


def dump_apps_to_bundle(work_dir, department=None):
    """Write manifest.json + data/<app>.json into work_dir. Returns the manifest."""
    work_dir = Path(work_dir)
    data_dir = work_dir / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)

    included_apps = resolve_included_apps(department)
    counts = {}

    if department is None:
        for app_label in included_apps:
            exclude = [m for m in DEFAULT_EXCLUDED_MODELS if m.startswith(f'{app_label}.')]
            _dumpdata_app_to_file(app_label, data_dir / f'{app_label}.json', exclude=exclude)
            counts.update(_model_counts_for_app(app_label, DEFAULT_EXCLUDED_MODELS))
        scope = {"type": "full"}
    else:
        shared_app = _shared_app_label()
        querysets = resolve_department_closure(department)
        if shared_app:
            shared_path = data_dir / f'{shared_app}.json'
            with shared_path.open('w', encoding='utf-8') as fh:
                serializers.serialize(
                    'json', chain(*querysets),
                    use_natural_foreign_keys=True, use_natural_primary_keys=False,
                    indent=2, stream=fh,
                )
            for qs in querysets:
                counts[f'{shared_app}.{qs.model.__name__}'] = qs.count()

        _dumpdata_app_to_file(department, data_dir / f'{department}.json')
        counts.update(_model_counts_for_app(department, DEFAULT_EXCLUDED_MODELS))

        scope = {"type": "department", "department": department}

    migrations = capture_migration_state(included_apps)
    manifest = build_manifest(scope, included_apps, counts, migrations=migrations)
    (work_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def archive_prefix():
    """Filename prefix for generated packages
    (settings.DB_PORTABILITY_ARCHIVE_PREFIX).
    """
    return getattr(settings, 'DB_PORTABILITY_ARCHIVE_PREFIX', None) or DEFAULT_ARCHIVE_PREFIX


def create_bundle_archive(work_dir, output_dir=None):
    """Pack manifest.json + data/ into a .tar.gz inside output_dir
    (default: default_output_dir()).
    """
    work_dir = Path(work_dir)
    output_dir = Path(output_dir) if output_dir else default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    archive_name = f'{archive_prefix()}-{timestamp}'
    archive_path = output_dir / f'{archive_name}.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as tar:
        tar.add(work_dir, arcname=archive_name)
    return archive_path


def _upload_callback():
    """Optional external upload callable (object storage, a cloud drive,
    anything), configured by the host project through
    settings.DB_PORTABILITY_UPLOAD_CALLBACK as a dotted path to
    `callback(path: str, folder_id) -> dict`. None disables the upload
    step, leaving only the local copy. No cloud SDK is imported by this
    module; whoever configures the callback brings their own dependency.
    """
    dotted_path = getattr(settings, 'DB_PORTABILITY_UPLOAD_CALLBACK', None)
    return import_string(dotted_path) if dotted_path else None


def upload_bundle_with_fallback(bundle_path, folder_id=None):
    """Try to upload bundle_path through the configured callback. Any
    failure, or no callback configured, falls back to the local copy --
    which was already written before this call and is never removed, so a
    broken upload can never cost an extraction.
    """
    callback = _upload_callback()
    if callback is None:
        return {
            "status": "local_fallback", "local_path": str(bundle_path),
            "error": "DB_PORTABILITY_UPLOAD_CALLBACK is not configured",
        }

    try:
        result = callback(str(bundle_path), folder_id=folder_id)
    except Exception as exc:
        logger.warning("Upload failed, keeping the local copy: %s", exc)
        return {"status": "local_fallback", "local_path": str(bundle_path), "error": str(exc)}

    return {**result, "status": "uploaded", "local_path": str(bundle_path)}


def extract_bundle_to_workdir(input_path):
    """Accept a .tar.gz or an already-extracted directory; return the
    directory holding manifest.json + data/.
    """
    input_path = Path(input_path)
    if input_path.is_dir():
        return input_path

    work_dir = Path(tempfile.mkdtemp(prefix='db-portability-import-'))
    try:
        with tarfile.open(input_path, 'r:gz') as tar:
            tar.extractall(work_dir, filter='data')
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"Invalid or corrupted package: {exc}") from exc

    entries = list(work_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return work_dir


def validate_bundle(work_dir):
    """Validate manifest.json and the presence of every declared
    data/<app>.json. Returns the manifest.
    """
    work_dir = Path(work_dir)
    manifest_path = work_dir / 'manifest.json'
    if not manifest_path.exists():
        raise ValueError("Invalid package: manifest.json is missing.")

    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Invalid package: manifest.json is corrupted ({exc}).") from exc

    if manifest.get('format_version') != BUNDLE_FORMAT_VERSION:
        raise ValueError(
            f"Unsupported package format version: {manifest.get('format_version')!r} "
            f"(expected {BUNDLE_FORMAT_VERSION!r})."
        )

    apps_list = manifest.get('apps') or []
    if not apps_list:
        raise ValueError("Invalid package: the manifest declares no apps.")

    for app_label in apps_list:
        data_path = work_dir / 'data' / f'{app_label}.json'
        if not data_path.exists():
            raise ValueError(f"Invalid package: data file missing for '{app_label}'.")

    return manifest


def _iter_bundle_objects(work_dir, manifest):
    for app_label in manifest['apps']:
        data_path = Path(work_dir) / 'data' / f'{app_label}.json'
        with data_path.open('r', encoding='utf-8') as fh:
            entries = json.load(fh)
        for entry in entries:
            yield entry['model'], entry['pk']


def check_destination_conflicts(work_dir, manifest):
    """Return the [{"model", "pk"}] already present in the destination,
    checked per individual primary key rather than per whole table -- so
    importing one department's package is not blocked by another
    department's rows already living in the same destination.
    """
    conflicts = []
    for model_label, pk in _iter_bundle_objects(work_dir, manifest):
        try:
            model = apps.get_model(model_label)
        except LookupError:
            continue
        if model._base_manager.filter(pk=pk).exists():
            conflicts.append({"model": model_label, "pk": pk})
    return conflicts


def ensure_migrations_applied():
    """Apply the destination's pending migrations, if any -- always
    automatic, with no flag, so an import never requires a separate manual
    schema step. Returns True when something was applied.
    """
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        return False
    call_command('migrate', verbosity=0, interactive=False)
    return True


def load_bundle(work_dir, manifest):
    """Load every data/*.json (in manifest['apps'] order, so dependencies
    precede the rows referencing them) through loaddata, inside a single
    atomic transaction. Returns the loaded counts, echoed from the
    manifest as confirmation.
    """
    data_files = [str(Path(work_dir) / 'data' / f'{app_label}.json') for app_label in manifest['apps']]
    with transaction.atomic():
        call_command('loaddata', *data_files, verbosity=0)
    return dict(manifest.get('counts') or {})


def write_import_log(*, source_manifest, target_engine, migrations_applied, status,
                      started_at, finished_at, counts_loaded=None,
                      abort_reason=None, conflicting_objects=None):
    log = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "source_manifest": source_manifest,
        "target_engine": target_engine,
        "migrations_applied": migrations_applied,
        "status": status,
        "counts_loaded": counts_loaded or {},
        "abort_reason": abort_reason,
        "conflicting_objects": conflicting_objects or [],
    }
    output_dir = default_output_dir()
    ts = finished_at.strftime('%Y%m%dT%H%M%SZ')
    log_path = output_dir / f'import-log-{ts}.json'
    log_path.write_text(json.dumps(log, indent=2), encoding='utf-8')
    return log_path, log
