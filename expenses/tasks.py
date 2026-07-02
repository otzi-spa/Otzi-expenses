import logging

from celery import shared_task

from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_sync import RindegastosCatalogSync


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
