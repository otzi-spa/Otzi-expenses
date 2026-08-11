from django.conf import settings


def funds_access(request):
    user = getattr(request, "user", None)
    allowed_emails = {email.strip().lower() for email in getattr(settings, "FUNDS_ALLOWED_USER_EMAILS", []) if email}
    user_email = (getattr(user, "email", "") or "").strip().lower()
    return {"can_access_funds": bool(getattr(user, "is_authenticated", False) and user_email in allowed_emails)}
