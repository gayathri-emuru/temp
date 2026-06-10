from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_companyrecruiter_apollo_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companyrecruiter",
            name="apollo_person_id",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="companyrecruiter",
            name="apollo_title",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="companyrecruiter",
            name="apollo_location",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RunSQL(
            sql=[
                "UPDATE core_companyrecruiter SET apollo_person_id = NULL WHERE apollo_person_id = '';",
                "UPDATE core_companyrecruiter SET apollo_title = NULL WHERE apollo_title = '';",
                "UPDATE core_companyrecruiter SET apollo_location = NULL WHERE apollo_location = '';",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

