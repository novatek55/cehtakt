from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0007_productattachment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name='product', name='merged_into',
            field=models.ForeignKey(blank=True, editable=False, null=True,
                on_delete=django.db.models.deletion.PROTECT, related_name='merged_duplicates', to='catalog.product'),
        ),
        migrations.CreateModel(
            name='ProductMerge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('revision', models.CharField(max_length=64)),
                ('details', models.JSONField(default=dict)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('source', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='merge_record', to='catalog.product')),
                ('target', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='merge_history', to='catalog.product')),
            ],
            options={'ordering': ['-created_at', '-id']},
        ),
    ]
