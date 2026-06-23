from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from expenses.models import (
    CategoryCatalog,
    Expense,
    ExpenseAuditLog,
    ExpenseTypeCatalog,
    RindegastosExpenseFieldCatalog,
    RindegastosTaxCatalog,
    SupplierCatalog,
)
from expenses.rindegastos_sync import RindegastosCatalogSync
from expenses.views import _expense_export_id, _missing_fields_for_parametrization, _rindegastos_note


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

        response = self.client.get(reverse("expense_rindegastos_export"))
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
        self.assertContains(response, 'class="modal fade supplier-quick-modal"')
        self.assertContains(response, "supplier-quick-backdrop")
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
        self.assertContains(page, "No se puede dividir en este estado")

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
