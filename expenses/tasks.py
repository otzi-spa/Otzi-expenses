import logging

from celery import shared_task

from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_sync import RindegastosCatalogSync
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, rolling_uploaded_sync_since
from expenses.views import _expense_export_id


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
        stats = RindegastosUploadedExpenseSync(export_id_func=_expense_export_id).sync(
            since=rolling_uploaded_sync_since(),
            max_pages=20,
        )
    except (RindegastosAPIError, ValueError):
        logger.exception("No se pudo sincronizar gastos subidos a Rindegastos desde tarea programada.")
        raise
    logger.info("Sincronización programada de gastos subidos a Rindegastos completada: %s", stats)
    return stats
