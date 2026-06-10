from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_companyrecruiter_source_quality"),
    ]

    operations = [
        migrations.CreateModel(
            name="BlacklistedCompany",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("raw_name_latest", models.CharField(blank=True, default="", max_length=255)),
                ("normalized_name", models.CharField(db_index=True, max_length=255, unique=True)),
                ("canonical_name", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("reason", models.TextField(blank=True, default="")),
                ("source", models.CharField(blank=True, default="zero_usable_recipients", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="blacklist_entries",
                        to="core.company",
                    ),
                ),
            ],
            options={
                "db_table": "blacklisted_companies",
                "ordering": ["normalized_name"],
            },
        ),
    ]
