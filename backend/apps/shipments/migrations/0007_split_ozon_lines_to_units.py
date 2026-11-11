from decimal import Decimal

from django.db import migrations


def split_ozon_lines_to_units(apps, schema_editor):
    ShipmentLine = apps.get_model('shipments', 'ShipmentLine')

    lines = (
        ShipmentLine.objects
        .filter(shipment__source='ozon', quantity__gt=1)
        .select_related('shipment')
        .order_by('id')
    )
    for line in lines:
        quantity = line.quantity or Decimal(0)
        if quantity != quantity.to_integral_value():
            continue
        count = int(quantity)
        if count <= 1:
            continue

        line.quantity = Decimal('1')
        line.save(update_fields=['quantity'])

        copies = []
        for _ in range(count - 1):
            copies.append(ShipmentLine(
                shipment_id=line.shipment_id,
                product_id=line.product_id,
                sku_raw=line.sku_raw,
                name_raw=line.name_raw,
                quantity=Decimal('1'),
                external_posting=line.external_posting,
                external_status=line.external_status,
                external_trbx_id=line.external_trbx_id,
                external_product_id=line.external_product_id,
                external_warehouse=line.external_warehouse,
                manually_edited=line.manually_edited,
                deleted=line.deleted,
            ))
        ShipmentLine.objects.bulk_create(copies)


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0006_shipmentline_external_warehouse'),
    ]

    operations = [
        migrations.RunPython(split_ozon_lines_to_units, migrations.RunPython.noop),
    ]
