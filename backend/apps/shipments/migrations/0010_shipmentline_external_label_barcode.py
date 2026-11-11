from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0009_shipmentline_external_trbx_barcode'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipmentline',
            name='external_label_barcode',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
