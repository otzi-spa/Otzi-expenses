import csv
import json
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
        parser.add_argument(
            "--remesa",
            action="append",
            default=[],
            help="Busca una o más remesas específicas en Rindegastos, independiente del estado Notion.",
        )
        parser.add_argument(
            "--dump-matches",
            action="store_true",
            help="Imprime el objeto JSON donde se encontró cada remesa.",
        )
        parser.add_argument(
            "--raw-json",
            default="",
            help="Ruta opcional para guardar el payload crudo de fondos consultados.",
        )

    def handle(self, *args, **options):
        target_status = options["notion_status"].strip()
        explicit_remittances = [_remittance_record(value) for value in options["remesa"] if value]
        if explicit_remittances:
            remittances = explicit_remittances
        else:
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

        if options["raw_json"]:
            with open(options["raw_json"], "w", encoding="utf-8") as output:
                json.dump(fund_payloads, output, ensure_ascii=False, indent=2, default=str)
            self.stdout.write(self.style.SUCCESS(f"Payload crudo escrito en {options['raw_json']}"))

        matches, diagnostics = self._match_remittances(remittances, fund_payloads)
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
        self.stdout.write(
            "Diagnóstico Rindegastos: "
            f"{diagnostics['funds_scanned']} fondos escaneados, "
            f"{diagnostics['objects_scanned']} objetos JSON revisados, "
            f"{diagnostics['remesas_found_count']} remesas distintas encontradas en payload."
        )
        if diagnostics["remesas_found"]:
            self.stdout.write("Remesas encontradas en Rindegastos: " + ", ".join(diagnostics["remesas_found"][:50]))
        for row in rows:
            status = "OK" if row["matched"] == "yes" else "SIN MATCH"
            self.stdout.write(
                f"{status} {row['remesa']} | Notion {row['amount']} {row['currency']} | "
                f"fondo {row['fund_id'] or '-'} | tx {row['transaction_amount'] or '-'} | "
                f"{row['transaction_date'] or '-'}"
            )
            if options["dump_matches"] and row.get("match_json"):
                self.stdout.write(row["match_json"])

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
        diagnostics = {
            "funds_scanned": len(fund_payloads),
            "objects_scanned": 0,
            "remesas_found": [],
            "remesas_found_count": 0,
        }
        all_found_ids = set()
        for fund in fund_payloads:
            fund_info = _fund_identity(fund)
            fund_id = fund_info["id"]
            fund_title = fund_info["title"]
            for candidate in _match_candidates(fund):
                diagnostics["objects_scanned"] += 1
                haystack = _flatten_text(candidate)
                found_ids = {value.upper() for value in REMESA_PATTERN.findall(haystack)}
                all_found_ids.update(found_ids)
                for remittance_id in found_ids:
                    if remittance_id in remittance_by_id and remittance_id not in matches:
                        matches[remittance_id] = {
                            "fund_id": fund_id,
                            "fund_title": fund_title,
                            "transaction": candidate,
                            "text": haystack,
                        }
        diagnostics["remesas_found"] = sorted(all_found_ids)
        diagnostics["remesas_found_count"] = len(all_found_ids)
        return matches, diagnostics

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
        if options["dump_matches"] and transaction:
            row["match_json"] = json.dumps(transaction, ensure_ascii=False, indent=2, default=str)
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
        if any("match_json" in row for row in rows):
            fieldnames.append("match_json")
        with open(path, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def _match_candidates(fund):
    candidates = []
    for value in _walk_dicts_and_lists(fund):
        if isinstance(value, dict):
            candidates.append(value)
    return candidates or [fund]


def _fund_identity(fund):
    root = fund
    for key in ("Fund", "fund", "ExpenseFund", "expenseFund"):
        if isinstance(fund.get(key), dict):
            root = fund[key]
            break
    return {
        "id": root.get("Id") or root.get("id") or fund.get("Id") or fund.get("id") or "",
        "title": (
            root.get("Title")
            or root.get("Name")
            or root.get("FundName")
            or fund.get("Title")
            or fund.get("Name")
            or fund.get("FundName")
            or ""
        ),
    }


def _walk_dicts_and_lists(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_dicts_and_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts_and_lists(child)


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


def _remittance_record(value):
    class Record:
        pass

    record = Record()
    record.record_id = value.strip().upper()
    record.notion_status = ""
    record.beneficiary_name = ""
    record.amount = None
    record.currency = ""
    record.payment_date = None
    record.cost_center = ""
    record.url = ""
    return record
