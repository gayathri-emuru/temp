from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_appsetting_email_generation_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobposting",
            name="source_platform",
            field=models.CharField(
                choices=[
                    ("linkedin", "LinkedIn"),
                    ("dice", "Dice"),
                    ("external", "External"),
                ],
                db_index=True,
                default="linkedin",
                max_length=30,
            ),
        ),
    ]
