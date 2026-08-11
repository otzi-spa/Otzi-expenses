import hashlib
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
    record_id: str
    beneficiary_name: str
    beneficiary_rut: str
    amount: Decimal | None
    currency: str
    payment_date: object
    cost_center: str
    raw_payload: dict
    normalized_payload: dict


class NotionFundsSync:
    def __init__(self, client=None):
        self.client = client or NotionClient()

    def sync(self, dry_run=False):
        stats = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "ready": 0,
            "errors": 0,
            "ignored": 0,
            "dry_run": dry_run,
        }
        records = self.fetch_records()
        stats["fetched"] = len(records)
        if dry_run:
            stats["preview"] = [record.normalized_payload for record in records[:20]]
            return stats

        for record in records:
            result = self.upsert_record(record)
            stats[result] += 1
            if result == "ready":
                stats["updated"] += 1
        return stats

    def fetch_records(self):
        source_id = settings.NOTION_DATA_SOURCE_ID
        database_id = settings.NOTION_DATABASE_ID
        filter_payload = self._work_key_filter()
        if source_id:
            pages = self.client.query_data_source(source_id, filter_payload=filter_payload)
        elif database_id:
            pages = self.client.query_database(database_id, filter_payload=filter_payload)
        else:
            raise ValueError("Configura NOTION_DATA_SOURCE_ID o NOTION_DATABASE_ID.")
        records = [self.normalize_page(page) for page in pages]
        expected = settings.NOTION_FUNDS_WORK_KEY_VALUE.casefold()
        return [record for record in records if record.work_key.casefold() == expected]

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
        beneficiary = _property_text(properties.get(settings.NOTION_FUNDS_BENEFICIARY_PROPERTY))
        rut = normalize_rut(_property_text(properties.get(settings.NOTION_FUNDS_RUT_PROPERTY)))
        amount = _property_decimal(properties.get(settings.NOTION_FUNDS_AMOUNT_PROPERTY))
        currency = _property_text(properties.get(settings.NOTION_FUNDS_CURRENCY_PROPERTY)) or "CLP"
        payment_date = _property_date(properties.get(settings.NOTION_FUNDS_PAYMENT_DATE_PROPERTY))
        work_key = _property_text(properties.get(settings.NOTION_FUNDS_WORK_KEY_PROPERTY))
        record_id = _property_text(properties.get(settings.NOTION_FUNDS_REMITTANCE_PROPERTY))
        cost_center = _property_text(properties.get(settings.NOTION_FUNDS_COST_CENTER_PROPERTY))
        normalized = {
            "page_id": page.get("id") or "",
            "url": page.get("url") or "",
            "work_key": work_key,
            "record_id": record_id,
            "beneficiary_name": beneficiary,
            "beneficiary_rut": rut,
            "amount": str(amount) if amount is not None else "",
            "currency": currency,
            "payment_date": payment_date.isoformat() if payment_date else "",
            "cost_center": cost_center,
        }
        return NotionFundRecord(
            page_id=page.get("id") or "",
            url=page.get("url") or "",
            work_key=work_key,
            record_id=record_id,
            beneficiary_name=beneficiary,
            beneficiary_rut=rut,
            amount=amount,
            currency=currency,
            payment_date=payment_date,
            cost_center=cost_center,
            raw_payload=page,
            normalized_payload=normalized,
        )

    @transaction.atomic
    def upsert_record(self, record):
        mapping = self._find_mapping(record)
        status, error = self._validate(record, mapping)
        defaults = {
            "notion_url": record.url,
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
            "rindegastos_fund_id": mapping.rindegastos_fund_id if mapping else "",
            "notion_raw_payload": record.raw_payload,
            "normalized_payload": record.normalized_payload,
            "last_error": error,
            "last_synced_at": timezone.now(),
        }
        log, created = NotionFundSyncLog.objects.update_or_create(
            notion_page_id=record.page_id,
            defaults=defaults,
        )
        if created:
            return "created"
        if log.local_status == NotionFundSyncLog.STATUS_READY:
            return "ready"
        if log.local_status == NotionFundSyncLog.STATUS_ERROR:
            return "errors"
        if log.local_status == NotionFundSyncLog.STATUS_IGNORED:
            return "ignored"
        return "updated"

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
        if not record.page_id:
            errors.append("Notion no entregó page_id.")
        if record.work_key != settings.NOTION_FUNDS_WORK_KEY_VALUE:
            errors.append(f"k de trabajo no coincide con {settings.NOTION_FUNDS_WORK_KEY_VALUE}.")
        if not record.beneficiary_name:
            errors.append("Beneficiario vacío.")
        if not record.beneficiary_rut:
            errors.append("RUT vacío.")
        if record.amount is None or record.amount <= 0:
            errors.append("Monto vacío o menor/igual a cero.")
        if not record.payment_date:
            errors.append("Fecha de pago vacía o inválida.")
        if not mapping:
            errors.append("No existe mapeo activo beneficiario -> Rindegastos.")
        elif not mapping.rindegastos_user and not mapping.rindegastos_fund_id:
            errors.append("El mapeo no tiene usuario ni fondo Rindegastos.")
        if errors:
            return NotionFundSyncLog.STATUS_ERROR, " ".join(errors)
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
    if prop_type == "unique_id":
        prefix = (value or {}).get("prefix") or ""
        number = (value or {}).get("number")
        return f"{prefix}-{number}" if prefix and number is not None else str(number or "")
    if prop_type in {"email", "phone_number", "url"}:
        return (value or "").strip()
    if prop_type == "checkbox":
        return "true" if value else "false"
    return ""


def _formula_text(value):
    formula_type = value.get("type")
    formula_value = value.get(formula_type)
    if formula_type in {"string", "number", "boolean"}:
        return "" if formula_value is None else str(formula_value)
    if formula_type == "date":
        return ((formula_value or {}).get("start") or "").strip()
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
