def funds_access(request):
    user = getattr(request, "user", None)
    return {
        "can_access_funds": bool(
            getattr(user, "is_authenticated", False)
            and getattr(user, "is_superuser", False)
        )
    }
