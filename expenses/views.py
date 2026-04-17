from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from django.http import FileResponse
from django.shortcuts import render, get_object_or_404, redirect
from .models import (
    Expense,
    Attachment,
    AllowedSender,
    CategoryCatalog,
    ExpenseAuditLog,
    ExpenseTypeCatalog,
    VehicleCatalog,
    WorksiteCatalog,
    SYNC_STATUS,
)
from django.contrib.auth.decorators import login_required
from django.utils.dateparse import parse_date
from django.contrib import messages
from django.utils import timezone


ALLOWED_RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_RECEIPT_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_RECEIPT_SIZE_BYTES = 10 * 1024 * 1024


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
        "vehicles": VehicleCatalog.objects.filter(is_active=True).order_by("name"),
        "categories": categories,
    }
    return render(request, "dashboard.html", context)


def _settings_menu_urls():
    return {
        "settings",
        "settings_users",
        "settings_vehicles",
        "settings_worksites",
        "settings_categories",
        "settings_expense_types",
    }


def _normalize_empty(value):
    text = (value or "").strip()
    return text or None


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
        actor_name = actor.get_full_name() or actor.username
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


def _missing_fields_for_parametrization(expense, has_receipt=None):
    missing = []
    if expense.amount is None:
        missing.append("Monto")
    if not _normalize_empty(expense.currency):
        missing.append("Moneda")
    if not _normalize_empty(expense.category) or expense.category == "Sin Categoria":
        missing.append("Categoría")
    if not _normalize_empty(expense.supplier):
        missing.append("Proveedor")
    if not _normalize_empty(expense.worksite):
        missing.append("Obra reportada")
    if not _normalize_empty(expense.worksite_standard):
        missing.append("Obra parametrizada")
    if not expense.paid_at:
        missing.append("Fecha del gasto")
    if not _normalize_empty(expense.document_type):
        missing.append("Tipo de documento")
    if not expense.is_vehicle and not _normalize_empty(expense.expense_type):
        missing.append("Tipo de gasto")
    if expense.is_vehicle and not _normalize_empty(expense.vehicle):
        missing.append("Vehículo")
    if has_receipt is None:
        has_receipt = expense.attachments.exists()
    if not has_receipt:
        missing.append("Comprobante")
    return missing


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
        tracked_fields = [
            "status",
            "amount",
            "currency",
            "category",
            "supplier",
            "worksite",
            "worksite_standard",
            "notes",
            "paid_at",
            "document_type",
            "is_vehicle",
            "vehicle",
            "expense_type",
            "expense_type_other",
        ]
        before = {field: getattr(expense, field) for field in tracked_fields}

        requested_status = request.POST.get("status", expense.status)
        if expense.status in {"approved", "rejected"}:
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

        category_select = request.POST.get("category_select", "").strip()
        new_category_name = request.POST.get("new_category_name", "").strip()
        category_legacy = request.POST.get("category", "").strip()
        if (category_select == "__new__" and new_category_name) or (not category_select and new_category_name):
            cat_obj, _ = CategoryCatalog.objects.get_or_create(name=new_category_name, defaults={"is_active": True})
            if not cat_obj.is_active:
                cat_obj.is_active = True
                cat_obj.save(update_fields=["is_active"])
            expense.category = cat_obj.name
            messages.success(request, f"Categoría '{cat_obj.name}' creada desde el modal.")
        elif category_select and category_select != "__new__":
            cat_obj, _ = CategoryCatalog.objects.get_or_create(name=category_select, defaults={"is_active": True})
            if not cat_obj.is_active:
                cat_obj.is_active = True
                cat_obj.save(update_fields=["is_active"])
            expense.category = cat_obj.name
        elif category_legacy:
            expense.category = category_legacy
        else:
            expense.category = Expense._meta.get_field("category").default

        supplier_select = request.POST.get("supplier_select", "").strip()
        new_supplier_name = request.POST.get("new_supplier_name", "").strip()
        supplier_legacy = request.POST.get("supplier", "")
        if (supplier_select == "__new__" and new_supplier_name) or (not supplier_select and new_supplier_name):
            expense.supplier = new_supplier_name.strip()
        elif supplier_select and supplier_select != "__new__":
            expense.supplier = supplier_select
        else:
            expense.supplier = supplier_legacy.strip()

        worksite_raw = request.POST.get("worksite", "")
        expense.worksite = worksite_raw.strip()

        new_worksite_name = request.POST.get("new_worksite_name", "").strip()
        if new_worksite_name:
            ws, _ = WorksiteCatalog.objects.get_or_create(name=new_worksite_name, defaults={"is_active": True})
            if not ws.is_active:
                ws.is_active = True
                ws.save(update_fields=["is_active"])
            messages.success(request, f"Obra '{ws.name}' creada desde el modal.")
            if not request.POST.get("worksite_standard", "").strip():
                request.POST = request.POST.copy()
                request.POST["worksite_standard"] = ws.name

        worksite_std = request.POST.get("worksite_standard", "").strip()
        if worksite_std:
            ws_std_obj, _ = WorksiteCatalog.objects.get_or_create(name=worksite_std, defaults={"is_active": True})
            if not ws_std_obj.is_active:
                ws_std_obj.is_active = True
                ws_std_obj.save(update_fields=["is_active"])
        expense.worksite_standard = worksite_std or None

        document_type = request.POST.get("document_type", "").strip()
        expense.document_type = document_type or None

        is_vehicle_raw = request.POST.get("is_vehicle")
        expense.is_vehicle = bool(is_vehicle_raw)

        vehicle_raw = request.POST.get("vehicle", "")
        new_vehicle_name = request.POST.get("new_vehicle_name", "").strip()
        if new_vehicle_name:
            vh, _ = VehicleCatalog.objects.get_or_create(name=new_vehicle_name, defaults={"is_active": True})
            if not vh.is_active:
                vh.is_active = True
                vh.save(update_fields=["is_active"])
            messages.success(request, f"Vehículo '{vh.name}' creado desde el modal.")
            if not request.POST.get("vehicle_standard", "").strip():
                request.POST = request.POST.copy()
                request.POST["vehicle_standard"] = vh.name

        vehicle_std = request.POST.get("vehicle_standard", "").strip()
        if vehicle_std:
            vh_std_obj, _ = VehicleCatalog.objects.get_or_create(name=vehicle_std, defaults={"is_active": True})
            if not vh_std_obj.is_active:
                vh_std_obj.is_active = True
                vh_std_obj.save(update_fields=["is_active"])
        expense.vehicle = (vehicle_std or vehicle_raw.strip()) if expense.is_vehicle else None

        expense_type_select = request.POST.get("expense_type_select", "").strip()
        if expense.is_vehicle:
            expense.expense_type = None
            expense.expense_type_other = None
        else:
            if expense_type_select:
                et_obj = ExpenseTypeCatalog.objects.filter(
                    is_active=True,
                    name=expense_type_select,
                ).first()
                expense.expense_type = et_obj.name if et_obj else None
            else:
                expense.expense_type = None

            expense_type_other = request.POST.get("expense_type_other", "").strip()
            expense.expense_type_other = expense_type_other or None

        notes = request.POST.get("notes", "")
        expense.notes = notes.strip()

        paid_at_raw = request.POST.get("paid_at", "").strip()
        expense.paid_at = parse_date(paid_at_raw) if paid_at_raw else None

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

    category_select = request.POST.get("category_select", "").strip()
    new_category_name = request.POST.get("new_category_name", "").strip()
    if (category_select == "__new__" and new_category_name) or (not category_select and new_category_name):
        cat_obj, _ = CategoryCatalog.objects.get_or_create(name=new_category_name, defaults={"is_active": True})
        if not cat_obj.is_active:
            cat_obj.is_active = True
            cat_obj.save(update_fields=["is_active"])
        expense.category = cat_obj.name
    elif category_select and category_select != "__new__":
        cat_obj, _ = CategoryCatalog.objects.get_or_create(name=category_select, defaults={"is_active": True})
        if not cat_obj.is_active:
            cat_obj.is_active = True
            cat_obj.save(update_fields=["is_active"])
        expense.category = cat_obj.name

    supplier_select = request.POST.get("supplier_select", "").strip()
    new_supplier_name = request.POST.get("new_supplier_name", "").strip()
    if (supplier_select == "__new__" and new_supplier_name) or (not supplier_select and new_supplier_name):
        expense.supplier = new_supplier_name
    elif supplier_select and supplier_select != "__new__":
        expense.supplier = supplier_select

    expense.worksite = request.POST.get("worksite", "").strip()

    new_worksite_name = request.POST.get("new_worksite_name", "").strip()
    if new_worksite_name:
        ws, _ = WorksiteCatalog.objects.get_or_create(name=new_worksite_name, defaults={"is_active": True})
        if not ws.is_active:
            ws.is_active = True
            ws.save(update_fields=["is_active"])
        messages.success(request, f"Obra '{ws.name}' creada desde el modal.")
        if not request.POST.get("worksite_standard", "").strip():
            request.POST = request.POST.copy()
            request.POST["worksite_standard"] = ws.name

    worksite_std = request.POST.get("worksite_standard", "").strip()
    if worksite_std:
        ws_std_obj, _ = WorksiteCatalog.objects.get_or_create(name=worksite_std, defaults={"is_active": True})
        if not ws_std_obj.is_active:
            ws_std_obj.is_active = True
            ws_std_obj.save(update_fields=["is_active"])
    expense.worksite_standard = worksite_std or None

    expense.document_type = request.POST.get("document_type", "").strip() or None

    expense.is_vehicle = bool(request.POST.get("is_vehicle"))
    vehicle_raw = request.POST.get("vehicle", "").strip()
    new_vehicle_name = request.POST.get("new_vehicle_name", "").strip()
    if new_vehicle_name:
        vh, _ = VehicleCatalog.objects.get_or_create(name=new_vehicle_name, defaults={"is_active": True})
        if not vh.is_active:
            vh.is_active = True
            vh.save(update_fields=["is_active"])
        messages.success(request, f"Vehículo '{vh.name}' creado desde el modal.")
        if not request.POST.get("vehicle_standard", "").strip():
            request.POST = request.POST.copy()
            request.POST["vehicle_standard"] = vh.name

    vehicle_std = request.POST.get("vehicle_standard", "").strip()
    if vehicle_std:
        vh_std_obj, _ = VehicleCatalog.objects.get_or_create(name=vehicle_std, defaults={"is_active": True})
        if not vh_std_obj.is_active:
            vh_std_obj.is_active = True
            vh_std_obj.save(update_fields=["is_active"])
    expense.vehicle = (vehicle_std or vehicle_raw) if expense.is_vehicle else None

    expense_type_select = request.POST.get("expense_type_select", "").strip()
    if expense.is_vehicle:
        expense.expense_type = None
        expense.expense_type_other = None
    else:
        if expense_type_select:
            et_obj = ExpenseTypeCatalog.objects.filter(
                is_active=True,
                name=expense_type_select,
            ).first()
            expense.expense_type = et_obj.name if et_obj else None
        expense.expense_type_other = request.POST.get("expense_type_other", "").strip() or None
    expense.notes = request.POST.get("notes", "").strip()

    paid_at_raw = request.POST.get("paid_at", "").strip()
    expense.paid_at = parse_date(paid_at_raw) if paid_at_raw else None

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
    gastos = (
        Expense.objects.select_related("created_by", "wa_sender")
        .prefetch_related("attachments", "audit_logs")
        .order_by("-created_at")
    )
    senders_by_phone = {
        s.phone: s
        for s in AllowedSender.objects.filter(is_deleted=False)
    }
    for gasto in gastos:
        if not gasto.wa_sender and gasto.wa_sender_phone:
            sender = senders_by_phone.get(gasto.wa_sender_phone)
            if sender:
                name = f"{sender.first_name} {sender.last_name}".strip()
                gasto.wa_sender_name = name or sender.phone
        logs = list(gasto.audit_logs.all())
        gasto.audit_entries = logs[:5]
        gasto.audit_entries_all = logs
        gasto.can_approve_or_reject = gasto.status == "completed"
        gasto.split_label = ""
        if gasto.split_group_id and gasto.split_index and gasto.split_total:
            gasto.split_label = f"División {gasto.split_index}/{gasto.split_total}"
    context = {
        "gastos": gastos,
        "status_choices": Expense.STATUS,
        "vehicles": VehicleCatalog.objects.filter(is_active=True).order_by("name"),
        "worksites": WorksiteCatalog.objects.filter(is_active=True).order_by("name"),
        "categories_catalog": CategoryCatalog.objects.filter(is_active=True).order_by("name"),
        "expense_types_catalog": ExpenseTypeCatalog.objects.filter(is_active=True).order_by("name"),
        "suppliers_catalog": (
            Expense.objects.exclude(supplier__isnull=True)
            .exclude(supplier="")
            .values_list("supplier", flat=True)
            .distinct()
            .order_by("supplier")
        ),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "expenses/gastos.html", context)


@login_required
def attachment_serve(request, pk: int):
    attachment = get_object_or_404(Attachment.objects.select_related("expense"), pk=pk)
    file_handle = attachment.file.open("rb")
    content_type = attachment.content_type or "application/octet-stream"
    response = FileResponse(file_handle, content_type=content_type)
    filename = attachment.file.name.rsplit("/", 1)[-1]
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


@login_required
def expense_action(request, pk: int, action: str):
    if request.method != "POST":
        return redirect("expense_list")

    expense = get_object_or_404(Expense, pk=pk)
    reason = request.POST.get("reason", "").strip()

    if action == "approve":
        if expense.status != "completed":
            messages.error(request, "Solo se puede aprobar un gasto parametrizado.")
            return redirect("expense_list")
        old_status = expense.status
        expense.status = "approved"
        expense.save(update_fields=["status"])
        _log_expense_event(
            expense,
            action="approved",
            actor=request.user,
            reason=reason,
            changes={"status": {"before": old_status, "after": expense.status}},
        )
        messages.success(request, "Gasto aprobado.")
        return redirect("expense_list")

    if action == "reject":
        if expense.status != "completed":
            messages.error(request, "Solo se puede rechazar un gasto parametrizado.")
            return redirect("expense_list")
        old_status = expense.status
        expense.status = "rejected"
        expense.save(update_fields=["status"])
        _log_expense_event(
            expense,
            action="rejected",
            actor=request.user,
            reason=reason,
            changes={"status": {"before": old_status, "after": expense.status}},
        )
        messages.warning(request, "Gasto rechazado.")
        return redirect("expense_list")

    if action == "delete":
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
        return redirect("expense_list")

    if action == "split":
        if expense.status in {"approved", "rejected"}:
            messages.error(request, "No se puede dividir un gasto aprobado o rechazado.")
            return redirect("expense_list")
        if expense.split_group_id:
            messages.error(request, "Este gasto ya fue dividido y no se puede volver a dividir.")
            return redirect("expense_list")

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
                supplier=expense.supplier,
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
                is_vehicle=expense.is_vehicle,
                vehicle=expense.vehicle,
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
        return redirect("expense_list")

    messages.error(request, "Acción no soportada.")
    return redirect("expense_list")


@login_required
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
def settings_vehicles(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_vehicle":
            name = request.POST.get("name", "").strip()
            external_id = request.POST.get("external_id", "").strip() or None
            sync_status = request.POST.get("sync_status", "manual")
            if not name:
                messages.error(request, "El nombre del vehículo/equipo es obligatorio.")
            else:
                VehicleCatalog.objects.create(
                    name=name,
                    external_id=external_id,
                    sync_status=sync_status,
                    last_synced_at=timezone.now(),
                )
                messages.success(request, "Vehículo/equipo agregado.")

        elif action == "toggle_vehicle":
            v = get_object_or_404(VehicleCatalog, pk=request.POST.get("vehicle_id"))
            v.is_active = not v.is_active
            v.save(update_fields=["is_active"])
            messages.info(request, f"Vehículo '{v.name}' {'activado' if v.is_active else 'desactivado'}.")

        elif action == "sync_vehicle":
            v = get_object_or_404(VehicleCatalog, pk=request.POST.get("vehicle_id"))
            v.sync_status = request.POST.get("sync_status", "synced")
            v.last_synced_at = timezone.now()
            v.save(update_fields=["sync_status", "last_synced_at"])
            messages.success(request, f"Vehículo '{v.name}' marcado como sincronizado.")
        elif action == "update_vehicle":
            v = get_object_or_404(VehicleCatalog, pk=request.POST.get("vehicle_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre del vehículo/equipo es obligatorio.")
            else:
                v.name = name
                v.external_id = request.POST.get("external_id", "").strip() or None
                sync_status = request.POST.get("sync_status", "").strip()
                if sync_status in dict(SYNC_STATUS):
                    v.sync_status = sync_status
                v.save(update_fields=["name", "external_id", "sync_status"])
                messages.success(request, "Vehículo/equipo actualizado.")

        return redirect("settings_vehicles")

    context = {
        "vehicles": VehicleCatalog.objects.order_by("name"),
        "sync_status_choices": dict(SYNC_STATUS),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/vehicles.html", context)


@login_required
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
def settings_categories(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_category":
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre de la categoría es obligatorio.")
            else:
                obj, created = CategoryCatalog.objects.get_or_create(name=name, defaults={"is_active": True})
                if not created and not obj.is_active:
                    obj.is_active = True
                    obj.save(update_fields=["is_active"])
                messages.success(request, f"Categoría '{obj.name}' guardada.")
        elif action == "toggle_category":
            category = get_object_or_404(CategoryCatalog, pk=request.POST.get("category_id"))
            category.is_active = not category.is_active
            category.save(update_fields=["is_active"])
            messages.info(request, f"Categoría '{category.name}' {'activada' if category.is_active else 'desactivada'}.")
        elif action == "update_category":
            category = get_object_or_404(CategoryCatalog, pk=request.POST.get("category_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre de la categoría es obligatorio.")
            else:
                exists = CategoryCatalog.objects.exclude(pk=category.pk).filter(name=name).exists()
                if exists:
                    messages.error(request, "Ya existe una categoría con ese nombre.")
                else:
                    category.name = name
                    category.save(update_fields=["name"])
                    messages.success(request, "Categoría actualizada.")
        return redirect("settings_categories")

    context = {
        "categories": CategoryCatalog.objects.order_by("name"),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/categories.html", context)


@login_required
def settings_expense_types(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_expense_type":
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre del tipo de gasto es obligatorio.")
            else:
                obj, created = ExpenseTypeCatalog.objects.get_or_create(name=name, defaults={"is_active": True})
                if not created and not obj.is_active:
                    obj.is_active = True
                    obj.save(update_fields=["is_active"])
                messages.success(request, f"Tipo de gasto '{obj.name}' guardado.")
        elif action == "toggle_expense_type":
            expense_type = get_object_or_404(ExpenseTypeCatalog, pk=request.POST.get("expense_type_id"))
            expense_type.is_active = not expense_type.is_active
            expense_type.save(update_fields=["is_active"])
            messages.info(
                request,
                f"Tipo de gasto '{expense_type.name}' {'activado' if expense_type.is_active else 'desactivado'}.",
            )
        elif action == "update_expense_type":
            expense_type = get_object_or_404(ExpenseTypeCatalog, pk=request.POST.get("expense_type_id"))
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "El nombre del tipo de gasto es obligatorio.")
            else:
                exists = ExpenseTypeCatalog.objects.exclude(pk=expense_type.pk).filter(name=name).exists()
                if exists:
                    messages.error(request, "Ya existe un tipo de gasto con ese nombre.")
                else:
                    expense_type.name = name
                    expense_type.save(update_fields=["name"])
                    messages.success(request, "Tipo de gasto actualizado.")
        return redirect("settings_expense_types")

    context = {
        "expense_types": ExpenseTypeCatalog.objects.order_by("name"),
        "settings_menu_urls": _settings_menu_urls(),
    }
    return render(request, "settings/expense_types.html", context)


@login_required
def settings_view(request):
    return redirect("settings_users")
