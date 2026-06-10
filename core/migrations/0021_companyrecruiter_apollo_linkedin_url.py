from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_jobposting_is_manual_import"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyrecruiter",
            name="apollo_linkedin_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
    ]
