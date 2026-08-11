import requests
from django.conf import settings


class NotionAPIError(Exception):
    pass


class NotionClient:
    def __init__(self, base_url=None, token=None, version=None, timeout=None):
        self.base_url = (base_url or settings.NOTION_API_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.NOTION_API_KEY
        self.version = version or settings.NOTION_API_VERSION
        self.timeout = timeout or settings.NOTION_API_TIMEOUT
        if not self.token:
            raise NotionAPIError("NOTION_API_KEY no está configurado.")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "Content-Type": "application/json",
        }

    def _request(self, method, endpoint, payload=None, params=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = requests.request(
            method,
            url,
            json=payload,
            params=params or {},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise NotionAPIError(f"{endpoint}: HTTP {response.status_code} - {response.text[:500]}")
        try:
            return response.json()
        except ValueError as exc:
            raise NotionAPIError(f"{endpoint}: respuesta no es JSON") from exc

    def query_data_source(self, data_source_id, filter_payload=None, page_size=100):
        return self._query_paginated(
            f"data_sources/{data_source_id}/query",
            filter_payload=filter_payload,
            page_size=page_size,
        )

    def query_database(self, database_id, filter_payload=None, page_size=100):
        return self._query_paginated(
            f"databases/{database_id}/query",
            filter_payload=filter_payload,
            page_size=page_size,
        )

    def _query_paginated(self, endpoint, filter_payload=None, page_size=100):
        results = []
        cursor = None
        while True:
            payload = {"page_size": page_size}
            if filter_payload:
                payload["filter"] = filter_payload
            if cursor:
                payload["start_cursor"] = cursor
            response = self._request("POST", endpoint, payload=payload)
            results.extend(response.get("results") or [])
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            if not cursor:
                break
        return results

    def update_page_properties(self, page_id, properties):
        return self._request("PATCH", f"pages/{page_id}", payload={"properties": properties})
