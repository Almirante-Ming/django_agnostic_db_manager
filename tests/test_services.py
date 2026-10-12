"""Service-layer regression suite for db_portability.services.

Collected by a plain `manage.py test` in any project that installed
this app's fixture apps, and runnable with no host project at all via
`python db_portability/tests/runtests.py`. See base.py for both.

What this suite is for: the app discovers the host project's shape by
introspection and reads the rest from settings, so the things most likely
to break on a future change are (a) the discovery rules, (b) the
boundaries of a per-department package, and (c) the refusals that keep a
destination from being half-written. Those are what is pinned here,
against fixture models the app knows nothing about.
"""

import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

from django.test import override_settings

from db_portability import services as svc
from db_portability.tests.base import PortabilityTestCase, require_fixture_apps

# Importing the fixture models requires their apps to be installed, so the
# skip check has to run first -- hence the imports below the call.
require_fixture_apps()

from db_portability.tests.people.models import (  # noqa: E402
    Account, Announcement, BrandingConfig, Person,
)
from db_portability.tests.plain.models import PlainRecord  # noqa: E402
from db_portability.tests.sales.models import SalesNote, SalesPerson  # noqa: E402
from db_portability.tests.support.models import SupportPerson, SupportTicket  # noqa: E402


def _tempdir(test):
    path = Path(tempfile.mkdtemp(prefix='db-portability-test-'))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class SettingsReadingTests(PortabilityTestCase):
    """Every knob is optional and read at call time, never cached at import."""

    def test_valid_departments_reads_settings(self):
        self.assertEqual(svc.valid_departments(), ['sales', 'support', 'plain'])

    @override_settings(DB_PORTABILITY_DEPARTMENTS=None)
    def test_valid_departments_empty_when_unset(self):
        self.assertEqual(svc.valid_departments(), [])

    @override_settings(DB_PORTABILITY_DEPARTMENTS=['sales'])
    def test_valid_departments_follows_a_changed_setting(self):
        self.assertEqual(svc.valid_departments(), ['sales'])

    def test_person_model_resolves_from_dotted_label(self):
        self.assertIs(svc._person_model(), Person)

    @override_settings(DB_PORTABILITY_PERSON_MODEL=None)
    def test_person_model_none_when_unset(self):
        self.assertIsNone(svc._person_model())

    def test_archive_prefix_defaults_without_setting(self):
        self.assertEqual(svc.archive_prefix(), svc.DEFAULT_ARCHIVE_PREFIX)

    @override_settings(DB_PORTABILITY_ARCHIVE_PREFIX='acme-backup')
    def test_archive_prefix_honors_setting(self):
        self.assertEqual(svc.archive_prefix(), 'acme-backup')


class ResolveIncludedAppsTests(PortabilityTestCase):

    def test_full_scope_includes_apps_with_models(self):
        included = svc.resolve_included_apps()
        for app_label in ('people', 'sales', 'support', 'plain'):
            self.assertIn(app_label, included)

    def test_full_scope_excludes_framework_bookkeeping_apps(self):
        included = svc.resolve_included_apps()
        for excluded in svc.DEFAULT_EXCLUDED_APPS:
            self.assertNotIn(excluded, included)

    def test_full_scope_excludes_apps_without_models(self):
        # the app itself ships no models, so it must never try to dump itself
        self.assertNotIn('db_portability', svc.resolve_included_apps())

    def test_department_scope_is_shared_app_then_department(self):
        self.assertEqual(svc.resolve_included_apps(department='sales'), ['people', 'sales'])

    @override_settings(DB_PORTABILITY_SHARED_APP=None)
    def test_department_scope_without_shared_app_is_department_only(self):
        self.assertEqual(svc.resolve_included_apps(department='sales'), ['sales'])

    def test_unknown_department_is_refused(self):
        with self.assertRaises(ValueError):
            svc.resolve_included_apps(department='marketing')


class PersonModelDiscoveryTests(PortabilityTestCase):
    """The MTI subclass and the fields pointing at it are found by
    introspection; no naming convention, no hand-maintained map.
    """

    def test_finds_the_departments_mti_subclass(self):
        self.assertIs(svc._department_person_model('sales', Person), SalesPerson)
        self.assertIs(svc._department_person_model('support', Person), SupportPerson)

    def test_returns_none_for_department_without_a_subclass(self):
        self.assertIsNone(svc._department_person_model('plain', Person))

    def test_discovers_fields_referencing_the_person_model(self):
        found = {(model, field) for model, field in svc._person_referencing_fields(Person)}
        self.assertIn((Account, 'person'), found)
        # FKs onto a SUBCLASS count too -- they still reach a person row
        self.assertIn((SalesNote, 'author'), found)
        self.assertIn((SupportTicket, 'assignee'), found)

    def test_ignores_the_implicit_mti_parent_link(self):
        # SalesPerson.person_ptr is the inherited pk, not a reference that
        # appears in serialized `fields`; including it would corrupt dumps.
        found = {(model, field) for model, field in svc._person_referencing_fields(Person)}
        self.assertNotIn((SalesPerson, 'person_ptr'), found)

    def test_ignores_models_unrelated_to_the_person_model(self):
        found_models = {model for model, _field in svc._person_referencing_fields(Person)}
        self.assertNotIn(Announcement, found_models)
        self.assertNotIn(PlainRecord, found_models)


class DepartmentClosureTests(PortabilityTestCase):

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana', quota=10)
        self.support_person = SupportPerson.objects.create(name='Eli', shift='night')
        self.sales_account = Account.objects.create(person=self.sales_person, username='dana')
        self.support_account = Account.objects.create(person=self.support_person, username='eli')
        BrandingConfig.objects.create(department='sales', headline='Sales HQ')
        BrandingConfig.objects.create(department='support', headline='Support HQ')
        self.announcement = Announcement.objects.create(title='All hands')

    def _closure_models(self, department):
        return {qs.model: qs for qs in svc.resolve_department_closure(department)}

    def test_includes_only_that_departments_person_rows(self):
        querysets = self._closure_models('sales')
        pks = set(querysets[Person].values_list('pk', flat=True))
        self.assertIn(self.sales_person.pk, pks)
        self.assertNotIn(self.support_person.pk, pks)

    def test_includes_only_accounts_of_that_departments_people(self):
        querysets = self._closure_models('sales')
        pks = set(querysets[Account].values_list('pk', flat=True))
        self.assertIn(self.sales_account.pk, pks)
        self.assertNotIn(self.support_account.pk, pks)

    def test_scoped_model_filtered_by_the_department_label(self):
        querysets = self._closure_models('sales')
        rows = list(querysets[BrandingConfig])
        self.assertEqual([row.department for row in rows], ['sales'])

    def test_departmentless_shared_rows_are_never_included(self):
        querysets = self._closure_models('sales')
        self.assertNotIn(Announcement, querysets)

    def test_person_rows_precede_the_rows_referencing_them(self):
        # load order inside the shared file depends on this ordering
        models_in_order = [qs.model for qs in svc.resolve_department_closure('sales')]
        self.assertLess(models_in_order.index(Person), models_in_order.index(Account))

    def test_department_without_a_person_subclass_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            svc.resolve_department_closure('plain')
        self.assertIn('plain', str(ctx.exception))

    def test_unknown_department_is_refused(self):
        with self.assertRaises(ValueError):
            svc.resolve_department_closure('marketing')

    @override_settings(DB_PORTABILITY_PERSON_MODEL=None)
    def test_scoped_models_still_apply_without_a_person_model(self):
        querysets = self._closure_models('sales')
        self.assertEqual(set(querysets), {BrandingConfig})

    @override_settings(DB_PORTABILITY_DEPARTMENT_SCOPED_MODELS={})
    def test_scoped_models_are_optional(self):
        querysets = self._closure_models('sales')
        self.assertNotIn(BrandingConfig, querysets)
        self.assertIn(Person, querysets)


class ManifestTests(PortabilityTestCase):

    def test_records_format_version_scope_apps_and_counts(self):
        manifest = svc.build_manifest({'type': 'full'}, ['people'], {'people.Person': 2})
        self.assertEqual(manifest['format_version'], svc.BUNDLE_FORMAT_VERSION)
        self.assertEqual(manifest['scope'], {'type': 'full'})
        self.assertEqual(manifest['apps'], ['people'])
        self.assertEqual(manifest['counts'], {'people.Person': 2})
        self.assertEqual(manifest['excluded_apps'], sorted(svc.DEFAULT_EXCLUDED_APPS))
        self.assertIn('created_at', manifest)
        self.assertIn('source_engine', manifest)

    def test_migrations_field_defaults_to_empty(self):
        manifest = svc.build_manifest({'type': 'full'}, ['people'], {})
        self.assertEqual(manifest['migrations'], {})

    def test_migrations_field_echoes_captured_state(self):
        manifest = svc.build_manifest(
            {'type': 'full'}, ['auth'], {}, migrations={'auth': ['0012_alter_user_first_name_max_length']},
        )
        self.assertEqual(manifest['migrations'], {'auth': ['0012_alter_user_first_name_max_length']})


class MigrationStateTests(PortabilityTestCase):
    """The schema-version stamp an import checks compatibility against."""

    def test_records_applied_leaves_for_apps_that_have_migrations(self):
        state = svc.capture_migration_state(['auth'])
        self.assertIn('auth', state)
        self.assertTrue(state['auth'])
        self.assertTrue(all(isinstance(name, str) for name in state['auth']))

    def test_apps_without_migrations_are_absent_rather_than_empty(self):
        self.assertEqual(svc.capture_migration_state(['people', 'sales']), {})

    def test_missing_or_empty_state_is_treated_as_compatible(self):
        self.assertEqual(svc.check_migration_compatibility({}), [])
        self.assertEqual(svc.check_migration_compatibility({'migrations': {}}), [])

    def test_state_captured_here_is_compatible_here(self):
        state = svc.capture_migration_state(['auth'])
        self.assertEqual(svc.check_migration_compatibility({'migrations': state}), [])

    def test_unknown_migration_is_reported(self):
        unknown = svc.check_migration_compatibility(
            {'migrations': {'auth': ['9999_from_the_future']}},
        )
        self.assertEqual(unknown, ['auth.9999_from_the_future'])

    def test_every_unknown_migration_is_reported_not_just_the_first(self):
        unknown = svc.check_migration_compatibility({'migrations': {
            'auth': ['9999_from_the_future'],
            'contenttypes': ['9998_also_unknown'],
        }})
        self.assertCountEqual(
            unknown, ['auth.9999_from_the_future', 'contenttypes.9998_also_unknown'],
        )

    def test_unknown_app_entirely_is_reported(self):
        unknown = svc.check_migration_compatibility(
            {'migrations': {'app_that_does_not_exist': ['0001_initial']}},
        )
        self.assertEqual(unknown, ['app_that_does_not_exist.0001_initial'])


class DumpAppsToBundleTests(PortabilityTestCase):

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana')
        self.support_person = SupportPerson.objects.create(name='Eli')
        BrandingConfig.objects.create(department='sales', headline='Sales HQ')

    def test_full_mode_writes_one_file_per_included_app(self):
        work_dir = _tempdir(self)
        manifest = svc.dump_apps_to_bundle(work_dir)
        self.assertEqual(manifest['scope'], {'type': 'full'})
        for app_label in manifest['apps']:
            self.assertTrue((work_dir / 'data' / f'{app_label}.json').exists())

    def test_full_mode_counts_every_model_including_empty_tables(self):
        work_dir = _tempdir(self)
        manifest = svc.dump_apps_to_bundle(work_dir)
        self.assertEqual(manifest['counts']['people.Person'], 2)
        self.assertEqual(manifest['counts']['people.Announcement'], 0)

    def test_department_mode_writes_only_shared_and_department_files(self):
        work_dir = _tempdir(self)
        svc.dump_apps_to_bundle(work_dir, department='sales')
        written = sorted(p.name for p in (work_dir / 'data').glob('*.json'))
        self.assertEqual(written, ['people.json', 'sales.json'])

    def test_department_mode_shared_file_holds_only_that_department(self):
        work_dir = _tempdir(self)
        svc.dump_apps_to_bundle(work_dir, department='sales')
        entries = json.loads((work_dir / 'data' / 'people.json').read_text())
        person_pks = {str(e['pk']) for e in entries if e['model'] == 'people.person'}
        self.assertIn(str(self.sales_person.pk), person_pks)
        self.assertNotIn(str(self.support_person.pk), person_pks)

    def test_department_mode_records_the_department_in_the_scope(self):
        work_dir = _tempdir(self)
        manifest = svc.dump_apps_to_bundle(work_dir, department='sales')
        self.assertEqual(manifest['scope'], {'type': 'department', 'department': 'sales'})

    def test_unknown_department_writes_nothing(self):
        work_dir = _tempdir(self)
        with self.assertRaises(ValueError):
            svc.dump_apps_to_bundle(work_dir, department='marketing')
        self.assertEqual(list((work_dir / 'data').glob('*.json')), [])


class BundleArchiveTests(PortabilityTestCase):

    def setUp(self):
        SalesPerson.objects.create(name='Dana')

    def _bundle(self):
        work_dir = _tempdir(self)
        svc.dump_apps_to_bundle(work_dir, department='sales')
        return work_dir

    def test_archive_is_named_with_the_configured_prefix(self):
        out_dir = _tempdir(self)
        with override_settings(DB_PORTABILITY_ARCHIVE_PREFIX='acme-backup'):
            archive = svc.create_bundle_archive(self._bundle(), output_dir=out_dir)
        self.assertTrue(archive.name.startswith('acme-backup-'))
        self.assertTrue(archive.name.endswith('.tar.gz'))

    def test_archive_contains_the_manifest_and_data(self):
        out_dir = _tempdir(self)
        archive = svc.create_bundle_archive(self._bundle(), output_dir=out_dir)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertTrue(any(n.endswith('manifest.json') for n in names))
        self.assertTrue(any(n.endswith('data/people.json') for n in names))

    def test_round_trips_back_out_of_the_archive(self):
        out_dir = _tempdir(self)
        archive = svc.create_bundle_archive(self._bundle(), output_dir=out_dir)
        work_dir = svc.extract_bundle_to_workdir(archive)
        manifest = svc.validate_bundle(work_dir)
        self.assertEqual(manifest['scope']['department'], 'sales')


class ExtractBundleToWorkdirTests(PortabilityTestCase):

    def test_accepts_an_already_extracted_directory(self):
        work_dir = _tempdir(self)
        self.assertEqual(svc.extract_bundle_to_workdir(work_dir), work_dir)

    def test_refuses_a_file_that_is_not_a_tarball(self):
        broken = _tempdir(self) / 'broken.tar.gz'
        broken.write_bytes(b'this is not a tarball')
        with self.assertRaises(ValueError):
            svc.extract_bundle_to_workdir(broken)


class ValidateBundleTests(PortabilityTestCase):

    def test_missing_manifest_is_refused(self):
        with self.assertRaises(ValueError):
            svc.validate_bundle(_tempdir(self))

    def test_corrupted_manifest_is_refused(self):
        work_dir = _tempdir(self)
        (work_dir / 'manifest.json').write_text('{ not json')
        with self.assertRaises(ValueError):
            svc.validate_bundle(work_dir)

    def test_unsupported_format_version_is_refused(self):
        work_dir = _tempdir(self)
        (work_dir / 'manifest.json').write_text(json.dumps(
            {'format_version': '999', 'apps': ['people']},
        ))
        with self.assertRaises(ValueError):
            svc.validate_bundle(work_dir)

    def test_manifest_without_apps_is_refused(self):
        work_dir = _tempdir(self)
        (work_dir / 'manifest.json').write_text(json.dumps(
            {'format_version': svc.BUNDLE_FORMAT_VERSION, 'apps': []},
        ))
        with self.assertRaises(ValueError):
            svc.validate_bundle(work_dir)

    def test_declared_but_missing_data_file_is_refused(self):
        work_dir = _tempdir(self)
        (work_dir / 'manifest.json').write_text(json.dumps(
            {'format_version': svc.BUNDLE_FORMAT_VERSION, 'apps': ['people']},
        ))
        with self.assertRaises(ValueError):
            svc.validate_bundle(work_dir)


class DestinationConflictTests(PortabilityTestCase):

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana')

    def _bundle(self, department):
        work_dir = _tempdir(self)
        manifest = svc.dump_apps_to_bundle(work_dir, department=department)
        return work_dir, manifest

    def test_detects_a_primary_key_already_present(self):
        work_dir, manifest = self._bundle('sales')
        conflicts = svc.check_destination_conflicts(work_dir, manifest)
        self.assertTrue(any(str(c['pk']) == str(self.sales_person.pk) for c in conflicts))

    def test_no_conflict_once_the_rows_are_gone(self):
        work_dir, manifest = self._bundle('sales')
        SalesPerson.objects.all().delete()
        Person.objects.all().delete()
        self.assertEqual(svc.check_destination_conflicts(work_dir, manifest), [])

    def test_another_departments_rows_are_not_a_conflict(self):
        support_person = SupportPerson.objects.create(name='Eli')
        work_dir, manifest = self._bundle('support')
        conflicting = {str(c['pk']) for c in svc.check_destination_conflicts(work_dir, manifest)}
        self.assertIn(str(support_person.pk), conflicting)
        self.assertNotIn(str(self.sales_person.pk), conflicting)


class EnsureMigrationsAppliedTests(PortabilityTestCase):

    def test_is_a_noop_when_nothing_is_pending(self):
        with mock.patch('db_portability.services.call_command') as mocked:
            self.assertFalse(svc.ensure_migrations_applied())
        mocked.assert_not_called()

    def test_runs_migrate_when_something_is_pending(self):
        with mock.patch.object(svc.MigrationExecutor, 'migration_plan', return_value=[object()]), \
                mock.patch('db_portability.services.call_command') as mocked:
            self.assertTrue(svc.ensure_migrations_applied())
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[0], 'migrate')


def _fake_upload(path, folder_id=None):
    _fake_upload.calls.append((path, folder_id))
    return {'file_id': 'abc123', 'web_view_link': 'https://example.invalid/abc123'}


_fake_upload.calls = []


def _failing_upload(path, folder_id=None):
    raise RuntimeError('upstream exploded')


class UploadCallbackTests(PortabilityTestCase):
    """The upload hook must stay optional and never cost an extraction."""

    def setUp(self):
        _fake_upload.calls = []
        self.bundle = _tempdir(self) / 'bundle.tar.gz'
        self.bundle.write_bytes(b'not really a tarball, never opened here')

    def test_no_callback_configured_falls_back_to_local(self):
        result = svc.upload_bundle_with_fallback(self.bundle)
        self.assertEqual(result['status'], 'local_fallback')
        self.assertEqual(result['local_path'], str(self.bundle))

    @override_settings(
        DB_PORTABILITY_UPLOAD_CALLBACK='db_portability.tests.test_services._fake_upload',
    )
    def test_configured_callback_is_resolved_and_called(self):
        result = svc.upload_bundle_with_fallback(self.bundle, folder_id='folder-9')
        self.assertEqual(result['status'], 'uploaded')
        self.assertEqual(result['web_view_link'], 'https://example.invalid/abc123')
        self.assertEqual(result['local_path'], str(self.bundle))
        self.assertEqual(_fake_upload.calls, [(str(self.bundle), 'folder-9')])

    @override_settings(
        DB_PORTABILITY_UPLOAD_CALLBACK='db_portability.tests.test_services._failing_upload',
    )
    def test_a_raising_callback_falls_back_to_local(self):
        with self.assertLogs('db_portability.services', level='WARNING'):
            result = svc.upload_bundle_with_fallback(self.bundle)
        self.assertEqual(result['status'], 'local_fallback')
        self.assertIn('upstream exploded', result['error'])
        self.assertTrue(self.bundle.exists(), 'the local copy must survive a failed upload')


class LoadBundleTests(PortabilityTestCase):

    def test_reinstates_rows_and_their_references(self):
        person = SalesPerson.objects.create(name='Dana', quota=7)
        note = SalesNote.objects.create(author=person, body='keep me')
        Account.objects.create(person=person, username='dana')

        work_dir = _tempdir(self)
        manifest = svc.dump_apps_to_bundle(work_dir, department='sales')

        SalesNote.objects.all().delete()
        Account.objects.all().delete()
        SalesPerson.objects.all().delete()
        Person.objects.all().delete()

        counts = svc.load_bundle(work_dir, manifest)

        self.assertEqual(counts, manifest['counts'])
        self.assertTrue(SalesPerson.objects.filter(pk=person.pk).exists())
        self.assertEqual(SalesPerson.objects.get(pk=person.pk).quota, 7)
        restored_note = SalesNote.objects.get(pk=note.pk)
        self.assertEqual(restored_note.author_id, person.pk)
        self.assertEqual(Account.objects.get(person_id=person.pk).username, 'dana')


class ImportLogTests(PortabilityTestCase):

    def test_writes_a_log_naming_the_outcome_and_the_conflicts(self):
        from datetime import datetime, timezone
        started = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)

        log_path, log = svc.write_import_log(
            source_manifest={'scope': {'type': 'full'}},
            target_engine='sqlite',
            migrations_applied=True,
            status='aborted',
            started_at=started,
            finished_at=finished,
            abort_reason='conflict',
            conflicting_objects=[{'model': 'people.person', 'pk': 1}],
        )

        self.assertTrue(log_path.exists())
        self.assertEqual(log['status'], 'aborted')
        self.assertEqual(log['abort_reason'], 'conflict')
        self.assertEqual(log['conflicting_objects'], [{'model': 'people.person', 'pk': 1}])
        self.assertEqual(json.loads(log_path.read_text())['target_engine'], 'sqlite')
