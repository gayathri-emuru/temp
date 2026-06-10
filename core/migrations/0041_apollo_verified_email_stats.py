# Generated for Apollo verified-vs-unverified email tracking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0040_senderaccount_oauth_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="last_apollo_verified_emails_found",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="company",
            name="last_apollo_unverified_emails_found",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="company",
            name="last_apollo_email_status_counts",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
