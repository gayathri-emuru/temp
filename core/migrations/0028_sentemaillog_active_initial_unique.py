from django.db import migrations, models
import django.db.models.query_utils


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_appsetting_apollo_checkpoint_baselines"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="sentemaillog",
            constraint=models.UniqueConstraint(
                condition=django.db.models.query_utils.Q(
                    ("message_type", "initial"),
                    ("send_type", "real"),
                    ("status__in", ["pending", "sent"]),
                ),
                fields=("to_email",),
                name="uniq_real_active_initial_once",
            ),
        ),
    ]
