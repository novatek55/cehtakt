from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('directory', '0005_userrole_own_tasks_only')]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='attendance_terminal_user_id',
            field=models.CharField(
                max_length=50, unique=True, blank=True, null=True,
                help_text='ID сотрудника BioTime для личного отчёта посещений',
            ),
        ),
    ]
