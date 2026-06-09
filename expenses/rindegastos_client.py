import requests
from django.conf import settings


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
