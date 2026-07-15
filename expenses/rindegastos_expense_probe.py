import re
from datetime import date

from django.utils import timezone

from .rindegastos_client import RindegastosClient


OTZI_EXPENSE_ID_RE = re.compile(r"\bOTZ-[A-Z0-9]+\b")


def default_probe_since():
    today = timezone.localdate()
    return date(today.year, 4, 1)


def extract_otzi_ids(value):
    return sorted(set(OTZI_EXPENSE_ID_RE.findall(str(value or "").upper())))


def summarize_rindegastos_expense(payload):
    note = payload.get("Note") or payload.get("Comment") or payload.get("Description") or ""
    otzi_ids = extract_otzi_ids(note)
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
        "otzi_ids": otzi_ids,
        "integration_date": payload.get("IntegrationDate"),
        "integration_external_code": payload.get("IntegrationExternalCode"),
        "raw_keys": sorted(payload.keys()),
    }


class RindegastosExpenseProbe:
    def __init__(self, client=None):
        self.client = client or RindegastosClient()

    def fetch(self, since=None, until=None, max_pages=5, results_per_page=100):
        since = since or default_probe_since()
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

        summarized = [summarize_rindegastos_expense(item) for item in all_expenses]
        with_otzi_id = [item for item in summarized if item["otzi_ids"]]
        return {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "pages_read": pages_read,
            "results_per_page": results_per_page,
            "total_fetched": len(summarized),
            "total_with_otzi_id": len(with_otzi_id),
            "otzi_ids": sorted({otzi_id for item in with_otzi_id for otzi_id in item["otzi_ids"]}),
            "matches": with_otzi_id,
            "sample": summarized[:10],
        }
