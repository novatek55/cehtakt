from django.db import migrations


def materials_not_producible(apps, schema_editor):
    """Материалы (закупаемые, buy=True) не производятся — снимаем make."""
    Process = apps.get_model('catalog', 'Process')
    Process.objects.filter(buy=True).update(make=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0002_operation_is_parallel'),
    ]

    operations = [
        migrations.RunPython(materials_not_producible, noop),
    ]
