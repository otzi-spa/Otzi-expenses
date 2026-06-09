from django.db import migrations


RINDEGASTOS_POLICIES = [
    "Departamento Maquinaria",
    "Oficina Central",
    "Combustibles",
    "Autopista de Antofagasta 2025",
    "Vialidad Choapa COMA",
    "Vialidad Puerto Aysén",
    "Vialidad Coyhaique",
    "Vialidad Cochrane Lechada",
    "Embalse los Aromos III",
    "Curimon III",
    "Autopista de Antofagasta 2026",
]


def seed_policies(apps, schema_editor):
    CategoryCatalog = apps.get_model("expenses", "CategoryCatalog")
    for policy_name in RINDEGASTOS_POLICIES:
        policy, _ = CategoryCatalog.objects.get_or_create(name=policy_name, defaults={"is_active": True})
        if not policy.is_active:
            policy.is_active = True
            policy.save(update_fields=["is_active"])


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0012_expense_split_fields"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="categorycatalog",
            options={"verbose_name": "Política", "verbose_name_plural": "Políticas"},
        ),
        migrations.AlterModelOptions(
            name="expensetypecatalog",
            options={"verbose_name": "Categoría Rindegastos", "verbose_name_plural": "Categorías Rindegastos"},
        ),
        migrations.RunPython(seed_policies, migrations.RunPython.noop),
    ]
