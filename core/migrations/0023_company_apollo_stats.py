from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_appsetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="last_apollo_run_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="company",
            name="last_apollo_emails_found",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="company",
            name="last_apollo_credits_consumed",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
