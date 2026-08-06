import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import Expense, RindegastosExpenseSnapshot, RindegastosReconcileRun
from .rindegastos_client import RindegastosClient
from .rindegastos_trace import expense_integration_code
from .rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, summarize_rindegastos_expense


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
        return str(Decimal(str(value).replace(",", ".")))
    except (InvalidOperation, ValueError):
        return normalize_string(value)


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


class RindegastosExpenseReconciler:
    def __init__(self, client=None, export_id_func=None):
        self.client = client or RindegastosClient()
        self.export_id_func = export_id_func or expense_integration_code

    def reconcile(self, since=None, until=None, max_pages=20, results_per_page=100, dry_run=False, fetch_detail=True):
        matcher = RindegastosUploadedExpenseSync(client=self.client, export_id_func=self.export_id_func)
        remote_expenses, pages_read, results_per_page = matcher.fetch_remote_expenses(
            since=since,
            until=until,
            max_pages=max_pages,
            results_per_page=results_per_page,
        )
        local_by_export_id = matcher.build_local_export_map()
        local_by_remote_id = matcher.build_local_remote_id_map()

        run = None
        if not dry_run:
            run = RindegastosReconcileRun.objects.create(
                since=since,
                until=until,
                max_pages=max_pages,
                metadata={"dry_run": False, "fetch_detail": fetch_detail, "pages_read": pages_read},
            )

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
            "matched_by": {"integration_code": 0, "note": 0, "remote_id": 0},
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

                changed = self._process_snapshot(
                    expense=expense,
                    payload=snapshot_payload,
                    source_endpoint=source_endpoint,
                    run=run,
                    dry_run=dry_run,
                )
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

    @transaction.atomic
    def _process_snapshot(self, expense, payload, source_endpoint, run=None, dry_run=False):
        normalized = normalize_rindegastos_expense(payload)
        payload_hash = normalized_payload_hash(normalized)
        exists = RindegastosExpenseSnapshot.objects.filter(expense=expense, payload_hash=payload_hash).exists()
        if exists:
            return False
        if dry_run:
            return True
        RindegastosExpenseSnapshot.objects.create(
            expense=expense,
            run=run,
            rindegastos_expense_id=normalized["rindegastos_expense_id"] or str(payload.get("Id") or ""),
            rindegastos_report_id=normalized["rindegastos_report_id"],
            payload_hash=payload_hash,
            normalized_payload=normalized,
            raw_payload=payload,
            source_endpoint=source_endpoint,
        )
        return True

    def _finish_run(self, run, stats, status):
        run.finished_at = timezone.now()
        run.fetched_count = stats["fetched"]
        run.matched_count = stats["matched"]
        run.changed_count = stats["changed_snapshots"]
        run.diff_count = 0
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
