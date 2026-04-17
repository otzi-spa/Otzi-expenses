from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0010_categorycatalog_expensetypecatalog_expenseauditlog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="expense",
            name="expense_type",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
