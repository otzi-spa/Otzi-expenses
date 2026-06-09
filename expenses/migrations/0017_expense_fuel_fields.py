from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0016_expense_rindegastos_cost_center_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="fuel_km",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="fuel_liters",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
    ]
