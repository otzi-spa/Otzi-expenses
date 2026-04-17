from django.db import migrations, models


def seed_standard_worksite(apps, schema_editor):
    Expense = apps.get_model("expenses", "Expense")
    Expense.objects.filter(
        status__in=["completed", "approved"],
        worksite__isnull=False,
    ).exclude(worksite="").update(worksite_standard=models.F("worksite"))


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0008_expense_wa_sender"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="worksite_standard",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RunPython(seed_standard_worksite, noop_reverse),
    ]
