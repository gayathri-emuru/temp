from django.db import migrations


def mark_existing_non_apollo_recruiters_legacy(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    CompanyRecruiter = apps.get_model("core", "CompanyRecruiter")

    legacy_recruiters = CompanyRecruiter.objects.filter(apollo_person_id__isnull=True) | CompanyRecruiter.objects.filter(apollo_person_id="")
    legacy_recruiters = legacy_recruiters.exclude(email__isnull=True).exclude(email="").exclude(email="none")
    legacy_recruiters.update(legacy=True)

    legacy_company_ids = legacy_recruiters.values_list("company_id", flat=True).distinct()
    Company.objects.filter(id__in=legacy_company_ids).update(legacy=True)


def noop_reverse(apps, schema_editor):
    # Do not automatically unset legacy flags on reverse; they may have been manually edited.
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_company_companyrecruiter_legacy"),
    ]

    operations = [
        migrations.RunPython(mark_existing_non_apollo_recruiters_legacy, noop_reverse),
    ]
