from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0011_alter_expense_expense_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="split_group_id",
            field=models.CharField(blank=True, db_index=True, max_length=36, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="split_index",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="expense",
            name="split_parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="split_children",
                to="expenses.expense",
            ),
        ),
        migrations.AddField(
            model_name="expense",
            name="split_total",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]

