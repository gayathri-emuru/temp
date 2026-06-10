from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_jobfilterreview_update_prompt"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="apollo_dashboard_credits_used",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Manual lifetime Apollo credits-used number copied from Apollo's dashboard for reconciliation.",
            ),
        ),
    ]
