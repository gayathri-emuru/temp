from django.db import migrations, models
import django.db.models.deletion


def update_applyreject_job_prompt(apps, schema_editor):
    PromptTemplate = apps.get_model("core", "PromptTemplate")
    try:
        from core.services.openai_filter_service import DEFAULT_JOB_FILTER_SYSTEM_PROMPT
    except Exception:
        DEFAULT_JOB_FILTER_SYSTEM_PROMPT = ""

    content = (DEFAULT_JOB_FILTER_SYSTEM_PROMPT or "").strip()
    if not content:
        return

    prompt, _ = PromptTemplate.objects.update_or_create(
        purpose="job_filter",
        name="applyreject_job",
        defaults={
            "content": content,
            "is_active": True,
            "notes": (
                "Strict APPLY/REJECT classifier prompt used by manual LinkedIn import, "
                "Apify import filtering, and re-filter review runs."
            ),
        },
    )
    PromptTemplate.objects.filter(purpose="job_filter", is_active=True).exclude(id=prompt.id).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_seed_applyreject_job_prompt"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobFilterReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "decision",
                    models.CharField(
                        choices=[("APPLY", "Apply"), ("REJECT", "Reject")],
                        db_index=True,
                        max_length=10,
                    ),
                ),
                ("reason", models.CharField(blank=True, default="", max_length=120)),
                ("raw_output", models.TextField(blank=True, default="")),
                ("prompt_name", models.CharField(blank=True, default="", max_length=120)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending Review"),
                            ("accepted", "Accepted"),
                            ("dismissed", "Dismissed"),
                            ("auto_keep", "Auto Keep"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "daily_batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="filter_reviews",
                        to="core.dailybatch",
                    ),
                ),
                (
                    "job_posting",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="filter_review",
                        to="core.jobposting",
                    ),
                ),
            ],
            options={
                "ordering": ["status", "daily_batch__batch_date", "job_posting__company", "job_posting__title"],
            },
        ),
        migrations.AddIndex(
            model_name="jobfilterreview",
            index=models.Index(fields=["daily_batch", "status"], name="core_jobfil_daily_b_d8ac48_idx"),
        ),
        migrations.AddIndex(
            model_name="jobfilterreview",
            index=models.Index(fields=["decision", "status"], name="core_jobfil_decisio_565abe_idx"),
        ),
        migrations.RunPython(update_applyreject_job_prompt, migrations.RunPython.noop),
    ]
