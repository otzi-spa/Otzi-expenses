import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0014_expense_supplier_rut_document_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="categorycatalog",
            name="code",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="categorycatalog",
            name="currency",
            field=models.CharField(blank=True, max_length=8, null=True),
        ),
        migrations.AddField(
            model_name="categorycatalog",
            name="external_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="categorycatalog",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="categorycatalog",
            name="raw_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="categorycatalog",
            name="sync_status",
            field=models.CharField(
                choices=[("manual", "Manual"), ("synced", "Sincronizado"), ("failed", "Error")],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="expensetypecatalog",
            name="name",
            field=models.CharField(max_length=255),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="account_code",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="external_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="group_code",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="group_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="instructions",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="policy",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rindegastos_categories",
                to="expenses.categorycatalog",
            ),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="raw_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="expensetypecatalog",
            name="sync_status",
            field=models.CharField(
                choices=[("manual", "Manual"), ("synced", "Sincronizado"), ("failed", "Error")],
                default="manual",
                max_length=16,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="expensetypecatalog",
            unique_together={("policy", "name", "group_name")},
        ),
        migrations.CreateModel(
            name="RindegastosExpenseFieldCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("field_type", models.CharField(blank=True, max_length=64, null=True)),
                ("default_value", models.CharField(blank=True, max_length=255, null=True)),
                ("default_code", models.CharField(blank=True, max_length=255, null=True)),
                ("options", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "sync_status",
                    models.CharField(
                        choices=[("manual", "Manual"), ("synced", "Sincronizado"), ("failed", "Error")],
                        default="synced",
                        max_length=16,
                    ),
                ),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rindegastos_expense_fields",
                        to="expenses.categorycatalog",
                    ),
                ),
            ],
            options={
                "verbose_name": "Campo extra Rindegastos",
                "verbose_name_plural": "Campos extra Rindegastos",
                "unique_together": {("policy", "name")},
            },
        ),
        migrations.CreateModel(
            name="RindegastosTaxCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("tax_type", models.CharField(blank=True, max_length=64, null=True)),
                ("value", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "sync_status",
                    models.CharField(
                        choices=[("manual", "Manual"), ("synced", "Sincronizado"), ("failed", "Error")],
                        default="synced",
                        max_length=16,
                    ),
                ),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rindegastos_taxes",
                        to="expenses.categorycatalog",
                    ),
                ),
            ],
            options={
                "verbose_name": "Impuesto Rindegastos",
                "verbose_name_plural": "Impuestos Rindegastos",
                "unique_together": {("policy", "name", "tax_type")},
            },
        ),
        migrations.CreateModel(
            name="RindegastosUserCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=255, unique=True)),
                ("first_name", models.CharField(blank=True, max_length=255, null=True)),
                ("last_name", models.CharField(blank=True, max_length=255, null=True)),
                ("full_name", models.CharField(max_length=255)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "sync_status",
                    models.CharField(
                        choices=[("manual", "Manual"), ("synced", "Sincronizado"), ("failed", "Error")],
                        default="synced",
                        max_length=16,
                    ),
                ),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "Usuario Rindegastos", "verbose_name_plural": "Usuarios Rindegastos"},
        ),
    ]
