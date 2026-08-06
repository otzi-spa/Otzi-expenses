import logging
from datetime import timedelta
from decimal import InvalidOperation

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from requests import RequestException

from expenses.models import ExpenseNotification
from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_sync import RindegastosCatalogSync
from expenses.rindegastos_trace import expense_integration_code
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, rolling_uploaded_sync_since
from expenses.tax_indicators_sync import SiiTaxIndicatorSync
from expenses.whatsapp_notifications import WhatsAppNotificationError, send_whatsapp_template


logger = logging.getLogger(__name__)


@shared_task(name="expenses.sync_rindegastos_catalogs")
def sync_rindegastos_catalogs_task():
    try:
        stats = RindegastosCatalogSync().sync_all()
    except (RindegastosAPIError, ValueError):
        logger.exception("No se pudo sincronizar Rindegastos desde tarea programada.")
        raise
    logger.info("Sincronización programada Rindegastos completada: %s", stats)
    return stats


@shared_task(name="expenses.sync_rindegastos_uploaded_expenses")
def sync_rindegastos_uploaded_expenses_task():
    try:
        stats = RindegastosUploadedExpenseSync(export_id_func=expense_integration_code).sync(
            since=rolling_uploaded_sync_since(),
            max_pages=20,
        )
    except (RindegastosAPIError, ValueError):
        logger.exception("No se pudo sincronizar gastos subidos a Rindegastos desde tarea programada.")
        raise
    logger.info("Sincronización programada de gastos subidos a Rindegastos completada: %s", stats)
    return stats


@shared_task(name="expenses.sync_tax_indicators")
def sync_tax_indicators_task(year=None):
    try:
        stats = SiiTaxIndicatorSync().sync_year(year or timezone.localdate().year)
    except (InvalidOperation, RequestException, ValueError):
        logger.exception("No se pudo sincronizar indicadores SII desde tarea programada.")
        raise
    logger.info("Sincronización programada de indicadores SII completada: %s", stats)
    return stats


@shared_task(name="expenses.send_expense_notification")
def send_expense_notification_task(notification_id):
    max_attempts = getattr(settings, "WA_NOTIFICATION_MAX_ATTEMPTS", 3)
    with transaction.atomic():
        notification = (
            ExpenseNotification.objects.select_for_update()
            .select_related("expense")
            .get(pk=notification_id)
        )
        if notification.status == ExpenseNotification.STATUS_SENT:
            return {"status": "already_sent", "notification_id": notification.id}
        if notification.status == ExpenseNotification.STATUS_PROCESSING:
            return {"status": "already_processing", "notification_id": notification.id}
        if notification.next_retry_at and notification.next_retry_at > timezone.now():
            return {"status": "retry_not_due", "notification_id": notification.id}

        notification.status = ExpenseNotification.STATUS_PROCESSING
        notification.attempt_count += 1
        notification.last_error = ""
        notification.save(update_fields=["status", "attempt_count", "last_error", "updated_at"])

    try:
        result = send_whatsapp_template(notification)
    except WhatsAppNotificationError as exc:
        notification.refresh_from_db()
        response_detail = ""
        if exc.response_status:
            response_detail = f" HTTP {exc.response_status}: {exc.response_body}"
        last_error = f"{exc}{response_detail}"
        attempts_exhausted = notification.attempt_count >= max_attempts
        notification.status = (
            ExpenseNotification.STATUS_FAILED
            if exc.permanent or attempts_exhausted
            else ExpenseNotification.STATUS_PENDING
        )
        notification.last_error = last_error[:2000]
        notification.next_retry_at = (
            None
            if notification.status == ExpenseNotification.STATUS_FAILED
            else timezone.now() + timedelta(minutes=5 * notification.attempt_count)
        )
        payload = dict(notification.payload or {})
        payload["last_provider_response"] = exc.response_body or {}
        payload["last_provider_status_code"] = exc.response_status
        notification.payload = payload
        notification.save(update_fields=["status", "last_error", "next_retry_at", "payload", "updated_at"])
        return {"status": notification.status, "notification_id": notification.id, "error": notification.last_error}
    except RequestException as exc:
        notification.refresh_from_db()
        attempts_exhausted = notification.attempt_count >= max_attempts
        notification.status = ExpenseNotification.STATUS_FAILED if attempts_exhausted else ExpenseNotification.STATUS_PENDING
        notification.last_error = str(exc)[:2000]
        notification.next_retry_at = (
            None
            if attempts_exhausted
            else timezone.now() + timedelta(minutes=5 * notification.attempt_count)
        )
        notification.save(update_fields=["status", "last_error", "next_retry_at", "updated_at"])
        return {"status": notification.status, "notification_id": notification.id, "error": notification.last_error}

    notification.refresh_from_db()
    payload = dict(notification.payload or {})
    payload["last_provider_response"] = result.response_body
    payload["last_provider_status_code"] = result.status_code
    notification.status = ExpenseNotification.STATUS_SENT
    notification.provider_message_id = result.message_id
    notification.payload = payload
    notification.last_error = ""
    notification.next_retry_at = None
    notification.sent_at = timezone.now()
    notification.save(
        update_fields=[
            "status",
            "provider_message_id",
            "payload",
            "last_error",
            "next_retry_at",
            "sent_at",
            "updated_at",
        ]
    )
    return {"status": "sent", "notification_id": notification.id, "provider_message_id": result.message_id}
