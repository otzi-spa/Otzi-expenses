from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0031_expense_rindegastos_tax"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="gasoline_type",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
    ]
