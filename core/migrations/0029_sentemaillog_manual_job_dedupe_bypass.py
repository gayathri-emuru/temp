from django.db import migrations, models
import django.db.models.query_utils


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_sentemaillog_active_initial_unique"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="sentemaillog",
            name="uniq_real_sent_initial_once",
        ),
        migrations.RemoveConstraint(
            model_name="sentemaillog",
            name="uniq_real_active_initial_once",
        ),
        migrations.AddField(
            model_name="sentemaillog",
            name="bypass_global_dedupe",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="sentemaillog",
            constraint=models.UniqueConstraint(
                condition=django.db.models.query_utils.Q(
                    ("bypass_global_dedupe", False),
                    ("message_type", "initial"),
                    ("send_type", "real"),
                    ("status", "sent"),
                ),
                fields=("to_email",),
                name="uniq_real_sent_initial_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="sentemaillog",
            constraint=models.UniqueConstraint(
                condition=django.db.models.query_utils.Q(
                    ("bypass_global_dedupe", False),
                    ("message_type", "initial"),
                    ("send_type", "real"),
                    ("status__in", ["pending", "sent"]),
                ),
                fields=("to_email",),
                name="uniq_real_active_initial_once",
            ),
        ),
    ]
