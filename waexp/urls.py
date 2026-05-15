from django.contrib import admin
from django.contrib.auth import views as auth_views
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

    path(
        "accounts/password_reset/",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/accounts/password_reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "accounts/password_reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url="/accounts/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "accounts/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("accounts/password_change/", LoggedPasswordChangeView.as_view(), name="password_change"),
    path("accounts/password_change/done/", LoggedPasswordChangeDoneView.as_view(), name="password_change_done"),

    # 🔐 Auth built-in (login, logout, password_change, etc.)
    path("accounts/", include("django.contrib.auth.urls")),
]
