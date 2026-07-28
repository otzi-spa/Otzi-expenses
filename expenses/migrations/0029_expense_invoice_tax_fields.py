from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0028_suppliercatalog_rut_optional"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="iva_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="specific_tax_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="tax_calculation_source",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="expense",
            name="tax_calculation_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
