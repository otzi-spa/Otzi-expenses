from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("expenses", "0042_funddepositinjectionattempt_audit_summary")]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rindegastos_report_name",
            field=models.TextField(blank=True, default=""),
        ),
    ]
