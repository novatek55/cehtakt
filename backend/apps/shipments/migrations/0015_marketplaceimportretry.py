# Generated manually for the WB return import retry queue.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0014_ozon_warehouse_selection'),
    ]

    operations = [
        migrations.CreateModel(
            name='MarketplaceImportRetry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=32, unique=True)),
                ('next_attempt_at', models.DateTimeField()),
                ('last_error', models.TextField(blank=True)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_succeeded_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['next_attempt_at', 'id']},
        ),
    ]
