from __future__ import annotations

import os

from core.utils import safe_str


EMAIL_AI_PROVIDER_OPENAI = "openai"
EMAIL_AI_PROVIDER_ANTHROPIC = "anthropic"
EMAIL_AI_PROVIDERS = (EMAIL_AI_PROVIDER_OPENAI, EMAIL_AI_PROVIDER_ANTHROPIC)

DEFAULT_OPENAI_EMAIL_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_EMAIL_MODEL = "claude-sonnet-4-6"

OPENAI_EMAIL_MODEL_OPTIONS = (
    "gpt-5.4",
    "gpt-5.1",
    "gpt-5",
    "gpt-4.1",
    "gpt-4.1-mini",
)
ANTHROPIC_EMAIL_MODEL_OPTIONS = (
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
)


def default_openai_email_model() -> str:
    return safe_str(os.getenv("OPENAI_COLD_EMAIL_MODEL")).strip() or DEFAULT_OPENAI_EMAIL_MODEL


def default_anthropic_email_model() -> str:
    return safe_str(os.getenv("ANTHROPIC_COLD_EMAIL_MODEL")).strip() or DEFAULT_ANTHROPIC_EMAIL_MODEL


def normalize_email_ai_provider(provider: str) -> str:
    provider = safe_str(provider).strip().lower()
    return provider if provider in EMAIL_AI_PROVIDERS else EMAIL_AI_PROVIDER_OPENAI


def get_email_ai_generation_settings() -> dict:
    from core.models import AppSetting

    setting = AppSetting.get_solo()
    provider = normalize_email_ai_provider(getattr(setting, "email_generation_provider", "openai"))
    openai_model = safe_str(getattr(setting, "openai_email_model", "")).strip() or default_openai_email_model()
    anthropic_model = safe_str(getattr(setting, "anthropic_email_model", "")).strip() or default_anthropic_email_model()
    selected_model = anthropic_model if provider == EMAIL_AI_PROVIDER_ANTHROPIC else openai_model

    return {
        "provider": provider,
        "provider_label": "Claude" if provider == EMAIL_AI_PROVIDER_ANTHROPIC else "OpenAI",
        "model": selected_model,
        "openai_model": openai_model,
        "anthropic_model": anthropic_model,
        "openai_configured": bool(safe_str(os.getenv("OPENAI_API_KEY")).strip()),
        "anthropic_configured": bool(safe_str(os.getenv("ANTHROPIC_API_KEY")).strip()),
        "openai_model_options": OPENAI_EMAIL_MODEL_OPTIONS,
        "anthropic_model_options": ANTHROPIC_EMAIL_MODEL_OPTIONS,
    }


def save_email_ai_generation_settings(
    *,
    provider: str,
    openai_model: str = "",
    anthropic_model: str = "",
) -> dict:
    from core.models import AppSetting

    provider = normalize_email_ai_provider(provider)
    openai_model = safe_str(openai_model).strip() or default_openai_email_model()
    anthropic_model = safe_str(anthropic_model).strip() or default_anthropic_email_model()

    setting = AppSetting.get_solo()
    setting.email_generation_provider = provider
    setting.openai_email_model = openai_model[:120]
    setting.anthropic_email_model = anthropic_model[:120]
    setting.save(
        update_fields=[
            "email_generation_provider",
            "openai_email_model",
            "anthropic_email_model",
        ]
    )
    return get_email_ai_generation_settings()
