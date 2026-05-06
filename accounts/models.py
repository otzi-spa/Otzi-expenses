from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
    ROLE_CHOICES = (
        ("admin", "Admin"),
        ("reviewer", "Reviewer"),
        ("viewer", "Viewer"),
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default="reviewer")

    def save(self, *args, **kwargs):
        normalized_email = (self.email or "").strip().lower()
        self.email = normalized_email
        self.username = normalized_email
        super().save(*args, **kwargs)


class UserAuditLog(models.Model):
    ACTION_CHOICES = [
        ("created", "Creado"),
        ("updated", "Actualizado"),
        ("activated", "Activado"),
        ("deactivated", "Desactivado"),
        ("password_reset", "Password reseteada"),
        ("password_changed", "Password cambiada"),
    ]

    actor = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="performed_user_audit_logs",
    )
    target_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="received_user_audit_logs",
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.target_user.email} - {self.action}"
