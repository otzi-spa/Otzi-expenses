import os
from pathlib import Path
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent.parent
_env_path = BASE_DIR / ".env.dev"
if not _env_path.exists():
    _env_path = BASE_DIR / "env" / ".env.dev"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure")
# DEBUG = os.environ.get("DEBUG", "1") == "1"
DEBUG = 1

_default_allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}
_env_allowed_hosts = {
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
}
ALLOWED_HOSTS = sorted(_default_allowed_hosts | _env_allowed_hosts | {".ngrok-free.dev"})

INSTALLED_APPS = [
    "django.contrib.admin","django.contrib.auth","django.contrib.contenttypes",
    "django.contrib.sessions","django.contrib.messages","django.contrib.staticfiles",
    "rest_framework","django_filters",
    "accounts","expenses","ingestion","ui",
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

# Localización
TIME_ZONE = "America/Santiago"
USE_TZ = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB","waexp"),
        "USER": os.environ.get("POSTGRES_USER","waexp"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD","waexp"),
        "HOST": os.environ.get("POSTGRES_HOST","127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT","5432"),
    }
}

AUTH_USER_MODEL = "accounts.User"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "ui" / "static"]

TEMPLATES = [{
    "BACKEND":"django.template.backends.django.DjangoTemplates",
    "DIRS":[BASE_DIR / "ui" / "templates"],
    "APP_DIRS":True,
    "OPTIONS":{"context_processors":[
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# DRF básico
REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

# Celery + Redis
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL","redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND","redis://127.0.0.1:6379/1")

# # MinIO via S3 backend
# DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
# AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL")     # ej: http://minio:9000
# AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
# AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
# AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME","waexp-media")
# AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME","us-east-1")
# AWS_S3_SIGNATURE_VERSION = "s3v4"
# AWS_S3_ADDRESSING_STYLE = "path"  # necesario para MinIO



# se trabajara finalmente con Azure

# ✅ Azure Blob Storage
DEFAULT_FILE_STORAGE = "storages.backends.azure_storage.AzureStorage"

# Producción (Storage Account real) o desarrollo (Azurite con connection string)
AZURE_ACCOUNT_NAME = os.environ.get("AZURE_ACCOUNT_NAME")         # prod
AZURE_ACCOUNT_KEY  = os.environ.get("AZURE_ACCOUNT_KEY")          # prod
AZURE_CONTAINER    = os.environ.get("AZURE_CONTAINER", "waexp-media")

# Si está presente, Django usará esta cadena (ideal para Azurite local)
AZURE_CONNECTION_STRING = os.environ.get("AZURE_CONNECTION_STRING")

# URLs firmadas (descargas seguras)
AZURE_URL_EXPIRATION_SECS = int(os.environ.get("AZURE_URL_EXPIRATION_SECS", "3600"))
AZURE_CUSTOM_DOMAIN = os.environ.get("AZURE_CUSTOM_DOMAIN")
if AZURE_CUSTOM_DOMAIN:
    AZURE_CUSTOM_DOMAIN = AZURE_CUSTOM_DOMAIN.strip().lstrip("https://").lstrip("http://")

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "dashboard"   # nombre de la ruta a la que volver tras login
LOGOUT_REDIRECT_URL = "dashboard"

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "otzi_whatsapp_secret")
WA_TEMPORARY_TOKEN = os.environ.get("WA_TEMPORARY_TOKEN", "")

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
