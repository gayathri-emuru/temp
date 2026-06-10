from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_companyrecruiter_apollo_fields_nullable"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="legacy",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="companyrecruiter",
            name="legacy",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
