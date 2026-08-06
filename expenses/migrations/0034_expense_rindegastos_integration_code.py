import base64
import hashlib
import hmac

from django.conf import settings
from django.db import migrations, models


def expense_integration_code(expense_id):
    key = str(settings.SECRET_KEY).encode("utf-8")
    message = f"expense:{expense_id}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    token = base64.b32encode(digest[:5]).decode("ascii").rstrip("=")
    return f"OTZ-{token}"


def backfill_rindegastos_integration_code(apps, schema_editor):
    Expense = apps.get_model("expenses", "Expense")
    queryset = (
        Expense.objects.exclude(rindegastos_expense_id__isnull=True)
        .exclude(rindegastos_expense_id="")
        .filter(rindegastos_integration_code__isnull=True)
    )
    for expense in queryset.iterator():
        expense.rindegastos_integration_code = expense_integration_code(expense.id)
        expense.save(update_fields=["rindegastos_integration_code"])


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0033_rejection_whatsapp_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="rindegastos_integration_code",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.RunPython(backfill_rindegastos_integration_code, migrations.RunPython.noop),
    ]
