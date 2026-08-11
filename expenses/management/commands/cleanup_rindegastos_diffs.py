from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from expenses.models import RindegastosExpenseDiff
from expenses.rindegastos_diff_rules import canonical_diff_value, should_ignore_diff


class Command(BaseCommand):
    help = "Limpia diferencias Rindegastos abiertas duplicadas o sin impacto real."

    def add_arguments(self, parser):
        parser.add_argument("--status", default=RindegastosExpenseDiff.STATUS_OPEN)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        status = options["status"]
        dry_run = options["dry_run"]
        queryset = (
            RindegastosExpenseDiff.objects.select_related("snapshot", "expense")
            .filter(status=status)
            .order_by("expense_id", "field_name", "id")
        )
        diffs = list(queryset)
        noise_ids = set()
        groups = defaultdict(list)

        for diff in diffs:
            if should_ignore_diff(diff.field_name, diff.local_value, diff.remote_value):
                noise_ids.add(diff.id)
                continue
            key = (
                diff.expense_id,
                diff.field_name,
                canonical_diff_value(diff.field_name, diff.local_value),
                canonical_diff_value(diff.field_name, diff.remote_value),
            )
            groups[key].append(diff)

        duplicate_ids = set()
        for grouped_diffs in groups.values():
            if len(grouped_diffs) <= 1:
                continue
            keeper = max(grouped_diffs, key=_diff_keeper_score)
            for diff in grouped_diffs:
                if diff.id != keeper.id:
                    duplicate_ids.add(diff.id)

        ids_to_resolve = noise_ids | duplicate_ids
        if not dry_run and ids_to_resolve:
            RindegastosExpenseDiff.objects.filter(id__in=ids_to_resolve).update(
                status=RindegastosExpenseDiff.STATUS_RESOLVED,
                resolved_at=timezone.now(),
            )

        current_open = RindegastosExpenseDiff.objects.filter(status=status).count()
        remaining = current_open - len(ids_to_resolve) if dry_run and status == RindegastosExpenseDiff.STATUS_OPEN else current_open
        self.stdout.write("Rindegastos diff cleanup")
        self.stdout.write(f"Dry run: {dry_run}")
        self.stdout.write(f"Scanned: {len(diffs)}")
        self.stdout.write(f"Resolved zero-tax noise: {len(noise_ids)}")
        self.stdout.write(f"Resolved duplicates: {len(duplicate_ids)}")
        self.stdout.write(f"Total to resolve: {len(ids_to_resolve)}")
        self.stdout.write(f"Remaining open: {remaining}")


def _diff_keeper_score(diff):
    normalized = diff.snapshot.normalized_payload or {}
    has_report_number = 1 if normalized.get("rindegastos_report_number") else 0
    fetched_at = diff.snapshot.fetched_at or diff.created_at
    return (has_report_number, fetched_at, diff.id)
