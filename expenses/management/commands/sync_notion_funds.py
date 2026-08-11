from django.core.management.base import BaseCommand, CommandError

from expenses.funds_sync import NotionFundsSync
from expenses.notion_client import NotionAPIError


class Command(BaseCommand):
    help = "Sincroniza remesas de fondos por rendir desde Notion hacia logs locales."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Consulta Notion sin persistir logs locales.")

    def handle(self, *args, **options):
        try:
            stats = NotionFundsSync().sync(dry_run=options["dry_run"])
        except (NotionAPIError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Sync Notion fondos completado: {stats}"))
