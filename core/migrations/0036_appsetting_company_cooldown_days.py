from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0035_inboxscanevent_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="company_cooldown_days",
            field=models.PositiveIntegerField(
                default=10,
                help_text="Global company cooldown in days for imports and manual LinkedIn flow.",
            ),
        ),
    ]
