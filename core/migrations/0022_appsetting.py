from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_companyrecruiter_apollo_linkedin_url"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppSetting",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "max_people_per_company",
                    models.PositiveIntegerField(
                        default=20,
                        help_text="Global cap: max recruiters fetched and emails sent per company.",
                    ),
                ),
            ],
            options={
                "verbose_name": "App Setting",
            },
        ),
    ]
