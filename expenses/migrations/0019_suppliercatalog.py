from django.db import migrations, models


def seed_suppliers(apps, schema_editor):
    Expense = apps.get_model("expenses", "Expense")
    SupplierCatalog = apps.get_model("expenses", "SupplierCatalog")

    suppliers = {}
    rows = (
        Expense.objects.exclude(supplier__isnull=True)
        .exclude(supplier="")
        .values("supplier", "supplier_rut")
        .order_by("id")
    )
    for row in rows:
        name = (row["supplier"] or "").strip()
        if not name:
            continue
        key = name.casefold()
        rut = (row["supplier_rut"] or "").strip()
        if key not in suppliers or (rut and not suppliers[key]["rut"]):
            suppliers[key] = {"name": name, "rut": rut}

    for supplier in suppliers.values():
        SupplierCatalog.objects.create(
            name=supplier["name"],
            rut=supplier["rut"],
            is_active=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0018_delete_vehiclecatalog"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplierCatalog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("rut", models.CharField(max_length=32)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Proveedor",
                "verbose_name_plural": "Proveedores",
                "ordering": ("name",),
            },
        ),
        migrations.RunPython(seed_suppliers, migrations.RunPython.noop),
    ]
