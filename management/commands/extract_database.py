import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from db_portability import services as svc


class Command(BaseCommand):
    help = (
        'Extract the configured database (in full, or one department) into a '
        'portable .tar.gz package, without depending on any engine-specific '
        'tooling. See this app\'s README.md.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--department', choices=svc.valid_departments() or None, default=None,
            help='Limit the extraction to a single department (tenant).',
        )
        parser.add_argument(
            '--output-dir', default=None,
            help='Local output directory (default: BASE_DIR/db_extracts).',
        )
        parser.add_argument(
            '--upload', action='store_true', default=False,
            help=(
                'Also send the package through the configured upload callback '
                '(DB_PORTABILITY_UPLOAD_CALLBACK). Off by default: an extraction '
                'writes locally and sends nothing anywhere unless asked.'
            ),
        )
        parser.add_argument(
            '--upload-folder-id', default=None,
            help='Destination folder passed to the upload callback for this run.',
        )

    def handle(self, *args, **options):
        department = options.get('department')
        output_dir = options.get('output_dir')

        with tempfile.TemporaryDirectory(prefix='db-portability-extract-') as tmp:
            work_dir = Path(tmp)
            try:
                manifest = svc.dump_apps_to_bundle(work_dir, department=department)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

            archive_path = svc.create_bundle_archive(
                work_dir, output_dir=Path(output_dir) if output_dir else None,
            )

        scope = manifest['scope']
        scope_label = (
            'full' if scope['type'] == 'full' else f"department: {scope['department']}"
        )
        self.stdout.write(self.style.SUCCESS(f'Extraction complete ({scope_label}).'))
        self.stdout.write(f'Local file: {archive_path}')
        for key, count in manifest['counts'].items():
            self.stdout.write(f'  {key}: {count}')

        if not options.get('upload'):
            return

        result = svc.upload_bundle_with_fallback(
            archive_path, folder_id=options.get('upload_folder_id'),
        )
        if result['status'] == 'uploaded':
            link = result.get('web_view_link') or result.get('file_id')
            self.stdout.write(self.style.SUCCESS(f'Uploaded: {link}'))
        else:
            self.stdout.write(self.style.WARNING(
                f"Upload did not happen ({result.get('error')}); "
                f"kept locally only, at {archive_path}"
            ))
