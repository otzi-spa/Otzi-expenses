import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure")
DEBUG = False

_default_allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}
_env_allowed_hosts = {
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
}
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

AZURE_ACCOUNT_NAME = os.environ.get("AZURE_ACCOUNT_NAME")
AZURE_ACCOUNT_KEY = os.environ.get("AZURE_ACCOUNT_KEY")
AZURE_CONTAINER = os.environ.get("AZURE_CONTAINER", "waexp-media")
AZURE_CONNECTION_STRING = os.environ.get("AZURE_CONNECTION_STRING")
AZURE_URL_EXPIRATION_SECS = int(os.environ.get("AZURE_URL_EXPIRATION_SECS", "3600"))
AZURE_CUSTOM_DOMAIN = os.environ.get("AZURE_CUSTOM_DOMAIN")
if AZURE_CUSTOM_DOMAIN:
    AZURE_CUSTOM_DOMAIN = AZURE_CUSTOM_DOMAIN.strip().lstrip("https://").lstrip("http://")

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

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "otzi_whatsapp_secret")
WA_TEMPORARY_TOKEN = os.environ.get("WA_TEMPORARY_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "").strip()

_csrf_base = {
    "https://*.ngrok-free.dev",
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "https://localhost",
    "https://127.0.0.1",
}
_csrf_extra = {
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
}
CSRF_TRUSTED_ORIGINS = sorted(_csrf_base | _csrf_extra)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
