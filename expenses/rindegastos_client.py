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
        _raise_payload_error(endpoint, payload)
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
        _raise_payload_error(endpoint, payload)
        return payload

    def _post(self, endpoint, data=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = requests.post(
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
        _raise_payload_error(endpoint, payload)
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

    def get_funds(self, params=None):
        funds = self._get_paginated("getFunds", "Funds", params=params)
        if isinstance(funds, dict):
            return [funds]
        return funds or []

    def get_fund(self, fund_id):
        payload = self._get("getFund", params={"Id": fund_id})
        funds = payload.get("Funds") or payload.get("funds")
        if isinstance(funds, list):
            return funds[0] if funds else {}
        if isinstance(funds, dict):
            return funds
        fund = payload.get("Fund") or payload.get("fund")
        if fund:
            return fund
        return payload

    def deposit_money_to_fund(self, fund_id, admin_id, amount):
        data = {
            "Id": str(fund_id),
            "IdAdmin": str(admin_id),
            "DepositAmount": str(amount),
        }
        return self._post("depositMoneyToFund", data=data)

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


class RindegastosV2Client(RindegastosClient):
    def __init__(self, base_url=None, token=None, timeout=None):
        resolved_base_url = base_url
        if resolved_base_url is None:
            resolved_base_url = settings.RINDEGASTOS_API_BASE_URL.rstrip("/")
            if resolved_base_url.endswith("/v1"):
                resolved_base_url = resolved_base_url[:-3] + "/v2"
            elif not resolved_base_url.endswith("/v2"):
                resolved_base_url = resolved_base_url.rstrip("/") + "/v2"
        super().__init__(base_url=resolved_base_url, token=token, timeout=timeout)

    def get_funds(self, params=None):
        payload = self._get("getFunds", params=params)
        funds = payload.get("Funds") or payload.get("funds") or []
        if isinstance(funds, dict):
            return [funds]
        return funds or []

    def get_fund(self, fund_id):
        payload = self._get("getFund", params={"Id": fund_id})
        funds = payload.get("Funds") or payload.get("funds")
        if isinstance(funds, list):
            return funds[0] if funds else payload
        if isinstance(funds, dict):
            return funds
        return payload

    def get_fund_request(self, request_id):
        payload = self._get("getFundRequest", params={"Id": request_id})
        requests_payload = payload.get("FundRequests") or payload.get("fundRequests")
        if isinstance(requests_payload, list):
            return requests_payload[0] if requests_payload else payload
        if isinstance(requests_payload, dict):
            return requests_payload
        return payload


class RindegastosCoreClient:
    def __init__(self, base_url=None, token=None, timeout=None):
        self.base_url = (base_url or settings.RINDEGASTOS_CORE_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.RINDEGASTOS_CORE_TOKEN
        self.timeout = timeout or settings.RINDEGASTOS_API_TIMEOUT
        if not self.token:
            raise RindegastosAPIError("RINDEGASTOS_CORE_TOKEN no está configurado.")

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = requests.get(
            url,
            params=params or {},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "contenttype": "application/json",
                "Origin": "https://app.rindegastos.com",
                "Referer": "https://app.rindegastos.com/",
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RindegastosAPIError(f"{endpoint}: HTTP {response.status_code} - {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RindegastosAPIError(f"{endpoint}: respuesta no es JSON") from exc
        _raise_payload_error(endpoint, payload)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RindegastosAPIError(f"{endpoint}: respuesta ok=false - {payload}")
        return payload

    def get_company_fund(self, fund_id):
        payload = self._get("fund/getCompanyFund", params={"fundId": fund_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else payload


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


def _raise_payload_error(endpoint, payload):
    if not isinstance(payload, dict):
        return
    status_code = payload.get("statusCode") or payload.get("StatusCode")
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return
    if status_code >= 400:
        message = payload.get("message") or payload.get("Message") or payload
        raise RindegastosAPIError(f"{endpoint}: API statusCode {status_code} - {message}")
