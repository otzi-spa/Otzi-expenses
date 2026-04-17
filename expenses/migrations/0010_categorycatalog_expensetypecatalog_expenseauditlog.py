from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0009_expense_worksite_standard"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CategoryCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Categoría",
                "verbose_name_plural": "Categorías",
            },
        ),
        migrations.CreateModel(
            name="ExpenseTypeCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Tipo de gasto",
                "verbose_name_plural": "Tipos de gasto",
            },
        ),
        migrations.CreateModel(
            name="ExpenseAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Creado"),
                            ("updated", "Actualizado"),
                            ("status_changed", "Estado cambiado"),
                            ("status_change_blocked", "Cambio de estado bloqueado"),
                            ("approved", "Aprobado"),
                            ("rejected", "Rechazado"),
                            ("deleted", "Eliminado"),
                            ("whatsapp_update", "Actualización WhatsApp"),
                        ],
                        default="updated",
                        max_length=32,
                    ),
                ),
                ("expense_snapshot_id", models.IntegerField()),
                ("actor_name", models.CharField(blank=True, max_length=255)),
                ("source", models.CharField(default="web", max_length=32)),
                ("reason", models.TextField(blank=True)),
                ("changes", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "expense",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_logs",
                        to="expenses.expense",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
