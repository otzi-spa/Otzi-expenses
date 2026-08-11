from functools import wraps
import csv
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch
from django.db.models import Max, Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.auth import get_user_model
from accounts.models import UserAuditLog
from .models import (
    Expense,
    Attachment,
    AllowedSender,
    CategoryCatalog,
    ExpenseAuditLog,
    ExpenseNotification,
    ExpenseTypeCatalog,
    FuelSpecificTaxRate,
    RindegastosExpenseFieldCatalog,
    RindegastosExpenseDiff,
    RindegastosTaxCatalog,
    SupplierCatalog,
    TaxIndicatorValue,
    WorksiteCatalog,
    SYNC_STATUS,
    normalize_rut,
)
from django.contrib.auth.decorators import login_required
from django.utils.dateparse import parse_date
from django.contrib import messages
from django.utils import timezone
from requests import RequestException
from .invoice_tax_calculator import calculate_invoice_taxes
from .rindegastos_client import RindegastosAPIError
from .rindegastos_diff_rules import (
    AUTO_APPLY_RULES,
    MANUAL_REVIEW_RULES,
    apply_rindegastos_diff,
    ignore_rindegastos_diff,
)
from .rindegastos_trace import ensure_expense_integration_code, expense_integration_code, expense_integration_code_for_expense
from .rindegastos_sync import RindegastosCatalogSync
from .rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, default_uploaded_sync_since
from .tax_indicators_sync import SiiTaxIndicatorSync
from .whatsapp_notifications import create_rejection_notification, enqueue_notification_send


ALLOWED_RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_RECEIPT_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_RECEIPT_SIZE_BYTES = 10 * 1024 * 1024
ATTACHMENT_EXPORT_TOKEN_TTL_SECONDS = 60 * 60

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

RINDEGASTOS_DIFF_FIELD_LABELS = {
    "supplier": "Proveedor",
    "total": "Monto",
    "currency": "Moneda",
    "issue_date": "Fecha gasto",
    "policy_name": "Política",
    "category_name": "Categoría",
    "tax_name": "Impuesto",
    "tax_amount": "IVA",
    "other_taxes": "Otros impuestos",
    "custom_fields.Centro de Costo / Faena": "Centro de Costo / Faena",
    "custom_fields.Nombre quien rinde": "Nombre quien rinde",
    "custom_fields.RUT proveedor": "RUT proveedor",
    "custom_fields.Tipo de Documento": "Tipo de Documento",
    "custom_fields.Numero de Documento": "Número de Documento",
    "custom_fields.Vehiculo o Equipo": "Vehículo o Equipo",
    "custom_fields.Km.Carguio": "Km carguío",
    "custom_fields.Litros Combustible": "Litros combustible",
    "custom_fields.Categoria": "Categoría Rindegastos",
}


logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    categories = (
        Expense.objects.exclude(category__isnull=True)
        .exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )
    context = {
        "status_choices": Expense.STATUS,
        "worksites": WorksiteCatalog.objects.filter(is_active=True).order_by("name"),
        "vehicles": (
            Expense.objects.exclude(vehicle__isnull=True)
            .exclude(vehicle="")
            .values_list("vehicle", flat=True)
            .distinct()
            .order_by("vehicle")
        ),
        "categories": categories,
    }
    return render(request, "dashboard.html", context)


def _settings_menu_urls():
    return {
        "settings",
        "settings_system_users",
        "settings_users",
        "settings_suppliers",
        "settings_categories",
        "settings_expense_types",
        "settings_rindegastos_fields",
        "settings_rindegastos_rules",
        "settings_rindegastos_submitters",
        "settings_tax_indicators",
    }


def _catalog_sync_status(model):
    synced = model.objects.filter(sync_status="synced")
    return {
        "last_sync": synced.aggregate(value=Max("last_synced_at"))["value"],
        "synced_count": synced.count(),
        "active_count": synced.filter(is_active=True).count(),
    }


def _is_admin_user(user):
    return bool(user.is_authenticated and (user.is_superuser or getattr(user, "role", "") == "admin"))


def _can_manage_expenses(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser
            or getattr(user, "role", "") in {"admin", "reviewer"}
        )
    )


def _can_decide_expenses(user):
    return _can_manage_expenses(user)


def _is_final_expense(expense):
    return expense.status in {"approved", "rejected"}


def _log_user_event(actor, target_user, action, changes=None):
    UserAuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        target_user=target_user,
        action=action,
        changes=changes or {},
    )


def admin_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_admin_user(request.user):
            messages.error(request, "No tienes permisos para acceder a Configuración.")
            return redirect("dashboard")
        return view_func(request, *args, **kwargs)

    return _wrapped


def expense_manager_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _can_manage_expenses(request.user):
            messages.error(request, "No tienes permisos para acceder a esta sección.")
            return redirect("expense_list")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _normalize_empty(value):
    text = (value or "").strip()
    return text or None


def _is_fuel_policy(policy_name):
    return (policy_name or "").strip().casefold() == "combustibles"


def _is_generic_gasoline_category(value):
    normalized = (value or "").strip().casefold()
    if not normalized:
        return False
    return ("bencina" in normalized or "gasolina" in normalized) and not any(
        octane in normalized for octane in ("93", "95", "97")
    )


def _effective_fuel_type_for_tax(expense):
    if _is_generic_gasoline_category(expense.expense_type) and expense.gasoline_type:
        return f"Bencina {expense.gasoline_type}"
    return expense.expense_type or ""


def _is_invoice_document_type(*values):
    for value in values:
        normalized = (value or "").strip().casefold()
        if "factura" in normalized:
            return True
    return False


def _parse_optional_decimal(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    normalized = value.replace(" ", "").replace(",", ".")
    return Decimal(normalized)


def _parse_optional_money_decimal(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    normalized = value.replace(" ", "").replace("$", "")
    normalized = normalized.replace(".", "").replace(",", ".")
    return Decimal(normalized)


def _tax_fields_manually_overridden(post_data):
    return (post_data.get("tax_manual_override") or "").strip() == "1"


def _resolve_rindegastos_tax_name(expense):
    if not _is_invoice_document_type(expense.rindegastos_document_type, expense.document_type):
        return None

    policy = (
        CategoryCatalog.objects.filter(
            name=expense.category,
            is_active=True,
            external_id__isnull=False,
        )
        .exclude(external_id="")
        .first()
    )
    if not policy:
        return None

    taxes = list(RindegastosTaxCatalog.objects.filter(policy=policy, is_active=True).order_by("name"))
    if len(taxes) == 1:
        return taxes[0].name

    iva_taxes = [
        tax
        for tax in taxes
        if "iva" in (tax.name or "").strip().casefold() and (tax.value is None or tax.value == Decimal("19"))
    ]
    if len(iva_taxes) == 1:
        return iva_taxes[0].name
    return None


def _export_rindegastos_tax_name(expense):
    if not _is_invoice_document_type(expense.rindegastos_document_type, expense.document_type):
        return ""
    return _resolve_rindegastos_tax_name(expense) or expense.rindegastos_tax or ""


def _apply_invoice_tax_fields(expense, post_data):
    is_invoice = _is_invoice_document_type(expense.rindegastos_document_type, expense.document_type)
    is_fuel_invoice = is_invoice and _is_fuel_policy(expense.category)

    if not is_invoice:
        expense.iva_amount = Decimal("0")
        expense.specific_tax_amount = Decimal("0")
        expense.rindegastos_tax = None
        expense.tax_calculation_source = "none"
        expense.tax_calculation_metadata = {}
        return

    expense.rindegastos_tax = _resolve_rindegastos_tax_name(expense)

    calculation = calculate_invoice_taxes(
        total=expense.amount,
        paid_at=expense.paid_at,
        document_type=expense.rindegastos_document_type or expense.document_type,
        policy=expense.category,
        fuel_liters=expense.fuel_liters,
        fuel_type=_effective_fuel_type_for_tax(expense),
    )
    if calculation.can_autofill and not _tax_fields_manually_overridden(post_data):
        expense.iva_amount = calculation.iva_amount
        expense.specific_tax_amount = calculation.specific_tax_amount if is_fuel_invoice else Decimal("0")
        expense.tax_calculation_source = "auto"
        expense.tax_calculation_metadata = {
            **calculation.metadata,
            "editable_fields": ["iva_amount", "specific_tax_amount"] if is_fuel_invoice else ["iva_amount"],
        }
        return

    iva_amount = _parse_optional_money_decimal(post_data.get("iva_amount"))
    specific_tax_amount = (
        _parse_optional_money_decimal(post_data.get("specific_tax_amount")) if is_fuel_invoice else Decimal("0")
    )
    expense.iva_amount = iva_amount if iva_amount is not None else Decimal("0")
    expense.specific_tax_amount = specific_tax_amount if specific_tax_amount is not None else Decimal("0")
    expense.tax_calculation_source = "manual"
    expense.tax_calculation_metadata = {
        **calculation.metadata,
        "document_type": expense.document_type or "",
        "rindegastos_document_type": expense.rindegastos_document_type or "",
        "policy": expense.category or "",
        "warnings": calculation.warnings,
        "editable_fields": ["iva_amount", "specific_tax_amount"] if is_fuel_invoice else ["iva_amount"],
    }


def _ensure_rindegastos_policies():
    for policy_name in RINDEGASTOS_POLICIES:
        policy, _ = CategoryCatalog.objects.get_or_create(name=policy_name, defaults={"is_active": True})
        if not policy.is_active:
            policy.is_active = True
            policy.save(update_fields=["is_active"])


def _field_value_for_compare(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _collect_changes(before: dict, after: dict):
    changes = {}
    for key in before.keys():
        b = _field_value_for_compare(before.get(key))
        a = _field_value_for_compare(after.get(key))
        if b != a:
            changes[key] = {"before": b, "after": a}
    return changes


def _log_expense_event(expense, action, actor=None, source="web", reason="", changes=None):
    actor_name = ""
    if actor:
        actor_name = actor.get_full_name() or actor.email
    ExpenseAuditLog.objects.create(
        expense=expense,
        expense_snapshot_id=expense.id,
        action=action,
        actor=actor,
        actor_name=actor_name,
        source=source,
        reason=reason or "",
        changes=changes or {},
    )


def _normalized_duplicate_text(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        (value or "")
        .casefold()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n"),
    ).strip()


def _normalized_document_number(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _amount_similarity_score(current_amount, candidate_amount):
    if current_amount is None or candidate_amount is None:
        return 0, ""
    if current_amount == candidate_amount:
        return 35, "mismo monto"
    bigger = max(abs(current_amount), abs(candidate_amount))
    if bigger and abs(current_amount - candidate_amount) / bigger <= Decimal("0.01"):
        return 25, "monto muy cercano"
    return 0, ""


def _date_similarity_score(current_date, candidate_date):
    if isinstance(current_date, str):
        current_date = parse_date(current_date)
    if isinstance(candidate_date, str):
        candidate_date = parse_date(candidate_date)
    if not current_date or not candidate_date:
        return 0, ""
    delta_days = abs((current_date - candidate_date).days)
    if delta_days == 0:
        return 25, "misma fecha"
    if delta_days <= 2:
        return 15, "fecha cercana"
    return 0, ""


def _expense_attachment_checksums(expense):
    prefetched = getattr(expense, "_prefetched_objects_cache", {}).get("attachments")
    attachments = prefetched if prefetched is not None else expense.attachments.all()
    return {attachment.checksum_sha256 for attachment in attachments if attachment.checksum_sha256}


def _similar_expense_score(expense, candidate):
    score = 0
    reasons = []

    if _expense_attachment_checksums(expense) & _expense_attachment_checksums(candidate):
        score += 70
        reasons.append("mismo comprobante")

    amount_score, amount_reason = _amount_similarity_score(expense.amount, candidate.amount)
    if amount_score:
        score += amount_score
        reasons.append(amount_reason)

    date_score, date_reason = _date_similarity_score(expense.paid_at, candidate.paid_at)
    if date_score:
        score += date_score
        reasons.append(date_reason)

    current_rut = normalize_rut(expense.supplier_rut) if expense.supplier_rut else ""
    candidate_rut = normalize_rut(candidate.supplier_rut) if candidate.supplier_rut else ""
    if current_rut and candidate_rut and current_rut == candidate_rut:
        score += 25
        reasons.append("mismo RUT proveedor")

    current_document = _normalized_document_number(expense.document_number)
    candidate_document = _normalized_document_number(candidate.document_number)
    if current_document and candidate_document and current_document == candidate_document:
        score += 40
        reasons.append("mismo número de documento")

    current_supplier = _normalized_duplicate_text(expense.supplier)
    candidate_supplier = _normalized_duplicate_text(candidate.supplier)
    if current_supplier and candidate_supplier:
        if current_supplier == candidate_supplier:
            score += 20
            reasons.append("mismo proveedor")
        elif current_supplier in candidate_supplier or candidate_supplier in current_supplier:
            score += 10
            reasons.append("proveedor similar")

    current_doc_type = expense.rindegastos_document_type or expense.document_type
    candidate_doc_type = candidate.rindegastos_document_type or candidate.document_type
    if current_doc_type and candidate_doc_type and _normalized_duplicate_text(current_doc_type) == _normalized_duplicate_text(candidate_doc_type):
        score += 10
        reasons.append("mismo tipo de documento")

    if expense.category and candidate.category and _normalized_duplicate_text(expense.category) == _normalized_duplicate_text(candidate.category):
        score += 10
        reasons.append("misma política")

    if expense.wa_sender_phone and candidate.wa_sender_phone and expense.wa_sender_phone == candidate.wa_sender_phone:
        score += 5
        reasons.append("mismo usuario WhatsApp")

    return score, reasons


def _find_similar_expenses(expense, candidates, threshold=60, max_results=3):
    matches = []
    for candidate in candidates:
        if candidate.pk == expense.pk:
            continue
        score, reasons = _similar_expense_score(expense, candidate)
        if score < threshold:
            continue
        matches.append(
            {
                "expense": candidate,
                "export_id": _expense_export_id(candidate.id),
                "score": score,
                "reasons": reasons[:4],
            }
        )
    return sorted(matches, key=lambda item: (-item["score"], -item["expense"].created_at.timestamp()))[:max_results]


def _missing_fields_for_parametrization(expense, has_receipt=None):
    missing = []
    if expense.amount is None:
        missing.append("Monto")
    if not _normalize_empty(expense.currency):
        missing.append("Moneda")
    if not _normalize_empty(expense.category) or expense.category == "Sin Categoria":
        missing.append("Política")
    if not _normalize_empty(expense.supplier):
        missing.append("Proveedor")
    if not _normalize_empty(expense.rindegastos_cost_center):
        missing.append("Centro de Costo / Faena")
    if not _normalize_empty(expense.rindegastos_submitter):
        missing.append("Nombre quien rinde")
    if not expense.paid_at:
        missing.append("Fecha del gasto")
    if not _normalize_empty(expense.rindegastos_document_type):
        missing.append("Tipo de documento")
    if expense.is_vehicle and not _normalize_empty(expense.vehicle):
        missing.append("Vehículo")
    if _is_fuel_policy(expense.category):
        if expense.fuel_km is None:
            missing.append("Km carguío")
        if expense.fuel_liters is None:
            missing.append("Litros combustible")
        if _is_generic_gasoline_category(expense.expense_type) and not _normalize_empty(expense.gasoline_type):
            missing.append("Tipo de bencina")
    return missing


def _rindegastos_field_options_payload():
    target_names = {
        "Centro de Costo / Faena",
        "Nombre quien rinde",
        "Tipo de Documento",
        "Vehiculo o Equipo",
    }
    payload = []
    fields = (
        RindegastosExpenseFieldCatalog.objects.filter(is_active=True, name__in=target_names)
        .select_related("policy")
        .order_by("policy__name", "name")
    )
    for field in fields:
        for option in field.options or []:
            if isinstance(option, dict):
                value = (option.get("Value") or option.get("Name") or option.get("value") or "").strip()
                code = (option.get("Code") or option.get("code") or "").strip()
            else:
                value = str(option).strip()
                code = ""
            if not value:
                continue
            payload.append(
                {
                    "policy_id": field.policy_id,
                    "policy_external_id": field.policy.external_id or "",
                    "policy_name": field.policy.name,
                    "field_name": field.name,
                    "value": value,
                    "code": code,
                }
            )
    return payload


def _tax_calculation_payload():
    utm_values = TaxIndicatorValue.objects.filter(indicator="UTM").order_by("year", "month")
    fuel_rates = FuelSpecificTaxRate.objects.order_by("effective_date", "fuel_key")
    return {
        "utm": [
            {
                "year": item.year,
                "month": item.month,
                "value": str(item.value),
            }
            for item in utm_values
        ],
        "fuel_rates": [
            {
                "effective_date": item.effective_date.isoformat(),
                "fuel_name": item.fuel_name,
                "fuel_key": item.fuel_key,
                "resulting_tax": str(item.resulting_tax),
                "unit": item.unit,
            }
            for item in fuel_rates
        ],
    }


def _serialize_rindegastos_options(policy):
    target_names = {
        "Centro de Costo / Faena",
        "Nombre quien rinde",
        "Tipo de Documento",
        "Vehiculo o Equipo",
    }
    fields = RindegastosExpenseFieldCatalog.objects.filter(
        policy=policy,
        is_active=True,
        name__in=target_names,
    ).order_by("name")
    options = []
    for field in fields:
        for option in field.options or []:
            if isinstance(option, dict):
                value = (option.get("Value") or option.get("Name") or option.get("value") or "").strip()
                code = (option.get("Code") or option.get("code") or "").strip()
            else:
                value = str(option).strip()
                code = ""
            if value:
                options.append(
                    {
                        "field_name": field.name,
                        "value": value,
                        "code": code,
                    }
                )

    categories = [
        {
            "value": item.name,
            "label": f"{item.name} / {item.group_name}" if item.group_name else item.name,
        }
        for item in ExpenseTypeCatalog.objects.filter(policy=policy, is_active=True).order_by("group_name", "name")
    ]
    return {
        "policy": {
            "external_id": policy.external_id,
            "name": policy.name,
            "currency": policy.currency or "",
        },
        "field_options": options,
        "categories": categories,
    }


@login_required
def rindegastos_policy_options(request, external_id):
    policy = get_object_or_404(
        CategoryCatalog,
        external_id=external_id,
        is_active=True,
        sync_status="synced",
    )
    return JsonResponse(_serialize_rindegastos_options(policy))


def _apply_synced_policy(expense, raw_policy_name):
    policy_name = (raw_policy_name or "").strip()
    policy = (
        CategoryCatalog.objects.filter(
            name=policy_name,
            is_active=True,
            external_id__isnull=False,
        )
        .exclude(external_id="")
        .first()
    )
    if not policy:
        return False
    expense.category = policy.name
    return True


def _apply_supplier(expense, request):
    supplier_name = request.POST.get("supplier_select", "").strip()
    new_supplier_name = request.POST.get("new_supplier_name", "").strip()

    if new_supplier_name:
        existing = SupplierCatalog.objects.filter(name__iexact=new_supplier_name).first()
        if existing:
            supplier = existing
            if not supplier.is_active:
                supplier.is_active = True
                supplier.save(update_fields=["is_active", "updated_at"])
        else:
            new_supplier_rut = normalize_rut(request.POST.get("supplier_rut"))
            supplier = SupplierCatalog.objects.create(
                name=new_supplier_name,
                rut=new_supplier_rut,
                is_active=True,
            )
    elif supplier_name:
        supplier = SupplierCatalog.objects.filter(name__iexact=supplier_name, is_active=True).first()
        if not supplier:
            messages.error(request, "El proveedor seleccionado no está disponible en el mantenedor.")
            return False
    else:
        expense.supplier = ""
        expense.supplier_rut = None
        return True

    expense.supplier = supplier.name
    expense.supplier_rut = normalize_rut(supplier.rut) if supplier.rut else None
    return True


def _validate_receipt_file(uploaded_file):
    filename = (getattr(uploaded_file, "name", "") or "").strip()
    extension = Path(filename).suffix.lower()
    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    size = int(getattr(uploaded_file, "size", 0) or 0)

    if extension not in ALLOWED_RECEIPT_EXTENSIONS:
        return False, f"{filename}: tipo inválido. Solo PDF, JPG o PNG."
    if content_type and content_type not in ALLOWED_RECEIPT_MIME_TYPES:
        return False, f"{filename}: tipo inválido. Solo PDF, JPG o PNG."
    if size > MAX_RECEIPT_SIZE_BYTES:
        return False, f"{filename}: supera 10MB."
    return True, ""


def _rebalance_split_group(group_id: str, actor=None, reason: str = "", deleted_expense_id: int | None = None):
    remaining = list(
        Expense.objects.filter(split_group_id=group_id)
        .order_by("split_index", "created_at", "id")
    )
    if not remaining:
        return

    # If only one expense remains, remove split structure but keep audit trace.
    if len(remaining) == 1:
        single = remaining[0]
        before = {
            "group_id": single.split_group_id,
            "index": single.split_index,
            "total": single.split_total,
        }
        single.split_group_id = None
        single.split_parent = None
        single.split_index = None
        single.split_total = None
        single.save(update_fields=["split_group_id", "split_parent", "split_index", "split_total"])
        _log_expense_event(
            single,
            action="updated",
            actor=actor,
            reason=reason,
            changes={
                "split_structure": {
                    "before": before,
                    "after": None,
                    "event": "group_collapsed_after_delete",
                    "deleted_expense_id": deleted_expense_id,
                }
            },
        )
        return

    root = remaining[0]
    for idx, item in enumerate(remaining, start=1):
        before = {
            "index": item.split_index,
            "total": item.split_total,
            "parent_id": item.split_parent_id,
        }
        target_parent = None if idx == 1 else root
        item.split_index = idx
        item.split_total = len(remaining)
        item.split_parent = target_parent
        item.save(update_fields=["split_index", "split_total", "split_parent"])
        after = {
            "index": item.split_index,
            "total": item.split_total,
            "parent_id": item.split_parent_id,
        }
        if before != after:
            _log_expense_event(
                item,
                action="updated",
                actor=actor,
                reason=reason,
                changes={
                    "split_structure": {
                        "before": before,
                        "after": after,
                        "event": "group_rebalanced_after_delete",
                        "deleted_expense_id": deleted_expense_id,
                    }
                },
            )

@login_required
def expense_detail(request, pk: int):
    expense = get_object_or_404(Expense, pk=pk)
    if request.method == "POST":
        if not _can_manage_expenses(request.user):
            messages.error(request, "No tienes permisos para editar gastos.")
            return redirect("expense_list")
        if _is_final_expense(expense):
            messages.error(
                request,
                "El gasto está aprobado o rechazado y no puede modificarse. Solo un superadmin puede revertir la decisión.",
            )
            return redirect("expense_list")

        tracked_fields = [
            "status",
            "amount",
            "currency",
            "category",
            "supplier",
            "supplier_rut",
            "worksite",
            "worksite_standard",
            "rindegastos_cost_center",
            "rindegastos_submitter",
            "notes",
            "paid_at",
            "document_type",
            "rindegastos_document_type",
            "document_number",
            "is_vehicle",
            "vehicle",
            "fuel_km",
            "fuel_liters",
            "gasoline_type",
            "iva_amount",
            "specific_tax_amount",
            "rindegastos_tax",
            "tax_calculation_source",
            "tax_calculation_metadata",
            "expense_type",
            "expense_type_other",
        ]
        before = {field: getattr(expense, field) for field in tracked_fields}

        requested_status = request.POST.get("status", expense.status)
        if expense.status in {"incomplete", "not_completed"}:
            requested_status = expense.status
        elif expense.status in {"approved", "rejected"}:
            requested_status = expense.status
        elif requested_status not in {"pending", "completed"}:
            requested_status = expense.status
        original_status = expense.status
        change_reason = request.POST.get("change_reason", "").strip()

        raw_amount = request.POST.get("amount", "").strip()
        if raw_amount:
            try:
                normalized_amount = raw_amount.replace(" ", "").replace("$", "")
                normalized_amount = normalized_amount.replace(".", "").replace(",", ".")
                if normalized_amount in ("", "-", "+"):
                    raise InvalidOperation
                expense.amount = Decimal(normalized_amount)
            except InvalidOperation:
                messages.error(request, "Monto inválido. No se guardó el valor ingresado.")
        else:
            expense.amount = None

        currency = request.POST.get("currency", "").strip()
        if currency:
            expense.currency = currency

        if not _apply_synced_policy(expense, request.POST.get("category_select")):
            messages.error(request, "Selecciona una política vigente sincronizada desde Rindegastos.")
            return redirect("expense_list")

        if not _apply_supplier(expense, request):
            return redirect("expense_list")

        worksite_raw = request.POST.get("worksite", "")
        expense.worksite = worksite_raw.strip()

        document_type = request.POST.get("document_type")
        if document_type is not None:
            expense.document_type = document_type.strip() or None
        expense.rindegastos_cost_center = request.POST.get("rindegastos_cost_center", "").strip() or None
        expense.rindegastos_submitter = request.POST.get("rindegastos_submitter", "").strip() or None
        expense.rindegastos_document_type = (
            request.POST.get("rindegastos_document_type", "").strip() or None
        )
        expense.document_number = request.POST.get("document_number", "").strip() or None

        is_vehicle_raw = request.POST.get("is_vehicle")
        expense.is_vehicle = bool(is_vehicle_raw) or _is_fuel_policy(expense.category)

        expense.vehicle = (request.POST.get("vehicle", "").strip() or None) if expense.is_vehicle else None
        if _is_fuel_policy(expense.category):
            try:
                expense.fuel_km = _parse_optional_decimal(request.POST.get("fuel_km"))
                expense.fuel_liters = _parse_optional_decimal(request.POST.get("fuel_liters"))
            except InvalidOperation:
                messages.error(request, "Km carguío o litros combustible tienen un valor inválido.")

        expense_type_select = request.POST.get("expense_type_select", "").strip()
        if expense_type_select:
            et_queryset = ExpenseTypeCatalog.objects.filter(
                is_active=True,
                name=expense_type_select,
            )
            policy_id = request.POST.get("category_policy_id", "").strip()
            if policy_id:
                et_queryset = et_queryset.filter(policy_id=policy_id)
            et_obj = et_queryset.first()
            expense.expense_type = et_obj.name if et_obj else None
        else:
            expense.expense_type = None
        gasoline_type = request.POST.get("gasoline_type", "").strip()
        if _is_fuel_policy(expense.category) and _is_generic_gasoline_category(expense.expense_type):
            expense.gasoline_type = gasoline_type if gasoline_type in {"93", "95", "97"} else "93"
        else:
            expense.gasoline_type = None
        expense.expense_type_other = None

        notes = request.POST.get("notes", "")
        expense.notes = notes.strip()

        paid_at_raw = request.POST.get("paid_at", "").strip()
        expense.paid_at = parse_date(paid_at_raw) if paid_at_raw else None

        try:
            _apply_invoice_tax_fields(expense, request.POST)
        except InvalidOperation:
            messages.error(request, "IVA o impuesto específico tienen un valor inválido.")
            return redirect("expense_list")

        uploaded_receipts = request.FILES.getlist("receipt_files")
        added_receipt_names = []
        invalid_receipt_errors = []
        for uploaded_file in uploaded_receipts:
            is_valid, validation_error = _validate_receipt_file(uploaded_file)
            if not is_valid:
                invalid_receipt_errors.append(validation_error)
                continue
            Attachment.objects.create(
                expense=expense,
                file=uploaded_file,
                content_type=getattr(uploaded_file, "content_type", "") or "",
            )
            added_receipt_names.append(uploaded_file.name)
        has_receipt = expense.attachments.exists() or bool(added_receipt_names)

        missing_for_param = []
        auto_parametrized = False
        should_evaluate_param_completion = (
            original_status == "pending"
            and expense.source == "whatsapp"
            and requested_status in {"pending", "completed"}
        )
        if should_evaluate_param_completion:
            missing_for_param = _missing_fields_for_parametrization(expense, has_receipt=has_receipt)
            if missing_for_param:
                # Keep pending if there are missing required fields.
                expense.status = "pending"
            else:
                expense.status = "completed"
                auto_parametrized = requested_status != "completed"
        else:
            expense.status = requested_status

        expense.save(update_fields=tracked_fields)

        after = {field: getattr(expense, field) for field in tracked_fields}
        changes = _collect_changes(before, after)
        if added_receipt_names:
            changes["attachments_added"] = {"before": None, "after": added_receipt_names}
        if invalid_receipt_errors:
            changes["attachments_rejected"] = {"before": None, "after": invalid_receipt_errors}

        status_change_blocked = requested_status == "completed" and bool(missing_for_param)
        if status_change_blocked:
            changes["requested_status"] = {"before": original_status, "after": requested_status}
            changes["validation_missing"] = {"before": None, "after": missing_for_param}
            _log_expense_event(
                expense,
                action="status_change_blocked",
                actor=request.user,
                reason=change_reason,
                changes=changes,
            )
            messages.warning(
                request,
                "No se pudo pasar a 'Parametrizado'. Faltan datos: " + ", ".join(missing_for_param),
            )
            return redirect("expense_list")

        action = "status_changed" if original_status != expense.status else "updated"
        _log_expense_event(
            expense,
            action=action,
            actor=request.user,
            reason=change_reason,
            changes=changes or {"message": "Guardado sin cambios detectables"},
        )

        if auto_parametrized:
            messages.success(
                request,
                "El gasto ha cambiado su estado a parametrizado.",
                extra_tags="auto-parametrized",
            )
        for receipt_error in invalid_receipt_errors:
            messages.error(request, receipt_error)
        messages.success(request, "Gasto actualizado correctamente.")
        return redirect("expense_list")

    return render(request, "expense_detail.html", {"expense": expense})


@login_required
def expense_create(request):
    if request.method != "POST":
        return redirect("expense_list")
    if not _can_manage_expenses(request.user):
        messages.error(request, "No tienes permisos para crear gastos.")
        return redirect("expense_list")

    expense = Expense(
        source="web",
        created_by=request.user,
        status="pending",
    )

    requested_status = request.POST.get("status", "pending").strip() or "pending"
    if requested_status not in {"pending", "completed"}:
        requested_status = "pending"

    raw_amount = request.POST.get("amount", "").strip()
    if raw_amount:
        try:
            normalized_amount = raw_amount.replace(" ", "").replace("$", "")
            normalized_amount = normalized_amount.replace(".", "").replace(",", ".")
            if normalized_amount in ("", "-", "+"):
                raise InvalidOperation
            expense.amount = Decimal(normalized_amount)
        except InvalidOperation:
            messages.error(request, "Monto inválido. Se guardó sin monto.")

    currency = request.POST.get("currency", "").strip()
    if currency:
        expense.currency = currency

    if not _apply_synced_policy(expense, request.POST.get("category_select")):
        messages.error(request, "Selecciona una política vigente sincronizada desde Rindegastos.")
        return redirect("expense_list")

    if not _apply_supplier(expense, request):
        return redirect("expense_list")

    expense.worksite = request.POST.get("worksite", "").strip()

    expense.document_type = request.POST.get("document_type", "").strip() or None
    expense.rindegastos_cost_center = request.POST.get("rindegastos_cost_center", "").strip() or None
    expense.rindegastos_submitter = request.POST.get("rindegastos_submitter", "").strip() or None
    expense.rindegastos_document_type = (
        request.POST.get("rindegastos_document_type", "").strip() or None
    )
    expense.document_number = request.POST.get("document_number", "").strip() or None

    expense.is_vehicle = bool(request.POST.get("is_vehicle")) or _is_fuel_policy(expense.category)
    expense.vehicle = (request.POST.get("vehicle", "").strip() or None) if expense.is_vehicle else None
    if _is_fuel_policy(expense.category):
        try:
            expense.fuel_km = _parse_optional_decimal(request.POST.get("fuel_km"))
            expense.fuel_liters = _parse_optional_decimal(request.POST.get("fuel_liters"))
        except InvalidOperation:
            messages.error(request, "Km carguío o litros combustible tienen un valor inválido.")
    else:
        expense.fuel_km = None
        expense.fuel_liters = None

    expense_type_select = request.POST.get("expense_type_select", "").strip()
    if expense_type_select:
        et_queryset = ExpenseTypeCatalog.objects.filter(
            is_active=True,
            name=expense_type_select,
        )
        policy_id = request.POST.get("category_policy_id", "").strip()
        if policy_id:
            et_queryset = et_queryset.filter(policy_id=policy_id)
        et_obj = et_queryset.first()
        expense.expense_type = et_obj.name if et_obj else None
    else:
        expense.expense_type = None
    gasoline_type = request.POST.get("gasoline_type", "").strip()
    if _is_fuel_policy(expense.category) and _is_generic_gasoline_category(expense.expense_type):
        expense.gasoline_type = gasoline_type if gasoline_type in {"93", "95", "97"} else "93"
    else:
        expense.gasoline_type = None
    expense.expense_type_other = None
    expense.notes = request.POST.get("notes", "").strip()

    paid_at_raw = request.POST.get("paid_at", "").strip()
    expense.paid_at = parse_date(paid_at_raw) if paid_at_raw else None

    try:
        _apply_invoice_tax_fields(expense, request.POST)
    except InvalidOperation:
        messages.error(request, "IVA o impuesto específico tienen un valor inválido.")
        return redirect("expense_list")

    expense.save()

    uploaded_receipts = request.FILES.getlist("receipt_files")
    added_receipt_names = []
    invalid_receipt_errors = []
    for uploaded_file in uploaded_receipts:
        is_valid, validation_error = _validate_receipt_file(uploaded_file)
        if not is_valid:
            invalid_receipt_errors.append(validation_error)
            continue
        Attachment.objects.create(
            expense=expense,
            file=uploaded_file,
            content_type=getattr(uploaded_file, "content_type", "") or "",
        )
        added_receipt_names.append(uploaded_file.name)

    has_receipt = expense.attachments.exists() or bool(added_receipt_names)
    missing_for_param = []
    if requested_status == "completed":
        missing_for_param = _missing_fields_for_parametrization(expense, has_receipt=has_receipt)
        if missing_for_param:
            expense.status = "pending"
            messages.warning(
                request,
                "No se pudo crear como 'Parametrizado'. Faltan datos: " + ", ".join(missing_for_param),
            )
        else:
            expense.status = "completed"
    else:
        expense.status = "pending"
    expense.save(update_fields=["status"])

    changes = {
        "status": {"before": None, "after": expense.status},
        "source": {"before": None, "after": expense.source},
    }
    if added_receipt_names:
        changes["attachments_added"] = {"before": None, "after": added_receipt_names}
    if invalid_receipt_errors:
        changes["attachments_rejected"] = {"before": None, "after": invalid_receipt_errors}
    if missing_for_param:
        changes["validation_missing"] = {"before": None, "after": missing_for_param}

    _log_expense_event(
        expense,
        action="created",
        actor=request.user,
        changes=changes,
    )

    for receipt_error in invalid_receipt_errors:
        messages.error(request, receipt_error)
    messages.success(request, f"Gasto #{expense.id} creado correctamente.")
    return redirect("expense_list")


@login_required
def expense_list(request):
    can_manage_expenses = _can_manage_expenses(request.user)
    active_statuses = {"incomplete", "not_completed", "pending", "completed"}
    final_statuses = {"approved", "rejected"}
    valid_scopes = active_statuses | final_statuses | {
        "active",
        "all",
        "uploaded_rindegastos",
        "not_uploaded_rindegastos",
        "without_receipt",
    }
    scope = request.GET.get("scope", "active").strip() or "active"
    if scope not in valid_scopes:
        scope = "active"
    search_query = request.GET.get("q", "").strip()
    column_filter_params = {
        "trace_id": request.GET.get("trace_id", "").strip(),
        "created_from": request.GET.get("created_from", "").strip(),
        "created_to": request.GET.get("created_to", "").strip(),
        "paid_from": request.GET.get("paid_from", "").strip(),
        "paid_to": request.GET.get("paid_to", "").strip(),
        "reporter": request.GET.get("reporter", "").strip(),
        "supplier": request.GET.get("supplier", "").strip(),
        "amount_min": request.GET.get("amount_min", "").strip(),
        "amount_max": request.GET.get("amount_max", "").strip(),
        "category": request.GET.get("category", "").strip(),
        "worksite": request.GET.get("worksite", "").strip(),
        "vehicle": request.GET.get("vehicle", "").strip(),
        "rindegastos_upload": request.GET.get("rindegastos_upload", "").strip(),
        "status": request.GET.get("status", "").strip(),
        "received_from": request.GET.get("received_from", "").strip(),
        "received_to": request.GET.get("received_to", "").strip(),
        "has_receipt": request.GET.get("has_receipt", "").strip(),
    }
    try:
        page_size = int(request.GET.get("page_size", "50"))
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in {25, 50, 100, 200}:
        page_size = 50

    queryset = (
        Expense.objects.select_related("created_by", "wa_sender", "decision_by")
        .prefetch_related(
            "attachments",
            "audit_logs",
            "notifications",
            Prefetch(
                "rindegastos_diffs",
                queryset=RindegastosExpenseDiff.objects.filter(status=RindegastosExpenseDiff.STATUS_OPEN)
                .select_related("snapshot")
                .order_by("field_name", "id"),
                to_attr="open_rindegastos_diffs",
            ),
        )
    )
    status_filter = column_filter_params["status"]
    status_filter_is_valid = status_filter in dict(Expense.STATUS)
    trace_filter = column_filter_params["trace_id"].upper()
    trace_filter_is_otz = trace_filter.startswith("OTZ-")
    default_scope_bypassed_by_trace = scope == "active" and trace_filter_is_otz and not status_filter_is_valid
    if scope == "active" and not status_filter_is_valid and not trace_filter_is_otz:
        queryset = queryset.filter(status__in=active_statuses)
    elif scope in active_statuses | final_statuses:
        queryset = queryset.filter(status=scope)
    elif scope == "uploaded_rindegastos":
        queryset = queryset.exclude(rindegastos_expense_id__isnull=True).exclude(rindegastos_expense_id="")
    elif scope == "not_uploaded_rindegastos":
        queryset = queryset.filter(Q(rindegastos_expense_id__isnull=True) | Q(rindegastos_expense_id=""))
    elif scope == "without_receipt":
        queryset = queryset.filter(attachments__isnull=True)

    if trace_filter_is_otz:
        trace_ids = []
        for expense_id in Expense.objects.values_list("id", flat=True):
            if trace_filter in _expense_export_id(expense_id):
                trace_ids.append(expense_id)
        queryset = queryset.filter(Q(id__in=trace_ids) | Q(rindegastos_integration_code__icontains=trace_filter))
    elif trace_filter:
        queryset = queryset.filter(id__icontains=trace_filter)
    created_from = parse_date(column_filter_params["created_from"])
    created_to = parse_date(column_filter_params["created_to"])
    paid_from = parse_date(column_filter_params["paid_from"])
    paid_to = parse_date(column_filter_params["paid_to"])
    received_from = parse_date(column_filter_params["received_from"])
    received_to = parse_date(column_filter_params["received_to"])
    if created_from:
        queryset = queryset.filter(created_at__date__gte=created_from)
    if created_to:
        queryset = queryset.filter(created_at__date__lte=created_to)
    if paid_from:
        queryset = queryset.filter(paid_at__gte=paid_from)
    if paid_to:
        queryset = queryset.filter(paid_at__lte=paid_to)
    if received_from:
        queryset = queryset.filter(message_sent_at__date__gte=received_from)
    if received_to:
        queryset = queryset.filter(message_sent_at__date__lte=received_to)
    if column_filter_params["reporter"]:
        reporter = column_filter_params["reporter"]
        queryset = queryset.filter(
            Q(wa_sender_phone=reporter)
            | Q(wa_sender__phone=reporter)
            | Q(created_by__email=reporter)
        )
    if column_filter_params["supplier"]:
        queryset = queryset.filter(supplier__icontains=column_filter_params["supplier"])
    if column_filter_params["category"]:
        queryset = queryset.filter(category=column_filter_params["category"])
    if column_filter_params["worksite"]:
        queryset = queryset.filter(worksite__icontains=column_filter_params["worksite"])
    if column_filter_params["vehicle"]:
        queryset = queryset.filter(vehicle__icontains=column_filter_params["vehicle"])
    if column_filter_params["rindegastos_upload"] == "uploaded":
        queryset = queryset.exclude(rindegastos_expense_id__isnull=True).exclude(rindegastos_expense_id="")
    elif column_filter_params["rindegastos_upload"] == "not_uploaded":
        queryset = queryset.filter(Q(rindegastos_expense_id__isnull=True) | Q(rindegastos_expense_id=""))
    if status_filter_is_valid:
        queryset = queryset.filter(status=status_filter)
    if column_filter_params["has_receipt"] == "yes":
        queryset = queryset.filter(attachments__isnull=False)
    elif column_filter_params["has_receipt"] == "no":
        queryset = queryset.filter(attachments__isnull=True)
    try:
        amount_min = Decimal(column_filter_params["amount_min"]) if column_filter_params["amount_min"] else None
    except InvalidOperation:
        amount_min = None
    try:
        amount_max = Decimal(column_filter_params["amount_max"]) if column_filter_params["amount_max"] else None
    except InvalidOperation:
        amount_max = None
    if amount_min is not None:
        queryset = queryset.filter(amount__gte=amount_min)
    if amount_max is not None:
        queryset = queryset.filter(amount__lte=amount_max)

    if search_query:
        queryset = queryset.filter(
            Q(supplier__icontains=search_query)
            | Q(category__icontains=search_query)
            | Q(worksite__icontains=search_query)
            | Q(worksite_standard__icontains=search_query)
            | Q(vehicle__icontains=search_query)
            | Q(notes__icontains=search_query)
            | Q(wa_sender_phone__icontains=search_query)
            | Q(wa_sender__first_name__icontains=search_query)
            | Q(wa_sender__last_name__icontains=search_query)
            | Q(created_by__email__icontains=search_query)
            | Q(rindegastos_expense_id__icontains=search_query)
            | Q(rindegastos_integration_code__icontains=search_query)
            | Q(document_number__icontains=search_query)
        )
    sort_options = {
        "trace_id": "id",
        "created_at": "created_at",
        "paid_at": "paid_at",
        "reporter": "wa_sender__first_name",
        "supplier": "supplier",
        "amount": "amount",
        "category": "category",
        "worksite": "worksite",
        "vehicle": "vehicle",
        "rindegastos_upload": "rindegastos_expense_id",
        "status": "status",
        "received_at": "message_sent_at",
    }
    sort = request.GET.get("sort", "created_at").strip()
    if sort not in sort_options:
        sort = "created_at"
    direction = request.GET.get("direction", "desc").strip()
    if direction not in {"asc", "desc"}:
        direction = "desc"
    ordering = sort_options[sort]
    if direction == "desc":
        ordering = f"-{ordering}"
    queryset = queryset.distinct().order_by(ordering, "-id")

    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    gastos = list(page_obj.object_list)

    def query_url(**updates):
        params = request.GET.copy()
        for key, value in updates.items():
            if value in {None, ""}:
                params.pop(key, None)
            else:
                params[key] = str(value)
        return f"{request.path}?{params.urlencode()}" if params else request.path

    filter_query_keys = [
        "q",
        "trace_id",
        "created_from",
        "created_to",
        "paid_from",
        "paid_to",
        "reporter",
        "supplier",
        "amount_min",
        "amount_max",
        "category",
        "worksite",
        "vehicle",
        "rindegastos_upload",
        "status",
        "received_from",
        "received_to",
        "has_receipt",
        "sort",
        "direction",
        "page",
    ]
    clear_filter_updates = {key: "" for key in filter_query_keys}
    clear_filter_updates["scope"] = "active"

    quick_filter_options = [
        ("active", "Por atender"),
        ("all", "Todos"),
        ("incomplete", "Incompletos"),
        ("not_completed", "No completados"),
        ("pending", "Pendiente"),
        ("completed", "Parametrizado"),
        ("approved", "Aprobado"),
        ("rejected", "Rechazado"),
        ("uploaded_rindegastos", "Subidos RG"),
        ("not_uploaded_rindegastos", "No subidos RG"),
        ("without_receipt", "Sin comprobante"),
    ]
    quick_filter_labels = dict(quick_filter_options)
    quick_filters = [
        {
            "scope": option_scope,
            "label": label,
            "url": query_url(scope=option_scope, page=1),
            "active": scope == option_scope and not default_scope_bypassed_by_trace,
        }
        for option_scope, label in quick_filter_options
    ]
    column_filter_labels = {
        "trace_id": "ID OTZ",
        "created_from": "Fecha reporte desde",
        "created_to": "Fecha reporte hasta",
        "paid_from": "Fecha gasto desde",
        "paid_to": "Fecha gasto hasta",
        "reporter": "Usuario",
        "supplier": "Proveedor",
        "amount_min": "Monto mínimo",
        "amount_max": "Monto máximo",
        "category": "Política",
        "worksite": "Obra",
        "vehicle": "Vehículo",
        "rindegastos_upload": "Rindegastos",
        "status": "Status",
        "received_from": "Recibido desde",
        "received_to": "Recibido hasta",
        "has_receipt": "Comprobante",
    }
    value_labels = {
        "rindegastos_upload": {"uploaded": "Subido", "not_uploaded": "No subido"},
        "has_receipt": {"yes": "Con comprobante", "no": "Sin comprobante"},
        "status": dict(Expense.STATUS),
    }
    column_filter_values = {
        key: value
        for key, value in column_filter_params.items()
        if value
    }
    has_column_filters = bool(column_filter_values)
    active_filter_pills = []
    if not default_scope_bypassed_by_trace:
        active_filter_pills.append(
            {
                "label": "Vista",
                "value": quick_filter_labels.get(scope, scope),
                "url": query_url(scope="all", page=1),
            }
        )
    if search_query:
        active_filter_pills.append({"label": "Búsqueda", "value": search_query, "url": query_url(q="", page=1)})
    for key, value in column_filter_values.items():
        display_value = value_labels.get(key, {}).get(value, value)
        active_filter_pills.append(
            {
                "label": column_filter_labels.get(key, key),
                "value": display_value,
                "url": query_url(**{key: "", "page": 1}),
            }
        )
    if sort != "created_at" or direction != "desc":
        sort_label = next((name for name, field in sort_options.items() if name == sort), sort)
        active_filter_pills.append(
            {
                "label": "Orden",
                "value": f"{sort_label} {'desc' if direction == 'desc' else 'asc'}",
                "url": query_url(sort="created_at", direction="desc", page=1),
            }
        )
    page_size_options = [
        {"value": value, "url": query_url(page_size=value, page=1), "active": page_size == value}
        for value in (25, 50, 100, 200)
    ]
    page_links = []
    if page_obj.has_previous():
        page_links.append({"label": "Anterior", "url": query_url(page=page_obj.previous_page_number()), "active": False})
    start_page = max(1, page_obj.number - 2)
    end_page = min(paginator.num_pages, page_obj.number + 2)
    for page_number in range(start_page, end_page + 1):
        page_links.append(
            {
                "label": str(page_number),
                "url": query_url(page=page_number),
                "active": page_number == page_obj.number,
            }
        )
    if page_obj.has_next():
        page_links.append({"label": "Siguiente", "url": query_url(page=page_obj.next_page_number()), "active": False})

    sort_links = {}
    sort_columns = {
        0: "trace_id",
        1: "created_at",
        2: "paid_at",
        3: "reporter",
        4: "supplier",
        5: "amount",
        6: "category",
        7: "worksite",
        8: "vehicle",
        9: "rindegastos_upload",
        10: "status",
        11: "received_at",
    }
    for index, sort_name in sort_columns.items():
        next_direction = "desc" if sort == sort_name and direction == "asc" else "asc"
        sort_links[index] = {
            "url": query_url(sort=sort_name, direction=next_direction, page=1),
            "active": sort == sort_name,
            "direction": direction if sort == sort_name else "",
        }
    reporter_options = []
    for sender in AllowedSender.objects.filter(is_deleted=False).order_by("first_name", "last_name", "phone"):
        label = str(sender)
        reporter_options.append({"value": sender.phone, "label": label})
    for user in get_user_model().objects.exclude(email="").order_by("email"):
        reporter_options.append({"value": user.email, "label": user.get_full_name() or user.email})
    category_options = list(
        Expense.objects.exclude(category__isnull=True)
        .exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )

    senders_by_phone = {
        s.phone: s
        for s in AllowedSender.objects.filter(is_deleted=False)
    }
    for gasto in gastos:
        gasto.export_id = expense_integration_code_for_expense(gasto)
        gasto.policy_catalog_id = None
        if gasto.category:
            policy_catalog = CategoryCatalog.objects.filter(name=gasto.category).only("id").first()
            if policy_catalog:
                gasto.policy_catalog_id = policy_catalog.id
        if not gasto.wa_sender and gasto.wa_sender_phone:
            sender = senders_by_phone.get(gasto.wa_sender_phone)
            if sender:
                name = f"{sender.first_name} {sender.last_name}".strip()
                gasto.wa_sender_name = name or sender.phone
        gasto.reporter_label = _reporter_label(gasto)
        logs = list(gasto.audit_logs.all())
        gasto.audit_entries = logs[:5]
        gasto.audit_entries_all = logs
        notifications = list(gasto.notifications.all())
        gasto.rejection_notification = next(
            (
                notification
                for notification in notifications
                if notification.notification_type == ExpenseNotification.TYPE_REJECTION
                and notification.channel == ExpenseNotification.CHANNEL_WHATSAPP
            ),
            None,
        )
        gasto.can_approve_or_reject = _can_decide_expenses(request.user) and gasto.status == "completed"
        gasto.can_revert_decision = request.user.is_superuser and _is_final_expense(gasto)
        gasto.can_retry_rejection_notification = (
            request.user.is_superuser
            and gasto.rejection_notification
            and gasto.rejection_notification.status == ExpenseNotification.STATUS_FAILED
        )
        for diff in getattr(gasto, "open_rindegastos_diffs", []):
            diff.field_label = RINDEGASTOS_DIFF_FIELD_LABELS.get(diff.field_name, diff.field_name)
        gasto.open_rindegastos_diff_count = len(getattr(gasto, "open_rindegastos_diffs", []))
        gasto.is_locked = _is_final_expense(gasto)
        gasto.can_manage = can_manage_expenses
        gasto.split_label = ""
        if gasto.split_group_id and gasto.split_index and gasto.split_total:
            gasto.split_label = f"División {gasto.split_index}/{gasto.split_total}"
    for gasto in gastos:
        gasto.similar_expenses = _find_similar_expenses(gasto, gastos)
    context = {
        "gastos": gastos,
        "page_obj": page_obj,
        "paginator": paginator,
        "quick_filters": quick_filters,
        "current_scope": scope,
        "search_query": search_query,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "page_links": page_links,
        "column_filters": column_filter_values,
        "has_column_filters": has_column_filters,
        "active_filter_pills": active_filter_pills,
        "sort_links": sort_links,
        "sort": sort,
        "direction": direction,
        "reporter_filter_options": reporter_options,
        "category_filter_options": category_options,
        "status_filter_options": Expense.STATUS,
        "search_url": query_url(page=1, q=search_query),
        "clear_filters_url": query_url(**clear_filter_updates),
        "can_manage_expenses": can_manage_expenses,
        "status_choices": Expense.STATUS,
        "categories_catalog": (
            CategoryCatalog.objects.filter(is_active=True, external_id__isnull=False)
            .exclude(external_id="")
            .order_by("name")
        ),
        "expense_types_catalog": ExpenseTypeCatalog.objects.filter(is_active=True).select_related("policy").order_by(
            "policy__name",
            "group_name",
            "name",
        ),
        "rindegastos_field_options": _rindegastos_field_options_payload(),
        "tax_calculation_payload": _tax_calculation_payload(),
        "suppliers_catalog": SupplierCatalog.objects.filter(is_active=True).order_by("name"),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "expenses/gastos.html", context)


def _reporter_label(expense):
    if expense.wa_sender:
        full_name = f"{expense.wa_sender.first_name or ''} {expense.wa_sender.last_name or ''}".strip()
        return full_name or expense.wa_sender.phone or ""
    if hasattr(expense, "wa_sender_name"):
        return expense.wa_sender_name
    if expense.created_by:
        return expense.created_by.get_full_name() or expense.created_by.email or ""
    return expense.wa_sender_phone or ""


def _export_amount(amount):
    if amount is None:
        return ""
    if amount == amount.to_integral():
        return int(amount)
    return amount


def _expense_export_id(expense_id):
    return expense_integration_code(expense_id)


def _rindegastos_note(note, export_id):
    clean_note = re.sub(r"\s*\r?\n+\s*", " | ", (note or "").strip())
    suffix = f"Gasto id {export_id}"
    if not clean_note:
        return suffix
    return f"{clean_note}. {suffix}"


def _attachment_export_signature(attachment_id, expires_at):
    key = str(settings.SECRET_KEY).encode("utf-8")
    payload = f"attachment:{attachment_id}:{expires_at}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _signed_attachment_export_url(request, attachment):
    expires_at = int(timezone.now().timestamp()) + ATTACHMENT_EXPORT_TOKEN_TTL_SECONDS
    signature = _attachment_export_signature(attachment.id, expires_at)
    path = reverse("attachment_export_serve", args=[attachment.id])
    return request.build_absolute_uri(f"{path}?expires={expires_at}&sig={signature}")


def _attachment_file_response(attachment, disposition="inline", allow_cross_origin=False):
    file_handle = attachment.file.open("rb")
    content_type = attachment.content_type or "application/octet-stream"
    response = FileResponse(file_handle, content_type=content_type)
    filename = attachment.file.name.rsplit("/", 1)[-1]
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    if allow_cross_origin:
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Type"
    return response


@login_required
def expense_rindegastos_export(request):
    status_scope = request.GET.get("status_scope", "completed")
    start_date = parse_date(request.GET.get("start_date", "") or "")
    end_date = parse_date(request.GET.get("end_date", "") or "")
    sync_before_export = request.GET.get("sync_before_export", "1") != "0"
    exclude_uploaded = request.GET.get("exclude_uploaded", "1") != "0"

    sync_since = start_date or default_uploaded_sync_since()
    sync_until = end_date or timezone.localdate()
    if sync_before_export:
        try:
            RindegastosUploadedExpenseSync(export_id_func=_expense_export_id).sync(
                since=sync_since,
                until=sync_until,
                max_pages=20,
            )
        except (RindegastosAPIError, ValueError) as exc:
            messages.error(
                request,
                "No se pudo sincronizar con Rindegastos antes de exportar. "
                "Desmarca la sincronización previa solo si quieres exportar de todas formas. "
                f"Detalle: {exc}",
            )
            return redirect("expense_list")

    queryset = (
        Expense.objects.select_related("created_by", "wa_sender")
        .prefetch_related("attachments")
        .order_by("category", "paid_at", "id")
    )
    if status_scope == "approved":
        queryset = queryset.filter(status="approved")
    elif status_scope == "completed_and_approved":
        queryset = queryset.filter(status__in=["completed", "approved"])
    elif status_scope == "completed":
        queryset = queryset.filter(status="completed")
    if start_date:
        queryset = queryset.filter(paid_at__gte=start_date)
    if end_date:
        queryset = queryset.filter(paid_at__lte=end_date)
    if exclude_uploaded:
        queryset = queryset.filter(Q(rindegastos_expense_id__isnull=True) | Q(rindegastos_expense_id=""))

    expenses = list(queryset)
    policy_by_name = {
        policy.name: policy
        for policy in CategoryCatalog.objects.filter(name__in={expense.category for expense in expenses if expense.category})
    }
    summary = {}
    for expense in expenses:
        policy = expense.category or "Sin Politica"
        summary[policy] = summary.get(policy, 0) + 1

    today = timezone.localdate().isoformat()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="rindegastos-export-{today}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)

    writer.writerow(["politica", "cantidad"])
    for policy, count in sorted(summary.items()):
        writer.writerow([policy, count])

    writer.writerow([])
    writer.writerow(
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

    for expense in expenses:
        export_id = ensure_expense_integration_code(expense)
        policy = policy_by_name.get(expense.category)
        attachments = list(expense.attachments.all())
        writer.writerow(
            [
                expense.category or "",
                export_id,
                expense.supplier or "",
                _export_amount(expense.amount),
                (policy.currency if policy and policy.currency else expense.currency) or "CLP",
                _export_rindegastos_tax_name(expense),
                _export_amount(expense.iva_amount),
                _export_amount(expense.specific_tax_amount),
                f"{expense.paid_at.day}/{expense.paid_at.month}/{expense.paid_at.year}" if expense.paid_at else "",
                expense.rindegastos_cost_center or "",
                expense.rindegastos_submitter or _reporter_label(expense),
                expense.document_number or "",
                normalize_rut(expense.supplier_rut) if expense.supplier_rut else "",
                expense.rindegastos_document_type or "",
                expense.vehicle or "",
                _export_amount(expense.fuel_km),
                _export_amount(expense.fuel_liters),
                expense.expense_type or "",
                "|".join(_signed_attachment_export_url(request, attachment) for attachment in attachments),
                "|".join(attachment.file.name.rsplit("/", 1)[-1] for attachment in attachments),
                _rindegastos_note(expense.notes, export_id),
            ]
        )

    return response

@login_required
def attachment_serve(request, pk: int):
    attachment = get_object_or_404(Attachment.objects.select_related("expense"), pk=pk)
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    return _attachment_file_response(attachment, disposition=disposition)


def attachment_export_serve(request, pk: int):
    expires_raw = request.GET.get("expires", "")
    signature = request.GET.get("sig", "")
    try:
        expires_at = int(expires_raw)
    except (TypeError, ValueError):
        return HttpResponse("Token invalido.", status=403)

    if expires_at < int(timezone.now().timestamp()):
        return HttpResponse("Token expirado.", status=403)

    expected_signature = _attachment_export_signature(pk, expires_at)
    if not signature or not hmac.compare_digest(signature, expected_signature):
        return HttpResponse("Token invalido.", status=403)

    attachment = get_object_or_404(Attachment.objects.select_related("expense"), pk=pk)
    return _attachment_file_response(attachment, disposition="attachment", allow_cross_origin=True)


@login_required
def expense_action(request, pk: int, action: str):
    def action_redirect():
        next_url = request.POST.get("next", "").strip()
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect("expense_list")

    if request.method != "POST":
        return action_redirect()
    if not _can_manage_expenses(request.user):
        messages.error(request, "No tienes permisos para ejecutar acciones sobre gastos.")
        return action_redirect()

    expense = get_object_or_404(Expense, pk=pk)
    reason = request.POST.get("reason", "").strip()

    if action == "approve":
        if not _can_decide_expenses(request.user):
            messages.error(request, "No tienes permisos para aprobar gastos.")
            return action_redirect()
        if expense.status != "completed":
            messages.error(request, "Solo se puede aprobar un gasto parametrizado.")
            return action_redirect()
        old_status = expense.status
        expense.status = "approved"
        expense.decision_by = request.user
        expense.decision_at = timezone.now()
        expense.save(update_fields=["status", "decision_by", "decision_at"])
        _log_expense_event(
            expense,
            action="approved",
            actor=request.user,
            reason=reason,
            changes={
                "status": {"before": old_status, "after": expense.status},
                "decision_by": {"before": None, "after": request.user.email},
                "decision_at": {"before": None, "after": expense.decision_at.isoformat()},
            },
        )
        messages.success(request, "Gasto aprobado.")
        return action_redirect()

    if action == "reject":
        if not _can_decide_expenses(request.user):
            messages.error(request, "No tienes permisos para rechazar gastos.")
            return action_redirect()
        if not reason:
            messages.error(request, "Debes indicar el motivo del rechazo.")
            return action_redirect()

        with transaction.atomic():
            expense = Expense.objects.select_for_update().get(pk=pk)
            if expense.status != "completed":
                messages.error(request, "Solo se puede rechazar un gasto parametrizado.")
                return action_redirect()
            old_status = expense.status
            old_reason = expense.rejection_reason
            expense.status = "rejected"
            expense.decision_by = request.user
            expense.decision_at = timezone.now()
            expense.rejection_reason = reason
            expense.save(update_fields=["status", "decision_by", "decision_at", "rejection_reason"])
            _log_expense_event(
                expense,
                action="rejected",
                actor=request.user,
                reason=reason,
                changes={
                    "status": {"before": old_status, "after": expense.status},
                    "decision_by": {"before": None, "after": request.user.email},
                    "decision_at": {"before": None, "after": expense.decision_at.isoformat()},
                    "rejection_reason": {"before": old_reason, "after": reason},
                },
            )

        try:
            notification = create_rejection_notification(expense)
            enqueued = enqueue_notification_send(notification)
        except Exception:
            logger.exception("No se pudo crear notificación WhatsApp de rechazo para gasto %s.", expense.id)
            notification = None
            enqueued = False
        if notification and enqueued:
            messages.warning(request, "Gasto rechazado. La notificación WhatsApp quedó en proceso.")
        elif notification:
            messages.warning(request, "Gasto rechazado. La notificación WhatsApp quedó pendiente de reintento.")
        else:
            messages.warning(request, "Gasto rechazado. No se pudo crear la notificación WhatsApp.")
        return action_redirect()

    if action == "revert_decision":
        if not request.user.is_superuser:
            messages.error(request, "Solo un superadmin puede revertir una aprobación o rechazo.")
            return action_redirect()
        if not _is_final_expense(expense):
            messages.error(request, "El gasto no tiene una decisión que pueda revertirse.")
            return action_redirect()
        old_status = expense.status
        old_decision_by = expense.decision_by.email if expense.decision_by else ""
        old_decision_at = expense.decision_at.isoformat() if expense.decision_at else None
        old_rejection_reason = expense.rejection_reason
        expense.status = "completed"
        expense.decision_by = None
        expense.decision_at = None
        expense.rejection_reason = ""
        expense.save(update_fields=["status", "decision_by", "decision_at", "rejection_reason"])
        _log_expense_event(
            expense,
            action="decision_reverted",
            actor=request.user,
            reason=reason,
            changes={
                "status": {"before": old_status, "after": expense.status},
                "decision_by": {"before": old_decision_by, "after": None},
                "decision_at": {"before": old_decision_at, "after": None},
                "rejection_reason": {"before": old_rejection_reason, "after": ""},
            },
        )
        messages.success(request, "La decisión fue revertida. El gasto volvió a Parametrizado.")
        return action_redirect()

    if action == "retry_notification":
        if not request.user.is_superuser:
            messages.error(request, "Solo un superadmin puede reintentar notificaciones.")
            return action_redirect()
        notification = (
            expense.notifications.filter(
                notification_type=ExpenseNotification.TYPE_REJECTION,
                channel=ExpenseNotification.CHANNEL_WHATSAPP,
                status=ExpenseNotification.STATUS_FAILED,
            )
            .order_by("-created_at")
            .first()
        )
        if not notification:
            messages.error(request, "No hay una notificación fallida para reintentar.")
            return action_redirect()
        notification.status = ExpenseNotification.STATUS_PENDING
        notification.last_error = ""
        notification.next_retry_at = timezone.now()
        notification.save(update_fields=["status", "last_error", "next_retry_at", "updated_at"])
        enqueue_notification_send(notification)
        messages.success(request, "La notificación WhatsApp quedó pendiente de reintento.")
        return action_redirect()

    if action == "apply_rindegastos_diff":
        diff = get_object_or_404(
            RindegastosExpenseDiff,
            pk=request.POST.get("diff_id"),
            expense=expense,
            status=RindegastosExpenseDiff.STATUS_OPEN,
        )
        if apply_rindegastos_diff(diff, actor=request.user, source="rindegastos_manual_review"):
            messages.success(request, "Cambio de Rindegastos aplicado al gasto.")
        else:
            messages.info(request, "La diferencia ya estaba resuelta.")
        return action_redirect()

    if action == "ignore_rindegastos_diff":
        diff = get_object_or_404(
            RindegastosExpenseDiff,
            pk=request.POST.get("diff_id"),
            expense=expense,
            status=RindegastosExpenseDiff.STATUS_OPEN,
        )
        ignore_rindegastos_diff(diff, actor=request.user)
        _log_expense_event(
            expense,
            action="updated",
            actor=request.user,
            source="rindegastos_manual_review",
            reason="Diferencia de Rindegastos ignorada manualmente.",
            changes={
                "rindegastos_diff": {
                    "field": diff.field_name,
                    "local": diff.local_value,
                    "remote": diff.remote_value,
                    "status": "ignored",
                }
            },
        )
        messages.success(request, "Diferencia de Rindegastos ignorada.")
        return action_redirect()

    if _is_final_expense(expense):
        messages.error(
            request,
            "El gasto está aprobado o rechazado y no admite más acciones. Solo un superadmin puede revertir la decisión.",
        )
        return action_redirect()

    if action == "delete":
        if not request.user.is_superuser:
            messages.error(request, "Solo un superadmin puede eliminar gastos.")
            return action_redirect()
        snapshot_id = expense.id
        split_group_id = expense.split_group_id
        if split_group_id:
            siblings = list(Expense.objects.filter(split_group_id=split_group_id).exclude(pk=expense.pk))
            for sibling in siblings:
                _log_expense_event(
                    sibling,
                    action="updated",
                    actor=request.user,
                    reason=reason,
                    changes={
                        "split_event": {
                            "event": "member_deleted",
                            "group_id": split_group_id,
                            "deleted_expense_id": snapshot_id,
                        }
                    },
                )
        _log_expense_event(
            expense,
            action="deleted",
            actor=request.user,
            reason=reason,
            changes={"status": {"before": expense.status, "after": "deleted"}},
        )
        expense.delete()
        if split_group_id:
            _rebalance_split_group(
                split_group_id,
                actor=request.user,
                reason=reason,
                deleted_expense_id=snapshot_id,
            )
        messages.warning(request, f"Gasto #{snapshot_id} eliminado.")
        return action_redirect()

    if action == "split":
        if expense.status in {"approved", "rejected"}:
            messages.error(request, "No se puede dividir un gasto aprobado o rechazado.")
            return action_redirect()
        if expense.split_group_id:
            messages.error(request, "Este gasto ya fue dividido y no se puede volver a dividir.")
            return action_redirect()

        raw_count = (request.POST.get("split_count") or "").strip()
        try:
            split_count = int(raw_count)
        except (TypeError, ValueError):
            split_count = 2
        split_count = max(2, min(split_count, 20))

        group_id = str(uuid4())
        expense.split_group_id = group_id
        expense.split_index = 1
        expense.split_total = split_count
        expense.save(update_fields=["split_group_id", "split_index", "split_total"])

        original_attachments = list(expense.attachments.all())
        created_ids = []
        for idx in range(2, split_count + 1):
            split_expense = Expense.objects.create(
                status=expense.status,
                amount=expense.amount,
                currency=expense.currency,
                category=expense.category,
                worksite=expense.worksite,
                worksite_standard=expense.worksite_standard,
                rindegastos_cost_center=expense.rindegastos_cost_center,
                rindegastos_submitter=expense.rindegastos_submitter,
                supplier=expense.supplier,
                supplier_rut=expense.supplier_rut,
                paid_at=expense.paid_at,
                notes=expense.notes,
                wa_message_id=None,
                wa_sender_phone=expense.wa_sender_phone,
                wa_media_id=expense.wa_media_id,
                wa_sender=expense.wa_sender,
                source=expense.source,
                created_by=expense.created_by,
                message_sent_at=expense.message_sent_at,
                document_type=expense.document_type,
                rindegastos_document_type=expense.rindegastos_document_type,
                document_number=expense.document_number,
                is_vehicle=expense.is_vehicle,
                vehicle=expense.vehicle,
                fuel_km=expense.fuel_km,
                fuel_liters=expense.fuel_liters,
                gasoline_type=expense.gasoline_type,
                iva_amount=expense.iva_amount,
                specific_tax_amount=expense.specific_tax_amount,
                rindegastos_tax=expense.rindegastos_tax,
                tax_calculation_source=expense.tax_calculation_source,
                tax_calculation_metadata=expense.tax_calculation_metadata,
                expense_type=expense.expense_type,
                expense_type_other=expense.expense_type_other,
                split_group_id=group_id,
                split_parent=expense,
                split_index=idx,
                split_total=split_count,
            )
            # Keep report date aligned with the original expense.
            Expense.objects.filter(pk=split_expense.pk).update(created_at=expense.created_at)
            split_expense.created_at = expense.created_at
            created_ids.append(split_expense.id)
            for attachment in original_attachments:
                Attachment.objects.create(
                    expense=split_expense,
                    file=attachment.file.name,
                    checksum_sha256=attachment.checksum_sha256,
                    content_type=attachment.content_type,
                    width=attachment.width,
                    height=attachment.height,
                )
            _log_expense_event(
                split_expense,
                action="created",
                actor=request.user,
                reason=reason,
                changes={
                    "split": {
                        "group_id": group_id,
                        "index": idx,
                        "total": split_count,
                        "from_expense_id": expense.id,
                    }
                },
            )

        _log_expense_event(
            expense,
            action="updated",
            actor=request.user,
            reason=reason,
            changes={
                "split": {
                    "group_id": group_id,
                    "index": 1,
                    "total": split_count,
                    "created_expense_ids": created_ids,
                }
            },
        )
        messages.success(
            request,
            f"Gasto #{expense.id} dividido en {split_count} gastos (grupo {group_id[:8]}).",
        )
        return action_redirect()

    messages.error(request, "Acción no soportada.")
    return action_redirect()


@login_required
@admin_required
def settings_system_users(request):
    User = get_user_model()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_system_user":
            email = request.POST.get("email", "").strip().lower()
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            password = request.POST.get("password", "")
            role = request.POST.get("role", "reviewer").strip() or "reviewer"
            is_active = request.POST.get("is_active") == "on"
            is_superuser = request.user.is_superuser and request.POST.get("is_superuser") == "on"

            if role not in {"admin", "reviewer", "viewer"}:
                role = "reviewer"

            if not email:
                messages.error(request, "El email es obligatorio.")
            elif not password:
                messages.error(request, "La contraseña inicial es obligatoria.")
            elif User.objects.filter(email__iexact=email).exists():
                messages.error(request, "Ya existe un usuario con ese email.")
            else:
                user = User(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    role=role,
                    is_active=is_active,
                    is_staff=is_superuser,
                    is_superuser=is_superuser,
                )
                user.set_password(password)
                user.save()
                _log_user_event(
                    request.user,
                    user,
                    action="created",
                    changes={
                        "email": {"before": None, "after": user.email},
                        "role": {"before": None, "after": user.role},
                        "is_active": {"before": None, "after": user.is_active},
                        "is_superuser": {"before": None, "after": user.is_superuser},
                    },
                )
                messages.success(request, "Usuario del sistema creado.")

        elif action == "update_system_user":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            before = {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
            }
            email = request.POST.get("email", "").strip().lower()
            if not email:
                messages.error(request, "El email es obligatorio.")
                return redirect("settings_system_users")
            if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                messages.error(request, "Ya existe un usuario con ese email.")
                return redirect("settings_system_users")

            user.email = email
            user.first_name = request.POST.get("first_name", "").strip()
            user.last_name = request.POST.get("last_name", "").strip()
            role = request.POST.get("role", "reviewer").strip() or "reviewer"
            if role not in {"admin", "reviewer", "viewer"}:
                role = "reviewer"
            user.role = role
            user.is_active = request.POST.get("is_active") == "on"
            if request.user.is_superuser:
                requested_superuser = request.POST.get("is_superuser") == "on"
                if user == request.user and not requested_superuser:
                    messages.error(request, "No puedes quitarte a ti mismo el permiso de superadmin.")
                    return redirect("settings_system_users")
                user.is_superuser = requested_superuser
                user.is_staff = requested_superuser

            new_password = request.POST.get("password", "")
            if new_password:
                user.set_password(new_password)

            if new_password:
                user.save()
            else:
                update_fields = ["email", "first_name", "last_name", "role", "is_active"]
                if request.user.is_superuser:
                    update_fields.extend(["is_superuser", "is_staff"])
                user.save(update_fields=update_fields)

            after = {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
            }
            changes = _collect_changes(before, after)
            _log_user_event(
                request.user,
                user,
                action="updated",
                changes=changes or {"message": "Guardado sin cambios detectables"},
            )
            if new_password:
                _log_user_event(
                    request.user,
                    user,
                    action="password_reset",
                    changes={"reset_mode": {"before": None, "after": "manual_update"}},
                )
            messages.success(request, "Usuario del sistema actualizado.")

        elif action == "toggle_system_user":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            if user == request.user:
                messages.error(request, "No puedes desactivarte a ti mismo.")
            else:
                old_active = user.is_active
                user.is_active = not user.is_active
                user.save(update_fields=["is_active"])
                _log_user_event(
                    request.user,
                    user,
                    action="activated" if user.is_active else "deactivated",
                    changes={"is_active": {"before": old_active, "after": user.is_active}},
                )
                messages.info(
                    request,
                    f"Usuario {user.email} {'activado' if user.is_active else 'desactivado'}.",
                )

        elif action == "reset_system_user_password":
            user = get_object_or_404(User, pk=request.POST.get("user_id"))
            temporary_password = secrets.token_urlsafe(9)
            user.set_password(temporary_password)
            user.save()
            _log_user_event(
                request.user,
                user,
                action="password_reset",
                changes={"reset_mode": {"before": None, "after": "temporary_password_generated"}},
            )
            messages.warning(
                request,
                f"Password temporal para {user.email}: {temporary_password}",
            )

        return redirect("settings_system_users")

    system_users = (
        User.objects.prefetch_related("received_user_audit_logs")
        .order_by("-is_active", "email")
    )
    for system_user in system_users:
        system_user.audit_entries = list(system_user.received_user_audit_logs.all()[:10])

    context = {
        "system_users": system_users,
        "settings_menu_urls": _settings_menu_urls(),
        "role_choices": User.ROLE_CHOICES,
    }
    return render(request, "settings/system_users.html", context)


@login_required
@admin_required
def settings_users(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_sender":
            phone = request.POST.get("phone", "").strip()
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            email = request.POST.get("email", "").strip()
            active = request.POST.get("active") == "on"
            if not phone:
                messages.error(request, "El teléfono es obligatorio.")
            else:
                AllowedSender.objects.update_or_create(
                    phone=phone,
                    defaults={
                        "first_name": first_name,
                        "last_name": last_name,
                        "email": email,
                        "active": active,
                    },
                )
                messages.success(request, "Usuario de WhatsApp guardado.")

        elif action == "update_sender":
            sender = get_object_or_404(AllowedSender, pk=request.POST.get("sender_id"))
            sender.first_name = request.POST.get("first_name", "").strip()
            sender.last_name = request.POST.get("last_name", "").strip()
            sender.phone = request.POST.get("phone", "").strip()
            sender.email = request.POST.get("email", "").strip()
            sender.active = request.POST.get("active") == "on"
            if not sender.phone:
                messages.error(request, "El teléfono es obligatorio.")
            else:
                sender.save(update_fields=["first_name", "last_name", "phone", "email", "active"])
                messages.success(request, "Usuario actualizado.")

        elif action == "toggle_sender":
            sender = get_object_or_404(AllowedSender, pk=request.POST.get("sender_id"))
            sender.active = not sender.active
            sender.save(update_fields=["active"])
            messages.info(request, f"Usuario {sender} {'activado' if sender.active else 'desactivado'}.")

        elif action == "delete_sender":
            sender = get_object_or_404(AllowedSender, pk=request.POST.get("sender_id"))
            sender.is_deleted = True
            sender.active = False
            sender.save(update_fields=["is_deleted", "active"])
            messages.warning(request, f"Usuario {sender} eliminado.")

        return redirect("settings_users")

    context = {
        "senders": AllowedSender.objects.filter(is_deleted=False).order_by("-active", "first_name"),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/users.html", context)


@login_required
@admin_required
def settings_worksites(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_worksite":
            name = request.POST.get("name", "").strip()
            external_id = request.POST.get("external_id", "").strip() or None
            sync_status = request.POST.get("sync_status", "manual")
            if not name:
                messages.error(request, "El nombre de la obra es obligatorio.")
            else:
                WorksiteCatalog.objects.create(
                    name=name,
                    external_id=external_id,
                    sync_status=sync_status,
                    last_synced_at=timezone.now(),
                )
                messages.success(request, "Obra/proyecto agregado.")

        elif action == "toggle_worksite":
            w = get_object_or_404(WorksiteCatalog, pk=request.POST.get("worksite_id"))
            w.is_active = not w.is_active
            w.save(update_fields=["is_active"])
            messages.info(request, f"Obra '{w.name}' {'activada' if w.is_active else 'desactivada'}.")

        elif action == "sync_worksite":
            w = get_object_or_404(WorksiteCatalog, pk=request.POST.get("worksite_id"))
            w.sync_status = request.POST.get("sync_status", "synced")
            w.last_synced_at = timezone.now()
            w.save(update_fields=["sync_status", "last_synced_at"])
            messages.success(request, f"Obra '{w.name}' marcada como sincronizada.")
        elif action == "update_worksite":
            w = get_object_or_404(WorksiteCatalog, pk=request.POST.get("worksite_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre de la obra es obligatorio.")
            else:
                w.name = name
                w.external_id = request.POST.get("external_id", "").strip() or None
                sync_status = request.POST.get("sync_status", "").strip()
                if sync_status in dict(SYNC_STATUS):
                    w.sync_status = sync_status
                w.save(update_fields=["name", "external_id", "sync_status"])
                messages.success(request, "Obra/proyecto actualizado.")

        return redirect("settings_worksites")

    context = {
        "worksites": WorksiteCatalog.objects.order_by("name"),
        "sync_status_choices": dict(SYNC_STATUS),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/worksites.html", context)


@login_required
@admin_required
def settings_suppliers(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_supplier":
            name = request.POST.get("name", "").strip()
            rut = normalize_rut(request.POST.get("rut"))
            if not name:
                messages.error(request, "El nombre es obligatorio.")
            elif SupplierCatalog.objects.filter(name__iexact=name).exists():
                messages.error(request, "Ya existe un proveedor con ese nombre.")
            else:
                SupplierCatalog.objects.create(name=name, rut=rut)
                messages.success(request, "Proveedor agregado.")

        elif action == "update_supplier":
            supplier = get_object_or_404(SupplierCatalog, pk=request.POST.get("supplier_id"))
            name = request.POST.get("name", "").strip()
            rut = normalize_rut(request.POST.get("rut"))
            if not name:
                messages.error(request, "El nombre es obligatorio.")
            elif SupplierCatalog.objects.filter(name__iexact=name).exclude(pk=supplier.pk).exists():
                messages.error(request, "Ya existe un proveedor con ese nombre.")
            else:
                supplier.name = name
                supplier.rut = rut
                supplier.save(update_fields=["name", "rut", "updated_at"])
                messages.success(request, "Proveedor actualizado.")

        elif action == "toggle_supplier":
            supplier = get_object_or_404(SupplierCatalog, pk=request.POST.get("supplier_id"))
            supplier.is_active = not supplier.is_active
            supplier.save(update_fields=["is_active", "updated_at"])
            messages.info(
                request,
                f"Proveedor '{supplier.name}' {'activado' if supplier.is_active else 'desactivado'}.",
            )

        return redirect("settings_suppliers")

    context = {
        "suppliers": SupplierCatalog.objects.order_by("-is_active", "name"),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/suppliers.html", context)


@login_required
@admin_required
def settings_categories(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action in {"sync_rindegastos", "rebuild_rindegastos"}:
            try:
                rebuild = action == "rebuild_rindegastos"
                stats = RindegastosCatalogSync().sync_all(rebuild=rebuild)
                messages.success(
                    request,
                    f"{'Reconstrucción' if rebuild else 'Sincronización'} Rindegastos completada: "
                    f"{stats['policies']} políticas, "
                    f"{stats['categories']} categorías, "
                    f"{stats['taxes']} impuestos, "
                    f"{stats['expense_fields']} campos extra, "
                    f"{stats['users']} usuarios, "
                    f"{stats['verified_policy_links']} relaciones verificadas.",
                )
            except (RindegastosAPIError, ValueError) as exc:
                messages.error(request, f"No se pudo sincronizar Rindegastos: {exc}")
        elif action == "toggle_category":
            category = get_object_or_404(CategoryCatalog, pk=request.POST.get("category_id"))
            category.is_active = not category.is_active
            category.save(update_fields=["is_active"])
            messages.info(request, f"Política '{category.name}' {'activada' if category.is_active else 'desactivada'}.")
        elif action == "update_category":
            category = get_object_or_404(CategoryCatalog, pk=request.POST.get("category_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre de la política es obligatorio.")
            else:
                exists = CategoryCatalog.objects.exclude(pk=category.pk).filter(name=name).exists()
                if exists:
                    messages.error(request, "Ya existe una política con ese nombre.")
                else:
                    category.name = name
                    category.save(update_fields=["name"])
                    messages.success(request, "Política actualizada.")
        return redirect("settings_categories")

    context = {
        "categories": CategoryCatalog.objects.order_by("name"),
        "settings_menu_urls": _settings_menu_urls(),
        **_catalog_sync_status(CategoryCatalog),
    }
    return render(request, "settings/categories.html", context)


@login_required
@admin_required
def settings_expense_types(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "toggle_expense_type":
            expense_type = get_object_or_404(ExpenseTypeCatalog, pk=request.POST.get("expense_type_id"))
            expense_type.is_active = not expense_type.is_active
            expense_type.save(update_fields=["is_active"])
            messages.info(
                request,
                f"Categoría Rindegastos '{expense_type.name}' {'activado' if expense_type.is_active else 'desactivado'}.",
            )
        elif action == "update_expense_type":
            expense_type = get_object_or_404(ExpenseTypeCatalog, pk=request.POST.get("expense_type_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre de la categoría Rindegastos es obligatorio.")
            else:
                exists = ExpenseTypeCatalog.objects.exclude(pk=expense_type.pk).filter(
                    policy=expense_type.policy,
                    name=name,
                ).exists()
                if exists:
                    messages.error(request, "Ya existe una categoría Rindegastos con ese nombre.")
                else:
                    expense_type.name = name
                    expense_type.save(update_fields=["name"])
                    messages.success(request, "Categoría Rindegastos actualizada.")
        return redirect("settings_expense_types")

    context = {
        "expense_types": ExpenseTypeCatalog.objects.select_related("policy").order_by("policy__name", "name"),
        "settings_menu_urls": _settings_menu_urls(),
        **_catalog_sync_status(ExpenseTypeCatalog),
    }
    return render(request, "settings/expense_types.html", context)


@login_required
@admin_required
def settings_rindegastos_fields(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "toggle_field":
            field = get_object_or_404(RindegastosExpenseFieldCatalog, pk=request.POST.get("field_id"))
            field.is_active = not field.is_active
            field.save(update_fields=["is_active"])
            messages.info(
                request,
                f"Campo '{field.name}' {'activado' if field.is_active else 'desactivado'}.",
            )
        return redirect("settings_rindegastos_fields")

    fields = list(
        RindegastosExpenseFieldCatalog.objects.select_related("policy")
        .order_by("policy__name", "name")
    )
    for field in fields:
        field.display_options = []
        for option in field.options or []:
            if isinstance(option, dict):
                value = option.get("Value") or option.get("Name") or option.get("value") or ""
                code = option.get("Code") or option.get("code") or ""
            else:
                value = str(option)
                code = ""
            if str(value).strip():
                field.display_options.append({"value": str(value).strip(), "code": str(code).strip()})

    context = {
        "fields": fields,
        "settings_menu_urls": _settings_menu_urls(),
        **_catalog_sync_status(RindegastosExpenseFieldCatalog),
    }
    return render(request, "settings/rindegastos_fields.html", context)


@login_required
@expense_manager_required
def settings_rindegastos_rules(request):
    context = {
        "auto_apply_rules": AUTO_APPLY_RULES,
        "manual_review_rules": MANUAL_REVIEW_RULES,
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/rindegastos_rules.html", context)


@login_required
@admin_required
def settings_rindegastos_submitters(request):
    fields = list(
        RindegastosExpenseFieldCatalog.objects.filter(name="Nombre quien rinde")
        .select_related("policy")
        .order_by("policy__name")
    )
    for field in fields:
        field.display_options = []
        for option in field.options or []:
            if isinstance(option, dict):
                value = option.get("Value") or option.get("Name") or option.get("value") or ""
                code = option.get("Code") or option.get("code") or ""
            else:
                value = str(option)
                code = ""
            if str(value).strip():
                field.display_options.append({"value": str(value).strip(), "code": str(code).strip()})

    context = {
        "fields": fields,
        "settings_menu_urls": _settings_menu_urls(),
        **_catalog_sync_status(RindegastosExpenseFieldCatalog),
    }
    return render(request, "settings/rindegastos_submitters.html", context)


@login_required
@admin_required
def settings_tax_indicators(request):
    selected_year = timezone.localdate().year
    if request.method == "POST":
        try:
            selected_year = int(request.POST.get("year") or selected_year)
        except ValueError:
            messages.error(request, "El año indicado no es válido.")
            return redirect("settings_tax_indicators")

        try:
            stats = SiiTaxIndicatorSync().sync_year(selected_year)
            messages.success(
                request,
                f"Sincronización SII completada: {stats['utm_values']} UTM y "
                f"{stats['fuel_rates']} tasas Mepco para {stats['year']}.",
            )
        except (InvalidOperation, RequestException, ValueError) as exc:
            messages.error(request, f"No se pudo sincronizar indicadores SII: {exc}")
        return redirect(f"{reverse('settings_tax_indicators')}?year={selected_year}")

    try:
        selected_year = int(request.GET.get("year") or selected_year)
    except ValueError:
        selected_year = timezone.localdate().year

    utm_values = TaxIndicatorValue.objects.filter(indicator="UTM", year=selected_year).order_by("month")
    fuel_rates = FuelSpecificTaxRate.objects.filter(effective_date__year=selected_year).order_by(
        "-effective_date",
        "fuel_name",
    )
    last_sync = max(
        [
            value
            for value in [
                TaxIndicatorValue.objects.aggregate(value=Max("last_synced_at"))["value"],
                FuelSpecificTaxRate.objects.aggregate(value=Max("last_synced_at"))["value"],
            ]
            if value
        ],
        default=None,
    )

    context = {
        "selected_year": selected_year,
        "utm_values": utm_values,
        "fuel_rates": fuel_rates[:200],
        "fuel_rates_count": fuel_rates.count(),
        "fuel_rates_loaded_count": min(fuel_rates.count(), 200),
        "last_sync": last_sync,
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/tax_indicators.html", context)


@login_required
@admin_required
def settings_tax_indicators_mepco_data(request):
    try:
        year = int(request.GET.get("year") or timezone.localdate().year)
        offset = max(int(request.GET.get("offset") or 0), 0)
        limit = min(max(int(request.GET.get("limit") or 200), 1), 200)
    except ValueError:
        return JsonResponse({"error": "Parámetros inválidos."}, status=400)

    search = (request.GET.get("search") or "").strip()
    queryset = FuelSpecificTaxRate.objects.filter(effective_date__year=year)
    if search:
        search_filter = (
            Q(fuel_name__icontains=search)
            | Q(fuel_key__icontains=search)
            | Q(unit__icontains=search)
        )
        try:
            search_filter |= Q(effective_date=datetime.strptime(search, "%d/%m/%Y").date())
        except ValueError:
            pass
        queryset = queryset.filter(search_filter)

    total = queryset.count()
    rows = queryset.order_by("-effective_date", "fuel_name")[offset : offset + limit]
    data = [
        {
            "effective_date": rate.effective_date.strftime("%d/%m/%Y"),
            "fuel_name": rate.fuel_name,
            "component_base": str(rate.component_base),
            "component_variable": str(rate.component_variable),
            "resulting_tax": str(rate.resulting_tax),
            "unit": rate.unit,
            "last_synced_at": timezone.localtime(rate.last_synced_at).strftime("%d/%m/%Y %H:%M")
            if rate.last_synced_at
            else "-",
        }
        for rate in rows
    ]
    return JsonResponse(
        {
            "rows": data,
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": offset + len(data),
            "has_more": offset + len(data) < total,
        }
    )


@login_required
@admin_required
def settings_view(request):
    return redirect("settings_system_users")
