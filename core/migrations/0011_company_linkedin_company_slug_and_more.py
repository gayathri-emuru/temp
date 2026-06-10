from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_apifyapikey'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "ALTER TABLE core_company ADD COLUMN linkedin_company_slug varchar(200) NOT NULL DEFAULT '';",
                        "CREATE INDEX IF NOT EXISTS core_company_linkedin_company_slug_idx ON core_company (linkedin_company_slug);",
                    ],
                    reverse_sql=[
                        "DROP INDEX IF EXISTS core_company_linkedin_company_slug_idx;",
                        # SQLite cannot drop columns reliably; leave the column in place on reverse.
                    ],
                ),
                migrations.RunSQL(
                    sql=[
                        "ALTER TABLE core_jobposting ADD COLUMN apify_linkedin_org_slug varchar(200) NOT NULL DEFAULT '';",
                        "CREATE INDEX IF NOT EXISTS core_jobposting_apify_linkedin_org_slug_idx ON core_jobposting (apify_linkedin_org_slug);",
                    ],
                    reverse_sql=[
                        "DROP INDEX IF EXISTS core_jobposting_apify_linkedin_org_slug_idx;",
                        # SQLite cannot drop columns reliably; leave the column in place on reverse.
                    ],
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='company',
                    name='linkedin_company_slug',
                    field=models.CharField(blank=True, db_index=True, default='', max_length=200),
                ),
                migrations.AddField(
                    model_name='jobposting',
                    name='apify_linkedin_org_slug',
                    field=models.CharField(blank=True, db_index=True, default='', max_length=200),
                ),
            ],
        ),
    ]
