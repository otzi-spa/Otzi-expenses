import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from expenses.models import (
    AllowedSender,
    Attachment,
    CategoryCatalog,
    Expense,
    ExpenseAuditLog,
    ExpenseTypeCatalog,
    FuelSpecificTaxRate,
    RindegastosExpenseFieldCatalog,
    RindegastosTaxCatalog,
    SupplierCatalog,
    TaxIndicatorValue,
    normalize_rut,
)
from expenses.invoice_tax_calculator import calculate_invoice_taxes
from expenses.rindegastos_sync import RindegastosCatalogSync
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, extract_otzi_ids, summarize_rindegastos_expense
from expenses.tax_indicators_sync import SiiTaxIndicatorSync
from expenses.views import _expense_export_id, _find_similar_expenses, _missing_fields_for_parametrization, _rindegastos_note

LOCAL_TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


class FuelExpenseValidationTests(TestCase):
    def test_combustibles_requires_km_and_liters(self):
        expense = Expense(
            amount=Decimal("50000"),
            currency="CLP",
            category="Combustibles",
            supplier="Proveedor",
            rindegastos_cost_center="Faena",
            rindegastos_submitter="Francisco Santibañez",
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            is_vehicle=True,
            vehicle="Camion 12",
        )

        missing = _missing_fields_for_parametrization(expense)

        self.assertIn("Km carguío", missing)
        self.assertIn("Litros combustible", missing)

    def test_combustibles_is_complete_with_fuel_fields(self):
        expense = Expense(
            amount=Decimal("50000"),
            currency="CLP",
            category="Combustibles",
            supplier="Proveedor",
            rindegastos_cost_center="Faena",
            rindegastos_submitter="Francisco Santibañez",
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            is_vehicle=True,
            vehicle="Camion 12",
            fuel_km=Decimal("154320"),
            fuel_liters=Decimal("45.5"),
        )

        self.assertEqual(_missing_fields_for_parametrization(expense), [])


class RindegastosUploadedExpenseSyncTests(TestCase):
    def test_extract_otzi_ids_from_note(self):
        self.assertEqual(
            extract_otzi_ids("Compra llave. Gasto id OTZ-7RHAFIME y OTZ-S5FAEKWM"),
            ["OTZ-7RHAFIME", "OTZ-S5FAEKWM"],
        )

    def test_summarize_rindegastos_expense_includes_otzi_ids(self):
        summary = summarize_rindegastos_expense(
            {
                "Id": 123,
                "Supplier": "Proveedor",
                "IssueDate": "2026-06-11",
                "Total": 5900,
                "Currency": "CLP",
                "Note": "Alimentación. Gasto id OTZ-YKQL54N2",
            }
        )

        self.assertEqual(summary["id"], 123)
        self.assertEqual(summary["otzi_ids"], ["OTZ-YKQL54N2"])

    def test_sync_marks_local_expense_from_rindegastos_note(self):
        expense = Expense.objects.create(
            status="completed",
            amount=Decimal("5900"),
            currency="CLP",
            category="Oficina Central",
            supplier="starbucks Coffee Chile S.A.",
            paid_at="2026-06-11",
        )
        export_id = _expense_export_id(expense.id)

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "ReportId": 654,
                        "Status": "Borrador",
                        "IssueDate": "2026-06-11",
                        "Total": 5900,
                        "Note": f"Alimentación. Gasto id {export_id}",
                    }
                ], {"Pages": 1}

        stats = RindegastosUploadedExpenseSync(client=FakeClient(), export_id_func=_expense_export_id).sync(
            since=timezone.datetime(2026, 4, 1).date(),
            until=timezone.datetime(2026, 7, 15).date(),
        )

        expense.refresh_from_db()
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(expense.rindegastos_expense_id, "987")
        self.assertEqual(expense.rindegastos_report_id, "654")


class FuelExpenseExportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="export@example.com",
            email="export@example.com",
            password="test",
        )
        self.client.force_login(self.user)

    def test_export_includes_combustible_columns_and_values(self):
        Expense.objects.create(
            status="completed",
            amount=Decimal("50000"),
            currency="CLP",
            category="Combustibles",
            supplier="Proveedor",
            rindegastos_cost_center="Faena",
            rindegastos_submitter="Francisco Santibañez",
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            is_vehicle=True,
            vehicle="Camion 12",
            fuel_km=Decimal("154320"),
            fuel_liters=Decimal("45.5"),
            expense_type="Diesel",
        )

        response = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"})
        content = response.content.decode("utf-8-sig")

        self.assertEqual(response.status_code, 200)
        self.assertIn("vehiculo_equipo,km_carguio,litros_combustible,categoria_rindegastos", content)
        self.assertIn("Camion 12,154320,45.500,Diesel", content)

        page = self.client.get(reverse("expense_list"))
        self.assertContains(page, 'name="fuel_km"')
        self.assertContains(page, 'name="fuel_liters"')
        self.assertContains(page, "Categoría Rindegastos (tipo de combustible)")
        self.assertContains(page, 'value="154320.00"')

    def test_export_maps_invoice_tax_name_and_amounts(self):
        policy = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={
                "external_id": "policy-fuel-tax",
                "is_active": True,
                "sync_status": "synced",
            },
        )[0]
        RindegastosTaxCatalog.objects.create(
            policy=policy,
            name="IVA (Solo para Facturas Afectas)",
            tax_type="1",
            value=Decimal("19"),
        )
        Expense.objects.create(
            status="completed",
            amount=Decimal("100000"),
            currency="CLP",
            category="Combustibles",
            supplier="Proveedor Factura Combustible",
            paid_at="2026-06-09",
            rindegastos_document_type="Factura afecta",
            iva_amount=Decimal("12546"),
            specific_tax_amount=Decimal("21420"),
        )

        response = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"})
        content = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content)))
        header = rows[rows.index([]) + 1]
        row = dict(zip(header, rows[rows.index([]) + 2]))

        self.assertEqual(row["impuesto"], "IVA (Solo para Facturas Afectas)")
        self.assertEqual(row["valor_impuesto"], "12546")
        self.assertEqual(row["otros_impuestos"], "21420")

    def test_export_leaves_tax_selector_blank_for_non_invoice(self):
        policy = CategoryCatalog.objects.update_or_create(
            name="Oficina Central",
            defaults={
                "external_id": "policy-office-tax",
                "is_active": True,
                "sync_status": "synced",
            },
        )[0]
        RindegastosTaxCatalog.objects.create(
            policy=policy,
            name="IVA",
            tax_type="1",
            value=Decimal("19"),
        )
        Expense.objects.create(
            status="completed",
            amount=Decimal("100000"),
            currency="CLP",
            category="Oficina Central",
            supplier="Proveedor Boleta",
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            iva_amount=Decimal("0"),
            specific_tax_amount=Decimal("0"),
        )

        response = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        header = rows[rows.index([]) + 1]
        row = dict(zip(header, rows[rows.index([]) + 2]))

        self.assertEqual(row["impuesto"], "")
        self.assertEqual(row["valor_impuesto"], "0")
        self.assertEqual(row["otros_impuestos"], "0")

    def test_export_includes_vehicle_for_machinery_policy(self):
        Expense.objects.create(
            status="completed",
            amount=Decimal("125000"),
            currency="CLP",
            category="Departamento Maquinaria",
            supplier="Proveedor Maquinaria",
            rindegastos_cost_center="Taller Central",
            rindegastos_submitter="Francisco Santibañez",
            paid_at="2026-06-10",
            rindegastos_document_type="Factura afecta",
            is_vehicle=True,
            vehicle="Camión Mack LXXR28",
        )

        response = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"})
        content = response.content.decode("utf-8-sig")

        self.assertEqual(response.status_code, 200)
        self.assertIn("vehiculo_equipo,km_carguio,litros_combustible", content)
        self.assertIn("Camión Mack LXXR28,,,", content)

    def test_export_note_flattens_whatsapp_line_breaks(self):
        note = _rindegastos_note("[Francisco]\nCompra informada por WhatsApp", "OTZ-TEST")

        self.assertEqual(
            note,
            "[Francisco] | Compra informada por WhatsApp. Gasto id OTZ-TEST",
        )

    def test_export_status_scopes(self):
        Expense.objects.create(status="completed", supplier="Parametrizado", category="Oficina Central")
        Expense.objects.create(status="approved", supplier="Aprobado", category="Oficina Central")
        Expense.objects.create(status="pending", supplier="Pendiente", category="Oficina Central")

        approved = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "approved", "sync_before_export": "0"}).content.decode("utf-8-sig")
        both = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed_and_approved", "sync_before_export": "0"}).content.decode("utf-8-sig")
        completed = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"}).content.decode("utf-8-sig")
        all_rows = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "all", "sync_before_export": "0"}).content.decode("utf-8-sig")

        self.assertIn("Aprobado", approved)
        self.assertNotIn("Parametrizado", approved)
        self.assertIn("Aprobado", both)
        self.assertIn("Parametrizado", both)
        self.assertNotIn("Pendiente", both)
        self.assertIn("Parametrizado", completed)
        self.assertNotIn("Aprobado", completed)
        self.assertIn("Pendiente", all_rows)

    @patch("expenses.views.RindegastosUploadedExpenseSync")
    def test_export_excludes_uploaded_expenses_by_default(self, sync_cls):
        sync_cls.return_value.sync.return_value = {"matched": 0}
        Expense.objects.create(
            status="approved",
            supplier="Ya subido",
            category="Oficina Central",
            rindegastos_expense_id="987",
        )
        Expense.objects.create(status="approved", supplier="No subido", category="Oficina Central")

        default_content = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "approved"}).content.decode(
            "utf-8-sig"
        )
        include_uploaded_content = self.client.get(
            reverse("expense_rindegastos_export"),
            {"status_scope": "approved", "sync_before_export": "0", "exclude_uploaded": "0"},
        ).content.decode("utf-8-sig")

        self.assertNotIn("Ya subido", default_content)
        self.assertIn("No subido", default_content)
        self.assertIn("Ya subido", include_uploaded_content)

    def test_export_uses_expense_date_not_creation_date(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Fecha",
            category="Oficina Central",
            paid_at="2026-06-09",
        )
        Expense.objects.filter(pk=expense.pk).update(created_at=timezone.datetime(2026, 7, 1, 10, 30, tzinfo=timezone.get_current_timezone()))

        content = self.client.get(
            reverse("expense_rindegastos_export"),
            {"status_scope": "completed", "sync_before_export": "0"},
        ).content.decode("utf-8-sig")

        self.assertIn("9/6/2026", content)
        self.assertNotIn("1/7/2026", content)

    @override_settings(STORAGES=LOCAL_TEST_STORAGES, MEDIA_ROOT="/tmp/otzi-expenses-test-media")
    def test_export_includes_signed_attachment_urls(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Archivo",
            category="Oficina Central",
            paid_at="2026-06-09",
        )
        Attachment.objects.create(
            expense=expense,
            file=ContentFile(b"receipt-bytes", name="receipt.jpg"),
            content_type="image/jpeg",
        )

        response = self.client.get(reverse("expense_rindegastos_export"), {"status_scope": "completed", "sync_before_export": "0"})
        content = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content)))
        header_index = rows.index(
            [
                "politica",
                "expenses_id",
                "proveedor",
                "total",
                "moneda",
                "impuesto",
                "valor_impuesto",
                "otros_impuestos",
                "fecha",
                "centro_costo_faena",
                "nombre_quien_rinde",
                "numero_documento",
                "rut_proveedor",
                "tipo_documento",
                "vehiculo_equipo",
                "km_carguio",
                "litros_combustible",
                "categoria_rindegastos",
                "archivo_urls",
                "archivo_nombres",
                "nota",
            ]
        )
        header = rows[header_index]
        row = dict(zip(header, rows[header_index + 1]))

        self.assertIn("receipt", row["archivo_nombres"])
        self.assertTrue(row["archivo_nombres"].endswith(".jpg"))
        self.assertIn(reverse("attachment_export_serve", args=[expense.attachments.first().pk]), row["archivo_urls"])
        signed_url = urlparse(row["archivo_urls"])
        public_response = self.client.get(f"{signed_url.path}?{signed_url.query}")

        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(public_response["Content-Type"], "image/jpeg")
        self.assertEqual(public_response["Access-Control-Allow-Origin"], "*")

    @override_settings(STORAGES=LOCAL_TEST_STORAGES, MEDIA_ROOT="/tmp/otzi-expenses-test-media")
    def test_signed_attachment_url_rejects_invalid_signature(self):
        expense = Expense.objects.create(status="completed", supplier="Proveedor Archivo", category="Oficina Central")
        attachment = Attachment.objects.create(
            expense=expense,
            file=ContentFile(b"receipt-bytes", name="receipt.jpg"),
            content_type="image/jpeg",
        )

        response = self.client.get(
            reverse("attachment_export_serve", args=[attachment.pk]),
            {"expires": int(timezone.now().timestamp()) + 3600, "sig": "bad"},
        )

        self.assertEqual(response.status_code, 403)


class RindegastosVehicleOptionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="vehicle-options@example.com",
            email="vehicle-options@example.com",
            password="test",
        )
        self.client.force_login(self.user)

    def test_expense_modal_includes_vehicle_options_from_policy_catalog(self):
        policy, _ = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={
                "external_id": "policy-fuel",
                "is_active": True,
            },
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=policy,
            name="Vehiculo o Equipo",
            field_type="Select",
            options=[
                {"Code": "CAM-01", "Value": "Camión 01"},
                {"Code": "RET-02", "Value": "Retroexcavadora 02"},
            ],
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=policy,
            name="Nombre quien rinde",
            field_type="Select",
            options=[{"Code": "FS", "Value": "Francisco Santibañez"}],
        )
        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'class="form-select js-searchable-select" data-rindegastos-field="Vehiculo o Equipo"',
        )
        self.assertContains(
            response,
            reverse("rindegastos_policy_options", kwargs={"external_id": policy.external_id}),
        )
        self.assertContains(response, 'data-rindegastos-field="Nombre quien rinde"')
        self.assertContains(response, "Categoría Rindegastos (tipo de combustible)")
        self.assertIn(
            {
                "policy_id": policy.id,
                "policy_external_id": policy.external_id,
                "policy_name": policy.name,
                "field_name": "Vehiculo o Equipo",
                "value": "Camión 01",
                "code": "CAM-01",
            },
            response.context["rindegastos_field_options"],
        )

    def test_policy_options_endpoint_uses_external_id_and_isolates_policies(self):
        machinery, _ = CategoryCatalog.objects.update_or_create(
            name="Departamento Maquinaria",
            defaults={
                "external_id": "41786",
                "sync_status": "synced",
                "is_active": True,
            },
        )
        other_policy = CategoryCatalog.objects.create(
            name="Autopista",
            external_id="99999",
            sync_status="synced",
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=machinery,
            name="Centro de Costo / Faena",
            options=[{"Value": "Taller Central"}],
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=machinery,
            name="Nombre quien rinde",
            options=[{"Value": "Francisco Santibañez"}],
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=other_policy,
            name="Centro de Costo / Faena",
            options=[{"Value": "Costo Directo"}],
        )
        ExpenseTypeCatalog.objects.create(
            policy=machinery,
            name="Mantención",
            group_name="Maquinaria",
            sync_status="synced",
        )

        response = self.client.get(
            reverse("rindegastos_policy_options", kwargs={"external_id": "41786"})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["policy"]["external_id"], "41786")
        self.assertIn(
            {
                "field_name": "Centro de Costo / Faena",
                "value": "Taller Central",
                "code": "",
            },
            payload["field_options"],
        )
        self.assertIn(
            {
                "field_name": "Nombre quien rinde",
                "value": "Francisco Santibañez",
                "code": "",
            },
            payload["field_options"],
        )
        self.assertNotIn(
            {
                "field_name": "Centro de Costo / Faena",
                "value": "Costo Directo",
                "code": "",
            },
            payload["field_options"],
        )
        self.assertEqual(
            payload["categories"],
            [{"value": "Mantención", "label": "Mantención / Maquinaria"}],
        )

    def test_policy_options_endpoint_rejects_unknown_external_id(self):
        response = self.client.get(
            reverse("rindegastos_policy_options", kwargs={"external_id": "missing"})
        )

        self.assertEqual(response.status_code, 404)


class RindegastosCatalogRebuildTests(TestCase):
    def test_rebuild_recreates_synced_relations_and_preserves_manual_catalogs(self):
        class FakeClient:
            def get_expense_policies(self, active_only=True):
                return [{"Id": "41786", "Name": "Departamento Maquinaria", "IsActive": True}]

            def get_expense_policy_categories(self, policy_id):
                return [{"Id": "cat-1", "Name": "Mantención", "GroupName": "Maquinaria"}]

            def get_expense_policy_taxes(self, policy_id):
                return []

            def get_expense_policy_expense_fields(self, policy_id):
                return [
                    {
                        "Name": "Centro de Costo / Faena",
                        "Type": "list",
                        "Options": [{"Value": "Taller Central"}],
                    },
                    {
                        "Name": "Nombre quien rinde",
                        "Type": "list",
                        "Options": [{"Value": "Francisco Santibañez"}],
                    },
                ]

            def get_users(self):
                return []

        policy, _ = CategoryCatalog.objects.update_or_create(
            name="Departamento Maquinaria",
            defaults={
                "external_id": "41786",
                "sync_status": "synced",
                "is_active": True,
            },
        )
        ExpenseTypeCatalog.objects.create(
            policy=policy,
            name="Sincronizada",
            sync_status="synced",
        )
        manual_category = ExpenseTypeCatalog.objects.create(
            name="Manual",
            sync_status="manual",
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=policy,
            name="Centro de Costo / Faena",
            sync_status="synced",
        )
        RindegastosTaxCatalog.objects.create(
            policy=policy,
            name="IVA",
            sync_status="synced",
        )

        stats = RindegastosCatalogSync(client=FakeClient()).sync_all(rebuild=True)

        self.assertTrue(CategoryCatalog.objects.filter(pk=policy.pk).exists())
        self.assertTrue(ExpenseTypeCatalog.objects.filter(pk=manual_category.pk).exists())
        self.assertTrue(
            ExpenseTypeCatalog.objects.filter(
                policy=policy,
                name="Mantención",
                sync_status="synced",
            ).exists()
        )
        field = RindegastosExpenseFieldCatalog.objects.get(
            policy=policy,
            name="Centro de Costo / Faena",
        )
        self.assertEqual(field.options, [{"Value": "Taller Central"}])
        self.assertFalse(RindegastosTaxCatalog.objects.filter(policy=policy).exists())
        self.assertEqual(stats["verified_policy_links"], 3)


class SupplierCatalogFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="supplier-admin@example.com",
            email="supplier-admin@example.com",
            password="test",
        )
        self.client.force_login(self.user)
        self.policy = CategoryCatalog.objects.update_or_create(
            name="Oficina Central",
            defaults={
                "external_id": "policy-office",
                "is_active": True,
            },
        )[0]
        RindegastosTaxCatalog.objects.create(
            policy=self.policy,
            name="IVA",
            tax_type="1",
            value=Decimal("19"),
        )

    def test_new_supplier_is_saved_in_catalog_with_expense(self):
        response = self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "new_supplier_name": "Proveedor Nuevo",
                "supplier_select": "Proveedor Nuevo",
                "supplier_rut": "76.123.456-7",
                "worksite": "Obra reportada",
            },
        )

        self.assertRedirects(response, reverse("expense_list"))
        supplier = SupplierCatalog.objects.get(name="Proveedor Nuevo")
        expense = Expense.objects.get(supplier="Proveedor Nuevo")
        self.assertEqual(supplier.rut, "76123456-7")
        self.assertEqual(expense.supplier_rut, supplier.rut)
        self.assertEqual(expense.worksite, "Obra reportada")
        self.assertIsNone(expense.worksite_standard)

    def test_new_supplier_rut_is_saved_with_verifier_hyphen(self):
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "new_supplier_name": "Proveedor Rut Sin Guion",
                "supplier_select": "Proveedor Rut Sin Guion",
                "supplier_rut": "166082188",
            },
        )

        supplier = SupplierCatalog.objects.get(name="Proveedor Rut Sin Guion")
        expense = Expense.objects.get(supplier="Proveedor Rut Sin Guion")
        self.assertEqual(supplier.rut, "16608218-8")
        self.assertEqual(expense.supplier_rut, "16608218-8")

    def test_new_supplier_can_be_created_without_rut(self):
        response = self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "new_supplier_name": "Proveedor Sin Rut",
                "supplier_select": "Proveedor Sin Rut",
                "supplier_rut": "",
            },
        )

        self.assertRedirects(response, reverse("expense_list"))
        supplier = SupplierCatalog.objects.get(name="Proveedor Sin Rut")
        expense = Expense.objects.get(supplier="Proveedor Sin Rut")
        self.assertEqual(supplier.rut, "")
        self.assertIsNone(expense.supplier_rut)

    def test_supplier_maintainer_accepts_blank_rut(self):
        response = self.client.post(
            reverse("settings_suppliers"),
            {
                "action": "add_supplier",
                "name": "Proveedor Mantenedor Sin Rut",
                "rut": "",
            },
        )

        self.assertRedirects(response, reverse("settings_suppliers"))
        supplier = SupplierCatalog.objects.get(name="Proveedor Mantenedor Sin Rut")
        self.assertEqual(supplier.rut, "")

    def test_rut_normalizer_accepts_dots_spaces_and_k(self):
        self.assertEqual(normalize_rut("76.123.456-7"), "76123456-7")
        self.assertEqual(normalize_rut("16 608 2188"), "16608218-8")
        self.assertEqual(normalize_rut("12345678k"), "12345678-K")

    def test_existing_supplier_uses_catalog_rut(self):
        SupplierCatalog.objects.create(
            name="Proveedor Existente",
            rut="77.777.777-7",
        )

        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "supplier_select": "Proveedor Existente",
                "supplier_rut": "11.111.111-1",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Existente")
        self.assertEqual(expense.supplier_rut, "77777777-7")

    def test_invoice_non_fuel_keeps_iva_and_resets_specific_tax(self):
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "new_supplier_name": "Proveedor Factura",
                "supplier_select": "Proveedor Factura",
                "rindegastos_document_type": "Factura afecta",
                "iva_amount": "19.000",
                "specific_tax_amount": "5.000",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Factura")
        self.assertEqual(expense.iva_amount, Decimal("19000"))
        self.assertEqual(expense.specific_tax_amount, Decimal("0"))
        self.assertEqual(expense.rindegastos_tax, "IVA")
        self.assertEqual(expense.tax_calculation_source, "manual")

    def test_fuel_invoice_keeps_iva_and_specific_tax(self):
        fuel_policy = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={"external_id": "policy-fuel", "is_active": True},
        )[0]
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": fuel_policy.name,
                "new_supplier_name": "Proveedor Combustible",
                "supplier_select": "Proveedor Combustible",
                "rindegastos_document_type": "Factura afecta",
                "iva_amount": "7.000",
                "specific_tax_amount": "3.500",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Combustible")
        self.assertEqual(expense.iva_amount, Decimal("7000"))
        self.assertEqual(expense.specific_tax_amount, Decimal("3500"))
        self.assertEqual(expense.tax_calculation_metadata["editable_fields"], ["iva_amount", "specific_tax_amount"])

    def test_fuel_invoice_uses_selected_gasoline_octane_for_auto_calculation(self):
        fuel_policy = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={"external_id": "policy-fuel-octane", "is_active": True},
        )[0]
        ExpenseTypeCatalog.objects.create(policy=fuel_policy, name="01 Bencina", is_active=True)
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=7, value=Decimal("68000"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 97",
            fuel_key="gasolina_automotriz_97",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.5000"),
            resulting_tax=Decimal("6.5000"),
            unit="UTM/M3",
        )

        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": fuel_policy.name,
                "category_policy_id": str(fuel_policy.id),
                "amount": "100.000",
                "paid_at": "2026-07-10",
                "new_supplier_name": "Proveedor Bencina 97",
                "supplier_select": "Proveedor Bencina 97",
                "rindegastos_document_type": "Factura afecta",
                "fuel_liters": "50",
                "expense_type_select": "01 Bencina",
                "gasoline_type": "97",
                "iva_amount": "0",
                "specific_tax_amount": "0",
                "tax_manual_override": "0",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Bencina 97")
        self.assertEqual(expense.expense_type, "01 Bencina")
        self.assertEqual(expense.gasoline_type, "97")
        self.assertEqual(expense.specific_tax_amount, Decimal("22100"))
        self.assertEqual(expense.tax_calculation_metadata["fuel_type"], "Bencina 97")
        self.assertEqual(expense.tax_calculation_metadata["fuel_key"], "gasolina_automotriz_97")
        self.assertEqual(expense.tax_calculation_source, "auto")

    def test_non_invoice_resets_tax_fields(self):
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "new_supplier_name": "Proveedor Boleta",
                "supplier_select": "Proveedor Boleta",
                "rindegastos_document_type": "Boleta",
                "iva_amount": "19.000",
                "specific_tax_amount": "5.000",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Boleta")
        self.assertEqual(expense.iva_amount, Decimal("0"))
        self.assertEqual(expense.specific_tax_amount, Decimal("0"))
        self.assertIsNone(expense.rindegastos_tax)
        self.assertEqual(expense.tax_calculation_source, "none")

    def test_invoice_without_manual_override_autocalculates_iva(self):
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "amount": "119.000",
                "paid_at": "2026-07-10",
                "new_supplier_name": "Proveedor Auto IVA",
                "supplier_select": "Proveedor Auto IVA",
                "rindegastos_document_type": "Factura afecta",
                "iva_amount": "0",
                "specific_tax_amount": "0",
                "tax_manual_override": "0",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Auto IVA")
        self.assertEqual(expense.iva_amount, Decimal("19000"))
        self.assertEqual(expense.specific_tax_amount, Decimal("0"))
        self.assertEqual(expense.rindegastos_tax, "IVA")
        self.assertEqual(expense.tax_calculation_source, "auto")

    def test_modal_uses_synced_policies_and_has_no_standard_worksite(self):
        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, "Obra (ingresada por usuario)")
        self.assertContains(response, "Tipo de documento reportado")
        self.assertContains(response, "Tipo de documento Rindegastos")
        self.assertContains(response, "findReportedDocumentTypeMatch")
        self.assertContains(response, "expenses-column-filters")
        self.assertContains(response, "data-filter-date")
        self.assertContains(response, "data-filter-value")
        self.assertContains(response, "expensesClearFiltersBtn")
        self.assertContains(response, "expensesFiltersActiveIndicator")
        self.assertContains(response, "expenses.tableFilters.v")
        self.assertContains(response, 'class="modal fade supplier-quick-modal"')
        self.assertContains(response, "supplier-quick-backdrop")
        self.assertContains(response, "Impuestos")
        self.assertContains(response, "Tipo de bencina")
        self.assertContains(response, 'name="gasoline_type"')
        self.assertContains(response, 'name="iva_amount"')
        self.assertContains(response, 'name="specific_tax_amount"')
        self.assertContains(response, "toggleTaxFields")
        self.assertContains(response, "toggleGasolineTypeField")
        self.assertContains(response, "data-bs-trigger', 'click'")
        self.assertContains(response, "data-bs-container', 'body'")
        self.assertContains(response, 'data-tax-info="iva"')
        self.assertContains(response, 'data-tax-info="specific"')
        self.assertNotContains(response, 'name="worksite_standard"')
        self.assertNotContains(response, 'name="new_category_name"')
        self.assertNotContains(response, 'name="expense_type_other"')
        self.assertNotContains(response, "Categoría Rindegastos (detalle)")
        self.assertContains(response, 'name="supplier_rut"')

    @override_settings(STORAGES=LOCAL_TEST_STORAGES, MEDIA_ROOT="/tmp/otzi-expenses-test-media")
    def test_edit_modal_uses_side_receipt_viewer(self):
        expense = Expense.objects.create(
            status="pending",
            category=self.policy.name,
            supplier="Proveedor",
        )
        Attachment.objects.create(
            expense=expense,
            file=ContentFile(b"receipt-bytes", name="receipt.jpg"),
            content_type="image/jpeg",
        )

        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, f'id="expenseModal{expense.pk}"')
        self.assertContains(response, 'class="modal-body expense-workspace"')
        self.assertContains(response, 'class="expense-form-pane"')
        self.assertContains(response, 'class="expense-receipt-pane"')
        self.assertContains(response, 'class="expense-receipt-canvas js-receipt-viewer"')
        self.assertContains(response, 'data-viewer-type="image"')
        self.assertContains(response, 'data-receipt-zoom="in"')
        self.assertContains(response, 'data-receipt-zoom="out"')
        self.assertContains(response, 'data-receipt-zoom="fit"')

    def test_similar_expense_warning_is_rendered_inside_modal(self):
        older = Expense.objects.create(
            status="completed",
            amount=Decimal("25000"),
            category=self.policy.name,
            supplier="Proveedor Duplicado",
            supplier_rut="76123456-7",
            paid_at="2026-06-10",
            document_number="12345",
            rindegastos_document_type="Boleta",
        )
        current = Expense.objects.create(
            status="pending",
            amount=Decimal("25000"),
            category=self.policy.name,
            supplier="Proveedor Duplicado",
            supplier_rut="76.123.456-7",
            paid_at="2026-06-10",
            document_number="12345",
            rindegastos_document_type="Boleta",
        )

        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, f'id="expenseModal{current.pk}"')
        self.assertContains(response, "Posible gasto duplicado")
        self.assertContains(response, _expense_export_id(older.id))
        self.assertContains(response, "mismo número de documento")

    def test_similar_expense_helper_ignores_unrelated_expenses(self):
        current = Expense.objects.create(
            status="pending",
            amount=Decimal("25000"),
            category=self.policy.name,
            supplier="Proveedor Actual",
            paid_at="2026-06-10",
        )
        unrelated = Expense.objects.create(
            status="completed",
            amount=Decimal("99000"),
            category="Combustibles",
            supplier="Otro Proveedor",
            paid_at="2026-06-01",
        )

        matches = _find_similar_expenses(current, [current, unrelated])

        self.assertEqual(matches, [])

    def test_edit_preserves_reported_document_type_without_rindegastos_fallback(self):
        supplier = SupplierCatalog.objects.create(
            name="Proveedor Documento",
            rut="76.000.000-0",
        )
        expense = Expense.objects.create(
            status="pending",
            source="whatsapp",
            category=self.policy.name,
            supplier=supplier.name,
            supplier_rut=supplier.rut,
            document_type="boleta",
        )

        response = self.client.post(
            reverse("expense_detail", args=[expense.pk]),
            {
                "status": "pending",
                "category_select": self.policy.name,
                "supplier_select": supplier.name,
                "supplier_rut": supplier.rut,
                "rindegastos_document_type": "",
            },
        )

        self.assertRedirects(response, reverse("expense_list"))
        expense.refresh_from_db()
        self.assertEqual(expense.document_type, "boleta")
        self.assertIsNone(expense.rindegastos_document_type)

    def test_edit_saves_expense_type_for_combustibles(self):
        supplier = SupplierCatalog.objects.create(
            name="Proveedor Combustible",
            rut="76.000.000-0",
        )
        fuel_policy, _ = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={
                "external_id": "policy-fuel-save",
                "is_active": True,
            },
        )
        ExpenseTypeCatalog.objects.create(
            policy=fuel_policy,
            name="Diesel",
            sync_status="synced",
            is_active=True,
        )
        expense = Expense.objects.create(
            status="pending",
            category=fuel_policy.name,
            supplier=supplier.name,
            supplier_rut=supplier.rut,
            is_vehicle=True,
        )

        response = self.client.post(
            reverse("expense_detail", args=[expense.pk]),
            {
                "status": "pending",
                "category_select": fuel_policy.name,
                "supplier_select": supplier.name,
                "supplier_rut": supplier.rut,
                "fuel_km": "1000",
                "fuel_liters": "45.5",
                "expense_type_select": "Diesel",
            },
        )

        self.assertRedirects(response, reverse("expense_list"))
        expense.refresh_from_db()
        self.assertEqual(expense.expense_type, "Diesel")

    def test_expense_table_displays_rindegastos_trace_id(self):
        expense = Expense.objects.create(
            status="pending",
            category=self.policy.name,
            supplier="Proveedor",
        )

        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, "ID Rindegastos")
        self.assertContains(response, _expense_export_id(expense.pk))
        self.assertContains(response, 'var sortState = { columnIndex: 1, direction: "desc" };')


class IncompleteExpenseStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="incomplete-admin@example.com",
            email="incomplete-admin@example.com",
            password="test",
        )
        self.client.force_login(self.user)
        self.policy = CategoryCatalog.objects.update_or_create(
            name="Oficina Central",
            defaults={"external_id": "policy-office", "is_active": True},
        )[0]
        self.supplier = SupplierCatalog.objects.create(
            name="Proveedor Incompleto",
            rut="76.000.000-0",
        )

    def test_incomplete_expense_cannot_be_parametrized_from_modal(self):
        expense = Expense.objects.create(
            status="incomplete",
            source="whatsapp",
            category=self.policy.name,
            supplier=self.supplier.name,
            supplier_rut=self.supplier.rut,
        )

        self.client.post(
            reverse("expense_detail", args=[expense.pk]),
            {
                "status": "completed",
                "category_select": self.policy.name,
                "supplier_select": self.supplier.name,
                "supplier_rut": self.supplier.rut,
            },
        )

        expense.refresh_from_db()
        self.assertEqual(expense.status, "incomplete")

    def test_not_completed_expense_cannot_be_parametrized_from_modal(self):
        expense = Expense.objects.create(
            status="not_completed",
            source="whatsapp",
            category=self.policy.name,
            supplier=self.supplier.name,
            supplier_rut=self.supplier.rut,
        )

        self.client.post(
            reverse("expense_detail", args=[expense.pk]),
            {
                "status": "completed",
                "category_select": self.policy.name,
                "supplier_select": self.supplier.name,
            },
        )

        expense.refresh_from_db()
        self.assertEqual(expense.status, "not_completed")


class ExpenseApprovalFlowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reviewer = User.objects.create_user(
            username="parametrizador@example.com",
            email="parametrizador@example.com",
            password="test",
            role="reviewer",
        )
        self.superadmin = User.objects.create_superuser(
            username="superadmin@example.com",
            email="superadmin@example.com",
            password="test",
        )
        self.expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
        )

    def test_reviewer_can_approve_only_parametrized_expense(self):
        self.client.force_login(self.reviewer)

        self.client.post(reverse("expense_action", args=[self.expense.pk, "approve"]))

        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "approved")
        self.assertEqual(self.expense.decision_by, self.reviewer)
        self.assertIsNotNone(self.expense.decision_at)
        self.assertTrue(
            ExpenseAuditLog.objects.filter(
                expense=self.expense,
                action="approved",
                actor=self.reviewer,
            ).exists()
        )

    def test_final_expense_cannot_be_edited_or_deleted_by_reviewer(self):
        self.expense.status = "rejected"
        self.expense.decision_by = self.reviewer
        self.expense.decision_at = timezone.now()
        self.expense.save()
        self.client.force_login(self.reviewer)

        self.client.post(
            reverse("expense_detail", args=[self.expense.pk]),
            {"supplier_select": "Proveedor modificado", "status": "completed"},
        )
        self.client.post(reverse("expense_action", args=[self.expense.pk, "delete"]))

        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "rejected")
        self.assertEqual(self.expense.supplier, "Proveedor")

    def test_reviewer_cannot_delete_non_final_expense_and_does_not_see_delete_button(self):
        self.client.force_login(self.reviewer)

        page = self.client.get(reverse("expense_list"))
        delete_url = reverse("expense_action", args=[self.expense.pk, "delete"])
        self.assertNotContains(page, delete_url)

        self.client.post(delete_url)

        self.assertTrue(Expense.objects.filter(pk=self.expense.pk).exists())

    def test_superadmin_sees_delete_button_and_final_form_is_locked(self):
        self.expense.status = "approved"
        self.expense.decision_by = self.superadmin
        self.expense.decision_at = timezone.now()
        self.expense.save()
        self.client.force_login(self.superadmin)

        page = self.client.get(reverse("expense_list"))

        self.assertContains(
            page,
            reverse("expense_action", args=[self.expense.pk, "delete"]),
        )
        self.assertContains(page, 'data-form-locked="true"')
        self.assertContains(page, "window.jQuery(select).prop('disabled', true)")
        self.assertContains(
            page,
            f'data-split-url="{reverse("expense_action", args=[self.expense.pk, "split"])}"',
        )
        self.assertContains(page, "disabled")

    def test_only_superadmin_can_revert_decision(self):
        self.expense.status = "approved"
        self.expense.decision_by = self.reviewer
        self.expense.decision_at = timezone.now()
        self.expense.save()

        self.client.force_login(self.reviewer)
        self.client.post(reverse("expense_action", args=[self.expense.pk, "revert_decision"]))
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "approved")

        self.client.force_login(self.superadmin)
        self.client.post(reverse("expense_action", args=[self.expense.pk, "revert_decision"]))
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "completed")
        self.assertIsNone(self.expense.decision_by)
        self.assertIsNone(self.expense.decision_at)
        self.assertTrue(
            ExpenseAuditLog.objects.filter(
                expense=self.expense,
                action="decision_reverted",
                actor=self.superadmin,
            ).exists()
        )


class RindegastosFieldsSettingsTests(TestCase):
    def setUp(self):
        self.superadmin = get_user_model().objects.create_superuser(
            username="fields-admin@example.com",
            email="fields-admin@example.com",
            password="test",
        )
        self.client.force_login(self.superadmin)

    def test_fields_maintainer_lists_policy_fields_and_options(self):
        policy, _ = CategoryCatalog.objects.update_or_create(
            name="Departamento Maquinaria",
            defaults={"external_id": "41786", "sync_status": "synced"},
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=policy,
            name="Vehiculo o Equipo",
            field_type="list",
            options=[{"Value": "Camión 1", "Code": "EVT01"}],
        )

        response = self.client.get(reverse("settings_rindegastos_fields"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Campos Rindegastos")
        self.assertContains(response, "Vehiculo o Equipo")
        self.assertContains(response, "Camión 1")
        self.assertNotContains(response, 'href="/configuracion/obras/"')

    def test_submitters_maintainer_lists_names_by_policy(self):
        policy, _ = CategoryCatalog.objects.update_or_create(
            name="Oficina Central",
            defaults={"external_id": "office-1", "sync_status": "synced"},
        )
        RindegastosExpenseFieldCatalog.objects.create(
            policy=policy,
            name="Nombre quien rinde",
            field_type="list",
            options=[{"Value": "Francisco Santibañez", "Code": "FS"}],
        )

        response = self.client.get(reverse("settings_rindegastos_submitters"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Usuarios Rindegastos")
        self.assertContains(response, "Oficina Central")
        self.assertContains(response, "Francisco Santibañez")
        self.assertContains(response, "FS")

    def test_sync_is_centralized_and_manual_creation_is_disabled(self):
        policy, _ = CategoryCatalog.objects.update_or_create(
            name="Oficina Central",
            defaults={
                "external_id": "office-1",
                "sync_status": "synced",
                "last_synced_at": timezone.now(),
            },
        )
        ExpenseTypeCatalog.objects.create(
            policy=policy,
            name="Gastos de oficina",
            sync_status="synced",
            last_synced_at=timezone.now(),
        )

        policies_page = self.client.get(reverse("settings_categories"))
        categories_page = self.client.get(reverse("settings_expense_types"))
        fields_page = self.client.get(reverse("settings_rindegastos_fields"))

        self.assertContains(policies_page, "Sincronización central")
        self.assertContains(policies_page, 'value="sync_rindegastos"')
        self.assertContains(policies_page, 'value="rebuild_rindegastos"')
        self.assertNotContains(policies_page, 'value="add_category"')
        self.assertNotContains(categories_page, 'value="add_expense_type"')
        self.assertNotContains(categories_page, 'value="sync_rindegastos"')
        self.assertNotContains(fields_page, 'value="sync_rindegastos"')
        self.assertContains(categories_page, "Última sincronización:")
        self.assertContains(fields_page, "Última sincronización:")
        self.assertContains(categories_page, reverse("settings_categories"))
        self.assertContains(fields_page, reverse("settings_categories"))

        self.client.post(reverse("settings_categories"), {"action": "add_category", "name": "Manual no permitida"})
        self.client.post(
            reverse("settings_expense_types"),
            {"action": "add_expense_type", "name": "Manual no permitida"},
        )

        self.assertFalse(CategoryCatalog.objects.filter(name="Manual no permitida").exists())
        self.assertFalse(ExpenseTypeCatalog.objects.filter(name="Manual no permitida").exists())


class SiiTaxIndicatorsSyncTests(TestCase):
    UTM_HTML = """
    <html><body>
      <table>
        <tr><td>Enero</td><td>67.294</td></tr>
        <tr><td>Febrero</td><td>67.429</td></tr>
      </table>
    </body></html>
    """

    MEPCO_HTML = """
    <html><body>
      <h2>Vigencia desde 25-06-2026</h2>
      <table>
        <tr><td>Gasolina Automotriz 93</td><td>6,0000</td><td>0,1234</td><td>6,1234</td><td>UTM/m3</td></tr>
        <tr><td>Petróleo Diesel</td><td>1,5000</td><td>-0,0500</td><td>1,4500</td><td>UTM/m3</td></tr>
      </table>
    </body></html>
    """

    def test_parses_utm_and_mepco_sample_html(self):
        sync = SiiTaxIndicatorSync(http_get=lambda url: "")

        utm_rows = sync.parse_utm_values(self.UTM_HTML, "https://www.sii.cl/utm2026.htm", 2026)
        mepco_rows = sync.parse_mepco_rates(self.MEPCO_HTML, "https://www.sii.cl/mepco2026.htm", 2026)

        self.assertEqual(len(utm_rows), 2)
        self.assertEqual(utm_rows[0]["month"], 1)
        self.assertEqual(utm_rows[0]["value"], Decimal("67294"))
        self.assertEqual(len(mepco_rows), 2)
        self.assertEqual(mepco_rows[0]["effective_date"], date(2026, 6, 25))
        self.assertEqual(mepco_rows[0]["fuel_key"], "gasolina_automotriz_93")
        self.assertEqual(mepco_rows[0]["resulting_tax"], Decimal("6.1234"))
        self.assertEqual(mepco_rows[1]["component_variable"], Decimal("-0.0500"))

    def test_sync_year_is_idempotent(self):
        def fake_get(url):
            if "utm" in url:
                return self.UTM_HTML
            return self.MEPCO_HTML

        sync = SiiTaxIndicatorSync(http_get=fake_get)

        first_stats = sync.sync_year(2026)
        second_stats = sync.sync_year(2026)

        self.assertEqual(first_stats, {"year": 2026, "utm_values": 2, "fuel_rates": 2})
        self.assertEqual(second_stats, first_stats)
        self.assertEqual(TaxIndicatorValue.objects.count(), 2)
        self.assertEqual(FuelSpecificTaxRate.objects.count(), 2)

    def test_tax_indicators_settings_view_lists_synced_values(self):
        superadmin = get_user_model().objects.create_superuser(
            username="tax-admin@example.com",
            email="tax-admin@example.com",
            password="test",
        )
        self.client.force_login(superadmin)
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=1, value=Decimal("67294"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 6, 25),
            fuel_name="Gasolina Automotriz 93",
            fuel_key="gasolina_automotriz_93",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.1234"),
            resulting_tax=Decimal("6.1234"),
            unit="UTM/m3",
        )

        response = self.client.get(reverse("settings_tax_indicators"), {"year": 2026})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Indicadores SII")
        self.assertContains(response, "Gasolina Automotriz 93")
        self.assertContains(response, "6.1234")

    def test_tax_indicators_mepco_endpoint_returns_incremental_rows(self):
        superadmin = get_user_model().objects.create_superuser(
            username="tax-endpoint-admin@example.com",
            email="tax-endpoint-admin@example.com",
            password="test",
        )
        self.client.force_login(superadmin)
        for index in range(3):
            FuelSpecificTaxRate.objects.create(
                effective_date=date(2026, 6, 25),
                fuel_name=f"Gasolina Automotriz {index}",
                fuel_key=f"gasolina_automotriz_{index}",
                component_base=Decimal("6.0000"),
                component_variable=Decimal("0.1234"),
                resulting_tax=Decimal("6.1234"),
                unit="UTM/m3",
            )

        response = self.client.get(
            reverse("settings_tax_indicators_mepco_data"),
            {"year": 2026, "offset": 1, "limit": 2},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["next_offset"], 3)
        self.assertFalse(payload["has_more"])
        self.assertEqual(len(payload["rows"]), 2)


class InvoiceTaxCalculationTests(TestCase):
    def test_calculates_non_fuel_invoice_iva_from_total(self):
        calculation = calculate_invoice_taxes(
            total=Decimal("119000"),
            paid_at=date(2026, 7, 10),
            document_type="Factura afecta",
            policy="Oficina Central",
        )

        self.assertEqual(calculation.source, "auto")
        self.assertEqual(calculation.iva_amount, Decimal("19000"))
        self.assertEqual(calculation.specific_tax_amount, Decimal("0"))

    def test_generic_gasoline_defaults_to_93_when_no_octane_is_selected(self):
        user = get_user_model().objects.create_superuser(
            username="tax-default-octane@example.com",
            email="tax-default-octane@example.com",
            password="test",
        )
        self.client.force_login(user)
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=7, value=Decimal("68000"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 93",
            fuel_key="gasolina_automotriz_93",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.1000"),
            resulting_tax=Decimal("6.1000"),
            unit="UTM/M3",
        )
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 97",
            fuel_key="gasolina_automotriz_97",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.5000"),
            resulting_tax=Decimal("6.5000"),
            unit="UTM/M3",
        )

        fuel_policy = CategoryCatalog.objects.update_or_create(
            name="Combustibles",
            defaults={"external_id": "policy-fuel-default-octane", "is_active": True},
        )[0]
        ExpenseTypeCatalog.objects.create(policy=fuel_policy, name="01 Bencina", is_active=True)
        self.client.post(
            reverse("expense_create"),
            {
                "status": "pending",
                "category_select": fuel_policy.name,
                "category_policy_id": str(fuel_policy.id),
                "amount": "100.000",
                "paid_at": "2026-07-10",
                "new_supplier_name": "Proveedor Bencina Default",
                "supplier_select": "Proveedor Bencina Default",
                "rindegastos_document_type": "Factura afecta",
                "fuel_liters": "50",
                "expense_type_select": "01 Bencina",
                "iva_amount": "0",
                "specific_tax_amount": "0",
                "tax_manual_override": "0",
            },
        )

        expense = Expense.objects.get(supplier="Proveedor Bencina Default")
        self.assertEqual(expense.gasoline_type, "93")
        self.assertEqual(expense.specific_tax_amount, Decimal("20740"))
        self.assertEqual(expense.tax_calculation_metadata["fuel_key"], "gasolina_automotriz_93")

    def test_calculates_fuel_invoice_with_direct_93_rate(self):
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=7, value=Decimal("68000"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 93",
            fuel_key="gasolina_automotriz_93",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.1000"),
            resulting_tax=Decimal("6.1000"),
            unit="UTM/M3",
        )

        calculation = calculate_invoice_taxes(
            total=Decimal("100000"),
            paid_at=date(2026, 7, 10),
            document_type="Factura afecta",
            policy="Combustibles",
            fuel_liters=Decimal("50"),
            fuel_type="Bencina 93",
        )

        self.assertEqual(calculation.source, "auto")
        self.assertEqual(calculation.specific_tax_amount, Decimal("20740"))
        self.assertEqual(calculation.iva_amount, Decimal("12655"))
        self.assertEqual(calculation.metadata["rate_strategy"], "sii_direct")
        self.assertEqual(calculation.metadata["fuel_key"], "gasolina_automotriz_93")

    def test_calculates_fuel_invoice_95_with_average_rate(self):
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=7, value=Decimal("68000"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 93",
            fuel_key="gasolina_automotriz_93",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.1000"),
            resulting_tax=Decimal("6.1000"),
            unit="UTM/M3",
        )
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 1),
            fuel_name="Gasolina Automotriz 97",
            fuel_key="gasolina_automotriz_97",
            component_base=Decimal("6.0000"),
            component_variable=Decimal("0.5000"),
            resulting_tax=Decimal("6.5000"),
            unit="UTM/M3",
        )

        calculation = calculate_invoice_taxes(
            total=Decimal("100000"),
            paid_at=date(2026, 7, 10),
            document_type="Factura afecta",
            policy="Combustibles",
            fuel_liters=Decimal("50"),
            fuel_type="Bencina 95",
        )

        self.assertEqual(calculation.source, "auto")
        self.assertEqual(calculation.specific_tax_amount, Decimal("21420"))
        self.assertEqual(calculation.metadata["rate_strategy"], "average_93_97")

    def test_calculates_fuel_invoice_iva_as_residual_after_integer_net(self):
        TaxIndicatorValue.objects.create(indicator="UTM", year=2026, month=7, value=Decimal("1000"))
        FuelSpecificTaxRate.objects.create(
            effective_date=date(2026, 7, 16),
            fuel_name="Petróleo Diesel",
            fuel_key="petroleo_diesel",
            component_base=Decimal("0"),
            component_variable=Decimal("0"),
            resulting_tax=Decimal("42.1250"),
            unit="UTM/M3",
        )

        calculation = calculate_invoice_taxes(
            total=Decimal("49920"),
            paid_at=date(2026, 7, 21),
            document_type="Factura afecta",
            policy="Combustibles",
            fuel_liters=Decimal("40"),
            fuel_type="02 Petróleo",
        )

        self.assertEqual(calculation.source, "auto")
        self.assertEqual(calculation.specific_tax_amount, Decimal("1685"))
        self.assertEqual(calculation.metadata["taxable_gross"], "48235")
        self.assertEqual(calculation.metadata["net_amount"], "40533")
        self.assertEqual(calculation.iva_amount, Decimal("7702"))


class SystemUsersSettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superadmin = User.objects.create_superuser(
            username="superadmin-users@example.com",
            email="superadmin-users@example.com",
            password="test",
        )
        self.admin = User.objects.create_user(
            username="admin-users@example.com",
            email="admin-users@example.com",
            password="test",
            role="admin",
        )
        self.target = User.objects.create_user(
            username="target-users@example.com",
            email="target-users@example.com",
            password="test",
            role="reviewer",
        )

    def test_superadmin_can_promote_user_to_superadmin(self):
        self.client.force_login(self.superadmin)

        self.client.post(
            reverse("settings_system_users"),
            {
                "action": "update_system_user",
                "user_id": self.target.pk,
                "email": self.target.email,
                "first_name": self.target.first_name,
                "last_name": self.target.last_name,
                "role": "admin",
                "is_active": "on",
                "is_superuser": "on",
            },
        )

        self.target.refresh_from_db()
        self.assertTrue(self.target.is_superuser)
        self.assertTrue(self.target.is_staff)

    def test_admin_cannot_promote_user_to_superadmin(self):
        self.client.force_login(self.admin)

        self.client.post(
            reverse("settings_system_users"),
            {
                "action": "update_system_user",
                "user_id": self.target.pk,
                "email": self.target.email,
                "first_name": self.target.first_name,
                "last_name": self.target.last_name,
                "role": "admin",
                "is_active": "on",
                "is_superuser": "on",
            },
        )

        self.target.refresh_from_db()
        self.assertFalse(self.target.is_superuser)
        self.assertFalse(self.target.is_staff)


class ExpenseAPICreationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.api_user = User.objects.create_user(
            username="api-user@example.com",
            email="api-user@example.com",
            password="test",
        )
        self.sender = AllowedSender.objects.create(
            phone="56911111111",
            first_name="Juan",
            last_name="Pérez",
            active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.api_user)

    def test_whatsapp_expense_uses_allowed_sender_not_api_user_as_creator(self):
        response = self.client.post(
            "/api/v1/expenses/",
            {
                "source": "whatsapp",
                "wa_sender_phone": self.sender.phone,
                "wa_message_id": "api-whatsapp-message-1",
                "status": "pending",
                "supplier": "Proveedor",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        expense = Expense.objects.get(wa_message_id="api-whatsapp-message-1")
        self.assertEqual(expense.wa_sender, self.sender)
        self.assertIsNone(expense.created_by)
        self.assertEqual(expense.source, "whatsapp")

    def test_web_expense_created_by_api_user_when_not_whatsapp(self):
        response = self.client.post(
            "/api/v1/expenses/",
            {
                "source": "web",
                "status": "pending",
                "supplier": "Proveedor web",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        expense = Expense.objects.get(supplier="Proveedor web")
        self.assertEqual(expense.created_by, self.api_user)
        self.assertIsNone(expense.wa_sender)
