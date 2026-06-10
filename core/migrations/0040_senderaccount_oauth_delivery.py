# Generated for Microsoft Graph sender support.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_jobposting_source_platform"),
    ]

    operations = [
        migrations.AlterField(
            model_name="senderaccount",
            name="app_password",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="auth_method",
            field=models.CharField(
                choices=[
                    ("smtp_password", "SMTP password / app password"),
                    ("microsoft_graph", "Microsoft Graph OAuth"),
                ],
                db_index=True,
                default="smtp_password",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="oauth_refresh_token",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="oauth_access_token",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="oauth_token_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="oauth_scope",
            field=models.CharField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="senderaccount",
            name="oauth_token_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
