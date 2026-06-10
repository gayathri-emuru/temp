from pathlib import Path
import os
import sqlite3
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Local dev convenience: allow `.env` to override any pre-set environment values.
# This prevents a stale system/user env var (e.g., EMAIL_SENDING_ENABLED=0) from blocking local toggles.
load_dotenv(BASE_DIR / ".env", override=True)
# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

def _env_bool(name: str, default: str = "0") -> bool:
    value = os.getenv(name, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("SECRET_KEY", "django-initial-local-secret")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DEBUG", "1")

_allowed = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()]

TIME_ZONE = "America/New_York"
USE_TZ = True


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        "DIRS": [BASE_DIR / "templates"],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

try:
    with sqlite3.connect(":memory:") as _sqlite_check_conn:
        _sqlite_check_conn.execute("select json_valid('{\"ok\": true}')").fetchone()
    _SQLITE_HAS_JSON1 = True
except Exception:
    _SQLITE_HAS_JSON1 = False

if not _SQLITE_HAS_JSON1:
    # This local Python 3.8 SQLite build lacks JSON1, so Django 4.2 raises
    # fields.E180 for existing JSONField columns during management commands.
    # The app stores these fields as JSON text and does not need JSON lookups
    # for the local dashboard workflow.
    SILENCED_SYSTEM_CHECKS = ["fields.E180"]


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = "America/New_York"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

RESUME_DIR = BASE_DIR / "resumes"
# Hard rule: always attach THIS resume (fail fast if missing).
DEFAULT_RESUME_PATH = os.getenv(
    "DEFAULT_RESUME_PATH",
    str(BASE_DIR / "resumes" / "Gayathri_Resume.pdf"),
)

# Safety: prevent accidental SMTP sends unless explicitly enabled.
# Set EMAIL_SENDING_ENABLED=1 to allow sends.
EMAIL_SENDING_ENABLED = _env_bool("EMAIL_SENDING_ENABLED", "0")

# Prompt configuration (used by cold-email generation)
COLD_EMAIL_PROMPT_PATH = os.getenv(
    "COLD_EMAIL_PROMPT_PATH",
    str(BASE_DIR / "prompts" / "cold_email_dev_prompt.txt"),
)

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
