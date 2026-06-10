from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_sentemaillog_manual_job_dedupe_bypass"),
    ]

    operations = [
        migrations.AddField(
            model_name="senderaccount",
            name="pause_reason",
            field=models.CharField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="paused_until",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name="sentemaillog",
            index=models.Index(
                fields=["sender_account", "send_type", "status", "sent_at"],
                name="core_sentem_sender__79de49_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="sentemaillog",
            index=models.Index(fields=["send_type", "status", "sent_at"], name="core_sentem_send_ty_3b3c14_idx"),
        ),
        migrations.CreateModel(
            name="SuppressedEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("hard_bounce", "Hard Bounce"),
                            ("bad_address", "Bad Address"),
                            ("opt_out", "Opt Out"),
                            ("complaint", "Complaint"),
                            ("manual", "Manual"),
                        ],
                        default="manual",
                        max_length=30,
                    ),
                ),
                ("source_error", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["email"],
            },
        ),
    ]
