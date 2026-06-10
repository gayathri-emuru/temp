from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_alter_appsetting_max_people_per_company"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobposting",
            name="is_manual_email_job",
            field=models.BooleanField(default=False, db_index=True),
        ),
    ]
