from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from expenses.models import FundDepositInjectionAttempt, NotionFundSyncLog
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

    rindegastos_client = rindegastos_client or RindegastosClient()
    notion_client = notion_client or NotionClient()
    attempt = FundDepositInjectionAttempt.objects.create(
        notion_log=log,
        actor=actor,
        internal_note=note,
        rindegastos_fund_id=log.rindegastos_fund_id,
        rindegastos_admin_id=settings.RINDEGASTOS_FUNDS_ADMIN_ID,
        amount=log.amount,
        currency=log.currency or "CLP",
        requested_payment_date=log.payment_date,
        request_payload=request_payload,
    )

    log.rindegastos_request_payload = request_payload
    log.last_error = ""
    log.save(update_fields=["rindegastos_request_payload", "last_error", "updated_at"])

    try:
        before_fund = rindegastos_client.get_fund(log.rindegastos_fund_id)
        attempt.before_fund_payload = _fund_audit_snapshot(before_fund)
        attempt.save(update_fields=["before_fund_payload", "updated_at"])
        rindegastos_response = rindegastos_client.deposit_money_to_fund(
            fund_id=log.rindegastos_fund_id,
            admin_id=settings.RINDEGASTOS_FUNDS_ADMIN_ID,
            amount=amount,
        )
        after_fund = rindegastos_client.get_fund(log.rindegastos_fund_id)
    except RindegastosAPIError as exc:
        attempt.status = FundDepositInjectionAttempt.STATUS_FAILED
        attempt.error = f"No se pudo crear abono en Rindegastos: {exc}"
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["status", "error", "completed_at", "updated_at"])
        log.local_status = NotionFundSyncLog.STATUS_ERROR
        log.last_error = attempt.error
        log.save(update_fields=["local_status", "last_error", "updated_at"])
        raise FundDepositSyncError(log.last_error) from exc

    detected_transaction, anomaly = _detect_new_transaction(
        before_fund,
        after_fund,
        fund_id=log.rindegastos_fund_id,
        amount=amount,
    )
    detected_reference = _transaction_reference(detected_transaction, fund_id=log.rindegastos_fund_id, amount=amount)
    attempt.response_payload = rindegastos_response
    attempt.after_fund_payload = _fund_audit_snapshot(after_fund)
    attempt.detected_transaction = detected_transaction
    attempt.detected_transaction_reference = detected_reference
    attempt.anomaly = anomaly
    attempt.status = (
        FundDepositInjectionAttempt.STATUS_AMBIGUOUS
        if anomaly
        else FundDepositInjectionAttempt.STATUS_RINDEGASTOS_OK
    )
    attempt.save(
        update_fields=[
            "response_payload",
            "after_fund_payload",
            "detected_transaction",
            "detected_transaction_reference",
            "anomaly",
            "status",
            "updated_at",
        ]
    )

    log.rindegastos_response_payload = rindegastos_response
    log.rindegastos_deposit_id = detected_reference
    log.local_status = NotionFundSyncLog.STATUS_RINDEGASTOS_OK
    log.last_error = anomaly
    log.save(
        update_fields=[
            "rindegastos_response_payload",
            "rindegastos_deposit_id",
            "local_status",
            "last_error",
            "updated_at",
        ]
    )
    if anomaly:
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["completed_at", "updated_at"])
        log.local_status = NotionFundSyncLog.STATUS_RINDEGASTOS_OK_REVIEW
        log.last_error = anomaly
        log.save(update_fields=["local_status", "last_error", "updated_at"])
        raise FundDepositSyncError(
            "Abono creado en Rindegastos, pero la identificación del movimiento quedó para revisión: "
            f"{anomaly}"
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
        attempt.status = FundDepositInjectionAttempt.STATUS_NOTION_FAILED
        attempt.error = f"Abono creado en Rindegastos, pero falló actualización Notion: {exc}"
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["status", "error", "completed_at", "updated_at"])
        log.local_status = NotionFundSyncLog.STATUS_RINDEGASTOS_OK_NOTION_ERROR
        log.last_error = attempt.error
        log.save(update_fields=["local_status", "last_error", "updated_at"])
        raise FundDepositSyncError(log.last_error) from exc

    attempt.status = FundDepositInjectionAttempt.STATUS_COMPLETED if not anomaly else FundDepositInjectionAttempt.STATUS_AMBIGUOUS
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["status", "completed_at", "updated_at"])
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


def _detect_new_transaction(before_fund, after_fund, fund_id="", amount=""):
    before = _transactions(before_fund)
    after = _transactions(after_fund)
    target_amount = _decimal_or_none(amount)
    if len(after) <= len(before):
        return {}, "Rindegastos no mostró movimientos nuevos al consultar el fondo después del abono."

    before_counts = {}
    for transaction in before:
        fingerprint = _transaction_fingerprint(transaction)
        before_counts[fingerprint] = before_counts.get(fingerprint, 0) + 1

    new_transactions = []
    for transaction in after:
        fingerprint = _transaction_fingerprint(transaction)
        if before_counts.get(fingerprint, 0):
            before_counts[fingerprint] -= 1
        else:
            new_transactions.append(transaction)

    deposit_candidates = [
        transaction
        for transaction in new_transactions
        if _is_deposit_transaction(transaction) and _transaction_amount(transaction) == target_amount
    ]
    if len(deposit_candidates) == 1:
        return deposit_candidates[0], ""
    if len(deposit_candidates) > 1:
        return deposit_candidates[-1], (
            f"Se detectaron {len(deposit_candidates)} movimientos nuevos por el mismo monto en el fondo {fund_id}; "
            "la referencia puede ser ambigua."
        )
    if len(new_transactions) == 1:
        return new_transactions[0], (
            "Se detectó un movimiento nuevo, pero no calza claramente como abono por el monto solicitado."
        )
    return {}, (
        f"Se detectaron {len(new_transactions)} movimientos nuevos, pero ninguno calza claramente como abono por el monto solicitado."
    )


def _fund_audit_snapshot(payload):
    root = _fund_root(payload)
    transactions = _transactions(payload)
    limit = max(0, int(getattr(settings, "FUNDS_DEPOSIT_AUDIT_TRANSACTION_SNAPSHOT_LIMIT", 50)))
    return {
        "fund_id": root.get("Id") or root.get("id") or "",
        "title": root.get("Title") or root.get("name") or root.get("Name") or "",
        "currency": root.get("Currency") or root.get("currencyCode") or root.get("CurrencyCode") or "",
        "deposits": root.get("Deposits") or root.get("deposits") or root.get("ManualDeposit") or "",
        "withdrawals": root.get("Withdrawals") or root.get("withdrawals") or root.get("Charges") or "",
        "balance": root.get("Balance") or root.get("balance") or root.get("finalBalance") or "",
        "transactions_count": len(transactions),
        "transactions_tail_limit": limit,
        "transactions_tail": transactions[-limit:] if limit else [],
    }


def _fund_root(payload):
    if not isinstance(payload, dict):
        return {}
    for key in ("Fund", "fund"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    for key in ("Funds", "funds"):
        nested = payload.get(key)
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            return nested[0]
        if isinstance(nested, dict):
            return nested
    return payload


def _transactions(payload):
    if not isinstance(payload, dict):
        return []
    for key in ("Transactions", "transactions"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("Fund", "fund"):
        nested = payload.get(key)
        nested_transactions = _transactions(nested)
        if nested_transactions:
            return nested_transactions
    for key in ("Funds", "funds"):
        nested = payload.get(key)
        if isinstance(nested, list) and nested:
            nested_transactions = _transactions(nested[0])
            if nested_transactions:
                return nested_transactions
        if isinstance(nested, dict):
            nested_transactions = _transactions(nested)
            if nested_transactions:
                return nested_transactions
    return []


def _transaction_fingerprint(transaction):
    return "|".join(
        [
            str(transaction.get("Id") or transaction.get("id") or ""),
            str(transaction.get("TransactionDate") or transaction.get("date") or transaction.get("Date") or ""),
            str(transaction.get("TransactionAmount") or transaction.get("amount") or transaction.get("Amount") or ""),
            str(transaction.get("TransactionType") or transaction.get("type") or transaction.get("Type") or ""),
            str(transaction.get("TransactionTypeName") or transaction.get("typeName") or ""),
            str(transaction.get("ReportId") or transaction.get("reportId") or ""),
        ]
    )


def _is_deposit_transaction(transaction):
    is_deposit = transaction.get("isDeposit") or transaction.get("IsDeposit")
    if str(is_deposit).strip() == "1":
        return True
    transaction_type = str(transaction.get("TransactionType") or transaction.get("type") or "").strip()
    if transaction_type == "1":
        return True
    type_name = str(transaction.get("TransactionTypeName") or "").casefold()
    return "abono" in type_name or "deposit" in type_name


def _transaction_amount(transaction):
    return _decimal_or_none(
        transaction.get("TransactionAmount")
        or transaction.get("amount")
        or transaction.get("Amount")
    )


def _transaction_reference(transaction, fund_id="", amount=""):
    if not transaction:
        return ""
    for key in ("DepositId", "depositId", "TransactionId", "transactionId", "Id", "id"):
        value = transaction.get(key)
        if value not in (None, ""):
            return str(value)
    tx_date = str(transaction.get("TransactionDate") or transaction.get("date") or transaction.get("Date") or "")[:10]
    tx_amount = transaction.get("TransactionAmount") or transaction.get("amount") or transaction.get("Amount") or amount
    tx_type = transaction.get("TransactionType") or transaction.get("type") or "deposit"
    return f"fund:{fund_id}:type:{tx_type}:date:{tx_date}:amount:{tx_amount}"


def _decimal_or_none(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_status(value):
    return " ".join(str(value or "").casefold().split())
