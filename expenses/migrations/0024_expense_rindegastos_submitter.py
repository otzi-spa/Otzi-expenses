from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0023_expense_decision_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rindegastos_submitter",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
