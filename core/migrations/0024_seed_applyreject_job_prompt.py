from django.db import migrations


def seed_applyreject_job_prompt(apps, schema_editor):
    PromptTemplate = apps.get_model("core", "PromptTemplate")

    try:
        from core.constants import SYSTEM_PROMPT
    except Exception:
        SYSTEM_PROMPT = ""

    content = (SYSTEM_PROMPT or "").strip()
    if not content:
        return

    prompt, created = PromptTemplate.objects.get_or_create(
        purpose="job_filter",
        name="applyreject_job",
        defaults={
            "content": content,
            "is_active": True,
            "notes": "APPLY/REJECT classifier prompt used by manual LinkedIn import and Apify import filtering.",
        },
    )
    if created:
        PromptTemplate.objects.filter(purpose="job_filter", is_active=True).exclude(id=prompt.id).update(is_active=False)


def unseed_applyreject_job_prompt(apps, schema_editor):
    PromptTemplate = apps.get_model("core", "PromptTemplate")
    PromptTemplate.objects.filter(purpose="job_filter", name="applyreject_job").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_company_apollo_stats"),
    ]

    operations = [
        migrations.RunPython(seed_applyreject_job_prompt, unseed_applyreject_job_prompt),
    ]
