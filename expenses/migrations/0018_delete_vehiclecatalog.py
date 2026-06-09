from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0017_expense_fuel_fields"),
    ]

    operations = [
        migrations.DeleteModel(
            name="VehicleCatalog",
        ),
    ]
