import os
from pathlib import Path
from urllib.parse import urlparse

from celery.schedules import crontab


BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def host_from_url(raw_url: str) -> str:
    return (urlparse(raw_url).hostname or "").strip()


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure")
DEBUG = env_bool("DJANGO_DEBUG", False)
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")
APP_HOST = host_from_url(APP_URL) if APP_URL else ""

_default_allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}
_env_allowed_hosts = set(env_list("ALLOWED_HOSTS"))
if APP_HOST:
    _env_allowed_hosts.add(APP_HOST)
ALLOWED_HOSTS = sorted(_default_allowed_hosts | _env_allowed_hosts | {".ngrok-free.dev"})

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "accounts",
    "expenses",
    "ingestion",
    "ui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "waexp.urls"
WSGI_APPLICATION = "waexp.wsgi.application"
ASGI_APPLICATION = "waexp.asgi.application"

TIME_ZONE = "America/Santiago"
USE_TZ = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "waexp"),
        "USER": os.environ.get("POSTGRES_USER", "waexp"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "waexp"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "accounts.auth_backends.EmailBackend",
]

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "ui" / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "receipts"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "ui" / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "expenses.context_processors.funds_access",
    ]},
}]

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "")
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "sync-rindegastos-catalogs-daily": {
        "task": "expenses.sync_rindegastos_catalogs",
        "schedule": crontab(hour=2, minute=0),
    },
    "sync-rindegastos-uploaded-expenses-daily": {
        "task": "expenses.sync_rindegastos_uploaded_expenses",
        "schedule": crontab(hour=3, minute=0),
    },
    "reconcile-rindegastos-expenses-nightly": {
        "task": "expenses.reconcile_rindegastos_expenses",
        "schedule": crontab(hour=3, minute=0),
    },
    "reconcile-rindegastos-expenses-midday": {
        "task": "expenses.reconcile_rindegastos_expenses",
        "schedule": crontab(hour=12, minute=0),
    },
    "reconcile-rindegastos-expenses-afternoon": {
        "task": "expenses.reconcile_rindegastos_expenses",
        "schedule": crontab(hour=16, minute=0),
    },
    "sync-funds-sources-nightly": {
        "task": "expenses.sync_funds_sources",
        "schedule": crontab(hour=3, minute=0),
    },
    "sync-funds-sources-midday": {
        "task": "expenses.sync_funds_sources",
        "schedule": crontab(hour=12, minute=0),
    },
    "sync-funds-sources-afternoon": {
        "task": "expenses.sync_funds_sources",
        "schedule": crontab(hour=16, minute=0),
    },
    "sync-tax-indicators-daily": {
        "task": "expenses.sync_tax_indicators",
        "schedule": crontab(hour=2, minute=30),
    },
}

RINDEGASTOS_API_BASE_URL = os.environ.get("RINDEGASTOS_API_BASE_URL", "https://api.rindegastos.com/v1").rstrip("/")
RINDEGASTOS_API_TOKEN = os.environ.get("RINDEGASTOS_API_TOKEN", "").strip()
RINDEGASTOS_API_TIMEOUT = int(os.environ.get("RINDEGASTOS_API_TIMEOUT", "20"))
FUNDS_RINDEGASTOS_CACHE_TIMEOUT_SECONDS = int(
    os.environ.get("FUNDS_RINDEGASTOS_CACHE_TIMEOUT_SECONDS", str(24 * 60 * 60))
)
RINDEGASTOS_MARK_INTEGRATION_CODE_ENABLED = (
    os.environ.get("RINDEGASTOS_MARK_INTEGRATION_CODE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
)
RINDEGASTOS_CORE_BASE_URL = os.environ.get("RINDEGASTOS_CORE_BASE_URL", "https://prod-core.rindegastos.com").rstrip("/")
RINDEGASTOS_CORE_TOKEN = os.environ.get("RINDEGASTOS_CORE_TOKEN", "").strip()

NOTION_API_BASE_URL = os.environ.get("NOTION_API_BASE_URL", "https://api.notion.com/v1").rstrip("/")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_API_VERSION = os.environ.get("NOTION_API_VERSION", "2026-03-11").strip()
NOTION_API_TIMEOUT = int(os.environ.get("NOTION_API_TIMEOUT", "20"))
NOTION_DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID", "").strip()
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
NOTION_FUNDS_WORK_KEY_PROPERTY = os.environ.get("NOTION_FUNDS_WORK_KEY_PROPERTY", "K de trabajo").strip()
NOTION_FUNDS_WORK_KEY_VALUE = os.environ.get("NOTION_FUNDS_WORK_KEY_VALUE", "Fondos por rendir").strip()
NOTION_FUNDS_WORK_KEY_FILTER_TYPE = os.environ.get("NOTION_FUNDS_WORK_KEY_FILTER_TYPE", "").strip()
NOTION_FUNDS_BENEFICIARY_PROPERTY = os.environ.get("NOTION_FUNDS_BENEFICIARY_PROPERTY", "Beneficiario").strip()
NOTION_FUNDS_RUT_PROPERTY = os.environ.get("NOTION_FUNDS_RUT_PROPERTY", "Rut").strip()
NOTION_FUNDS_AMOUNT_PROPERTY = os.environ.get("NOTION_FUNDS_AMOUNT_PROPERTY", "Monto total Solicitado").strip()
NOTION_FUNDS_CURRENCY_PROPERTY = os.environ.get("NOTION_FUNDS_CURRENCY_PROPERTY", "Moneda").strip()
NOTION_FUNDS_PAYMENT_DATE_PROPERTY = os.environ.get("NOTION_FUNDS_PAYMENT_DATE_PROPERTY", "FECHA DE PAGO").strip()
NOTION_FUNDS_REMITTANCE_PROPERTY = os.environ.get("NOTION_FUNDS_REMITTANCE_PROPERTY", "ID").strip()
NOTION_FUNDS_COST_CENTER_PROPERTY = os.environ.get("NOTION_FUNDS_COST_CENTER_PROPERTY", "Centro de Costo").strip()
NOTION_FUNDS_STATUS_PROPERTY = os.environ.get("NOTION_FUNDS_STATUS_PROPERTY", "Estado").strip()
NOTION_FUNDS_RINDEGASTOS_FUND_PROPERTY = os.environ.get(
    "NOTION_FUNDS_RINDEGASTOS_FUND_PROPERTY",
    "Fondo de Rindegastos",
).strip()
AZURE_ACCOUNT_NAME = os.environ.get("AZURE_ACCOUNT_NAME")
AZURE_ACCOUNT_KEY = os.environ.get("AZURE_ACCOUNT_KEY")
AZURE_CONTAINER = os.environ.get("AZURE_CONTAINER", "waexp-media")
AZURE_CONNECTION_STRING = os.environ.get("AZURE_CONNECTION_STRING")
AZURE_URL_EXPIRATION_SECS = int(os.environ.get("AZURE_URL_EXPIRATION_SECS", "3600"))
AZURE_CUSTOM_DOMAIN = os.environ.get("AZURE_CUSTOM_DOMAIN")
if AZURE_CUSTOM_DOMAIN:
    AZURE_CUSTOM_DOMAIN = AZURE_CUSTOM_DOMAIN.strip().removeprefix("https://").removeprefix("http://")

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

if AZURE_CONNECTION_STRING or (AZURE_ACCOUNT_NAME and AZURE_ACCOUNT_KEY):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
    }

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "dashboard"

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@expenses.otzi.cl")

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "otzi_whatsapp_secret")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "").strip() or os.environ.get("WA_TEMPORARY_TOKEN", "").strip()
WA_TEMPORARY_TOKEN = WA_ACCESS_TOKEN
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "").strip()
WA_REJECTION_TEMPLATE_NAME = os.environ.get("WA_REJECTION_TEMPLATE_NAME", "expense_rejection").strip()
WA_REJECTION_TEMPLATE_LANGUAGE = os.environ.get("WA_REJECTION_TEMPLATE_LANGUAGE", "es_CL").strip()
WA_NOTIFICATION_MAX_ATTEMPTS = int(os.environ.get("WA_NOTIFICATION_MAX_ATTEMPTS", "3"))

_csrf_base = {
    "https://*.ngrok-free.dev",
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "https://localhost",
    "https://127.0.0.1",
}
_csrf_extra = set(env_list("CSRF_TRUSTED_ORIGINS"))
if APP_URL:
    _csrf_extra.add(APP_URL)
CSRF_TRUSTED_ORIGINS = sorted(_csrf_base | _csrf_extra)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
