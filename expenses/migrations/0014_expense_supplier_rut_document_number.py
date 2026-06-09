from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0013_seed_rindegastos_policies"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="document_number",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="supplier_rut",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
