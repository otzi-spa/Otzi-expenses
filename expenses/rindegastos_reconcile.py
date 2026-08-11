import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Expense, RindegastosExpenseDiff, RindegastosExpenseSnapshot, RindegastosReconcileRun
from .rindegastos_client import RindegastosClient
from .rindegastos_diff_rules import apply_rindegastos_diff, classify_diff
from .rindegastos_trace import expense_integration_code
from .rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, extract_otzi_ids, summarize_rindegastos_expense


CUSTOM_FIELD_COLLECTION_KEYS = (
    "CustomFields",
    "customFields",
    "custom_fields",
    "ExpenseExtraFields",
    "expenseExtraFields",
    "ExpenseFields",
    "expenseFields",
    "ExtraFields",
    "extraFields",
)


def first_present_value(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in {None, ""}:
            return value
    return None


def normalize_string(value):
    if value in {None, ""}:
        return ""
    return " ".join(str(value).strip().split())


def normalize_decimal(value):
    if value in {None, ""}:
        return None
    try:
        value = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return normalize_string(value)
    return format(value.normalize(), "f")


def normalize_custom_fields(payload):
    fields = {}
    for collection_key in CUSTOM_FIELD_COLLECTION_KEYS:
        collection = payload.get(collection_key) or []
        if isinstance(collection, dict):
            collection = collection.values()
        for item in collection:
            if not isinstance(item, dict):
                continue
            name = first_present_value(item, "Name", "name", "FieldName", "fieldName", "Label", "label")
            value = first_present_value(item, "Value", "value", "SelectedValue", "selectedValue", "Text", "text")
            if name:
                fields[normalize_string(name)] = normalize_string(value)
    return fields


def normalize_rindegastos_expense(payload):
    custom_fields = normalize_custom_fields(payload)
    note = first_present_value(payload, "Note", "Comment", "Description", "note", "comment", "description")
    return {
        "rindegastos_expense_id": normalize_string(first_present_value(payload, "Id", "id")),
        "rindegastos_report_id": normalize_string(first_present_value(payload, "ReportId", "reportId", "report_id")),
        "rindegastos_status": normalize_string(first_present_value(payload, "Status", "status")),
        "integration_code": normalize_string(
            first_present_value(payload, "IntegrationCode", "integrationCode", "integration_code")
        ),
        "integration_external_code": normalize_string(
            first_present_value(payload, "IntegrationExternalCode", "integrationExternalCode", "integration_external_code")
        ),
        "supplier": normalize_string(first_present_value(payload, "Supplier", "Merchant", "supplier", "merchant")),
        "total": normalize_decimal(first_present_value(payload, "Total", "OriginalTotal", "total", "originalTotal")),
        "currency": normalize_string(first_present_value(payload, "Currency", "currency")),
        "issue_date": normalize_string(first_present_value(payload, "IssueDate", "Date", "issueDate", "date")),
        "policy_name": normalize_string(
            first_present_value(payload, "ExpensePolicyName", "PolicyName", "expensePolicyName", "policyName")
        ),
        "category_name": normalize_string(first_present_value(payload, "Category", "CategoryName", "category", "categoryName")),
        "note": normalize_string(note),
        "tax_name": normalize_string(first_present_value(payload, "TaxName", "Tax", "taxName", "tax")),
        "tax_amount": normalize_decimal(first_present_value(payload, "TaxAmount", "TaxValue", "taxAmount", "taxValue")),
        "other_taxes": normalize_decimal(first_present_value(payload, "OtherTaxes", "OtherTaxAmount", "otherTaxes")),
        "custom_fields": custom_fields,
    }


def normalized_payload_hash(normalized_payload):
    encoded = json.dumps(normalized_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalized_local_expense(expense):
    return {
        "supplier": normalize_string(expense.supplier),
        "total": normalize_decimal(expense.amount),
        "currency": normalize_string(expense.currency),
        "issue_date": expense.paid_at.isoformat() if expense.paid_at else "",
        "policy_name": normalize_string(expense.category),
        "category_name": normalize_string(expense.expense_type),
        "tax_name": normalize_string(expense.rindegastos_tax),
        "tax_amount": normalize_decimal(expense.iva_amount),
        "other_taxes": normalize_decimal(expense.specific_tax_amount),
        "custom_fields": {
            "Centro de Costo / Faena": normalize_string(expense.rindegastos_cost_center),
            "Nombre quien rinde": normalize_string(expense.rindegastos_submitter),
            "RUT proveedor": normalize_string(expense.supplier_rut),
            "Tipo de Documento": normalize_string(expense.rindegastos_document_type or expense.document_type),
            "Numero de Documento": normalize_string(expense.document_number),
            "Vehiculo o Equipo": normalize_string(expense.vehicle),
            "Km.Carguio": normalize_decimal(expense.fuel_km),
            "Litros Combustible": normalize_decimal(expense.fuel_liters),
            "Categoria": normalize_string(expense.expense_type),
        },
    }


def compare_expense_to_remote(expense, remote_payload):
    local = normalized_local_expense(expense)
    diffs = []
    field_map = [
        ("supplier", "supplier", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("total", "total", RindegastosExpenseDiff.SEVERITY_CONFLICT),
        ("currency", "currency", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("issue_date", "issue_date", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("policy_name", "policy_name", RindegastosExpenseDiff.SEVERITY_CONFLICT),
        ("category_name", "category_name", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("tax_name", "tax_name", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("tax_amount", "tax_amount", RindegastosExpenseDiff.SEVERITY_WARNING),
        ("other_taxes", "other_taxes", RindegastosExpenseDiff.SEVERITY_WARNING),
    ]
    for field_name, remote_key, severity in field_map:
        local_value = local.get(field_name)
        remote_value = remote_payload.get(remote_key)
        if remote_value in {None, ""}:
            continue
        if local_value != remote_value:
            diffs.append(
                {
                    "field_name": field_name,
                    "local_value": local_value,
                    "remote_value": remote_value,
                    "severity": severity,
                }
            )

    local_custom_fields = local["custom_fields"]
    remote_custom_fields = remote_payload.get("custom_fields") or {}
    for field_name, local_value in local_custom_fields.items():
        remote_value = remote_custom_fields.get(field_name)
        if remote_value in {None, ""}:
            continue
        if local_value != remote_value:
            diffs.append(
                {
                    "field_name": f"custom_fields.{field_name}",
                    "local_value": local_value,
                    "remote_value": remote_value,
                    "severity": RindegastosExpenseDiff.SEVERITY_WARNING,
                }
            )
    return diffs


class RindegastosExpenseReconciler:
    def __init__(self, client=None, export_id_func=None):
        self.client = client or RindegastosClient()
        self.export_id_func = export_id_func or expense_integration_code

    def reconcile(
        self,
        since=None,
        until=None,
        max_pages=20,
        results_per_page=100,
        dry_run=False,
        fetch_detail=True,
        mark_integration_code=False,
        integration_status=1,
        apply_safe_diffs=False,
    ):
        matcher = RindegastosUploadedExpenseSync(client=self.client, export_id_func=self.export_id_func)
        remote_expenses, pages_read, results_per_page = matcher.fetch_remote_expenses(
            since=since,
            until=until,
            max_pages=max_pages,
            results_per_page=results_per_page,
        )
        local_by_export_id = matcher.build_local_export_map()
        local_by_remote_id = matcher.build_local_remote_id_map()
        matched_remote_counts = self._matched_remote_counts(
            remote_expenses,
            matcher,
            local_by_export_id,
            local_by_remote_id,
        )

        run = None
        if not dry_run:
            run = RindegastosReconcileRun.objects.create(
                since=since,
                until=until,
                max_pages=max_pages,
                metadata={
                    "dry_run": False,
                    "fetch_detail": fetch_detail,
                    "pages_read": pages_read,
                    "mark_integration_code": mark_integration_code,
                    "apply_safe_diffs": apply_safe_diffs,
                },
            )

        mark_enabled = bool(getattr(settings, "RINDEGASTOS_MARK_INTEGRATION_CODE_ENABLED", False))
        stats = {
            "since": since.isoformat() if since else "",
            "until": until.isoformat() if until else "",
            "pages_read": pages_read,
            "results_per_page": results_per_page,
            "dry_run": dry_run,
            "fetch_detail": fetch_detail,
            "fetched": len(remote_expenses),
            "matched": 0,
            "changed_snapshots": 0,
            "unchanged_snapshots": 0,
            "unmatched": 0,
            "errors": 0,
            "diffs_opened": 0,
            "diffs_auto_applied": 0,
            "diffs_manual_review": 0,
            "matched_by": {"integration_code": 0, "note": 0, "remote_id": 0},
            "integration_code": {
                "empty": 0,
                "matching_otzi": 0,
                "conflicting_otzi": 0,
                "non_otzi": 0,
                "marked": 0,
                "skipped_existing": 0,
                "skipped_conflict": 0,
                "skipped_disabled": 0,
                "skipped_missing_remote_id": 0,
                "mark_errors": 0,
                "mark_enabled": mark_enabled,
                "mark_requested": mark_integration_code,
            },
        }

        try:
            for payload in remote_expenses:
                summary = summarize_rindegastos_expense(payload)
                match = matcher.match_remote_expense(summary, local_by_export_id, local_by_remote_id)
                if not match:
                    stats["unmatched"] += 1
                    continue
                expense, _otzi_id, match_source = match
                stats["matched"] += 1
                stats["matched_by"][match_source] += 1

                snapshot_payload = payload
                source_endpoint = "getExpenses"
                if fetch_detail and summary["id"]:
                    try:
                        snapshot_payload = self.client.get_expense(summary["id"])
                        source_endpoint = "getExpense"
                    except Exception:
                        stats["errors"] += 1
                        snapshot_payload = payload

                self._record_integration_code_diagnostic(snapshot_payload, _otzi_id, stats)
                self._maybe_mark_integration_code(
                    summary=summary,
                    payload=snapshot_payload,
                    otzi_id=_otzi_id,
                    stats=stats,
                    dry_run=dry_run,
                    mark_integration_code=mark_integration_code,
                    mark_enabled=mark_enabled,
                    integration_status=integration_status,
                )

                changed, snapshot, normalized = self._process_snapshot(
                    expense=expense,
                    payload=snapshot_payload,
                    source_endpoint=source_endpoint,
                    run=run,
                    dry_run=dry_run,
                )
                diff_specs = compare_expense_to_remote(expense, normalized)
                diff_result = self._handle_diffs(
                    expense,
                    snapshot,
                    diff_specs,
                    remote_ids_count=matched_remote_counts.get(expense.id, 1),
                    dry_run=dry_run,
                    apply_safe_diffs=apply_safe_diffs,
                )
                stats["diffs_opened"] += diff_result["opened"]
                stats["diffs_auto_applied"] += diff_result["auto_applied"]
                stats["diffs_manual_review"] += diff_result["manual_review"]
                if changed:
                    stats["changed_snapshots"] += 1
                else:
                    stats["unchanged_snapshots"] += 1
        except Exception:
            if run:
                self._finish_run(run, stats, status=RindegastosReconcileRun.STATUS_FAILED)
            raise

        if run:
            self._finish_run(run, stats, status=RindegastosReconcileRun.STATUS_COMPLETED)
        return stats

    def _matched_remote_counts(self, remote_expenses, matcher, local_by_export_id, local_by_remote_id):
        remote_ids_by_expense = {}
        for payload in remote_expenses:
            summary = summarize_rindegastos_expense(payload)
            match = matcher.match_remote_expense(summary, local_by_export_id, local_by_remote_id)
            if not match:
                continue
            expense, _otzi_id, _match_source = match
            remote_id = str(summary["id"] or "")
            if not remote_id:
                continue
            remote_ids_by_expense.setdefault(expense.id, set()).add(remote_id)
        return {expense_id: len(remote_ids) for expense_id, remote_ids in remote_ids_by_expense.items()}

    @transaction.atomic
    def _process_snapshot(self, expense, payload, source_endpoint, run=None, dry_run=False):
        normalized = normalize_rindegastos_expense(payload)
        payload_hash = normalized_payload_hash(normalized)
        existing = RindegastosExpenseSnapshot.objects.filter(expense=expense, payload_hash=payload_hash).first()
        if existing:
            return False, existing, normalized
        if dry_run:
            return True, None, normalized
        snapshot = RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            run=run,
            rindegastos_expense_id=normalized["rindegastos_expense_id"] or str(payload.get("Id") or ""),
            rindegastos_report_id=normalized["rindegastos_report_id"],
            payload_hash=payload_hash,
            normalized_payload=normalized,
            raw_payload=payload,
            source_endpoint=source_endpoint,
        )
        return True, snapshot, normalized

    def _handle_diffs(self, expense, snapshot, diff_specs, remote_ids_count=1, dry_run=False, apply_safe_diffs=False):
        result = {"opened": 0, "auto_applied": 0, "manual_review": 0}
        for diff_spec in diff_specs:
            classification = classify_diff(diff_spec, expense, remote_ids_count=remote_ids_count)
            if classification == "manual_review":
                result["manual_review"] += 1
            if dry_run:
                result["opened"] += 1
                if apply_safe_diffs and classification == "auto_apply":
                    result["auto_applied"] += 1
                continue
            exists = RindegastosExpenseDiff.objects.filter(
                expense=expense,
                field_name=diff_spec["field_name"],
                local_value=diff_spec["local_value"],
                remote_value=diff_spec["remote_value"],
                status=RindegastosExpenseDiff.STATUS_OPEN,
            ).first()
            if exists:
                if apply_safe_diffs and classification == "auto_apply":
                    if apply_rindegastos_diff(exists):
                        result["auto_applied"] += 1
                continue
            diff = RindegastosExpenseDiff.objects.create(
                expense=expense,
                snapshot=snapshot,
                field_name=diff_spec["field_name"],
                local_value=diff_spec["local_value"],
                remote_value=diff_spec["remote_value"],
                severity=diff_spec["severity"],
            )
            result["opened"] += 1
            if apply_safe_diffs and classification == "auto_apply":
                if apply_rindegastos_diff(diff):
                    result["auto_applied"] += 1
        return result

    def _record_integration_code_diagnostic(self, payload, expected_otzi_id, stats):
        normalized = normalize_rindegastos_expense(payload)
        remote_code = normalized["integration_code"] or normalized["integration_external_code"]
        if not remote_code:
            stats["integration_code"]["empty"] += 1
            return
        remote_otzi_ids = extract_otzi_ids(remote_code)
        if not remote_otzi_ids:
            stats["integration_code"]["non_otzi"] += 1
            return
        if expected_otzi_id in remote_otzi_ids:
            stats["integration_code"]["matching_otzi"] += 1
            return
        stats["integration_code"]["conflicting_otzi"] += 1

    def _maybe_mark_integration_code(
        self,
        summary,
        payload,
        otzi_id,
        stats,
        dry_run,
        mark_integration_code,
        mark_enabled,
        integration_status,
    ):
        if not mark_integration_code:
            return
        normalized = normalize_rindegastos_expense(payload)
        remote_code = normalized["integration_code"] or normalized["integration_external_code"]
        if remote_code:
            remote_otzi_ids = extract_otzi_ids(remote_code)
            if otzi_id in remote_otzi_ids:
                stats["integration_code"]["skipped_existing"] += 1
            else:
                stats["integration_code"]["skipped_conflict"] += 1
            return
        if dry_run:
            stats["integration_code"]["marked"] += 1
            return
        if not mark_enabled:
            stats["integration_code"]["skipped_disabled"] += 1
            return
        if not summary["id"]:
            stats["integration_code"]["skipped_missing_remote_id"] += 1
            return
        try:
            self.client.set_expense_integration(
                summary["id"],
                integration_status=integration_status,
                integration_code=otzi_id,
                integration_date=timezone.now(),
            )
            stats["integration_code"]["marked"] += 1
        except Exception:
            stats["integration_code"]["mark_errors"] += 1
            stats["errors"] += 1

    def _finish_run(self, run, stats, status):
        run.finished_at = timezone.now()
        run.fetched_count = stats["fetched"]
        run.matched_count = stats["matched"]
        run.changed_count = stats["changed_snapshots"]
        run.diff_count = stats["diffs_opened"]
        run.error_count = stats["errors"]
        run.status = status
        run.metadata = {
            "dry_run": stats["dry_run"],
            "fetch_detail": stats["fetch_detail"],
            "pages_read": stats["pages_read"],
            "results_per_page": stats["results_per_page"],
            "unmatched": stats["unmatched"],
            "unchanged_snapshots": stats["unchanged_snapshots"],
            "matched_by": stats["matched_by"],
            "integration_code": stats["integration_code"],
            "diffs_auto_applied": stats["diffs_auto_applied"],
            "diffs_manual_review": stats["diffs_manual_review"],
        }
        run.save(
            update_fields=[
                "finished_at",
                "fetched_count",
                "matched_count",
                "changed_count",
                "diff_count",
                "error_count",
                "status",
                "metadata",
            ]
        )
