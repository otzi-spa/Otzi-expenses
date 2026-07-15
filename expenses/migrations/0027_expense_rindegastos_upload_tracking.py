from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0026_normalize_supplier_ruts"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rindegastos_expense_id",
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="rindegastos_report_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="rindegastos_uploaded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="rindegastos_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="rindegastos_status",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="rindegastos_raw_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
