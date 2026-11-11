from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0016_merge_0015_marketplace_retry'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipmentline',
            name='external_label_code',
            field=models.CharField(blank=True, help_text='Читаемый код этикетки WB: partA / partB', max_length=64),
        ),
    ]
