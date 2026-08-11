from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import ExpenseAuditLog, RindegastosExpenseDiff


ZERO_EQUIVALENT_DIFF_FIELDS = {"tax_amount", "other_taxes"}

AUTO_APPLY_FIELD_MAP = {
    "supplier": "supplier",
    "policy_name": "category",
    "category_name": "expense_type",
    "tax_name": "rindegastos_tax",
    "custom_fields.RUT proveedor": "supplier_rut",
    "custom_fields.Centro de Costo / Faena": "rindegastos_cost_center",
}

APPLY_FIELD_MAP = {
    **AUTO_APPLY_FIELD_MAP,
    "total": "amount",
    "currency": "currency",
    "issue_date": "paid_at",
    "tax_amount": "iva_amount",
    "other_taxes": "specific_tax_amount",
    "custom_fields.Nombre quien rinde": "rindegastos_submitter",
    "custom_fields.Tipo de Documento": "rindegastos_document_type",
    "custom_fields.Numero de Documento": "document_number",
    "custom_fields.Vehiculo o Equipo": "vehicle",
    "custom_fields.Km.Carguio": "fuel_km",
    "custom_fields.Litros Combustible": "fuel_liters",
    "custom_fields.Categoria": "expense_type",
}

MANUAL_REVIEW_FIELDS = {
    "total",
    "currency",
    "issue_date",
    "custom_fields.Vehiculo o Equipo",
    "custom_fields.Nombre quien rinde",
    "custom_fields.Tipo de Documento",
    "custom_fields.Numero de Documento",
    "custom_fields.Km.Carguio",
    "custom_fields.Litros Combustible",
}

AUTO_APPLY_RULES = [
    {
        "field": "Proveedor",
        "condition": "Se aplica si Rindegastos trae un proveedor no vacío y el gasto local no está aprobado ni rechazado.",
    },
    {
        "field": "Política",
        "condition": "Se aplica si Rindegastos trae una política distinta y el gasto local no está aprobado ni rechazado.",
    },
    {
        "field": "Categoría / tipo de gasto",
        "condition": "Se aplica si Rindegastos trae una categoría distinta y el gasto local no está aprobado ni rechazado.",
    },
    {
        "field": "Impuesto",
        "condition": "Se aplica el nombre de impuesto cuando viene informado desde Rindegastos.",
    },
    {
        "field": "RUT proveedor",
        "condition": "Se aplica si Rindegastos trae RUT proveedor no vacío.",
    },
    {
        "field": "Centro de Costo / Faena",
        "condition": "Se aplica si Rindegastos trae centro de costo no vacío.",
    },
    {
        "field": "Monto menor",
        "condition": "Solo se aplica si la diferencia es menor o igual a $500 CLP y menor o igual a 1%.",
    },
]

MANUAL_REVIEW_RULES = [
    {
        "field": "Monto relevante",
        "condition": "Queda pendiente si supera $500 CLP, supera 1%, cambia moneda o hay más de un gasto Rindegastos asociado.",
    },
    {
        "field": "Moneda",
        "condition": "Siempre requiere revisión manual.",
    },
    {
        "field": "Fecha del gasto",
        "condition": "Siempre requiere revisión manual.",
    },
    {
        "field": "Vehículo o Equipo",
        "condition": "Siempre requiere revisión manual por impacto operacional.",
    },
    {
        "field": "Rendidor",
        "condition": "Siempre requiere revisión manual.",
    },
    {
        "field": "Documento",
        "condition": "Tipo y número de documento requieren revisión manual.",
    },
    {
        "field": "Km / litros",
        "condition": "Campos de combustible requieren revisión manual.",
    },
    {
        "field": "Gasto aprobado o rechazado",
        "condition": "No se autoactualiza ningún campo si el gasto local ya tiene decisión final.",
    },
    {
        "field": "Múltiples gastos remotos",
        "condition": "Si un OTZ local aparece asociado a más de un gasto Rindegastos, todo queda en revisión manual.",
    },
]


def classify_diff(diff_spec, expense, remote_ids_count=1):
    field_name = diff_spec["field_name"]
    if remote_ids_count > 1:
        return "manual_review"
    if expense.status in {"approved", "rejected"}:
        return "manual_review"
    if field_name in AUTO_APPLY_FIELD_MAP and diff_spec.get("remote_value") not in {None, ""}:
        return "auto_apply"
    if field_name == "total" and _can_auto_apply_small_total_change(diff_spec, expense):
        return "auto_apply"
    return "manual_review"


def canonical_diff_value(field_name, value):
    if field_name in ZERO_EQUIVALENT_DIFF_FIELDS:
        zeroish = _zeroish_decimal(value)
        if zeroish is not None:
            return format(zeroish.normalize(), "f")
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).strip()


def diff_values_equivalent(field_name, left, right):
    return canonical_diff_value(field_name, left) == canonical_diff_value(field_name, right)


def should_ignore_diff(field_name, local_value, remote_value):
    if field_name not in ZERO_EQUIVALENT_DIFF_FIELDS:
        return False
    return _zeroish_decimal(local_value) == Decimal("0") and _zeroish_decimal(remote_value) == Decimal("0")


def apply_rindegastos_diff(diff, actor=None, source="rindegastos_reconcile"):
    target_field = _target_field_for_diff(diff)
    if not target_field:
        return False

    with transaction.atomic():
        expense = diff.expense.__class__.objects.select_for_update().get(pk=diff.expense_id)
        report_context = _snapshot_report_context(diff)
        before = getattr(expense, target_field)
        after = _coerce_remote_value(target_field, diff.remote_value)
        if _values_equal(before, after):
            diff.status = RindegastosExpenseDiff.STATUS_RESOLVED
            diff.resolved_at = timezone.now()
            diff.resolved_by = actor
            diff.save(update_fields=["status", "resolved_at", "resolved_by"])
            return False

        setattr(expense, target_field, after)
        update_fields = [target_field]
        extra_changes = {}
        if target_field == "vehicle" and after and not expense.is_vehicle:
            before_vehicle_flag = expense.is_vehicle
            expense.is_vehicle = True
            update_fields.append("is_vehicle")
            extra_changes["is_vehicle"] = {
                "before": before_vehicle_flag,
                "after": True,
                "rindegastos_expense_id": diff.snapshot.rindegastos_expense_id,
                "rindegastos_report_id": diff.snapshot.rindegastos_report_id,
                **report_context,
                "rindegastos_field": diff.field_name,
            }
        expense.save(update_fields=update_fields)
        diff.status = RindegastosExpenseDiff.STATUS_APPLIED
        diff.resolved_at = timezone.now()
        diff.resolved_by = actor
        diff.save(update_fields=["status", "resolved_at", "resolved_by"])
        ExpenseAuditLog.objects.create(
            expense=expense,
            expense_snapshot_id=expense.id,
            action="updated",
            actor=actor,
            actor_name=(actor.get_full_name() or actor.email) if actor else "",
            source=source,
            reason="Cambio aplicado desde diferencias detectadas en Rindegastos.",
            changes={
                target_field: {
                    "before": _serialize_value(before),
                    "after": _serialize_value(after),
                    "rindegastos_expense_id": diff.snapshot.rindegastos_expense_id,
                    "rindegastos_report_id": diff.snapshot.rindegastos_report_id,
                    **report_context,
                    "rindegastos_field": diff.field_name,
                },
                **extra_changes,
            },
        )
        return True


def ignore_rindegastos_diff(diff, actor=None):
    diff.status = RindegastosExpenseDiff.STATUS_IGNORED
    diff.resolved_at = timezone.now()
    diff.resolved_by = actor
    diff.save(update_fields=["status", "resolved_at", "resolved_by"])


def _target_field_for_diff(diff):
    return APPLY_FIELD_MAP.get(diff.field_name)


def _can_auto_apply_small_total_change(diff_spec, expense):
    if expense.currency != "CLP" or expense.amount is None:
        return False
    try:
        local = Decimal(str(diff_spec.get("local_value")))
        remote = Decimal(str(diff_spec.get("remote_value")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if local == 0:
        return False
    absolute_delta = abs(remote - local)
    percent_delta = absolute_delta / abs(local)
    return absolute_delta <= Decimal("500") and percent_delta <= Decimal("0.01")


def _zeroish_decimal(value):
    if value in {None, ""}:
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _coerce_remote_value(target_field, value):
    if target_field in {"amount", "iva_amount", "specific_tax_amount", "fuel_km", "fuel_liters"}:
        return Decimal(str(value))
    if target_field == "paid_at":
        parsed = parse_date(str(value))
        return parsed
    return "" if value is None else str(value)


def _values_equal(left, right):
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, TypeError, ValueError):
            return False
    return (left or "") == (right or "")


def _serialize_value(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _snapshot_report_context(diff):
    normalized = diff.snapshot.normalized_payload or {}
    return {
        "rindegastos_report_number": normalized.get("rindegastos_report_number") or "",
        "rindegastos_report_title": normalized.get("rindegastos_report_title") or "",
        "rindegastos_report_employee": normalized.get("rindegastos_report_employee") or "",
    }
