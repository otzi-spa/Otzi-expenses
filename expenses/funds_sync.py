import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from expenses.models import EmployeeFundMapping, NotionFundSyncLog, normalize_rut
from expenses.notion_client import NotionClient


@dataclass
class NotionFundRecord:
    page_id: str
    url: str
    work_key: str
    notion_status: str
    record_id: str
    beneficiary_name: str
    beneficiary_rut: str
    amount: Decimal | None
    currency: str
    payment_date: object
    cost_center: str
    rindegastos_fund_id: str
    notion_rindegastos_fund: str
    raw_payload: dict
    normalized_payload: dict


class NotionFundsSync:
    def __init__(self, client=None):
        self.client = client or NotionClient()
        self._related_page_cache = {}

    def sync(self, dry_run=False):
        stats = {
            "queried": 0,
            "matched": 0,
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "ready": 0,
            "pending_mapping": 0,
            "errors": 0,
            "ignored": 0,
            "dry_run": dry_run,
        }
        pages = self.fetch_pages()
        records = [self.normalize_page(page) for page in pages]
        matched_records = self.matching_records(records)
        stats["queried"] = len(records)
        stats["matched"] = len(matched_records)
        stats["fetched"] = len(matched_records)
        stats["work_key_values"] = self.work_key_values(records)
        stats["notion_status_values"] = self.notion_status_values(matched_records)
        if dry_run:
            stats["preview"] = [record.normalized_payload for record in matched_records[:20]]
            return stats

        for record in matched_records:
            created, status = self.upsert_record(record)
            if created:
                stats["created"] += 1
            else:
                stats["updated"] += 1
            if status == NotionFundSyncLog.STATUS_READY:
                stats["ready"] += 1
            elif status == NotionFundSyncLog.STATUS_PENDING_MAPPING:
                stats["pending_mapping"] += 1
            elif status == NotionFundSyncLog.STATUS_ERROR:
                stats["errors"] += 1
            elif status == NotionFundSyncLog.STATUS_IGNORED:
                stats["ignored"] += 1
        return stats

    def fetch_pages(self):
        source_id = settings.NOTION_DATA_SOURCE_ID
        database_id = settings.NOTION_DATABASE_ID
        filter_payload = self._work_key_filter()
        if source_id:
            return self.client.query_data_source(source_id, filter_payload=filter_payload)
        elif database_id:
            return self.client.query_database(database_id, filter_payload=filter_payload)
        raise ValueError("Configura NOTION_DATA_SOURCE_ID o NOTION_DATABASE_ID.")

    def fetch_records(self):
        pages = self.fetch_pages()
        records = [self.normalize_page(page) for page in pages]
        return self.matching_records(records)

    def matching_records(self, records):
        expected = settings.NOTION_FUNDS_WORK_KEY_VALUE.casefold()
        return [record for record in records if record.work_key.casefold() == expected]

    def work_key_values(self, records):
        counts = {}
        for record in records:
            value = record.work_key or "(vacío)"
            counts[value] = counts.get(value, 0) + 1
        return counts

    def notion_status_values(self, records):
        counts = {}
        for record in records:
            value = record.notion_status or "(vacío)"
            counts[value] = counts.get(value, 0) + 1
        return counts

    def inspect(self, limit=10):
        pages = self.fetch_pages()
        all_records = [self.normalize_page(page) for page in pages]
        records = all_records[:limit]
        return {
            "queried": len(pages),
            "sampled": len(records),
            "work_key_property": settings.NOTION_FUNDS_WORK_KEY_PROPERTY,
            "work_key_value_expected": settings.NOTION_FUNDS_WORK_KEY_VALUE,
            "work_key_values": self.work_key_values(all_records),
            "notion_status_values": self.notion_status_values(self.matching_records(all_records)),
            "sample": [
                {
                    "normalized": record.normalized_payload,
                    "configured_properties": self.inspect_configured_properties(record.raw_payload),
                    "property_names": sorted((record.raw_payload.get("properties") or {}).keys()),
                }
                for record in records
            ],
        }

    def inspect_configured_properties(self, page):
        properties = page.get("properties") or {}
        configured = {
            "work_key": settings.NOTION_FUNDS_WORK_KEY_PROPERTY,
            "beneficiary": settings.NOTION_FUNDS_BENEFICIARY_PROPERTY,
            "rut": settings.NOTION_FUNDS_RUT_PROPERTY,
            "amount": settings.NOTION_FUNDS_AMOUNT_PROPERTY,
            "currency": settings.NOTION_FUNDS_CURRENCY_PROPERTY,
            "payment_date": settings.NOTION_FUNDS_PAYMENT_DATE_PROPERTY,
            "record_id": settings.NOTION_FUNDS_REMITTANCE_PROPERTY,
            "cost_center": settings.NOTION_FUNDS_COST_CENTER_PROPERTY,
            "notion_status": settings.NOTION_FUNDS_STATUS_PROPERTY,
            "rindegastos_fund": settings.NOTION_FUNDS_RINDEGASTOS_FUND_PROPERTY,
        }
        found = {}
        for label, configured_name in configured.items():
            actual_name, prop = _find_property(properties, configured_name)
            found[label] = {
                "configured": configured_name,
                "actual": actual_name,
                "type": (prop or {}).get("type") or "",
                "text": _property_text(prop),
            }
        return found

    def _work_key_filter(self):
        prop = settings.NOTION_FUNDS_WORK_KEY_PROPERTY
        value = settings.NOTION_FUNDS_WORK_KEY_VALUE
        filter_type = settings.NOTION_FUNDS_WORK_KEY_FILTER_TYPE
        if filter_type in {"select", "status"}:
            return {"property": prop, filter_type: {"equals": value}}
        if filter_type in {"multi_select", "rich_text", "title"}:
            return {"property": prop, filter_type: {"contains": value}}
        return None

    def normalize_page(self, page):
        properties = page.get("properties") or {}
        beneficiary = _property_text(_get_property(properties, settings.NOTION_FUNDS_BENEFICIARY_PROPERTY))
        if not beneficiary:
            beneficiary = self._resolve_relation_names(_get_property(properties, "Beneficiario"))
        rut = normalize_rut(_property_text(_get_property(properties, settings.NOTION_FUNDS_RUT_PROPERTY)))
        amount = _property_decimal(_get_property(properties, settings.NOTION_FUNDS_AMOUNT_PROPERTY))
        currency = _property_text(_get_property(properties, settings.NOTION_FUNDS_CURRENCY_PROPERTY)) or "CLP"
        payment_date = _property_date(_get_property(properties, settings.NOTION_FUNDS_PAYMENT_DATE_PROPERTY))
        work_key = _property_text(_get_property(properties, settings.NOTION_FUNDS_WORK_KEY_PROPERTY))
        notion_status = _property_text(_get_property(properties, settings.NOTION_FUNDS_STATUS_PROPERTY))
        record_id = _property_text(_get_property(properties, settings.NOTION_FUNDS_REMITTANCE_PROPERTY))
        cost_center = _property_text(_get_property(properties, settings.NOTION_FUNDS_COST_CENTER_PROPERTY))
        notion_rindegastos_fund = self._property_text_or_relation_names(
            properties,
            settings.NOTION_FUNDS_RINDEGASTOS_FUND_PROPERTY,
        )
        rindegastos_fund_id = _extract_fund_id(notion_rindegastos_fund)
        normalized = {
            "page_id": page.get("id") or "",
            "url": page.get("url") or "",
            "work_key": work_key,
            "notion_status": notion_status,
            "record_id": record_id,
            "beneficiary_name": beneficiary,
            "beneficiary_rut": rut,
            "amount": str(amount) if amount is not None else "",
            "currency": currency,
            "payment_date": payment_date.isoformat() if payment_date else "",
            "cost_center": cost_center,
            "rindegastos_fund_id": rindegastos_fund_id,
            "notion_rindegastos_fund": notion_rindegastos_fund,
        }
        return NotionFundRecord(
            page_id=page.get("id") or "",
            url=page.get("url") or "",
            work_key=work_key,
            notion_status=notion_status,
            record_id=record_id,
            beneficiary_name=beneficiary,
            beneficiary_rut=rut,
            amount=amount,
            currency=currency,
            payment_date=payment_date,
            cost_center=cost_center,
            rindegastos_fund_id=rindegastos_fund_id,
            notion_rindegastos_fund=notion_rindegastos_fund,
            raw_payload=page,
            normalized_payload=normalized,
        )

    @transaction.atomic
    def upsert_record(self, record):
        mapping = self._find_mapping(record)
        status, error = self._validate(record, mapping)
        defaults = {
            "notion_url": record.url,
            "notion_status": record.notion_status,
            "notion_work_key": record.work_key,
            "notion_record_id": record.record_id,
            "beneficiary_name": record.beneficiary_name,
            "beneficiary_rut": record.beneficiary_rut,
            "amount": record.amount,
            "currency": record.currency,
            "payment_date": record.payment_date,
            "cost_center": record.cost_center,
            "local_status": status,
            "idempotency_key": self.idempotency_key(record),
            "mapping": mapping,
            "rindegastos_user": mapping.rindegastos_user if mapping else None,
            "rindegastos_fund_id": record.rindegastos_fund_id or (mapping.rindegastos_fund_id if mapping else ""),
            "notion_rindegastos_fund": record.notion_rindegastos_fund,
            "notion_raw_payload": record.raw_payload,
            "normalized_payload": record.normalized_payload,
            "last_error": error,
            "last_synced_at": timezone.now(),
        }
        log, created = NotionFundSyncLog.objects.update_or_create(
            notion_page_id=record.page_id,
            defaults=defaults,
        )
        return created, log.local_status

    def _find_mapping(self, record):
        if record.beneficiary_rut:
            mapping = (
                EmployeeFundMapping.objects.select_related("rindegastos_user")
                .filter(is_active=True, rut=record.beneficiary_rut)
                .first()
            )
            if mapping:
                return mapping
        if record.beneficiary_name:
            return (
                EmployeeFundMapping.objects.select_related("rindegastos_user")
                .filter(is_active=True, notion_beneficiary_name__iexact=record.beneficiary_name)
                .first()
            )
        return None

    def _validate(self, record, mapping):
        errors = []
        pending = []
        if not record.page_id:
            errors.append("Notion no entregó page_id.")
        if record.work_key != settings.NOTION_FUNDS_WORK_KEY_VALUE:
            errors.append(f"k de trabajo no coincide con {settings.NOTION_FUNDS_WORK_KEY_VALUE}.")
        if not record.beneficiary_name:
            errors.append("Beneficiario vacío.")
        if not record.beneficiary_rut:
            pending.append("RUT vacío.")
        if record.amount is None or record.amount <= 0:
            errors.append("Monto vacío o menor/igual a cero.")
        if not record.payment_date:
            errors.append("Fecha de pago vacía o inválida.")
        if not mapping and not record.rindegastos_fund_id:
            pending.append("No existe mapeo activo beneficiario -> Rindegastos.")
        elif mapping and not mapping.rindegastos_user and not mapping.rindegastos_fund_id and not record.rindegastos_fund_id:
            pending.append("El mapeo no tiene usuario ni fondo Rindegastos.")
        if errors:
            return NotionFundSyncLog.STATUS_ERROR, " ".join(errors)
        if pending:
            return NotionFundSyncLog.STATUS_PENDING_MAPPING, " ".join(pending)
        return NotionFundSyncLog.STATUS_READY, ""

    def idempotency_key(self, record):
        raw = "|".join(
            [
                record.page_id,
                record.record_id,
                record.beneficiary_rut,
                str(record.amount or ""),
                record.payment_date.isoformat() if record.payment_date else "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _resolve_relation_names(self, prop):
        relation_ids = _property_relation_ids(prop)
        names = []
        for page_id in relation_ids:
            try:
                page = self._related_page_cache.get(page_id)
                if page is None:
                    page = self.client.retrieve_page(page_id)
                    self._related_page_cache[page_id] = page
            except Exception:
                continue
            name = _page_title(page)
            if name:
                names.append(name)
        return ", ".join(names)

    def _property_text_or_relation_names(self, properties, property_name):
        prop = _get_property(properties, property_name)
        if (prop or {}).get("type") == "relation":
            return self._resolve_relation_names(prop)
        return _property_text(prop)


def _property_text(prop):
    if not prop:
        return ""
    prop_type = prop.get("type")
    value = prop.get(prop_type)
    if prop_type in {"title", "rich_text"}:
        return "".join(part.get("plain_text") or "" for part in value or []).strip()
    if prop_type in {"select", "status"}:
        return ((value or {}).get("name") or "").strip()
    if prop_type == "multi_select":
        return ", ".join(item.get("name") or "" for item in value or []).strip()
    if prop_type == "number":
        return "" if value is None else str(value)
    if prop_type == "date":
        return ((value or {}).get("start") or "").strip()
    if prop_type == "formula":
        return _formula_text(value or {})
    if prop_type == "rollup":
        return _rollup_text(value or {})
    if prop_type == "relation":
        return ", ".join(item.get("id") or "" for item in value or []).strip()
    if prop_type == "unique_id":
        prefix = (value or {}).get("prefix") or ""
        number = (value or {}).get("number")
        return f"{prefix}-{number}" if prefix and number is not None else str(number or "")
    if prop_type in {"email", "phone_number", "url"}:
        return (value or "").strip()
    if prop_type == "checkbox":
        return "true" if value else "false"
    return ""


def _extract_fund_id(value):
    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(r"\bID\s*[:#-]?\s*(\d{3,})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{3,})\b", text)
    return match.group(1) if match else ""


def _property_relation_ids(prop):
    if not prop or prop.get("type") != "relation":
        return []
    return [item.get("id") for item in prop.get("relation") or [] if item.get("id")]


def _page_title(page):
    properties = page.get("properties") or {}
    for prop in properties.values():
        if prop.get("type") == "title":
            text = _property_text(prop)
            if text:
                return text
    for prop in properties.values():
        if prop.get("type") in {"rich_text", "select", "status", "email"}:
            text = _property_text(prop)
            if text:
                return text
    return ""


def _get_property(properties, name):
    return _find_property(properties, name)[1]


def _find_property(properties, name):
    if not properties or not name:
        return "", None
    if name in properties:
        return name, properties[name]
    expected = _normalize_property_name(name)
    for property_name, value in properties.items():
        if _normalize_property_name(property_name) == expected:
            return property_name, value
    return "", None


def _normalize_property_name(value):
    return " ".join((value or "").replace("\xa0", " ").strip().casefold().split())


def _formula_text(value):
    formula_type = value.get("type")
    formula_value = value.get(formula_type)
    if formula_type in {"string", "number", "boolean"}:
        return "" if formula_value is None else str(formula_value)
    if formula_type == "date":
        return ((formula_value or {}).get("start") or "").strip()
    return ""


def _rollup_text(value):
    rollup_type = value.get("type")
    rollup_value = value.get(rollup_type)
    if rollup_type in {"number", "incomplete", "unsupported"}:
        return "" if rollup_value is None else str(rollup_value)
    if rollup_type == "date":
        return ((rollup_value or {}).get("start") or "").strip()
    if rollup_type == "array":
        parts = [_property_text(item) for item in rollup_value or []]
        return ", ".join(part for part in parts if part).strip()
    return ""


def _property_decimal(prop):
    text = _property_text(prop)
    if not text:
        return None
    normalized = text.replace(".", "").replace(",", ".") if "," in text else text
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _property_date(prop):
    value = _property_text(prop)
    if not value:
        return None
    return parse_date(value[:10])
