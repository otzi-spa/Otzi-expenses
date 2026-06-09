from django.core.management.base import BaseCommand, CommandError

from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_sync import RindegastosCatalogSync


class Command(BaseCommand):
    help = "Sincroniza políticas, categorías, impuestos, campos extra y usuarios desde Rindegastos."

    def handle(self, *args, **options):
        try:
            stats = RindegastosCatalogSync().sync_all()
        except RindegastosAPIError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Sincronización Rindegastos completada."))
        for key, value in stats.items():
            self.stdout.write(f"{key}: {value}")
