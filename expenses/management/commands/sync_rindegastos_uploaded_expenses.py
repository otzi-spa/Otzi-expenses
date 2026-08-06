from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_trace import expense_integration_code
from expenses.rindegastos_uploaded_sync import RindegastosUploadedExpenseSync, rolling_uploaded_sync_since


class Command(BaseCommand):
    help = "Sincroniza gastos ya subidos a Rindegastos buscando IDs OTZ en las notas."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Fecha inicial YYYY-MM-DD. Default: últimos 120 días.")
        parser.add_argument("--until", help="Fecha final YYYY-MM-DD. Default: hoy.")
        parser.add_argument("--max-pages", type=int, default=20)

    def handle(self, *args, **options):
        since = parse_date(options.get("since") or "") or rolling_uploaded_sync_since()
        until = parse_date(options.get("until") or "")
        try:
            stats = RindegastosUploadedExpenseSync(export_id_func=expense_integration_code).sync(
                since=since,
                until=until,
                max_pages=options["max_pages"],
            )
        except (RindegastosAPIError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Sincronización de gastos subidos a Rindegastos completada."))
        for key, value in stats.items():
            self.stdout.write(f"{key}: {value}")
