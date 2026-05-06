from django.contrib import admin
from django.urls import path, include
from accounts.views import LoggedPasswordChangeDoneView, LoggedPasswordChangeView
from ui.views import data_deletion, privacy_policy, terms_of_service

urlpatterns = [
    path("admin/", admin.site.urls),
    path("privacy/", privacy_policy, name="privacy_policy"),
    path("data-deletion/", data_deletion, name="data_deletion"),
    path("terms/", terms_of_service, name="terms_of_service"),

    # Web vistas SSR (bandeja/lista/detalle) - las pondrás en expenses/urls_ssr.py
    path("", include("expenses.urls_ssr")),

    # API REST v1
    path("api/v1/", include("accounts.api.urls")),
    path("api/v1/", include("expenses.api.urls")),

    # Webhook WhatsApp
    path("webhook/whatsapp/", include("ingestion.api.urls")),

    path("accounts/password_change/", LoggedPasswordChangeView.as_view(), name="password_change"),
    path("accounts/password_change/done/", LoggedPasswordChangeDoneView.as_view(), name="password_change_done"),

    # 🔐 Auth built-in (login, logout, password_change, etc.)
    path("accounts/", include("django.contrib.auth.urls")),
]
