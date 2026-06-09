from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from expenses.models import CategoryCatalog, Expense, RindegastosExpenseFieldCatalog
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
        self.assertContains(response, 'data-rindegastos-field="Vehiculo o Equipo"')
        self.assertIn(
            {
                "policy_id": policy.id,
                "field_name": "Vehiculo o Equipo",
                "value": "Camión 01",
                "code": "CAM-01",
            },
            response.context["rindegastos_field_options"],
        )
