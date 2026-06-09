from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0020_alter_suppliercatalog_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="expense",
            name="status",
            field=models.CharField(
                choices=[
                    ("incomplete", "Incompleto"),
                    ("pending", "Pendiente"),
                    ("completed", "Parametrizado"),
                    ("approved", "Aprobado"),
                    ("rejected", "Rechazada"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="WhatsAppExpenseConversation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone", models.CharField(db_index=True, max_length=50)),
                ("stage", models.CharField(max_length=64)),
                ("context", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "expense",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="whatsapp_conversation",
                        to="expenses.expense",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="whatsapp_conversations",
                        to="expenses.allowedsender",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
    ]
