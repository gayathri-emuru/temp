from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_company_linkedin_company_slug_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                # Enforce uniqueness when we have strong identifiers.
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_core_company_linkedin_slug_ci "
                "ON core_company (lower(linkedin_company_slug)) "
                "WHERE linkedin_company_slug <> '';",
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_core_company_active_domain_ci "
                "ON core_company (lower(active_domain)) "
                "WHERE active_domain <> '';",
            ],
            reverse_sql=[
                "DROP INDEX IF EXISTS uniq_core_company_active_domain_ci;",
                "DROP INDEX IF EXISTS uniq_core_company_linkedin_slug_ci;",
            ],
        ),
    ]

