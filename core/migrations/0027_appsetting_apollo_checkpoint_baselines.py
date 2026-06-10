from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_appsetting_apollo_dashboard_credits_used"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="apollo_checkpoint_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="apollo_checkpoint_local_unique_emails",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="apollo_checkpoint_today_logged_credits",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="apollo_checkpoint_today_logged_emails",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="apollo_checkpoint_today_not_converted",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
