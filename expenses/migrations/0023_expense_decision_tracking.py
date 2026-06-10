from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0022_expense_not_completed_status"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="decision_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="decision_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="decided_expenses",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="expenseauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("created", "Creado"),
                    ("updated", "Actualizado"),
                    ("status_changed", "Estado cambiado"),
                    ("status_change_blocked", "Cambio de estado bloqueado"),
                    ("approved", "Aprobado"),
                    ("rejected", "Rechazado"),
                    ("decision_reverted", "Decisión revertida"),
                    ("deleted", "Eliminado"),
                    ("whatsapp_update", "Actualización WhatsApp"),
                ],
                default="updated",
                max_length=32,
            ),
        ),
    ]
