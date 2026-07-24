from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0027_expense_rindegastos_upload_tracking"),
    ]

    operations = [
        migrations.AlterField(
            model_name="suppliercatalog",
            name="rut",
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
