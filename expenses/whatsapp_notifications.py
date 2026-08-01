import logging
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from expenses.models import Expense, ExpenseNotification


logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v24.0"


class WhatsAppNotificationError(Exception):
    def __init__(self, message, *, permanent=False, response_status=None, response_body=None):
        super().__init__(message)
        self.permanent = permanent
        self.response_status = response_status
        self.response_body = response_body


@dataclass
class WhatsAppTemplateResponse:
    message_id: str
    status_code: int
    response_body: dict


def format_expense_amount(expense: Expense) -> str:
    currency = expense.currency or "CLP"
    if expense.amount is None:
        return f"Monto no informado {currency}"
    amount = expense.amount
    if isinstance(amount, Decimal) and amount == amount.to_integral():
        value = f"{int(amount):,}".replace(",", ".")
    else:
        value = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${value} {currency}"


def format_rejection_expense_summary(expense: Expense) -> str:
    amount = format_expense_amount(expense)
    if expense.supplier:
        return f"{amount} ({expense.supplier})"
    return amount


def expense_trace_id(expense: Expense) -> str:
    from expenses.views import _expense_export_id

    return _expense_export_id(expense.id)


def build_rejection_payload(expense: Expense) -> dict:
    reason = (expense.rejection_reason or "").strip() or "Sin motivo informado"
    trace_id = expense_trace_id(expense)
    return {
        "trace_id": trace_id,
        "amount": format_expense_amount(expense),
        "supplier": expense.supplier or "",
        "summary": format_rejection_expense_summary(expense),
        "reason": reason,
        "header_parameters": [
            trace_id,
        ],
        "template_parameters": [
            trace_id,
            format_rejection_expense_summary(expense),
            reason,
        ],
    }


def create_rejection_notification(expense: Expense) -> ExpenseNotification:
    payload = build_rejection_payload(expense)
    notification, _ = ExpenseNotification.objects.get_or_create(
        expense=expense,
        notification_type=ExpenseNotification.TYPE_REJECTION,
        channel=ExpenseNotification.CHANNEL_WHATSAPP,
        decision_at=expense.decision_at,
        defaults={
            "recipient": expense.wa_sender_phone or "",
            "status": ExpenseNotification.STATUS_PENDING,
            "template_name": settings.WA_REJECTION_TEMPLATE_NAME,
            "template_language": settings.WA_REJECTION_TEMPLATE_LANGUAGE,
            "payload": payload,
        },
    )
    return notification


def build_whatsapp_template_request(notification: ExpenseNotification) -> dict:
    header_parameters = notification.payload.get("header_parameters") or []
    body_parameters = notification.payload.get("template_parameters") or []
    components = []
    if header_parameters:
        components.append(
            {
                "type": "header",
                "parameters": [
                    {"type": "text", "text": str(value)}
                    for value in header_parameters
                ],
            }
        )
    components.append(
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": str(value)}
                for value in body_parameters
            ],
        }
    )
    return {
        "messaging_product": "whatsapp",
        "to": notification.recipient,
        "type": "template",
        "template": {
            "name": notification.template_name,
            "language": {"code": notification.template_language},
            "components": components,
        },
    }


def send_whatsapp_template(notification: ExpenseNotification) -> WhatsAppTemplateResponse:
    if not notification.recipient:
        raise WhatsAppNotificationError("Falta teléfono destino.", permanent=True)
    phone_number_id = notification.expense.wa_phone_number_id or settings.WA_PHONE_NUMBER_ID
    if not phone_number_id:
        raise WhatsAppNotificationError("Falta identificador del número empresarial de WhatsApp.", permanent=True)
    if not settings.WA_ACCESS_TOKEN:
        raise WhatsAppNotificationError("Falta WA_ACCESS_TOKEN.", permanent=True)
    if not notification.template_name:
        raise WhatsAppNotificationError("Falta nombre de plantilla WhatsApp.", permanent=True)
    if not notification.template_language:
        raise WhatsAppNotificationError("Falta idioma de plantilla WhatsApp.", permanent=True)

    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json=build_whatsapp_template_request(notification),
        timeout=15,
    )
    response_body = _safe_response_json(response)
    if response.status_code >= 400:
        permanent = 400 <= response.status_code < 500 and response.status_code != 429
        raise WhatsAppNotificationError(
            f"WhatsApp respondió HTTP {response.status_code}.",
            permanent=permanent,
            response_status=response.status_code,
            response_body=response_body,
        )
    messages = response_body.get("messages") or []
    message_id = messages[0].get("id", "") if messages else ""
    return WhatsAppTemplateResponse(
        message_id=message_id,
        status_code=response.status_code,
        response_body=response_body,
    )


def _safe_response_json(response):
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:500]}


def enqueue_notification_send(notification: ExpenseNotification) -> bool:
    try:
        from expenses.tasks import send_expense_notification_task

        send_expense_notification_task.delay(notification.id)
        return True
    except Exception:
        logger.exception("No se pudo encolar notificación WhatsApp de rechazo %s.", notification.id)
        notification.last_error = "No se pudo encolar el envío asíncrono."
        notification.next_retry_at = timezone.now()
        notification.save(update_fields=["last_error", "next_retry_at", "updated_at"])
        return False
