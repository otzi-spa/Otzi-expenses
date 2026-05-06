from django.contrib import admin
from .models import User, UserAuditLog


admin.site.register(User)
admin.site.register(UserAuditLog)
