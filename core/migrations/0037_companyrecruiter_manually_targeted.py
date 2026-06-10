from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_appsetting_company_cooldown_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyrecruiter",
            name="manually_targeted",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
