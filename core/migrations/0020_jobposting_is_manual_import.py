from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_blacklistedcompany"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobposting",
            name="is_manual_import",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
