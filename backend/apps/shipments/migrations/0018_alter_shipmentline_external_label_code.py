from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0017_shipmentline_external_label_code'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shipmentline',
            name='external_label_code',
            field=models.CharField(blank=True, help_text='Крупные 4 цифры с этикетки WB (partB)', max_length=64),
        ),
    ]
