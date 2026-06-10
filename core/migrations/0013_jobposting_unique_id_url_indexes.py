from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_company_unique_slug_domain_indexes"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                # Prevent inserting the same LinkedIn job twice.
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_core_jobposting_external_job_id "
                "ON core_jobposting (external_job_id) "
                "WHERE external_job_id <> '';",
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_core_jobposting_norm_linkedin_url_ci "
                "ON core_jobposting (lower(normalized_linkedin_url)) "
                "WHERE normalized_linkedin_url <> '';",
            ],
            reverse_sql=[
                "DROP INDEX IF EXISTS uniq_core_jobposting_norm_linkedin_url_ci;",
                "DROP INDEX IF EXISTS uniq_core_jobposting_external_job_id;",
            ],
        ),
    ]

