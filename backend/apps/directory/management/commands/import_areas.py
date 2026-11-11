"""Импорт участков из data/areas.json (реальные данные, снятые из proplan).

Upsert по имени: повторный запуск обновляет существующие участки, не плодит дубли.
Запуск: python manage.py import_areas
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.directory.models import Area


class Command(BaseCommand):
    help = 'Импортирует участки из data/areas.json'

    def handle(self, *args, **options):
        path = Path(settings.BASE_DIR) / 'data' / 'areas.json'
        rows = json.loads(path.read_text(encoding='utf-8'))

        created, updated = 0, 0
        for row in rows:
            obj, is_new = Area.objects.update_or_create(
                name=row['name'],
                defaults={
                    'sort_index': row.get('sort_index', 0),
                    'cost_per_hour': row.get('cost_per_hour', 0),
                    'weekly_limit': row.get('weekly_limit'),
                    'default_parallel': row.get('default_parallel', 1),
                    'distribute_jobs': row.get('distribute_jobs', False),
                    'job_restriction_by_ingredients': row.get('job_restriction_by_ingredients', False),
                },
            )
            created += is_new
            updated += not is_new

        self.stdout.write(self.style.SUCCESS(
            f'Участки импортированы: создано {created}, обновлено {updated}, всего в БД {Area.objects.count()}'
        ))
