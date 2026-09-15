def funds_access(request):
    user = getattr(request, "user", None)
    return {
        "can_access_funds": bool(
            getattr(user, "is_authenticated", False)
            and getattr(user, "is_superuser", False)
        ),
        "can_access_funds_audit": _can_access_funds_audit(user),
    }


def _can_access_funds_audit(user):
    if not getattr(user, "is_authenticated", False):
        return False
    from django.conf import settings

    allowed_emails = {email.casefold() for email in settings.FUNDS_AUDIT_ALLOWED_USER_EMAILS}
    return (getattr(user, "email", "") or "").casefold() in allowed_emails
