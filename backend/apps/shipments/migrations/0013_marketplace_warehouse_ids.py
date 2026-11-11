from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0012_alter_shipmentscan_kind'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplacesettings',
            name='ozon_warehouse_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='marketplacesettings',
            name='wb_warehouse_id',
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
