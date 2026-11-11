from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0013_marketplace_warehouse_ids'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplacesettings',
            name='ozon_warehouse_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='MarketplaceWarehouse',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('manual', 'Вручную'), ('ozon', 'Ozon'), ('wb', 'Wildberries')], max_length=16)),
                ('warehouse_id', models.CharField(max_length=64)),
                ('name', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(blank=True, max_length=64)),
                ('is_rfbs', models.BooleanField(default=False)),
                ('warehouse_type', models.CharField(blank=True, max_length=64)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['source', 'name', 'warehouse_id'],
            },
        ),
        migrations.AddConstraint(
            model_name='marketplacewarehouse',
            constraint=models.UniqueConstraint(fields=('source', 'warehouse_id'), name='uniq_marketplace_warehouse_source_id'),
        ),
    ]
