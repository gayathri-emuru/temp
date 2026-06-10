from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0045_dailycompanyreplystop_decision_confidence_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobposting",
            name="manual_job_reference_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
