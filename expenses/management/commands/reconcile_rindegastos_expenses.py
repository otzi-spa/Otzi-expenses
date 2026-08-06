import argparse

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from expenses.rindegastos_client import RindegastosAPIError
from expenses.rindegastos_reconcile import RindegastosExpenseReconciler
from expenses.rindegastos_uploaded_sync import rolling_uploaded_sync_since


class Command(BaseCommand):
    help = "Reconciliación no destructiva de gastos Rindegastos contra Expenses."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Fecha inicial YYYY-MM-DD. Default: últimos 120 días.")
        parser.add_argument("--until", help="Fecha final YYYY-MM-DD. Default: hoy.")
        parser.add_argument("--max-pages", type=int, default=20)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--fetch-detail", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument("--mark-integration-code", action="store_true")
        parser.add_argument("--integration-status", type=int, default=1)

    def handle(self, *args, **options):
        since = parse_date(options.get("since") or "") or rolling_uploaded_sync_since()
        until = parse_date(options.get("until") or "") or timezone.localdate()
        try:
            stats = RindegastosExpenseReconciler().reconcile(
                since=since,
                until=until,
                max_pages=options["max_pages"],
                dry_run=options["dry_run"],
                fetch_detail=options["fetch_detail"],
                mark_integration_code=options["mark_integration_code"],
                integration_status=options["integration_status"],
            )
        except (RindegastosAPIError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Rindegastos reconcile"))
        self.stdout.write(f"Fetched: {stats['fetched']}")
        self.stdout.write(f"Matched: {stats['matched']}")
        self.stdout.write(f"Changed snapshots: {stats['changed_snapshots']}")
        self.stdout.write(f"Unchanged snapshots: {stats['unchanged_snapshots']}")
        self.stdout.write(f"Diffs opened: {stats['diffs_opened']}")
        self.stdout.write(f"Unmatched: {stats['unmatched']}")
        self.stdout.write(f"Errors: {stats['errors']}")
        self.stdout.write(f"Matched by: {stats['matched_by']}")
        self.stdout.write(f"Integration code: {stats['integration_code']}")
