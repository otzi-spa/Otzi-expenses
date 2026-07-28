from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0030_tax_indicators"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rindegastos_tax",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
