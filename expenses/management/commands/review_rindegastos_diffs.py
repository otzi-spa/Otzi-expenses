import csv
import json
from collections import defaultdict

from django.core.management.base import BaseCommand

from expenses.models import RindegastosExpenseDiff
from expenses.rindegastos_trace import expense_integration_code_for_expense


SENSITIVE_FIELDS = {
    "total",
    "supplier",
    "category_name",
    "policy_name",
    "custom_fields.Vehiculo o Equipo",
}


class Command(BaseCommand):
    help = "Lista diferencias Rindegastos abiertas con contexto para revisión."

    def add_arguments(self, parser):
        parser.add_argument("--status", default="open")
        parser.add_argument("--expense-id", type=int)
        parser.add_argument("--field-name")
        parser.add_argument("--format", choices=["text", "csv"], default="text")
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **options):
        queryset = (
            RindegastosExpenseDiff.objects.select_related("expense", "snapshot")
            .filter(status=options["status"])
            .order_by("expense_id", "field_name", "snapshot__rindegastos_expense_id", "id")
        )
        if options.get("expense_id"):
            queryset = queryset.filter(expense_id=options["expense_id"])
        if options.get("field_name"):
            queryset = queryset.filter(field_name=options["field_name"])

        diffs = list(queryset[: max(1, options["limit"])])
        context = self._build_context(diffs)
        rows = [self._row_for_diff(diff, context) for diff in diffs]

        if options["format"] == "csv":
            self._write_csv(rows)
            return
        self._write_text(rows)

    def _build_context(self, diffs):
        remote_ids_by_expense = defaultdict(set)
        remote_totals_by_expense = defaultdict(set)
        for diff in diffs:
            remote_ids_by_expense[diff.expense_id].add(diff.snapshot.rindegastos_expense_id)
            if diff.field_name == "total":
                remote_totals_by_expense[diff.expense_id].add(_format_value(diff.remote_value))
        return {
            "remote_ids_by_expense": remote_ids_by_expense,
            "remote_totals_by_expense": remote_totals_by_expense,
        }

    def _row_for_diff(self, diff, context):
        expense = diff.expense
        remote_ids = context["remote_ids_by_expense"][diff.expense_id]
        remote_totals = context["remote_totals_by_expense"][diff.expense_id]
        flags = []
        if len(remote_ids) > 1:
            flags.append("multiple_remote_expenses")
        if len(remote_totals) > 1:
            flags.append("multiple_remote_totals")
        if diff.field_name in SENSITIVE_FIELDS:
            flags.append("sensitive")
        if "multiple_remote_expenses" in flags or "multiple_remote_totals" in flags:
            flags.append("manual_review")

        return {
            "expense_id": expense.id,
            "otz_id": expense_integration_code_for_expense(expense),
            "rindegastos_expense_id": diff.snapshot.rindegastos_expense_id,
            "rindegastos_report_id": diff.snapshot.rindegastos_report_id,
            "expense_paid_at": expense.paid_at.isoformat() if expense.paid_at else "",
            "expense_supplier": expense.supplier or "",
            "expense_total": _format_value(expense.amount),
            "expense_policy": expense.category or "",
            "expense_category": expense.expense_type or "",
            "field_name": diff.field_name,
            "local_value": _format_value(diff.local_value),
            "remote_value": _format_value(diff.remote_value),
            "severity": diff.severity,
            "snapshot_fetched_at": diff.snapshot.fetched_at.isoformat() if diff.snapshot.fetched_at else "",
            "flags": ",".join(flags),
        }

    def _write_csv(self, rows):
        writer = csv.DictWriter(self.stdout, fieldnames=FIELD_NAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    def _write_text(self, rows):
        self.stdout.write("\t".join(FIELD_NAMES))
        for row in rows:
            self.stdout.write("\t".join(str(row[field]) for field in FIELD_NAMES))


FIELD_NAMES = [
    "expense_id",
    "otz_id",
    "rindegastos_expense_id",
    "rindegastos_report_id",
    "expense_paid_at",
    "expense_supplier",
    "expense_total",
    "expense_policy",
    "expense_category",
    "field_name",
    "local_value",
    "remote_value",
    "severity",
    "snapshot_fetched_at",
    "flags",
]


def _format_value(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
