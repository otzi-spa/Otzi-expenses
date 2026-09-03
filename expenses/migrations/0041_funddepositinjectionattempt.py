import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("expenses", "0040_alter_notionfundsynclog_local_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="FundDepositInjectionAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("started", "Iniciado"), ("rindegastos_ok", "Rindegastos OK"), ("completed", "Completado"), ("failed", "Fallido"), ("notion_failed", "Notion falló"), ("ambiguous", "Ambiguo")], db_index=True, default="started", max_length=32)),
                ("internal_note", models.CharField(blank=True, max_length=255)),
                ("rindegastos_fund_id", models.CharField(blank=True, max_length=255)),
                ("rindegastos_admin_id", models.CharField(blank=True, max_length=255)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("currency", models.CharField(default="CLP", max_length=8)),
                ("requested_payment_date", models.DateField(blank=True, null=True)),
                ("request_payload", models.JSONField(blank=True, default=dict)),
                ("response_payload", models.JSONField(blank=True, default=dict)),
                ("before_fund_payload", models.JSONField(blank=True, default=dict)),
                ("after_fund_payload", models.JSONField(blank=True, default=dict)),
                ("detected_transaction", models.JSONField(blank=True, default=dict)),
                ("detected_transaction_reference", models.CharField(blank=True, max_length=255)),
                ("anomaly", models.TextField(blank=True)),
                ("error", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fund_deposit_attempts", to=settings.AUTH_USER_MODEL)),
                ("notion_log", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deposit_attempts", to="expenses.notionfundsynclog")),
            ],
            options={
                "verbose_name": "Intento abono fondo",
                "verbose_name_plural": "Intentos abono fondos",
                "ordering": ("-started_at",),
            },
        ),
        migrations.AddIndex(
            model_name="funddepositinjectionattempt",
            index=models.Index(fields=["status", "started_at"], name="expenses_fu_status_e19e28_idx"),
        ),
        migrations.AddIndex(
            model_name="funddepositinjectionattempt",
            index=models.Index(fields=["rindegastos_fund_id", "started_at"], name="expenses_fu_rindega_520fa4_idx"),
        ),
        migrations.AddIndex(
            model_name="funddepositinjectionattempt",
            index=models.Index(fields=["internal_note"], name="expenses_fu_interna_6c8843_idx"),
        ),
    ]
