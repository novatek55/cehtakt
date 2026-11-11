from django.db import migrations, models


def ensure_kind_column(apps, schema_editor):
    scan_model = apps.get_model('shipments', 'ShipmentScan')
    table = scan_model._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(cursor, table)
        }
    if 'kind' in columns:
        return
    field = models.CharField(
        choices=[('item', 'Item'), ('cargo', 'Cargo place')],
        default='item',
        max_length=16,
    )
    field.set_attributes_from_name('kind')
    schema_editor.add_field(scan_model, field)


def dedupe_item_scans(apps, schema_editor):
    scan_model = apps.get_model('shipments', 'ShipmentScan')
    duplicates = (
        scan_model.objects
        .values('shipment_id', 'line_id', 'kind')
        .annotate(keep_id=models.Min('id'), count=models.Count('id'))
        .filter(count__gt=1)
    )
    for row in duplicates:
        scan_model.objects.filter(
            shipment_id=row['shipment_id'],
            line_id=row['line_id'],
            kind=row['kind'],
        ).exclude(id=row['keep_id']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0010_shipmentline_external_label_barcode'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(ensure_kind_column, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='shipmentscan',
                    name='kind',
                    field=models.CharField(
                        choices=[('item', 'Item'), ('cargo', 'Cargo place')],
                        default='item',
                        max_length=16,
                    ),
                ),
            ],
        ),
        migrations.RunPython(dedupe_item_scans, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='shipmentscan',
            constraint=models.UniqueConstraint(
                fields=('shipment', 'line', 'kind'),
                name='uniq_shipment_scan_line_kind',
            ),
        ),
    ]
