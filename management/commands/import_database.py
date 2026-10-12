from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from db_portability import services as svc


class Command(BaseCommand):
    help = (
        'Import a package produced by extract_database into the configured '
        'database. Takes no department argument: the scope (full, or which '
        'department) comes from the package manifest. Pending migrations are '
        'applied automatically before any data is loaded.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--input', required=True,
            help='Path to the .tar.gz (or extracted directory) from extract_database.',
        )
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help='Validate the package and run the conflict check without writing.',
        )

    def handle(self, *args, **options):
        started_at = datetime.now(timezone.utc)
        input_path = options['input']
        dry_run = options.get('dry_run', False)

        migrations_applied = svc.ensure_migrations_applied()

        try:
            work_dir = svc.extract_bundle_to_workdir(input_path)
            manifest = svc.validate_bundle(work_dir)
        except ValueError as exc:
            finished_at = datetime.now(timezone.utc)
            svc.write_import_log(
                source_manifest={}, target_engine=connection.vendor,
                migrations_applied=migrations_applied, status='aborted',
                started_at=started_at, finished_at=finished_at,
                abort_reason='corrupted_bundle',
            )
            raise CommandError(str(exc)) from exc

        unknown_migrations = svc.check_migration_compatibility(manifest)
        if unknown_migrations:
            finished_at = datetime.now(timezone.utc)
            svc.write_import_log(
                source_manifest=manifest, target_engine=connection.vendor,
                migrations_applied=migrations_applied, status='aborted',
                started_at=started_at, finished_at=finished_at,
                abort_reason='migration_mismatch',
            )
            raise CommandError(
                "Import aborted: the package was extracted with migration(s) this "
                "destination's code does not know (" + ", ".join(unknown_migrations) + "). "
                "Update the destination to a version that includes them before "
                "importing. Nothing was written."
            )

        conflicts = svc.check_destination_conflicts(work_dir, manifest)
        scope = manifest['scope']
        scope_label = (
            'full' if scope['type'] == 'full' else f"department: {scope['department']}"
        )

        if conflicts:
            finished_at = datetime.now(timezone.utc)
            svc.write_import_log(
                source_manifest=manifest, target_engine=connection.vendor,
                migrations_applied=migrations_applied, status='aborted',
                started_at=started_at, finished_at=finished_at,
                abort_reason='conflict', conflicting_objects=conflicts,
            )
            raise CommandError(
                f"Import aborted: {len(conflicts)} record(s) already exist in the "
                f"destination (see the import log for the conflicting PKs). "
                f"Nothing was written."
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[dry-run] Scope: {scope_label}. Nothing was written.'
            ))
            for key, count in manifest['counts'].items():
                self.stdout.write(f'  {key}: {count}')
            return

        counts_loaded = svc.load_bundle(work_dir, manifest)
        finished_at = datetime.now(timezone.utc)
        svc.write_import_log(
            source_manifest=manifest, target_engine=connection.vendor,
            migrations_applied=migrations_applied, status='success',
            started_at=started_at, finished_at=finished_at,
            counts_loaded=counts_loaded,
        )

        self.stdout.write(self.style.SUCCESS(f'Import complete ({scope_label}).'))
        for key, count in counts_loaded.items():
            self.stdout.write(f'  {key}: {count}')
