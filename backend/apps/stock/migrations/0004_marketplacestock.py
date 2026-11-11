from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_backfill_paint_area'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('stock', '0003_stockmovement_job'),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketplaceStock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('ozon', 'Ozon'), ('wb', 'Wildberries')], max_length=16)),
                ('warehouse_id', models.CharField(max_length=64)),
                ('quantity', models.DecimalField(decimal_places=3, default=0, max_digits=16)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='marketplace_stocks', to='catalog.product')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name='marketplacestock',
            constraint=models.UniqueConstraint(fields=('product', 'source', 'warehouse_id'), name='uniq_marketplace_stock_product_source_wh'),
        ),
    ]
