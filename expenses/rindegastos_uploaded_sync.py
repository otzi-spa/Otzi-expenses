import re
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Expense
from .rindegastos_client import RindegastosClient


OTZI_EXPENSE_ID_RE = re.compile(r"\bOTZ-[A-Z0-9]+\b")


def default_uploaded_sync_since():
    today = timezone.localdate()
    return date(today.year, 4, 1)


def rolling_uploaded_sync_since(days=120):
    return timezone.localdate() - timedelta(days=days)


def extract_otzi_ids(value):
    return sorted(set(OTZI_EXPENSE_ID_RE.findall(str(value or "").upper())))


def expense_export_id(expense_id, export_id_func):
    return export_id_func(expense_id)


def summarize_rindegastos_expense(payload):
    note = payload.get("Note") or payload.get("Comment") or payload.get("Description") or ""
    return {
        "id": payload.get("Id"),
        "status": payload.get("Status"),
        "supplier": payload.get("Supplier"),
        "issue_date": payload.get("IssueDate"),
        "total": payload.get("Total"),
        "currency": payload.get("Currency"),
        "category": payload.get("Category"),
        "policy_id": payload.get("ExpensePolicyId") or payload.get("PolicyId"),
        "policy_name": payload.get("ExpensePolicyName") or payload.get("PolicyName"),
        "report_id": payload.get("ReportId"),
        "note": note,
        "otzi_ids": extract_otzi_ids(note),
        "integration_date": payload.get("IntegrationDate"),
        "integration_external_code": payload.get("IntegrationExternalCode"),
        "raw_keys": sorted(payload.keys()),
    }


class RindegastosUploadedExpenseSync:
    def __init__(self, client=None, export_id_func=None):
        self.client = client or RindegastosClient()
        self.export_id_func = export_id_func

    def fetch_remote_expenses(self, since=None, until=None, max_pages=10, results_per_page=100):
        since = since or default_uploaded_sync_since()
        until = until or timezone.localdate()
        max_pages = max(1, int(max_pages or 1))
        results_per_page = min(100, max(1, int(results_per_page or 100)))

        all_expenses = []
        pages_read = 0
        for page in range(1, max_pages + 1):
            page_expenses, records = self.client.get_expenses_page(
                {
                    "Since": since.isoformat(),
                    "Until": until.isoformat(),
                    "ResultsPerPage": results_per_page,
                    "Page": page,
                    "OrderBy": 1,
                    "Order": "DESC",
                }
            )
            pages_read = page
            all_expenses.extend(page_expenses)
            total_pages = int((records or {}).get("Pages") or page)
            if page >= total_pages or len(page_expenses) < results_per_page:
                break

        return all_expenses, pages_read, results_per_page

    def build_local_export_map(self):
        if not self.export_id_func:
            raise ValueError("export_id_func es obligatorio para vincular gastos locales.")
        return {
            expense_export_id(expense.id, self.export_id_func): expense
            for expense in Expense.objects.all()
        }

    @transaction.atomic
    def sync(self, since=None, until=None, max_pages=10, results_per_page=100):
        now = timezone.now()
        remote_expenses, pages_read, results_per_page = self.fetch_remote_expenses(
            since=since,
            until=until,
            max_pages=max_pages,
            results_per_page=results_per_page,
        )
        local_by_export_id = self.build_local_export_map()

        matched = 0
        remote_with_otzi = 0
        unmatched_ids = set()
        matched_ids = set()
        for payload in remote_expenses:
            summary = summarize_rindegastos_expense(payload)
            if not summary["otzi_ids"]:
                continue
            remote_with_otzi += 1
            for otzi_id in summary["otzi_ids"]:
                expense = local_by_export_id.get(otzi_id)
                if not expense:
                    unmatched_ids.add(otzi_id)
                    continue
                matched_ids.add(otzi_id)
                uploaded_at = _parse_rindegastos_datetime(summary["issue_date"]) or now
                Expense.objects.filter(pk=expense.pk).update(
                    rindegastos_expense_id=str(summary["id"] or otzi_id),
                    rindegastos_report_id=str(summary["report_id"] or "") or None,
                    rindegastos_uploaded_at=uploaded_at,
                    rindegastos_synced_at=now,
                    rindegastos_status=str(summary["status"] or "") or None,
                    rindegastos_raw_payload=payload,
                )
                matched += 1

        return {
            "since": (since or default_uploaded_sync_since()).isoformat(),
            "until": (until or timezone.localdate()).isoformat(),
            "pages_read": pages_read,
            "results_per_page": results_per_page,
            "remote_fetched": len(remote_expenses),
            "remote_with_otzi_id": remote_with_otzi,
            "matched": matched,
            "matched_ids": sorted(matched_ids),
            "unmatched_ids": sorted(unmatched_ids),
        }


def _parse_rindegastos_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed
