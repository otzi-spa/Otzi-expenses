from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0029_expense_invoice_tax_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaxIndicatorValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("indicator", models.CharField(max_length=32)),
                ("year", models.PositiveSmallIntegerField()),
                ("month", models.PositiveSmallIntegerField()),
                ("value", models.DecimalField(decimal_places=4, max_digits=14)),
                ("source_url", models.URLField(blank=True, max_length=500)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Indicador tributario",
                "verbose_name_plural": "Indicadores tributarios",
                "ordering": ("-year", "-month", "indicator"),
                "unique_together": {("indicator", "year", "month")},
            },
        ),
        migrations.CreateModel(
            name="FuelSpecificTaxRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("effective_date", models.DateField()),
                ("fuel_name", models.CharField(max_length=255)),
                ("fuel_key", models.CharField(max_length=80)),
                ("component_base", models.DecimalField(decimal_places=4, max_digits=12)),
                ("component_variable", models.DecimalField(decimal_places=4, max_digits=12)),
                ("resulting_tax", models.DecimalField(decimal_places=4, max_digits=12)),
                ("unit", models.CharField(max_length=32)),
                ("source_url", models.URLField(blank=True, max_length=500)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Tasa impuesto especifico combustible",
                "verbose_name_plural": "Tasas impuesto especifico combustible",
                "ordering": ("-effective_date", "fuel_key"),
                "unique_together": {("effective_date", "fuel_key", "unit")},
            },
        ),
    ]
