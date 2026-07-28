from decimal import InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from requests import RequestException

from expenses.tax_indicators_sync import SiiTaxIndicatorSync


class Command(BaseCommand):
    help = "Sincroniza UTM y tasas Mepco desde el SII."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            default=timezone.localdate().year,
            help="Año SII a sincronizar. Por defecto usa el año actual.",
        )

    def handle(self, *args, **options):
        try:
            stats = SiiTaxIndicatorSync().sync_year(options["year"])
        except (InvalidOperation, RequestException, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Sincronización SII completada."))
        self.stdout.write(f"Año: {stats['year']}")
        self.stdout.write(f"UTM: {stats['utm_values']}")
        self.stdout.write(f"Tasas combustibles: {stats['fuel_rates']}")
