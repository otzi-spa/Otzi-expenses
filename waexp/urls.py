from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),

    # Web vistas SSR (bandeja/lista/detalle) - las pondrás en expenses/urls_ssr.py
    path("", include("expenses.urls_ssr")),

    # API REST v1
    path("api/v1/", include("accounts.api.urls")),
    path("api/v1/", include("expenses.api.urls")),

    # Webhook WhatsApp
    path("webhook/whatsapp/", include("ingestion.api.urls")),

    # 🔐 Auth built-in (login, logout, password_change, etc.)
    path("accounts/", include("django.contrib.auth.urls")),
]