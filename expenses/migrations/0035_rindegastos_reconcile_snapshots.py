from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0034_expense_rindegastos_integration_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="RindegastosReconcileRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("since", models.DateField(blank=True, null=True)),
                ("until", models.DateField(blank=True, null=True)),
                ("max_pages", models.PositiveIntegerField(blank=True, null=True)),
                ("fetched_count", models.PositiveIntegerField(default=0)),
                ("matched_count", models.PositiveIntegerField(default=0)),
                ("changed_count", models.PositiveIntegerField(default=0)),
                ("diff_count", models.PositiveIntegerField(default=0)),
                ("error_count", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(default="running", max_length=16)),
                ("metadata", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "verbose_name": "Corrida reconciliación Rindegastos",
                "verbose_name_plural": "Corridas reconciliación Rindegastos",
                "ordering": ("-started_at",),
            },
        ),
        migrations.CreateModel(
            name="RindegastosExpenseSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rindegastos_expense_id", models.CharField(db_index=True, max_length=64)),
                ("rindegastos_report_id", models.CharField(blank=True, max_length=64)),
                ("payload_hash", models.CharField(db_index=True, max_length=64)),
                ("normalized_payload", models.JSONField(default=dict)),
                ("raw_payload", models.JSONField(default=dict)),
                ("source_endpoint", models.CharField(default="getExpense", max_length=32)),
                ("fetched_at", models.DateTimeField(auto_now_add=True)),
                (
                    "expense",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rindegastos_snapshots",
                        to="expenses.expense",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="snapshots",
                        to="expenses.rindegastosreconcilerun",
                    ),
                ),
            ],
            options={
                "verbose_name": "Snapshot gasto Rindegastos",
                "verbose_name_plural": "Snapshots gastos Rindegastos",
                "ordering": ("-fetched_at",),
            },
        ),
        migrations.AddIndex(
            model_name="rindegastosexpensesnapshot",
            index=models.Index(fields=["expense", "payload_hash"], name="expenses_ri_expense_26099e_idx"),
        ),
        migrations.AddIndex(
            model_name="rindegastosexpensesnapshot",
            index=models.Index(fields=["rindegastos_expense_id", "fetched_at"], name="expenses_ri_rindega_04385d_idx"),
        ),
    ]
