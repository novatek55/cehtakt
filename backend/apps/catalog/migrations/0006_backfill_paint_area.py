from django.db import migrations

from apps.catalog.migrations import _run_paint_backfill


def backfill(apps, schema_editor):
    n = _run_paint_backfill(apps)
    print(f'  paint_area backfill: обновлено {n} материалов (трубы/листы)')


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_backfill_ingredient_option'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
