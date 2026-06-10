from django.db import migrations, models


def backfill_recruiter_source_quality(apps, schema_editor):
    CompanyRecruiter = apps.get_model("core", "CompanyRecruiter")

    CompanyRecruiter.objects.filter(legacy=True).update(
        source="legacy",
        email_status="legacy",
    )
    CompanyRecruiter.objects.filter(legacy=False).exclude(apollo_person_id__isnull=True).exclude(apollo_person_id="").update(
        source="apollo",
        email_status="unknown",
    )


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_backfill_legacy_recruiters"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyrecruiter",
            name="source",
            field=models.CharField(
                choices=[("unknown", "Unknown"), ("legacy", "Legacy"), ("apollo", "Apollo")],
                db_index=True,
                default="unknown",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="email_status",
            field=models.CharField(blank=True, db_index=True, default="unknown", max_length=30),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="title_match",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="location_match",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(backfill_recruiter_source_quality, noop_reverse),
    ]
