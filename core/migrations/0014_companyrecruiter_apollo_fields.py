from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_jobposting_unique_id_url_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyrecruiter",
            name="apollo_person_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="apollo_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="apollo_location",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddConstraint(
            model_name="companyrecruiter",
            constraint=models.UniqueConstraint(
                condition=Q(apollo_person_id__isnull=False) & ~Q(apollo_person_id=""),
                fields=("company", "apollo_person_id"),
                name="uniq_company_apollo_person_id",
            ),
        ),
    ]
