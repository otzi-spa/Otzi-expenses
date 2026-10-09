import re

from django.db import migrations


def backfill_report_names(apps, schema_editor):
    Expense = apps.get_model("expenses", "Expense")
    Snapshot = apps.get_model("expenses", "RindegastosExpenseSnapshot")
    database = schema_editor.connection.alias
    expenses = Expense.objects.using(database).filter(rindegastos_report_name="").exclude(
        rindegastos_report_id__isnull=True
    ).exclude(rindegastos_report_id="")
    report_ids = expenses.order_by().values_list("rindegastos_report_id", flat=True).distinct()
    snapshots = Snapshot.objects.using(database).filter(rindegastos_report_id__in=report_ids).order_by("-fetched_at", "-pk")
    filled_reports = set()
    for snapshot in snapshots.iterator():
        report_id = snapshot.rindegastos_report_id
        if report_id in filled_reports:
            continue
        normalized = snapshot.normalized_payload or {}
        title = str(normalized.get("rindegastos_report_title") or "").strip()
        number = str(normalized.get("rindegastos_report_number") or "").strip()
        if not title and not number:
            continue
        name = title
        if number and not re.match(r"^" + re.escape(number) + r"(?:\b|:)", title):
            name = f"{number} {title}".strip()
        expenses.filter(rindegastos_report_id=report_id).update(rindegastos_report_name=name)
        filled_reports.add(report_id)


class Migration(migrations.Migration):
    dependencies = [("expenses", "0043_expense_rindegastos_report_name")]

    operations = [migrations.RunPython(backfill_report_names, migrations.RunPython.noop)]
