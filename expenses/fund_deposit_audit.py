from decimal import Decimal


AUDIT_TSV_COLUMNS = [
    "remesa",
    "estado_intento",
    "inicio",
    "fin",
    "fondo_rindegastos",
    "admin_rindegastos",
    "monto",
    "moneda",
    "actor_expenses",
    "fecha_notion",
    "estado_notion",
    "sync_local",
    "movimientos_antes",
    "movimientos_despues",
    "delta_movimientos",
    "depositos_antes",
    "depositos_despues",
    "delta_depositos",
    "saldo_antes",
    "saldo_despues",
    "delta_saldo",
    "movimiento_detectado",
    "anomalia",
    "error",
]


def attempt_audit_summary(attempt):
    if isinstance(attempt.audit_summary, dict) and attempt.audit_summary:
        return attempt.audit_summary
    before = attempt.before_fund_payload if isinstance(attempt.before_fund_payload, dict) else {}
    after = attempt.after_fund_payload if isinstance(attempt.after_fund_payload, dict) else {}
    before_deposits = decimal_or_none(before.get("deposits"))
    after_deposits = decimal_or_none(after.get("deposits"))
    before_balance = decimal_or_none(before.get("balance"))
    after_balance = decimal_or_none(after.get("balance"))
    return {
        "expected_amount": decimal_to_string(attempt.amount),
        "before_transactions_count": before.get("transactions_count", ""),
        "after_transactions_count": after.get("transactions_count", ""),
        "transactions_delta": int_delta(after.get("transactions_count"), before.get("transactions_count")),
        "before_deposits": decimal_to_string(before_deposits),
        "after_deposits": decimal_to_string(after_deposits),
        "deposits_delta": decimal_to_string(decimal_delta(after_deposits, before_deposits)),
        "before_balance": decimal_to_string(before_balance),
        "after_balance": decimal_to_string(after_balance),
        "balance_delta": decimal_to_string(decimal_delta(after_balance, before_balance)),
        "detected_transaction_reference": attempt.detected_transaction_reference,
    }


def attempt_audit_row(attempt, fmt_dt):
    log = attempt.notion_log
    audit = attempt_audit_summary(attempt)
    actor = attempt.actor.email if attempt.actor_id and attempt.actor else ""
    return {
        "remesa": attempt.internal_note or log.notion_record_id or "",
        "estado_intento": attempt.status,
        "inicio": fmt_dt(attempt.started_at),
        "fin": fmt_dt(attempt.completed_at),
        "fondo_rindegastos": attempt.rindegastos_fund_id,
        "admin_rindegastos": attempt.rindegastos_admin_id,
        "monto": format_amount(attempt.amount),
        "moneda": attempt.currency,
        "actor_expenses": actor,
        "fecha_notion": attempt.requested_payment_date or "",
        "estado_notion": log.notion_status,
        "sync_local": log.local_status,
        "movimientos_antes": audit.get("before_transactions_count", ""),
        "movimientos_despues": audit.get("after_transactions_count", ""),
        "delta_movimientos": audit.get("transactions_delta", ""),
        "depositos_antes": format_amount(audit.get("before_deposits")),
        "depositos_despues": format_amount(audit.get("after_deposits")),
        "delta_depositos": format_amount(audit.get("deposits_delta")),
        "saldo_antes": format_amount(audit.get("before_balance")),
        "saldo_despues": format_amount(audit.get("after_balance")),
        "delta_saldo": format_amount(audit.get("balance_delta")),
        "movimiento_detectado": attempt.detected_transaction_reference or audit.get("detected_transaction_reference", ""),
        "anomalia": attempt.anomaly,
        "error": attempt.error,
    }


def movement_delta_text(audit):
    before = audit.get("before_transactions_count", "")
    after = audit.get("after_transactions_count", "")
    delta = audit.get("transactions_delta", "")
    if before == "" and after == "" and delta == "":
        return "-"
    return f"{before}->{after} ({delta})"


def format_amount(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(value)
    except Exception:
        return str(value)
    return f"{amount:,.0f}".replace(",", ".")


def decimal_or_none(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def decimal_delta(after_value, before_value):
    if after_value is None or before_value is None:
        return None
    return after_value - before_value


def decimal_to_string(value):
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(value)
    except Exception:
        return str(value)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, "f")


def int_delta(after_value, before_value):
    try:
        return int(after_value) - int(before_value)
    except Exception:
        return ""
