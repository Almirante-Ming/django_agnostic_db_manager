"""Command-level regression suite for extract_database / import_database.

Run with `python db_portability/selftests/runtests.py` (see runtests.py for
why these files are not named test_*.py).

This suite pins the CLI contract -- flag names, exit behavior, and what
each refusal protects -- plus the round trips end to end. The round trips
run against a single connection: to stand in for "a fresh destination",
they delete the rows locally and re-import, which is what makes
"nothing was written" assertable. Loading into a genuinely different
engine is a deployment concern, not something a unit suite can prove.
"""

import json
import shutil
import tarfile
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from db_portability import services as svc
from db_portability.selftests.people.models import Account, BrandingConfig, Person
from db_portability.selftests.sales.models import SalesNote, SalesPerson
from db_portability.selftests.support.models import SupportPerson


def _tempdir(test):
    path = Path(tempfile.mkdtemp(prefix='db-portability-selftest-cmd-'))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def _run(*args, **kwargs):
    out = StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


def _rewrite_manifest(archive, mutate, into):
    """Unpack `archive`, apply `mutate(manifest)`, repack. Lets the suite
    forge a package that claims things the current code disagrees with --
    the only way to exercise the compatibility refusal honestly.
    """
    work_dir = svc.extract_bundle_to_workdir(archive)
    manifest_path = Path(work_dir) / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    forged = into / 'forged.tar.gz'
    with tarfile.open(forged, 'w:gz') as tar:
        tar.add(work_dir, arcname='forged')
    return forged


class ExtractCommandTests(TestCase):

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana')

    def test_full_extraction_writes_one_archive_and_reports_the_scope(self):
        out_dir = _tempdir(self)
        output = _run('extract_database', '--skip-upload', f'--output-dir={out_dir}')
        archives = list(out_dir.glob('*.tar.gz'))
        self.assertEqual(len(archives), 1)
        self.assertIn('full', output)

    def test_department_extraction_reports_the_department(self):
        out_dir = _tempdir(self)
        output = _run(
            'extract_database', '--department=sales', '--skip-upload', f'--output-dir={out_dir}',
        )
        self.assertEqual(len(list(out_dir.glob('*.tar.gz'))), 1)
        self.assertIn('sales', output)

    def test_counts_are_printed_for_the_operator(self):
        out_dir = _tempdir(self)
        output = _run('extract_database', '--skip-upload', f'--output-dir={out_dir}')
        self.assertIn('people.Person', output)

    def test_unknown_department_is_rejected_by_the_parser(self):
        out_dir = _tempdir(self)
        with self.assertRaises(CommandError):
            call_command(
                'extract_database', '--department=marketing', '--skip-upload',
                f'--output-dir={out_dir}', stdout=StringIO(),
            )
        self.assertEqual(list(out_dir.glob('*.tar.gz')), [])

    def test_department_without_a_person_subclass_writes_no_archive(self):
        out_dir = _tempdir(self)
        with self.assertRaises(CommandError):
            call_command(
                'extract_database', '--department=plain', '--skip-upload',
                f'--output-dir={out_dir}', stdout=StringIO(),
            )
        self.assertEqual(list(out_dir.glob('*.tar.gz')), [])

    def test_archive_prefix_setting_names_the_file(self):
        out_dir = _tempdir(self)
        with override_settings(DB_PORTABILITY_ARCHIVE_PREFIX='acme-backup'):
            _run('extract_database', '--skip-upload', f'--output-dir={out_dir}')
        self.assertTrue(list(out_dir.glob('acme-backup-*.tar.gz')))


class ExtractCommandUploadTests(TestCase):

    def setUp(self):
        SalesPerson.objects.create(name='Dana')
        self.out_dir = _tempdir(self)

    def test_skip_upload_never_touches_the_callback(self):
        with mock.patch('db_portability.services.upload_bundle_with_fallback') as mocked:
            _run('extract_database', '--skip-upload', f'--output-dir={self.out_dir}')
        mocked.assert_not_called()

    def test_without_skip_the_callback_runs_and_receives_the_folder(self):
        with mock.patch(
            'db_portability.management.commands.extract_database.svc.upload_bundle_with_fallback',
            return_value={'status': 'uploaded', 'web_view_link': 'https://example.invalid/x'},
        ) as mocked:
            output = _run(
                'extract_database', f'--output-dir={self.out_dir}',
                '--upload-folder-id=folder-9',
            )
        self.assertEqual(mocked.call_args.kwargs['folder_id'], 'folder-9')
        self.assertIn('https://example.invalid/x', output)

    def test_a_failed_upload_still_leaves_the_local_archive_and_warns(self):
        with mock.patch(
            'db_portability.management.commands.extract_database.svc.upload_bundle_with_fallback',
            return_value={'status': 'local_fallback', 'error': 'no credentials'},
        ):
            output = _run('extract_database', f'--output-dir={self.out_dir}')
        self.assertIn('no credentials', output)
        self.assertEqual(len(list(self.out_dir.glob('*.tar.gz'))), 1)


class ImportCommandTests(TestCase):

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana', quota=4)
        self.support_person = SupportPerson.objects.create(name='Eli')
        BrandingConfig.objects.create(department='sales', headline='Sales HQ')
        self.out_dir = _tempdir(self)

    def _extract(self, department=None, output_dir=None):
        out_dir = output_dir or self.out_dir
        args = ['extract_database', '--skip-upload', f'--output-dir={out_dir}']
        if department:
            args.append(f'--department={department}')
        call_command(*args, stdout=StringIO())
        archives = list(Path(out_dir).glob('*.tar.gz'))
        self.assertEqual(len(archives), 1)
        return archives[0]

    def _drop_sales(self):
        """Remove everything a 'sales' package contains, so a re-import
        lands in a destination that is empty *for that scope* -- which
        includes the scoped BrandingConfig row, not just the people.
        """
        pks = list(SalesPerson.objects.values_list('pk', flat=True))
        SalesNote.objects.filter(author_id__in=pks).delete()
        Account.objects.filter(person_id__in=pks).delete()
        SalesPerson.objects.all().delete()
        Person.objects.filter(pk__in=pks).delete()
        BrandingConfig.objects.filter(department='sales').delete()

    def _drop_support(self):
        pks = list(SupportPerson.objects.values_list('pk', flat=True))
        Account.objects.filter(person_id__in=pks).delete()
        SupportPerson.objects.all().delete()
        Person.objects.filter(pk__in=pks).delete()
        BrandingConfig.objects.filter(department='support').delete()

    def test_full_round_trip_restores_everything(self):
        archive = self._extract()
        self._drop_sales()
        self._drop_support()
        self.assertEqual(Person.objects.count(), 0)

        output = _run('import_database', f'--input={archive}')

        self.assertIn('full', output)
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(SalesPerson.objects.get(pk=self.sales_person.pk).quota, 4)

    def test_department_round_trip_touches_only_that_department(self):
        archive = self._extract(department='sales')
        self._drop_sales()

        _run('import_database', f'--input={archive}')

        self.assertTrue(SalesPerson.objects.filter(pk=self.sales_person.pk).exists())
        self.assertTrue(SupportPerson.objects.filter(pk=self.support_person.pk).exists())

    def test_scope_is_taken_from_the_package_not_from_a_flag(self):
        archive = self._extract(department='sales')
        self._drop_sales()
        output = _run('import_database', f'--input={archive}')
        self.assertIn('sales', output)

    def test_two_departments_import_one_after_the_other(self):
        sales_archive = self._extract(department='sales')
        support_archive = self._extract(
            department='support', output_dir=_tempdir(self),
        )
        self._drop_sales()
        self._drop_support()

        _run('import_database', f'--input={sales_archive}')
        _run('import_database', f'--input={support_archive}')

        self.assertTrue(SalesPerson.objects.filter(pk=self.sales_person.pk).exists())
        self.assertTrue(SupportPerson.objects.filter(pk=self.support_person.pk).exists())

    def test_reimporting_the_same_package_is_refused_and_changes_nothing(self):
        archive = self._extract(department='sales')
        self._drop_sales()
        _run('import_database', f'--input={archive}')
        self.assertEqual(SalesPerson.objects.filter(pk=self.sales_person.pk).count(), 1)

        with self.assertRaises(CommandError) as ctx:
            call_command('import_database', f'--input={archive}', stdout=StringIO())

        self.assertIn('already exist', str(ctx.exception))
        self.assertEqual(SalesPerson.objects.filter(pk=self.sales_person.pk).count(), 1)

    def test_dry_run_writes_nothing(self):
        archive = self._extract(department='sales')
        self._drop_sales()

        output = _run('import_database', f'--input={archive}', '--dry-run')

        self.assertIn('dry-run', output)
        self.assertEqual(SalesPerson.objects.filter(pk=self.sales_person.pk).count(), 0)

    def test_a_corrupted_archive_is_refused(self):
        broken = self.out_dir / 'broken.tar.gz'
        broken.write_bytes(b'not a tarball at all')
        with self.assertRaises(CommandError):
            call_command('import_database', f'--input={broken}', stdout=StringIO())

    def test_an_extracted_directory_is_accepted_as_input(self):
        archive = self._extract(department='sales')
        work_dir = svc.extract_bundle_to_workdir(archive)
        self._drop_sales()

        _run('import_database', f'--input={work_dir}')

        self.assertTrue(SalesPerson.objects.filter(pk=self.sales_person.pk).exists())


class ImportCommandMigrationCompatibilityTests(TestCase):
    """The schema-version guard: a package stamped with migrations this
    code does not have must be refused before anything is written.
    """

    def setUp(self):
        self.sales_person = SalesPerson.objects.create(name='Dana')
        self.out_dir = _tempdir(self)
        call_command(
            'extract_database', '--department=sales', '--skip-upload',
            f'--output-dir={self.out_dir}', stdout=StringIO(),
        )
        self.archive = next(self.out_dir.glob('*.tar.gz'))

    def _drop_sales(self):
        pks = list(SalesPerson.objects.values_list('pk', flat=True))
        SalesPerson.objects.all().delete()
        Person.objects.filter(pk__in=pks).delete()

    def test_package_from_an_unknown_migration_is_refused(self):
        forged = _rewrite_manifest(
            self.archive,
            lambda m: m.__setitem__('migrations', {'auth': ['9999_from_the_future']}),
            _tempdir(self),
        )
        self._drop_sales()

        with self.assertRaises(CommandError) as ctx:
            call_command('import_database', f'--input={forged}', stdout=StringIO())

        self.assertIn('9999_from_the_future', str(ctx.exception))
        self.assertEqual(SalesPerson.objects.filter(pk=self.sales_person.pk).count(), 0)

    def test_refusal_happens_before_the_conflict_check(self):
        # rows still present, so a conflict exists too -- the migration
        # mismatch must be the reported reason, being the more fundamental one
        forged = _rewrite_manifest(
            self.archive,
            lambda m: m.__setitem__('migrations', {'auth': ['9999_from_the_future']}),
            _tempdir(self),
        )
        with self.assertRaises(CommandError) as ctx:
            call_command('import_database', f'--input={forged}', stdout=StringIO())
        self.assertIn('9999_from_the_future', str(ctx.exception))
        self.assertNotIn('already exist', str(ctx.exception))

    def test_package_without_migration_state_still_imports(self):
        # packages predating the field must stay importable
        forged = _rewrite_manifest(
            self.archive, lambda m: m.pop('migrations', None), _tempdir(self),
        )
        self._drop_sales()

        _run('import_database', f'--input={forged}')

        self.assertTrue(SalesPerson.objects.filter(pk=self.sales_person.pk).exists())

    def test_a_package_from_this_code_is_accepted(self):
        self._drop_sales()
        _run('import_database', f'--input={self.archive}')
        self.assertTrue(SalesPerson.objects.filter(pk=self.sales_person.pk).exists())
