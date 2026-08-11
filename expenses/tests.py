import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.management import call_command
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
    ExpenseNotification,
    ExpenseTypeCatalog,
    FuelSpecificTaxRate,
    RindegastosExpenseDiff,
    RindegastosExpenseSnapshot,
    RindegastosExpenseFieldCatalog,
    RindegastosReconcileRun,
    RindegastosTaxCatalog,
    SupplierCatalog,
    TaxIndicatorValue,
    normalize_rut,
)
from expenses.tasks import send_expense_notification_task
from expenses.invoice_tax_calculator import calculate_invoice_taxes
from expenses.rindegastos_client import RindegastosClient
from expenses.rindegastos_reconcile import (
    RindegastosExpenseReconciler,
    normalize_rindegastos_expense,
    normalized_payload_hash,
)
from expenses.rindegastos_sync import RindegastosCatalogSync
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, extract_otzi_ids, summarize_rindegastos_expense
from expenses.tax_indicators_sync import SiiTaxIndicatorSync
from expenses.views import _expense_export_id, _find_similar_expenses, _missing_fields_for_parametrization, _rindegastos_note
from expenses.whatsapp_notifications import build_rejection_payload, build_whatsapp_template_request

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

    def test_summarize_rindegastos_expense_includes_integration_otzi_ids(self):
        summary = summarize_rindegastos_expense(
            {
                "Id": 123,
                "IntegrationCode": "OTZ-YKQL54N2",
                "IntegrationExternalCode": "legacy OTZ-S5FAEKWM",
            }
        )

        self.assertEqual(summary["integration_otzi_ids"], ["OTZ-S5FAEKWM", "OTZ-YKQL54N2"])

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
        self.assertEqual(expense.rindegastos_integration_code, export_id)
        self.assertEqual(expense.rindegastos_report_id, "654")

    def test_sync_matches_local_expense_from_integration_code(self):
        expense = Expense.objects.create(
            status="completed",
            amount=Decimal("5900"),
            currency="CLP",
            category="Oficina Central",
            supplier="Proveedor",
            paid_at="2026-06-11",
        )
        export_id = _expense_export_id(expense.id)
        expense.rindegastos_integration_code = export_id
        expense.save(update_fields=["rindegastos_integration_code"])

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "ReportId": 654,
                        "Status": "Aprobado",
                        "IssueDate": "2026-06-11",
                        "Total": 5900,
                        "IntegrationCode": export_id,
                        "Note": "Nota sin identificador",
                    }
                ], {"Pages": 1}

        stats = RindegastosUploadedExpenseSync(client=FakeClient(), export_id_func=_expense_export_id).sync(
            since=timezone.datetime(2026, 4, 1).date(),
            until=timezone.datetime(2026, 7, 15).date(),
        )

        expense.refresh_from_db()
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["matched_by"]["integration_code"], 1)
        self.assertEqual(expense.rindegastos_expense_id, "987")
        self.assertEqual(expense.rindegastos_integration_code, export_id)

    def test_sync_matches_local_expense_from_existing_remote_id_when_note_lost_otzi(self):
        expense = Expense.objects.create(
            status="completed",
            amount=Decimal("5900"),
            currency="CLP",
            category="Oficina Central",
            supplier="Proveedor",
            paid_at="2026-06-11",
            rindegastos_expense_id="987",
        )
        export_id = _expense_export_id(expense.id)

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "ReportId": 654,
                        "Status": "Aprobado",
                        "IssueDate": "2026-06-11",
                        "Total": 5900,
                        "Note": "Nota editada en Rindegastos",
                    }
                ], {"Pages": 1}

        stats = RindegastosUploadedExpenseSync(client=FakeClient(), export_id_func=_expense_export_id).sync(
            since=timezone.datetime(2026, 4, 1).date(),
            until=timezone.datetime(2026, 7, 15).date(),
        )

        expense.refresh_from_db()
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["matched_by"]["remote_id"], 1)
        self.assertEqual(expense.rindegastos_integration_code, export_id)


class RindegastosClientTests(TestCase):
    class FakeResponse:
        def __init__(self, payload, status_code=200, text=""):
            self.payload = payload
            self.status_code = status_code
            self.text = text

        def json(self):
            return self.payload

    @patch("expenses.rindegastos_client.requests.get")
    def test_get_expense_fetches_detail_by_id(self, get_mock):
        get_mock.return_value = self.FakeResponse({"Expense": {"Id": 987, "Supplier": "Proveedor"}})

        result = RindegastosClient(base_url="https://api.example.test/v1", token="token", timeout=7).get_expense(987)

        self.assertEqual(result, {"Id": 987, "Supplier": "Proveedor"})
        get_mock.assert_called_once_with(
            "https://api.example.test/v1/getExpense",
            params={"Id": 987},
            headers={"Authorization": "Bearer token"},
            timeout=7,
        )

    @patch("expenses.rindegastos_client.requests.get")
    def test_get_expense_report_fetches_detail_by_id(self, get_mock):
        get_mock.return_value = self.FakeResponse({"ExpenseReport": {"Id": 654, "ReportNumber": "2095"}})

        result = RindegastosClient(base_url="https://api.example.test/v1", token="token", timeout=7).get_expense_report(654)

        self.assertEqual(result, {"Id": 654, "ReportNumber": "2095"})
        get_mock.assert_called_once_with(
            "https://api.example.test/v1/getExpenseReport",
            params={"Id": 654},
            headers={"Authorization": "Bearer token"},
            timeout=7,
        )

    @patch("expenses.rindegastos_client.requests.put")
    def test_set_expense_integration_sends_json_body(self, put_mock):
        put_mock.return_value = self.FakeResponse({"Id": 987, "IntegrationCode": "OTZ-ABC123"})

        result = RindegastosClient(base_url="https://api.example.test/v1", token="token").set_expense_integration(
            987,
            1,
            "OTZ-ABC123",
            "2026-08-05 10:30:00",
        )

        self.assertEqual(result["IntegrationCode"], "OTZ-ABC123")
        put_mock.assert_called_once_with(
            "https://api.example.test/v1/setExpenseIntegration",
            json={
                "Id": 987,
                "IntegrationStatus": 1,
                "IntegrationCode": "OTZ-ABC123",
                "IntegrationDate": "2026-08-05 10:30:00",
            },
            headers={
                "Authorization": "Bearer token",
                "Content-Type": "application/json",
            },
            timeout=20,
        )


class RindegastosExpenseReconcilerTests(TestCase):
    def test_normalized_payload_hash_is_stable_for_key_order(self):
        left = normalize_rindegastos_expense(
            {
                "Id": 987,
                "Supplier": " Proveedor   Uno ",
                "Total": "5900",
                "ExpenseExtraFields": [{"Name": "Centro de Costo / Faena", "Value": " Taller "}],
            }
        )
        right = normalize_rindegastos_expense(
            {
                "ExpenseExtraFields": [{"Value": "Taller", "Name": "Centro de Costo / Faena"}],
                "Total": 5900,
                "Supplier": "Proveedor Uno",
                "Id": 987,
            }
        )

        self.assertEqual(left, right)
        self.assertEqual(normalized_payload_hash(left), normalized_payload_hash(right))

    def test_reconcile_dry_run_does_not_create_snapshot_or_run(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "Total": 5900,
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "Total": 5900,
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
            dry_run=True,
        )

        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["changed_snapshots"], 1)
        self.assertEqual(RindegastosExpenseSnapshot.objects.count(), 0)
        self.assertEqual(RindegastosReconcileRun.objects.count(), 0)

    def test_reconcile_creates_snapshot_only_when_hash_changes(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def __init__(self):
                self.detail_total = 5900

            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "Total": self.detail_total,
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "Total": self.detail_total,
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

        client = FakeClient()
        reconciler = RindegastosExpenseReconciler(client=client, export_id_func=_expense_export_id)

        first = reconciler.reconcile(since=date(2026, 4, 1), until=date(2026, 8, 5))
        second = reconciler.reconcile(since=date(2026, 4, 1), until=date(2026, 8, 5))
        client.detail_total = 7900
        third = reconciler.reconcile(since=date(2026, 4, 1), until=date(2026, 8, 5))

        self.assertEqual(first["changed_snapshots"], 1)
        self.assertEqual(second["changed_snapshots"], 0)
        self.assertEqual(third["changed_snapshots"], 1)
        self.assertEqual(RindegastosExpenseSnapshot.objects.filter(expense=expense).count(), 2)
        self.assertEqual(RindegastosReconcileRun.objects.count(), 3)

    def test_reconcile_opens_diff_for_remote_value_mismatch(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            currency="CLP",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor Remoto",
                        "Total": 7900,
                        "Currency": "CLP",
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor Remoto",
                    "Total": 7900,
                    "Currency": "CLP",
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
        )

        self.assertEqual(stats["diffs_opened"], 2)
        self.assertEqual(RindegastosExpenseDiff.objects.filter(expense=expense, status="open").count(), 2)
        self.assertTrue(RindegastosExpenseDiff.objects.filter(field_name="supplier").exists())
        self.assertTrue(RindegastosExpenseDiff.objects.filter(field_name="total").exists())

    def test_reconcile_ignores_zero_equivalent_tax_noise(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            amount=Decimal("5900"),
            currency="CLP",
            specific_tax_amount=None,
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "Total": 5900,
                        "Currency": "CLP",
                        "OtherTaxes": 0,
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "Total": 5900,
                    "Currency": "CLP",
                    "OtherTaxes": 0,
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
        )

        self.assertEqual(stats["diffs_opened"], 0)
        self.assertFalse(RindegastosExpenseDiff.objects.filter(expense=expense, field_name="other_taxes").exists())

    def test_reconcile_reuses_existing_diff_when_snapshot_gets_report_context(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            currency="CLP",
            rindegastos_integration_code="OTZ-ABC123",
        )
        old_snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="987",
            rindegastos_report_id="13421804",
            payload_hash="a" * 64,
            normalized_payload={"supplier": "Proveedor Remoto"},
            raw_payload={"Id": "987"},
        )
        existing_diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=old_snapshot,
            field_name="supplier",
            local_value="Proveedor Local",
            remote_value="Proveedor Remoto",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "ReportId": "13421804",
                        "Supplier": "Proveedor Remoto",
                        "Total": 5900,
                        "Currency": "CLP",
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "ReportId": "13421804",
                    "Supplier": "Proveedor Remoto",
                    "Total": 5900,
                    "Currency": "CLP",
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

            def get_expense_report(self, report_id):
                return {"Id": report_id, "ReportNumber": "6571", "Title": "Informe visible"}

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
        )

        existing_diff.refresh_from_db()
        self.assertEqual(stats["diffs_opened"], 0)
        self.assertEqual(RindegastosExpenseDiff.objects.filter(expense=expense, status="open").count(), 1)
        self.assertNotEqual(existing_diff.snapshot_id, old_snapshot.id)
        self.assertEqual(existing_diff.snapshot.normalized_payload["rindegastos_report_number"], "6571")

    def test_reconcile_auto_applies_safe_diff_and_audits_change(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            currency="CLP",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor Remoto",
                        "Total": 5900,
                        "Currency": "CLP",
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "ReportId": "13421804",
                    "Supplier": "Proveedor Remoto",
                    "Total": 5900,
                    "Currency": "CLP",
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

            def get_expense_report(self, report_id):
                return {
                    "Id": report_id,
                    "ReportNumber": "6571",
                    "Title": "Informe usuario",
                    "EmployeeName": "Rendidor Otzi",
                }

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
            apply_safe_diffs=True,
        )

        expense.refresh_from_db()
        self.assertEqual(expense.supplier, "Proveedor Remoto")
        self.assertEqual(stats["diffs_auto_applied"], 1)
        self.assertEqual(stats["diffs_manual_review"], 0)
        self.assertTrue(
            RindegastosExpenseDiff.objects.filter(
                expense=expense,
                field_name="supplier",
                status=RindegastosExpenseDiff.STATUS_APPLIED,
            ).exists()
        )
        audit = ExpenseAuditLog.objects.get(expense=expense, source="rindegastos_reconcile")
        self.assertEqual(audit.changes["supplier"]["before"], "Proveedor Local")
        self.assertEqual(audit.changes["supplier"]["after"], "Proveedor Remoto")
        self.assertEqual(audit.changes["supplier"]["rindegastos_report_number"], "6571")
        self.assertEqual(audit.changes["supplier"]["rindegastos_report_title"], "Informe usuario")

    def test_reconcile_keeps_sensitive_diff_open_for_manual_review(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            amount=Decimal("5900"),
            currency="CLP",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "Total": 7900,
                        "Currency": "CLP",
                        "IntegrationCode": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "Total": 7900,
                    "Currency": "CLP",
                    "IntegrationCode": expense.rindegastos_integration_code,
                }

        stats = RindegastosExpenseReconciler(client=FakeClient(), export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
            apply_safe_diffs=True,
        )

        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("5900"))
        self.assertEqual(stats["diffs_auto_applied"], 0)
        self.assertEqual(stats["diffs_manual_review"], 1)
        self.assertTrue(
            RindegastosExpenseDiff.objects.filter(
                expense=expense,
                field_name="total",
                status=RindegastosExpenseDiff.STATUS_OPEN,
            ).exists()
        )

    def test_reconcile_does_not_overwrite_conflicting_remote_integration_code(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            marked = False

            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "IntegrationCode": "SAM-456",
                        "Note": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "IntegrationCode": "SAM-456",
                    "Note": expense.rindegastos_integration_code,
                }

            def set_expense_integration(self, *args, **kwargs):
                self.marked = True

        client = FakeClient()
        stats = RindegastosExpenseReconciler(client=client, export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
            mark_integration_code=True,
        )

        self.assertFalse(client.marked)
        self.assertEqual(stats["integration_code"]["non_otzi"], 1)
        self.assertEqual(stats["integration_code"]["skipped_conflict"], 1)

    @override_settings(RINDEGASTOS_MARK_INTEGRATION_CODE_ENABLED=True)
    def test_reconcile_marks_empty_remote_integration_code_when_enabled(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            rindegastos_integration_code="OTZ-ABC123",
        )

        class FakeClient:
            def __init__(self):
                self.marked_payload = None

            def get_expenses_page(self, params):
                return [
                    {
                        "Id": 987,
                        "Supplier": "Proveedor",
                        "Note": expense.rindegastos_integration_code,
                    }
                ], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "Supplier": "Proveedor",
                    "Note": expense.rindegastos_integration_code,
                }

            def set_expense_integration(self, expense_id, integration_status, integration_code, integration_date=None):
                self.marked_payload = {
                    "expense_id": expense_id,
                    "integration_status": integration_status,
                    "integration_code": integration_code,
                }

        client = FakeClient()
        stats = RindegastosExpenseReconciler(client=client, export_id_func=_expense_export_id).reconcile(
            since=date(2026, 4, 1),
            until=date(2026, 8, 5),
            mark_integration_code=True,
            integration_status=1,
        )

        self.assertEqual(stats["integration_code"]["empty"], 1)
        self.assertEqual(stats["integration_code"]["marked"], 1)
        self.assertEqual(
            client.marked_payload,
            {"expense_id": 987, "integration_status": 1, "integration_code": expense.rindegastos_integration_code},
        )

    def test_review_rindegastos_diffs_outputs_trace_remote_ids_and_flags(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            amount=Decimal("18679"),
            category="Departamento Maquinaria",
            paid_at=date(2026, 7, 15),
            rindegastos_expense_id="74973111",
            rindegastos_integration_code="OTZ-ABC123",
        )
        first_snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="74973111",
            rindegastos_report_id="100",
            payload_hash="a" * 64,
            normalized_payload={
                "total": "8962",
                "rindegastos_report_number": "6571",
                "rindegastos_report_title": "Informe visible",
            },
            raw_payload={"Id": "74973111"},
        )
        second_snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="74973112",
            rindegastos_report_id="100",
            payload_hash="b" * 64,
            normalized_payload={"total": "9521", "rindegastos_status": "Eliminado"},
            raw_payload={"Id": "74973112"},
        )
        RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=first_snapshot,
            field_name="total",
            local_value="18679",
            remote_value="8962",
            severity=RindegastosExpenseDiff.SEVERITY_CONFLICT,
        )
        RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=second_snapshot,
            field_name="total",
            local_value="18679",
            remote_value="9521",
            severity=RindegastosExpenseDiff.SEVERITY_CONFLICT,
        )

        out = io.StringIO()
        call_command("review_rindegastos_diffs", "--expense-id", str(expense.id), stdout=out)
        content = out.getvalue()

        self.assertIn("OTZ-ABC123", content)
        self.assertIn("74973111", content)
        self.assertIn("74973112", content)
        self.assertIn("6571", content)
        self.assertIn("Informe visible", content)
        self.assertIn("multiple_remote_expenses", content)
        self.assertIn("multiple_remote_totals", content)
        self.assertIn("remote_id_mismatch", content)
        self.assertIn("remote_deleted_like_status", content)
        self.assertIn("manual_review", content)

    def test_cleanup_rindegastos_diffs_resolves_duplicates_and_zero_tax_noise(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            amount=Decimal("5900"),
            rindegastos_integration_code="OTZ-ABC123",
        )
        old_snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="74704333",
            rindegastos_report_id="13257470",
            payload_hash="c" * 64,
            normalized_payload={"supplier": "Proveedor Remoto"},
            raw_payload={"Id": "74704333"},
        )
        enriched_snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="74704333",
            rindegastos_report_id="13257470",
            payload_hash="d" * 64,
            normalized_payload={
                "supplier": "Proveedor Remoto",
                "rindegastos_report_number": "6532",
                "rindegastos_report_title": "6532: Gastos mauricio Conejero",
            },
            raw_payload={"Id": "74704333"},
        )
        old_diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=old_snapshot,
            field_name="supplier",
            local_value="Proveedor",
            remote_value="Proveedor Remoto",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )
        enriched_diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=enriched_snapshot,
            field_name="supplier",
            local_value="Proveedor",
            remote_value="Proveedor Remoto",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )
        noise_diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=enriched_snapshot,
            field_name="other_taxes",
            local_value="",
            remote_value="0",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )

        out = io.StringIO()
        call_command("cleanup_rindegastos_diffs", "--dry-run", stdout=out)
        self.assertIn("Total to resolve: 2", out.getvalue())
        self.assertEqual(RindegastosExpenseDiff.objects.filter(status="open").count(), 3)

        call_command("cleanup_rindegastos_diffs", stdout=io.StringIO())

        old_diff.refresh_from_db()
        enriched_diff.refresh_from_db()
        noise_diff.refresh_from_db()
        self.assertEqual(old_diff.status, RindegastosExpenseDiff.STATUS_RESOLVED)
        self.assertEqual(enriched_diff.status, RindegastosExpenseDiff.STATUS_OPEN)
        self.assertEqual(noise_diff.status, RindegastosExpenseDiff.STATUS_RESOLVED)

    @patch("expenses.management.commands.inspect_rindegastos_expenses.RindegastosClient")
    def test_inspect_rindegastos_expenses_flags_detail_not_listed(self, client_class):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor",
            amount=Decimal("18679"),
            category="Departamento Maquinaria",
            paid_at=date(2026, 7, 15),
            rindegastos_expense_id="75528397",
            rindegastos_integration_code="OTZ-ABC123",
        )
        RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="75528979",
            rindegastos_report_id="13421804",
            payload_hash="c" * 64,
            normalized_payload={"total": "9521", "rindegastos_status": "Aprobado"},
            raw_payload={"Id": "75528979"},
        )

        class FakeClient:
            def get_expenses_page(self, params):
                return [], {"Pages": 1}

            def get_expense(self, expense_id):
                return {
                    "Id": expense_id,
                    "ReportId": "13421804",
                    "Status": 0,
                    "IssueDate": "2026-07-07",
                    "Supplier": "Proveedor remoto",
                    "Total": 9521,
                    "IntegrationCode": "OTZ-ABC123",
                }

            def get_expense_report(self, report_id):
                return {
                    "Id": report_id,
                    "ReportNumber": "8842",
                    "Title": "Repuestos en proceso",
                    "Status": 0,
                    "EmployeeName": "Octavio Olivares",
                    "SendDate": "2026-07-13",
                    "PolicyName": "Departamento Maquinaria",
                    "ReportTotal": 18483,
                    "ReportTotalApproved": 0,
                    "NbrExpenses": 2,
                    "NbrApprovedExpenses": 0,
                    "NbrRejectedExpenses": 0,
                }

        client_class.return_value = FakeClient()

        out = io.StringIO()
        call_command("inspect_rindegastos_expenses", "--expense-id", str(expense.id), stdout=out)
        content = out.getvalue()

        self.assertIn("75528397", content)
        self.assertIn("75528979", content)
        self.assertIn("detail_ok_but_not_listed", content)
        self.assertIn("not_found_in_getExpenses_window", content)
        self.assertIn("En proceso", content)
        self.assertIn("Abierto / En proceso", content)
        self.assertIn("8842", content)
        self.assertIn("Octavio Olivares", content)
        self.assertIn("2026-07-13", content)
        self.assertIn("Departamento Maquinaria", content)
        self.assertIn("18483", content)
        self.assertIn("OTZ-ABC123", content)


class FuelExpenseExportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="export@example.com",
            email="export@example.com",
            password="test",
        )
        self.client.force_login(self.user)

    def test_export_includes_combustible_columns_and_values(self):
        expense = Expense.objects.create(
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
        expense.refresh_from_db()
        self.assertEqual(expense.rindegastos_integration_code, _expense_export_id(expense.id))

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
        self.assertContains(response, "columnFilterValues")
        self.assertContains(response, "created_from")
        self.assertContains(response, "supplier")
        self.assertContains(response, "expensesClearFiltersBtn")
        self.assertContains(response, 'aria-label="Filtros activos"')
        self.assertContains(response, "Vista:")
        self.assertContains(response, "navigateWithParams")
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

        self.assertContains(response, "ID OTZ")
        self.assertContains(response, _expense_export_id(expense.pk))
        self.assertContains(response, 'var currentSort = "created_at";')

    def test_expense_list_trace_filter_uses_persisted_rindegastos_integration_code(self):
        Expense.objects.create(status="pending", supplier="Proveedor Comun")
        target = Expense.objects.create(
            status="approved",
            supplier="Proveedor Target",
            rindegastos_integration_code="OTZ-PERSISTED",
        )

        response = self.client.get(reverse("expense_list"), {"trace_id": "OTZ-PERSISTED"})

        self.assertEqual(response.context["paginator"].count, 1)
        self.assertContains(response, "Proveedor Target")
        self.assertContains(response, target.rindegastos_integration_code)
        self.assertEqual(
            [pill["label"] for pill in response.context["active_filter_pills"]],
            ["ID OTZ"],
        )
        self.assertNotContains(response, "Proveedor Comun")

    def test_expense_list_displays_active_filter_pills(self):
        Expense.objects.create(
            status="approved",
            supplier="Proveedor Target",
            rindegastos_integration_code="OTZ-PERSISTED",
        )

        response = self.client.get(
            reverse("expense_list"),
            {
                "scope": "approved",
                "trace_id": "OTZ-PERSISTED",
                "q": "Target",
                "page_size": "25",
            },
        )

        self.assertContains(response, "Vista:")
        self.assertContains(response, "Aprobado")
        self.assertContains(response, "ID OTZ:")
        self.assertContains(response, "OTZ-PERSISTED")
        self.assertContains(response, "Búsqueda:")
        self.assertContains(response, "Target")
        self.assertContains(response, "trace_id=")
        self.assertContains(response, "scope=active")

    def test_expense_list_displays_open_rindegastos_diffs(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            rindegastos_integration_code="OTZ-PERSISTED",
        )
        snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="75528397",
            rindegastos_report_id="13421804",
            payload_hash="d" * 64,
            normalized_payload={
                "supplier": "Proveedor Remoto",
                "rindegastos_report_number": "6571",
                "rindegastos_report_title": "Informe usuario",
            },
            raw_payload={"Id": "75528397"},
        )
        diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=snapshot,
            field_name="supplier",
            local_value="Proveedor Local",
            remote_value="Proveedor Remoto",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )

        response = self.client.get(reverse("expense_list"), {"trace_id": "OTZ-PERSISTED"})

        self.assertContains(response, "Cambios RG 1")
        self.assertContains(response, "Cambios detectados en Rindegastos")
        self.assertContains(response, "Proveedor Remoto")
        self.assertContains(response, "6571")
        self.assertContains(response, "Informe usuario")
        self.assertContains(response, str(diff.pk))
        self.assertContains(response, "apply_rindegastos_diff")
        self.assertContains(response, "ignore_rindegastos_diff")

    def test_viewer_does_not_see_rindegastos_diff_badge(self):
        viewer = get_user_model().objects.create_user(
            username="viewer@example.com",
            email="viewer@example.com",
            password="test",
            role="viewer",
        )
        self.client.force_login(viewer)
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            rindegastos_integration_code="OTZ-PERSISTED",
        )
        snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="75528397",
            rindegastos_report_id="13421804",
            payload_hash="f" * 64,
            normalized_payload={"supplier": "Proveedor Remoto"},
            raw_payload={"Id": "75528397"},
        )
        RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=snapshot,
            field_name="supplier",
            local_value="Proveedor Local",
            remote_value="Proveedor Remoto",
            severity=RindegastosExpenseDiff.SEVERITY_WARNING,
        )

        response = self.client.get(reverse("expense_list"), {"trace_id": "OTZ-PERSISTED"})

        self.assertNotContains(response, "Cambios RG 1")
        self.assertNotContains(response, "Cambios detectados en Rindegastos")

    def test_reviewer_can_view_rindegastos_rules(self):
        response = self.client.get(reverse("settings_rindegastos_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reglas Rindegastos")
        self.assertContains(response, "Autoactualización")
        self.assertContains(response, "Revisión manual")
        self.assertContains(response, "Proveedor")
        self.assertContains(response, "Múltiples gastos remotos")

    def test_viewer_cannot_view_rindegastos_rules(self):
        viewer = get_user_model().objects.create_user(
            username="viewer-rules@example.com",
            email="viewer-rules@example.com",
            password="test",
            role="viewer",
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("settings_rindegastos_rules"))

        self.assertRedirects(response, reverse("expense_list"))

    def test_apply_rindegastos_diff_action_updates_sensitive_field(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Local",
            amount=Decimal("5900"),
            rindegastos_integration_code="OTZ-PERSISTED",
        )
        snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="75528397",
            rindegastos_report_id="13421804",
            payload_hash="e" * 64,
            normalized_payload={"total": "7900"},
            raw_payload={"Id": "75528397"},
        )
        diff = RindegastosExpenseDiff.objects.create(
            expense=expense,
            snapshot=snapshot,
            field_name="total",
            local_value="5900",
            remote_value="7900",
            severity=RindegastosExpenseDiff.SEVERITY_CONFLICT,
        )

        response = self.client.post(
            reverse("expense_action", args=[expense.pk, "apply_rindegastos_diff"]),
            {"diff_id": diff.pk},
        )

        self.assertRedirects(response, reverse("expense_list"))
        expense.refresh_from_db()
        diff.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("7900"))
        self.assertEqual(diff.status, RindegastosExpenseDiff.STATUS_APPLIED)
        self.assertTrue(
            ExpenseAuditLog.objects.filter(
                expense=expense,
                source="rindegastos_manual_review",
                changes__amount__after="7900",
            ).exists()
        )

    def test_expense_list_displays_audit_change_details(self):
        expense = Expense.objects.create(
            status="completed",
            supplier="Proveedor Remoto",
            amount=Decimal("5900"),
            rindegastos_integration_code="OTZ-PERSISTED",
        )
        RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            rindegastos_expense_id="75528397",
            rindegastos_report_id="13421804",
            payload_hash="g" * 64,
            normalized_payload={
                "rindegastos_report_id": "13421804",
                "rindegastos_report_number": "6571",
                "rindegastos_report_title": "Informe usuario",
            },
            raw_payload={"Id": "75528397"},
        )
        ExpenseAuditLog.objects.create(
            expense=expense,
            expense_snapshot_id=expense.id,
            action="updated",
            source="rindegastos_reconcile",
            reason="Cambio aplicado desde diferencias detectadas en Rindegastos.",
            changes={
                "supplier": {
                    "before": "Proveedor Local",
                    "after": "Proveedor Remoto",
                    "rindegastos_expense_id": "75528397",
                    "rindegastos_report_id": "13421804",
                    "rindegastos_field": "supplier",
                }
            },
        )

        response = self.client.get(reverse("expense_list"), {"trace_id": "OTZ-PERSISTED"})

        self.assertContains(response, "Proveedor:")
        self.assertContains(response, "Proveedor Local")
        self.assertContains(response, "Proveedor Remoto")
        self.assertContains(response, "RG 75528397")
        self.assertContains(response, "Informe 6571")
        self.assertNotContains(response, "Informe API 13421804")

    def test_expense_list_defaults_to_active_statuses_and_paginates(self):
        for index in range(55):
            Expense.objects.create(status="pending", supplier=f"Proveedor Activo {index:02d}")
        approved = Expense.objects.create(status="approved", supplier="Proveedor Aprobado")

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.context["current_scope"], "active")
        self.assertEqual(response.context["page_size"], 50)
        self.assertEqual(len(response.context["gastos"]), 50)
        self.assertEqual(response.context["paginator"].count, 55)
        self.assertNotContains(response, _expense_export_id(approved.pk))
        self.assertContains(response, "Mostrando 1-50 de 55 gastos filtrados.")

    def test_expense_list_status_scope_reaches_final_expenses(self):
        rejected = Expense.objects.create(status="rejected", supplier="Proveedor Rechazado")
        Expense.objects.create(status="pending", supplier="Proveedor Pendiente")

        response = self.client.get(reverse("expense_list"), {"scope": "rejected"})

        self.assertEqual(response.context["current_scope"], "rejected")
        self.assertContains(response, _expense_export_id(rejected.pk))
        self.assertNotContains(response, "Proveedor Pendiente")

    def test_expense_list_search_filters_before_pagination(self):
        for index in range(60):
            Expense.objects.create(status="pending", supplier=f"Proveedor Comun {index:02d}")
        target = Expense.objects.create(status="approved", supplier="Transporte Especial Norte")

        response = self.client.get(reverse("expense_list"), {"scope": "all", "q": "Especial", "page_size": "25"})

        self.assertEqual(response.context["paginator"].count, 1)
        self.assertEqual(response.context["page_size"], 25)
        self.assertContains(response, _expense_export_id(target.pk))
        self.assertContains(response, "Transporte Especial Norte")
        self.assertNotContains(response, "Proveedor Comun 00")

    def test_expense_list_column_filters_run_before_pagination(self):
        for index in range(60):
            Expense.objects.create(
                status="pending",
                supplier=f"Proveedor Comun {index:02d}",
                category="Combustibles",
            )
        target = Expense.objects.create(
            status="rejected",
            supplier="Proveedor Rechazado Especial",
            category="Peajes",
            amount=Decimal("12345"),
        )

        response = self.client.get(
            reverse("expense_list"),
            {
                "status": "rejected",
                "supplier": "Especial",
                "category": "Peajes",
                "amount_min": "10000",
                "page_size": "25",
            },
        )

        self.assertEqual(response.context["current_scope"], "active")
        self.assertEqual(response.context["paginator"].count, 1)
        self.assertContains(response, _expense_export_id(target.pk))
        self.assertContains(response, "Proveedor Rechazado Especial")
        self.assertNotContains(response, "Proveedor Comun 00")

    def test_expense_list_sorts_in_backend_before_pagination(self):
        low = Expense.objects.create(status="pending", supplier="Monto Bajo", amount=Decimal("1000"))
        high = Expense.objects.create(status="pending", supplier="Monto Alto", amount=Decimal("999999"))
        for index in range(60):
            Expense.objects.create(status="pending", supplier=f"Monto Medio {index:02d}", amount=Decimal("50000"))

        response = self.client.get(
            reverse("expense_list"),
            {"sort": "amount", "direction": "desc", "page_size": "25"},
        )

        gastos = list(response.context["gastos"])
        self.assertEqual(gastos[0].pk, high.pk)
        self.assertNotIn(low.pk, [expense.pk for expense in gastos])


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
            amount=Decimal("50000"),
            currency="CLP",
            wa_sender_phone="56911111111",
            wa_phone_number_id="phone-number-id",
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

    @patch("expenses.views.enqueue_notification_send", return_value=True)
    def test_reject_requires_reason_and_creates_single_notification(self, enqueue_mock):
        self.client.force_login(self.reviewer)

        self.client.post(reverse("expense_action", args=[self.expense.pk, "reject"]), {"reason": ""})
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "completed")
        self.assertEqual(ExpenseNotification.objects.count(), 0)

        self.client.post(
            reverse("expense_action", args=[self.expense.pk, "reject"]),
            {"reason": "El comprobante no permite validar el monto."},
        )
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.status, "rejected")
        self.assertEqual(self.expense.decision_by, self.reviewer)
        self.assertIsNotNone(self.expense.decision_at)
        self.assertEqual(self.expense.rejection_reason, "El comprobante no permite validar el monto.")
        self.assertEqual(ExpenseNotification.objects.count(), 1)
        notification = ExpenseNotification.objects.get()
        self.assertEqual(notification.recipient, "56911111111")
        self.assertEqual(notification.status, "pending")
        self.assertEqual(notification.payload["trace_id"], _expense_export_id(self.expense.pk))
        self.assertEqual(notification.payload["template_parameters"][2], "El comprobante no permite validar el monto.")
        enqueue_mock.assert_called_once_with(notification)

        self.client.post(
            reverse("expense_action", args=[self.expense.pk, "reject"]),
            {"reason": "Segundo click"},
        )
        self.assertEqual(ExpenseNotification.objects.count(), 1)

    def test_rejection_payload_includes_trace_amount_supplier_and_reason(self):
        self.expense.rejection_reason = "Factura ilegible"
        payload = build_rejection_payload(self.expense)

        self.assertEqual(payload["trace_id"], _expense_export_id(self.expense.pk))
        self.assertEqual(payload["amount"], "$50.000 CLP")
        self.assertEqual(payload["supplier"], "Proveedor")
        self.assertEqual(payload["summary"], "$50.000 CLP (Proveedor)")
        self.assertEqual(payload["reason"], "Factura ilegible")
        self.assertEqual(payload["header_parameters"], [_expense_export_id(self.expense.pk)])
        self.assertEqual(
            payload["template_parameters"],
            [_expense_export_id(self.expense.pk), "$50.000 CLP (Proveedor)", "Factura ilegible"],
        )

    def test_template_request_includes_header_and_body_variables(self):
        self.expense.decision_at = timezone.now()
        self.expense.rejection_reason = "Factura ilegible"
        self.expense.save(update_fields=["decision_at", "rejection_reason"])
        notification = ExpenseNotification.objects.create(
            expense=self.expense,
            recipient="56911111111",
            decision_at=self.expense.decision_at,
            template_name="rechazo_rendicion",
            template_language="es_CL",
            payload=build_rejection_payload(self.expense),
        )

        request_payload = build_whatsapp_template_request(notification)

        components = request_payload["template"]["components"]
        self.assertEqual(components[0]["type"], "header")
        self.assertEqual(components[0]["parameters"][0]["text"], _expense_export_id(self.expense.pk))
        self.assertEqual(components[1]["type"], "body")
        self.assertEqual(
            [parameter["text"] for parameter in components[1]["parameters"]],
            [_expense_export_id(self.expense.pk), "$50.000 CLP (Proveedor)", "Factura ilegible"],
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

        page = self.client.get(reverse("expense_list"), {"scope": "all"})

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
        self.expense.rejection_reason = "Debe limpiarse"
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
        self.assertEqual(self.expense.rejection_reason, "")
        self.assertTrue(
            ExpenseAuditLog.objects.filter(
                expense=self.expense,
                action="decision_reverted",
                actor=self.superadmin,
            ).exists()
        )

    @override_settings(WA_ACCESS_TOKEN="token", WA_NOTIFICATION_MAX_ATTEMPTS=2, WA_PHONE_NUMBER_ID="")
    @patch("expenses.whatsapp_notifications.requests.post")
    def test_send_notification_task_marks_sent_and_stores_provider_id(self, post_mock):
        self.expense.status = "rejected"
        self.expense.decision_at = timezone.now()
        self.expense.rejection_reason = "Factura ilegible"
        self.expense.save()
        notification = ExpenseNotification.objects.create(
            expense=self.expense,
            recipient="56911111111",
            decision_at=self.expense.decision_at,
            template_name="expense_rejection",
            template_language="es_CL",
            payload=build_rejection_payload(self.expense),
        )
        post_mock.return_value.status_code = 200
        post_mock.return_value.json.return_value = {"messages": [{"id": "wamid.123"}]}

        result = send_expense_notification_task(notification.id)

        notification.refresh_from_db()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(notification.status, "sent")
        self.assertEqual(notification.provider_message_id, "wamid.123")
        self.assertEqual(notification.payload["last_provider_status_code"], 200)

    @override_settings(WA_ACCESS_TOKEN="token", WA_PHONE_NUMBER_ID="fallback-phone-number-id")
    @patch("expenses.whatsapp_notifications.requests.post")
    def test_send_notification_uses_configured_phone_number_id_when_expense_does_not_have_one(self, post_mock):
        self.expense.status = "rejected"
        self.expense.decision_at = timezone.now()
        self.expense.rejection_reason = "Factura ilegible"
        self.expense.wa_phone_number_id = ""
        self.expense.save()
        notification = ExpenseNotification.objects.create(
            expense=self.expense,
            recipient="56911111111",
            decision_at=self.expense.decision_at,
            template_name="expense_rejection",
            template_language="es_CL",
            payload=build_rejection_payload(self.expense),
        )
        post_mock.return_value.status_code = 200
        post_mock.return_value.json.return_value = {"messages": [{"id": "wamid.123"}]}

        send_expense_notification_task(notification.id)

        self.assertIn("/fallback-phone-number-id/messages", post_mock.call_args.args[0])

    @override_settings(WA_ACCESS_TOKEN="token", WA_NOTIFICATION_MAX_ATTEMPTS=2, WA_PHONE_NUMBER_ID="")
    @patch("expenses.whatsapp_notifications.requests.post")
    def test_send_notification_task_retries_transient_errors_and_fails_permanent_errors(self, post_mock):
        self.expense.status = "rejected"
        self.expense.decision_at = timezone.now()
        self.expense.rejection_reason = "Factura ilegible"
        self.expense.save()
        notification = ExpenseNotification.objects.create(
            expense=self.expense,
            recipient="56911111111",
            decision_at=self.expense.decision_at,
            template_name="expense_rejection",
            template_language="es_CL",
            payload=build_rejection_payload(self.expense),
        )
        post_mock.return_value.status_code = 500
        post_mock.return_value.json.return_value = {"error": {"message": "Temporal"}}

        result = send_expense_notification_task(notification.id)

        notification.refresh_from_db()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(notification.status, "pending")
        self.assertIsNotNone(notification.next_retry_at)

        notification.next_retry_at = timezone.now()
        notification.save(update_fields=["next_retry_at"])
        post_mock.return_value.status_code = 400
        post_mock.return_value.json.return_value = {"error": {"message": "Plantilla inválida"}}

        result = send_expense_notification_task(notification.id)

        notification.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(notification.status, "failed")
        self.assertIn("HTTP 400", notification.last_error)


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
