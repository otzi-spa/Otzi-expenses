import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent.parent
_env_path = BASE_DIR / "env" / ".env.prod"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)

from .base import *  # noqa: E402,F401,F403


DEBUG = env_bool("DJANGO_DEBUG", False)
if SECRET_KEY == "dev-insecure":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")
if not APP_URL:
    raise ImproperlyConfigured("APP_URL must be set in production.")
if not WA_ACCESS_TOKEN:
    raise ImproperlyConfigured("WA_ACCESS_TOKEN must be set in production.")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],
]
STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
}
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", True)
USE_X_FORWARDED_PORT = env_bool("DJANGO_USE_X_FORWARDED_PORT", True)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", True)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", True)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", True)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = os.environ.get("DJANGO_SECURE_REFERRER_POLICY", "same-origin")
CSRF_USE_SESSIONS = env_bool("DJANGO_CSRF_USE_SESSIONS", False)

if APP_HOST:
    ALLOWED_HOSTS = sorted(set(ALLOWED_HOSTS) | {APP_HOST})
CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS) | {APP_URL})
