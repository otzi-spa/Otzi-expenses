from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0032_expense_gasoline_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rejection_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="wa_phone_number_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.CreateModel(
            name="ExpenseNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "notification_type",
                    models.CharField(choices=[("rejection", "Rechazo")], default="rejection", max_length=32),
                ),
                ("channel", models.CharField(choices=[("whatsapp", "WhatsApp")], default="whatsapp", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pendiente"),
                            ("processing", "Procesando"),
                            ("sent", "Enviada"),
                            ("failed", "Fallida"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("recipient", models.CharField(blank=True, max_length=64)),
                ("decision_at", models.DateTimeField()),
                ("template_name", models.CharField(blank=True, max_length=128)),
                ("template_language", models.CharField(blank=True, max_length=16)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("provider_message_id", models.CharField(blank=True, max_length=255)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.TextField(blank=True)),
                ("next_retry_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "expense",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to="expenses.expense",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="expensenotification",
            constraint=models.UniqueConstraint(
                fields=("expense", "notification_type", "channel", "decision_at"),
                name="uniq_expense_notification_decision",
            ),
        ),
    ]
