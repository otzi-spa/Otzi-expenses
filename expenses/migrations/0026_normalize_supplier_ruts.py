from django.db import migrations


def normalize_rut(value):
    raw_value = (value or "").strip()
    compact = "".join(char for char in raw_value.replace(".", "").replace("-", "") if char.isalnum())
    if len(compact) < 2:
        return raw_value.upper()
    return f"{compact[:-1]}-{compact[-1].upper()}"


def normalize_supplier_ruts(apps, schema_editor):
    SupplierCatalog = apps.get_model("expenses", "SupplierCatalog")
    Expense = apps.get_model("expenses", "Expense")

    for supplier in SupplierCatalog.objects.exclude(rut__isnull=True).exclude(rut="").iterator():
        normalized = normalize_rut(supplier.rut)
        if supplier.rut != normalized:
            supplier.rut = normalized
            supplier.save(update_fields=["rut", "updated_at"])

    for expense in Expense.objects.exclude(supplier_rut__isnull=True).exclude(supplier_rut="").iterator():
        normalized = normalize_rut(expense.supplier_rut)
        if expense.supplier_rut != normalized:
            expense.supplier_rut = normalized
            expense.save(update_fields=["supplier_rut"])


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0025_backfill_whatsapp_sender"),
    ]

    operations = [
        migrations.RunPython(normalize_supplier_ruts, migrations.RunPython.noop),
    ]
