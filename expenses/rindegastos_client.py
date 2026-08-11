import requests
from django.conf import settings
from django.utils import timezone


class RindegastosAPIError(Exception):
    pass


class RindegastosClient:
    def __init__(self, base_url=None, token=None, timeout=None):
        self.base_url = (base_url or settings.RINDEGASTOS_API_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.RINDEGASTOS_API_TOKEN
        self.timeout = timeout or settings.RINDEGASTOS_API_TIMEOUT
        if not self.token:
            raise RindegastosAPIError("RINDEGASTOS_API_TOKEN no está configurado.")

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = requests.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RindegastosAPIError(f"{endpoint}: HTTP {response.status_code} - {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RindegastosAPIError(f"{endpoint}: respuesta no es JSON") from exc
        return payload

    def _put(self, endpoint, data=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = requests.put(
            url,
            json=data or {},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RindegastosAPIError(f"{endpoint}: HTTP {response.status_code} - {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RindegastosAPIError(f"{endpoint}: respuesta no es JSON") from exc
        return payload

    def _get_paginated(self, endpoint, collection_key, params=None):
        params = dict(params or {})
        params.setdefault("ResultsPerPage", 100)
        page = int(params.get("Page", 1) or 1)
        items = []

        while True:
            params["Page"] = page
            payload = self._get(endpoint, params=params)
            current = payload.get(collection_key)
            if current is None:
                current = payload.get(collection_key.lower(), [])
            if isinstance(current, dict):
                current = [current]
            items.extend(current or [])

            records = payload.get("Records") or {}
            pages = int(records.get("Pages") or page)
            if page >= pages:
                break
            page += 1

        return items

    def get_expense_policies(self, active_only=True):
        params = {"Status": 1} if active_only else {}
        return self._get_paginated("getExpensePolicies", "Policies", params=params)

    def get_expense_policy_categories(self, policy_id):
        payload = self._get("getExpensePolicyCategories", params={"IdPolicy": policy_id})
        return payload.get("Categories") or []

    def get_expense_policy_taxes(self, policy_id):
        payload = self._get("getExpensePolicyTaxes", params={"IdPolicy": policy_id})
        return payload.get("Taxes") or []

    def get_expense_policy_expense_fields(self, policy_id):
        payload = self._get("getExpensePolicyExpenseFields", params={"IdPolicy": policy_id})
        return payload.get("ExpenseExtraFields") or []

    def get_users(self):
        users = self._get_paginated("getUsers", "Users")
        if users:
            return users
        return self._get_paginated("getUsers", "Employees")

    def get_expenses(self, params=None):
        return self._get_paginated("getExpenses", "Expenses", params=params)

    def get_expenses_page(self, params=None):
        payload = self._get("getExpenses", params=params)
        expenses = payload.get("Expenses")
        if expenses is None:
            expenses = payload.get("expenses", [])
        if isinstance(expenses, dict):
            expenses = [expenses]
        return expenses or [], payload.get("Records") or {}

    def get_expense(self, expense_id):
        payload = self._get("getExpense", params={"Id": expense_id})
        return payload.get("Expense") or payload.get("expense") or payload

    def get_expense_report(self, report_id):
        payload = self._get("getExpenseReport", params={"Id": report_id})
        report = payload.get("ExpenseReport") or payload.get("expenseReport")
        if report:
            return report
        reports = payload.get("ExpenseReports") or payload.get("expenseReports")
        if isinstance(reports, list):
            return reports[0] if reports else {}
        if isinstance(reports, dict):
            return reports
        return payload

    def set_expense_integration(self, expense_id, integration_status, integration_code, integration_date=None):
        data = {
            "Id": expense_id,
            "IntegrationStatus": int(integration_status),
            "IntegrationCode": integration_code or "",
        }
        formatted_date = _format_integration_date(integration_date)
        if formatted_date:
            data["IntegrationDate"] = formatted_date
        return self._put("setExpenseIntegration", data=data)


def _format_integration_date(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "date") and timezone.is_aware(value):
        value = timezone.localtime(value)
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)
