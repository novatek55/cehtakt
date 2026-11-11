from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0008_shipment_scan_finished_at_shipmentscan'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipmentline',
            name='external_trbx_barcode',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
