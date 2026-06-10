from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from expenses.models import CategoryCatalog, Expense, RindegastosExpenseFieldCatalog, SupplierCatalog
from expenses.views import _missing_fields_for_parametrization


class FuelExpenseValidationTests(TestCase):
    def test_combustibles_requires_km_and_liters(self):
        expense = Expense(
            amount=Decimal("50000"),
            currency="CLP",
            category="Combustibles",
            supplier="Proveedor",
            rindegastos_cost_center="Faena",
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
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            is_vehicle=True,
            vehicle="Camion 12",
            fuel_km=Decimal("154320"),
            fuel_liters=Decimal("45.5"),
        )

        self.assertEqual(_missing_fields_for_parametrization(expense), [])


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
            paid_at="2026-06-09",
            rindegastos_document_type="Boleta",
            is_vehicle=True,
            vehicle="Camion 12",
            fuel_km=Decimal("154320"),
            fuel_liters=Decimal("45.5"),
        )

        response = self.client.get(reverse("expense_rindegastos_export"))
        content = response.content.decode("utf-8-sig")

        self.assertEqual(response.status_code, 200)
        self.assertIn("vehiculo_equipo,km_carguio,litros_combustible", content)
        self.assertIn("Camion 12,154320,45.5", content)

        page = self.client.get(reverse("expense_list"))
        self.assertContains(page, 'name="fuel_km"')
        self.assertContains(page, 'name="fuel_liters"')
        self.assertContains(page, 'value="154320.00"')


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

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'class="form-select js-searchable-select" data-rindegastos-field="Vehiculo o Equipo"',
        )
        self.assertIn(
            {
                "policy_id": policy.id,
                "field_name": "Vehiculo o Equipo",
                "value": "Camión 01",
                "code": "CAM-01",
            },
            response.context["rindegastos_field_options"],
        )


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
        self.assertEqual(supplier.rut, "76.123.456-7")
        self.assertEqual(expense.supplier_rut, supplier.rut)
        self.assertEqual(expense.worksite, "Obra reportada")
        self.assertIsNone(expense.worksite_standard)

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
        self.assertEqual(expense.supplier_rut, "77.777.777-7")

    def test_modal_uses_synced_policies_and_has_no_standard_worksite(self):
        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, "Obra (ingresada por usuario)")
        self.assertNotContains(response, 'name="worksite_standard"')
        self.assertNotContains(response, 'name="new_category_name"')
        self.assertNotContains(response, 'name="expense_type_other"')
        self.assertNotContains(response, "Categoría Rindegastos (detalle)")
        self.assertContains(response, 'name="supplier_rut"')

    def test_edit_modal_uses_side_receipt_viewer(self):
        expense = Expense.objects.create(
            status="pending",
            category=self.policy.name,
            supplier="Proveedor",
        )

        response = self.client.get(reverse("expense_list"))

        self.assertContains(response, f'id="expenseModal{expense.pk}"')
        self.assertContains(response, 'class="modal-body expense-workspace"')
        self.assertContains(response, 'class="expense-form-pane"')
        self.assertContains(response, 'class="expense-receipt-pane"')


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
