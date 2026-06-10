from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_companyrecruiter_manually_targeted"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="email_generation_provider",
            field=models.CharField(default="openai", max_length=20),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="openai_email_model",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="anthropic_email_model",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
