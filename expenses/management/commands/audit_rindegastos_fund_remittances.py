import csv
import re
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from expenses.funds_sync import NotionFundsSync
from expenses.notion_client import NotionAPIError
from expenses.rindegastos_client import RindegastosAPIError, RindegastosClient


REMESA_PATTERN = re.compile(r"\bREMESA-\d+\b", re.IGNORECASE)


class Command(BaseCommand):
    help = (
        "Audita remesas Notion contra movimientos de fondos Rindegastos. "
        "No actualiza Notion ni Rindegastos."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--notion-status",
            default="Transferido y sincronizado",
            help="Estado Notion a auditar.",
        )
        parser.add_argument(
            "--fund-id",
            action="append",
            default=[],
            help="Limita la auditoría a uno o más IDs de fondo Rindegastos.",
        )
        parser.add_argument(
            "--max-funds",
            type=int,
            default=0,
            help="Límite defensivo de fondos a consultar. 0 = sin límite.",
        )
        parser.add_argument(
            "--csv",
            dest="csv_path",
            default="",
            help="Ruta opcional para escribir un CSV con el resultado.",
        )
        parser.add_argument(
            "--show-transaction-keys",
            action="store_true",
            help="Incluye llaves disponibles en la transacción encontrada.",
        )

    def handle(self, *args, **options):
        target_status = options["notion_status"].strip()
        try:
            notion_records = [
                record
                for record in NotionFundsSync().fetch_records()
                if record.notion_status.casefold() == target_status.casefold()
            ]
        except (NotionAPIError, ValueError) as exc:
            raise CommandError(f"No se pudo leer Notion: {exc}") from exc

        remittances = [record for record in notion_records if record.record_id]
        remittance_ids = [record.record_id.upper() for record in remittances]
        if not remittances:
            self.stdout.write(self.style.WARNING(f"No hay remesas Notion con estado '{target_status}'."))
            return

        try:
            fund_payloads = self._fetch_funds(options)
        except RindegastosAPIError as exc:
            raise CommandError(f"No se pudo leer Rindegastos: {exc}") from exc

        matches = self._match_remittances(remittances, fund_payloads)
        rows = [self._row_for_record(record, matches.get(record.record_id.upper()), options) for record in remittances]

        if options["csv_path"]:
            self._write_csv(options["csv_path"], rows)
            self.stdout.write(self.style.SUCCESS(f"CSV escrito en {options['csv_path']}"))

        matched_count = sum(1 for row in rows if row["matched"] == "yes")
        self.stdout.write(
            self.style.SUCCESS(
                f"Auditoría completada: {len(rows)} remesas estado '{target_status}', "
                f"{matched_count} con match en movimientos Rindegastos, "
                f"{len(rows) - matched_count} sin match."
            )
        )
        self.stdout.write(f"Remesas auditadas: {', '.join(remittance_ids)}")
        for row in rows:
            status = "OK" if row["matched"] == "yes" else "SIN MATCH"
            self.stdout.write(
                f"{status} {row['remesa']} | Notion {row['amount']} {row['currency']} | "
                f"fondo {row['fund_id'] or '-'} | tx {row['transaction_amount'] or '-'} | "
                f"{row['transaction_date'] or '-'}"
            )

    def _fetch_funds(self, options):
        client = RindegastosClient()
        fund_ids = [value for value in options["fund_id"] if value]
        if not fund_ids:
            funds = client.get_funds()
            max_funds = options["max_funds"]
            if max_funds:
                funds = funds[:max_funds]
            fund_ids = [str(fund.get("Id") or fund.get("id")) for fund in funds if fund.get("Id") or fund.get("id")]
        fund_payloads = []
        for fund_id in fund_ids:
            fund_payloads.append(client.get_fund(fund_id))
        return fund_payloads

    def _match_remittances(self, remittances, fund_payloads):
        remittance_by_id = {record.record_id.upper(): record for record in remittances}
        matches = {}
        for fund in fund_payloads:
            fund_id = fund.get("Id") or fund.get("id") or ""
            fund_title = fund.get("Title") or fund.get("Name") or fund.get("FundName") or ""
            for transaction in _transactions(fund):
                haystack = _flatten_text(transaction)
                found_ids = {value.upper() for value in REMESA_PATTERN.findall(haystack)}
                for remittance_id in found_ids:
                    if remittance_id in remittance_by_id and remittance_id not in matches:
                        matches[remittance_id] = {
                            "fund_id": fund_id,
                            "fund_title": fund_title,
                            "transaction": transaction,
                            "text": haystack,
                        }
        return matches

    def _row_for_record(self, record, match, options):
        transaction = (match or {}).get("transaction") or {}
        row = {
            "remesa": record.record_id,
            "matched": "yes" if match else "no",
            "notion_status": record.notion_status,
            "beneficiary": record.beneficiary_name,
            "amount": str(record.amount or ""),
            "currency": record.currency,
            "payment_date": record.payment_date.isoformat() if record.payment_date else "",
            "cost_center": record.cost_center,
            "notion_url": record.url,
            "fund_id": (match or {}).get("fund_id") or "",
            "fund_title": (match or {}).get("fund_title") or "",
            "transaction_type": _first_present(transaction, "TransactionTypeName", "Type", "type"),
            "transaction_amount": str(_first_present(transaction, "TransactionAmount", "Amount", "amount") or ""),
            "transaction_date": _first_present(transaction, "TransactionDate", "CreatedAt", "Date", "date"),
            "transaction_text": (match or {}).get("text") or "",
        }
        row["amount_matches"] = _amount_matches(row["amount"], row["transaction_amount"])
        if options["show_transaction_keys"]:
            row["transaction_keys"] = ", ".join(sorted(transaction.keys()))
        return row

    def _write_csv(self, path, rows):
        fieldnames = [
            "remesa",
            "matched",
            "amount_matches",
            "notion_status",
            "beneficiary",
            "amount",
            "currency",
            "payment_date",
            "cost_center",
            "fund_id",
            "fund_title",
            "transaction_type",
            "transaction_amount",
            "transaction_date",
            "transaction_text",
            "notion_url",
        ]
        if any("transaction_keys" in row for row in rows):
            fieldnames.append("transaction_keys")
        with open(path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def _transactions(fund):
    transactions = fund.get("Transactions") or fund.get("transactions") or []
    if isinstance(transactions, dict):
        return [transactions]
    return transactions or []


def _flatten_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _first_present(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return ""


def _amount_matches(left, right):
    if not left or not right:
        return ""
    try:
        return "yes" if Decimal(str(left)) == Decimal(str(right)) else "no"
    except (InvalidOperation, ValueError):
        return ""
