from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from expenses.models import NotionFundSyncLog
from expenses.notion_client import NotionAPIError, NotionClient
from expenses.rindegastos_client import RindegastosAPIError, RindegastosClient


class FundDepositSyncError(Exception):
    pass


def user_can_inject_fund_deposits(user):
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_superuser", False):
        return False
    allowed_emails = {email.casefold() for email in settings.FUNDS_DEPOSIT_ALLOWED_USER_EMAILS}
    return (getattr(user, "email", "") or "").casefold() in allowed_emails


def inject_notion_remittance_to_rindegastos(log_id, actor, rindegastos_client=None, notion_client=None):
    if not user_can_inject_fund_deposits(actor):
        raise FundDepositSyncError("Usuario no autorizado para inyectar abonos.")
    if not settings.RINDEGASTOS_FUNDS_ADMIN_ID:
        raise FundDepositSyncError("RINDEGASTOS_FUNDS_ADMIN_ID no está configurado.")

    log = NotionFundSyncLog.objects.get(pk=log_id)
    _validate_log_for_injection(log)

    note = _deposit_note(log)
    amount = _decimal_to_api_value(log.amount)
    request_payload = {
        "Id": str(log.rindegastos_fund_id),
        "IdAdmin": str(settings.RINDEGASTOS_FUNDS_ADMIN_ID),
        "DepositAmount": amount,
    }
    if note:
        request_payload["Note"] = note

    rindegastos_client = rindegastos_client or RindegastosClient()
    notion_client = notion_client or NotionClient()

    log.rindegastos_request_payload = request_payload
    log.last_error = ""
    log.save(update_fields=["rindegastos_request_payload", "last_error", "updated_at"])

    try:
        rindegastos_response = rindegastos_client.deposit_money_to_fund(
            fund_id=log.rindegastos_fund_id,
            admin_id=settings.RINDEGASTOS_FUNDS_ADMIN_ID,
            amount=amount,
            note=note,
        )
    except RindegastosAPIError as exc:
        log.local_status = NotionFundSyncLog.STATUS_ERROR
        log.last_error = f"No se pudo crear abono en Rindegastos: {exc}"
        log.save(update_fields=["local_status", "last_error", "updated_at"])
        raise FundDepositSyncError(log.last_error) from exc

    log.rindegastos_response_payload = rindegastos_response
    log.rindegastos_deposit_id = _response_identifier(rindegastos_response)
    log.local_status = NotionFundSyncLog.STATUS_RINDEGASTOS_OK
    log.last_error = ""
    log.save(
        update_fields=[
            "rindegastos_response_payload",
            "rindegastos_deposit_id",
            "local_status",
            "last_error",
            "updated_at",
        ]
    )

    notion_properties = _notion_synced_status_properties(log)
    notion_update_payload = {"properties": notion_properties, "is_locked": True}
    log.notion_update_payload = notion_update_payload
    log.save(update_fields=["notion_update_payload", "updated_at"])

    try:
        notion_response = notion_client.update_page(
            log.notion_page_id,
            properties=notion_properties,
            is_locked=True,
        )
    except NotionAPIError as exc:
        log.local_status = NotionFundSyncLog.STATUS_RINDEGASTOS_OK_NOTION_ERROR
        log.last_error = f"Abono creado en Rindegastos, pero falló actualización Notion: {exc}"
        log.save(update_fields=["local_status", "last_error", "updated_at"])
        raise FundDepositSyncError(log.last_error) from exc

    synced_status = settings.NOTION_FUNDS_SYNCED_STATUS_VALUE
    log.notion_update_response = notion_response
    log.notion_status = synced_status
    log.local_status = NotionFundSyncLog.STATUS_CLOSED
    log.last_error = ""
    log.last_synced_at = timezone.now()
    log.save(
        update_fields=[
            "notion_update_response",
            "notion_status",
            "local_status",
            "last_error",
            "last_synced_at",
            "updated_at",
        ]
    )
    return log


def _validate_log_for_injection(log):
    if log.local_status != NotionFundSyncLog.STATUS_READY:
        raise FundDepositSyncError("La remesa no está lista para abono.")
    if _normalize_status(log.notion_status) != _normalize_status("Transferido"):
        raise FundDepositSyncError("La remesa debe estar en estado Transferido en Notion.")
    if not log.notion_page_id:
        raise FundDepositSyncError("La remesa no tiene página Notion asociada.")
    if not log.notion_record_id:
        raise FundDepositSyncError("La remesa no tiene ID.")
    if not log.rindegastos_fund_id:
        raise FundDepositSyncError("La remesa no tiene fondo Rindegastos.")
    if log.amount is None or log.amount <= 0:
        raise FundDepositSyncError("La remesa no tiene monto válido.")
    if (log.currency or "CLP").upper() != "CLP":
        raise FundDepositSyncError("Por ahora solo se permiten abonos CLP.")


def _notion_synced_status_properties(log):
    prop_name, prop_type = _notion_status_property(log)
    synced_status = settings.NOTION_FUNDS_SYNCED_STATUS_VALUE
    if prop_type == "select":
        value = {"select": {"name": synced_status}}
    else:
        value = {"status": {"name": synced_status}}
    return {prop_name: value}


def _notion_status_property(log):
    configured = settings.NOTION_FUNDS_STATUS_PROPERTY
    properties = (log.notion_raw_payload or {}).get("properties") or {}
    for name, prop in properties.items():
        if name.casefold().strip() == configured.casefold().strip():
            return name, (prop or {}).get("type") or "status"
    return configured, "status"


def _deposit_note(log):
    return (log.notion_record_id or "").strip()


def _decimal_to_api_value(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FundDepositSyncError("Monto inválido para Rindegastos.") from exc
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, "f")


def _response_identifier(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("Id", "id", "DepositId", "depositId", "TransactionId", "transactionId"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    for key in ("Deposit", "deposit", "Transaction", "transaction", "Fund", "fund"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_id = _response_identifier(nested)
            if nested_id:
                return nested_id
    return ""


def _normalize_status(value):
    return " ".join(str(value or "").casefold().split())
