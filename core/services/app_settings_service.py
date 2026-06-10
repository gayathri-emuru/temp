from __future__ import annotations


def get_max_people_per_company() -> int:
    from core.models import AppSetting
    return int(AppSetting.get_solo().max_people_per_company or 10)


def set_max_people_per_company(value: int) -> int:
    value = max(1, min(int(value or 10), 100))
    from core.models import AppSetting
    AppSetting.objects.update_or_create(id=1, defaults={"max_people_per_company": value})
    return value


def get_company_cooldown_days() -> int:
    from core.models import AppSetting

    return max(0, int(AppSetting.get_solo().company_cooldown_days or 0))


def set_company_cooldown_days(value: int) -> int:
    from core.models import AppSetting

    value = max(0, min(int(value or 0), 365))
    AppSetting.objects.update_or_create(id=1, defaults={"company_cooldown_days": value})
    return value


def save_pipeline_control_settings(*, max_people_per_company: int, company_cooldown_days: int) -> dict:
    max_people = max(1, min(int(max_people_per_company or 10), 100))
    cooldown_days = set_company_cooldown_days(company_cooldown_days)
    from core.models import AppSetting

    AppSetting.objects.update_or_create(
        id=1,
        defaults={
            "max_people_per_company": max_people,
            "company_cooldown_days": cooldown_days,
        },
    )
    return {"max_people_per_company": max_people, "company_cooldown_days": cooldown_days}


def get_apollo_dashboard_credits_used() -> int:
    from core.models import AppSetting
    return int(AppSetting.get_solo().apollo_dashboard_credits_used or 0)


def set_apollo_dashboard_credits_used(value: int) -> int:
    value = max(0, int(value or 0))
    from core.models import AppSetting
    AppSetting.objects.update_or_create(id=1, defaults={"apollo_dashboard_credits_used": value})
    return value


def save_apollo_credit_checkpoint(
    *,
    dashboard_credits_used: int,
    local_unique_emails: int,
    today_logged_credits: int,
    today_logged_emails: int,
    today_not_converted: int,
):
    from django.utils import timezone
    from core.models import AppSetting

    obj = AppSetting.get_solo()
    obj.apollo_dashboard_credits_used = max(0, int(dashboard_credits_used or 0))
    obj.apollo_checkpoint_date = timezone.localdate()
    obj.apollo_checkpoint_local_unique_emails = max(0, int(local_unique_emails or 0))
    obj.apollo_checkpoint_today_logged_credits = max(0, int(today_logged_credits or 0))
    obj.apollo_checkpoint_today_logged_emails = max(0, int(today_logged_emails or 0))
    obj.apollo_checkpoint_today_not_converted = max(0, int(today_not_converted or 0))
    obj.save(
        update_fields=[
            "apollo_dashboard_credits_used",
            "apollo_checkpoint_date",
            "apollo_checkpoint_local_unique_emails",
            "apollo_checkpoint_today_logged_credits",
            "apollo_checkpoint_today_logged_emails",
            "apollo_checkpoint_today_not_converted",
        ]
    )
    return obj
