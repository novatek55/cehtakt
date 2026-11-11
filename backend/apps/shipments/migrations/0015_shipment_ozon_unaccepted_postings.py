from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipments', '0014_ozon_warehouse_selection'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='ozon_unaccepted_postings',
            field=models.JSONField(
                blank=True, default=list,
                help_text='Номера закрытых в Proplan Ozon-отправлений, которые Ozon всё ещё ждёт',
            ),
        ),
    ]
