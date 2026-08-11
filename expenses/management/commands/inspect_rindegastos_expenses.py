import csv
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from expenses.models import Expense, RindegastosExpenseSnapshot
from expenses.rindegastos_client import RindegastosClient
from expenses.rindegastos_reconcile import normalize_rindegastos_expense
from expenses.rindegastos_trace import expense_integration_code_for_expense
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync


class Command(BaseCommand):
    help = "Inspecciona gastos Rindegastos puntuales contra la API, snapshots y vínculos locales."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", help="Ids de gasto Rindegastos a revisar.")
        parser.add_argument("--id", action="append", dest="option_ids", default=[], help="Id Rindegastos a revisar.")
        parser.add_argument("--expense-id", type=int, action="append", default=[], help="Id local de Expense.")
        parser.add_argument("--since", help="Fecha desde para buscar en getExpenses. Default: 2026-04-01.")
        parser.add_argument("--until", help="Fecha hasta para buscar en getExpenses. Default: hoy.")
        parser.add_argument("--max-pages", type=int, default=20)
        parser.add_argument("--results-per-page", type=int, default=100)
        parser.add_argument("--format", choices=["text", "csv"], default="text")

    def handle(self, *args, **options):
        since = _parse_date_option(options.get("since"), date(2026, 4, 1), "since")
        until = _parse_date_option(options.get("until"), timezone.localdate(), "until")
        ids, local_expenses = self._resolve_targets(options)
        if not ids:
            raise CommandError("Entrega al menos un id Rindegastos o --expense-id.")

        client = RindegastosClient()
        listed_by_id = self._fetch_listed_by_id(client, ids, since, until, options)
        report_cache = {}
        rows = [
            self._inspect_remote_id(
                client,
                remote_id,
                listed_by_id.get(remote_id, []),
                local_expenses.get(remote_id, []),
                report_cache,
            )
            for remote_id in sorted(ids)
        ]

        if options["format"] == "csv":
            writer = csv.DictWriter(self.stdout, fieldnames=FIELD_NAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            return

        self.stdout.write("\t".join(FIELD_NAMES))
        for row in rows:
            self.stdout.write("\t".join(str(row[field]) for field in FIELD_NAMES))

    def _resolve_targets(self, options):
        ids = {str(value).strip() for value in options["ids"] + options["option_ids"] if str(value).strip()}
        local_expenses = {}
        for expense_id in options["expense_id"]:
            try:
                expense = Expense.objects.get(pk=expense_id)
            except Expense.DoesNotExist as exc:
                raise CommandError(f"No existe Expense #{expense_id}.") from exc
            if expense.rindegastos_expense_id:
                remote_id = str(expense.rindegastos_expense_id)
                ids.add(remote_id)
                local_expenses.setdefault(remote_id, []).append(expense)
            for snapshot_id in (
                expense.rindegastos_snapshots.exclude(rindegastos_expense_id="")
                .values_list("rindegastos_expense_id", flat=True)
                .distinct()
            ):
                remote_id = str(snapshot_id)
                ids.add(remote_id)
                local_expenses.setdefault(remote_id, []).append(expense)
        for remote_id in ids:
            for expense in Expense.objects.filter(rindegastos_expense_id=remote_id):
                local_expenses.setdefault(remote_id, []).append(expense)
        return ids, local_expenses

    def _fetch_listed_by_id(self, client, ids, since, until, options):
        sync = RindegastosUploadedExpenseSync(client=client)
        remote_expenses, _pages_read, _results_per_page = sync.fetch_remote_expenses(
            since=since,
            until=until,
            max_pages=options["max_pages"],
            results_per_page=options["results_per_page"],
        )
        listed_by_id = {remote_id: [] for remote_id in ids}
        for payload in remote_expenses:
            remote_id = str(payload.get("Id") or payload.get("id") or "")
            if remote_id in listed_by_id:
                listed_by_id[remote_id].append(payload)
        return listed_by_id

    def _inspect_remote_id(self, client, remote_id, listed_payloads, local_expenses, report_cache):
        detail_payload = None
        detail_error = ""
        try:
            detail_payload = client.get_expense(remote_id)
            detail_status = "ok"
        except Exception as exc:
            detail_status = "error"
            detail_error = str(exc)

        normalized = normalize_rindegastos_expense(detail_payload or listed_payloads[0]) if detail_payload or listed_payloads else {}
        report_context = self._report_context(client, normalized.get("rindegastos_report_id") or "", report_cache)
        local_ids = sorted({str(expense.id) for expense in local_expenses})
        local_otz_ids = sorted({expense_integration_code_for_expense(expense) for expense in local_expenses})
        snapshot_count = RindegastosExpenseSnapshot.objects.filter(rindegastos_expense_id=remote_id).count()
        flags = []
        if not listed_payloads:
            flags.append("not_found_in_getExpenses_window")
        if detail_status == "error":
            flags.append("getExpense_error")
        remote_status = normalized.get("rindegastos_status") or ""
        remote_status_label = _status_label(remote_status)
        if _looks_deleted(remote_status):
            flags.append("remote_deleted_like_status")
        if detail_status == "ok" and not listed_payloads:
            flags.append("detail_ok_but_not_listed")
        if snapshot_count and not local_ids:
            flags.append("snapshot_without_current_local_link")

        return {
            "rindegastos_expense_id": remote_id,
            "detail_status": detail_status,
            "listed_count": len(listed_payloads),
            "remote_status": remote_status,
            "remote_status_label": remote_status_label,
            "remote_report_id": normalized.get("rindegastos_report_id") or "",
            "report_detail_status": report_context["detail_status"],
            "report_status": report_context["status"],
            "report_status_label": report_context["status_label"],
            "report_number": report_context["number"],
            "report_title": report_context["title"],
            "report_employee": report_context["employee"],
            "report_error": report_context["error"],
            "remote_issue_date": normalized.get("issue_date") or "",
            "remote_supplier": normalized.get("supplier") or "",
            "remote_total": normalized.get("total") or "",
            "remote_policy": normalized.get("policy_name") or "",
            "remote_category": normalized.get("category_name") or "",
            "integration_code": normalized.get("integration_code") or "",
            "integration_external_code": normalized.get("integration_external_code") or "",
            "local_expense_ids": ",".join(local_ids),
            "local_otz_ids": ",".join(local_otz_ids),
            "snapshot_count": snapshot_count,
            "detail_error": detail_error[:300],
            "flags": ",".join(flags),
        }

    def _report_context(self, client, report_id, report_cache):
        if not report_id:
            return _empty_report_context()
        if report_id in report_cache:
            return report_cache[report_id]
        try:
            payload = client.get_expense_report(report_id)
            status = _first_present(payload, "Status", "status")
            context = {
                "detail_status": "ok",
                "status": str(status if status is not None else ""),
                "status_label": _report_status_label(status),
                "number": str(_first_present(payload, "ReportNumber", "Folio", "folio", "reportNumber") or ""),
                "title": str(_first_present(payload, "Title", "title") or ""),
                "employee": str(_first_present(payload, "EmployeeName", "UserName", "employeeName", "userName") or ""),
                "error": "",
            }
        except Exception as exc:
            context = _empty_report_context()
            context["detail_status"] = "error"
            context["error"] = str(exc)[:300]
        report_cache[report_id] = context
        return context


FIELD_NAMES = [
    "rindegastos_expense_id",
    "detail_status",
    "listed_count",
    "remote_status",
    "remote_status_label",
    "remote_report_id",
    "report_detail_status",
    "report_status",
    "report_status_label",
    "report_number",
    "report_title",
    "report_employee",
    "report_error",
    "remote_issue_date",
    "remote_supplier",
    "remote_total",
    "remote_policy",
    "remote_category",
    "integration_code",
    "integration_external_code",
    "local_expense_ids",
    "local_otz_ids",
    "snapshot_count",
    "detail_error",
    "flags",
]


def _parse_date_option(raw_value, default, name):
    if not raw_value:
        return default
    parsed = parse_date(raw_value)
    if not parsed:
        raise CommandError(f"{name} debe venir en formato YYYY-MM-DD.")
    return parsed


def _looks_deleted(value):
    normalized = str(value or "").strip().casefold()
    return any(term in normalized for term in ("deleted", "elimin", "borrad", "anulad", "cancel"))


def _status_label(value):
    return {
        "0": "En proceso",
        "1": "Aprobado",
        "2": "Rechazado",
    }.get(_normalized_code(value), "")


def _report_status_label(value):
    return {
        "0": "Abierto / En proceso",
        "1": "Cerrado",
    }.get(_normalized_code(value), "")


def _normalized_code(value):
    if value is None:
        return ""
    return str(value).strip()


def _first_present(payload, *keys):
    for key in keys:
        value = (payload or {}).get(key)
        if value not in {None, ""}:
            return value
    return None


def _empty_report_context():
    return {
        "detail_status": "",
        "status": "",
        "status_label": "",
        "number": "",
        "title": "",
        "employee": "",
        "error": "",
    }
