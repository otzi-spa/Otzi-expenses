import json

from django.core.management.base import BaseCommand, CommandError

from expenses.rindegastos_client import RindegastosAPIError, RindegastosClient, RindegastosV2Client


class Command(BaseCommand):
    help = "Inspecciona fondos Rindegastos y campos devueltos por getFunds/getFund."

    def add_arguments(self, parser):
        parser.add_argument("--fund-id", default="", help="ID de fondo para inspeccionar con getFund.")
        parser.add_argument(
            "--api-version",
            choices=["v1", "v2"],
            default="v1",
            help="Versión API Rindegastos a usar para fondos.",
        )
        parser.add_argument(
            "--fund-request-id",
            default="",
            help="ID de solicitud de fondo para probar v2/getFundRequest.",
        )
        parser.add_argument("--limit", type=int, default=30, help="Cantidad de fondos a listar desde getFunds.")
        parser.add_argument("--raw-json", default="", help="Ruta opcional para guardar el JSON crudo.")
        parser.add_argument("--full", action="store_true", help="Imprime JSON completo del fondo indicado.")
        parser.add_argument("--sample-transactions", type=int, default=5, help="Cantidad de movimientos a mostrar.")

    def handle(self, *args, **options):
        client = RindegastosV2Client() if options["api_version"] == "v2" else RindegastosClient()
        try:
            if options["fund_request_id"]:
                if not hasattr(client, "get_fund_request"):
                    raise CommandError("--fund-request-id requiere --api-version v2")
                payload = client.get_fund_request(options["fund_request_id"])
                self._print_fund_detail(payload, options)
                if options["raw_json"]:
                    self._write_json(options["raw_json"], payload)
                return
            if options["fund_id"]:
                payload = client.get_fund(options["fund_id"])
                self._print_fund_detail(payload, options)
                if options["raw_json"]:
                    self._write_json(options["raw_json"], payload)
                return

            funds = client.get_funds()
        except RindegastosAPIError as exc:
            raise CommandError(str(exc)) from exc

        if options["raw_json"]:
            self._write_json(options["raw_json"], funds)

        self.stdout.write(self.style.SUCCESS(f"getFunds devolvió {len(funds)} fondos."))
        for index, fund in enumerate(funds[: options["limit"]], start=1):
            fund_id = _first_present(fund, "Id", "id")
            title = _first_present(fund, "Title", "Name", "FundName", "Description")
            employee = _first_present(fund, "EmployeeName", "UserName", "User", "FullName")
            balance = _first_present(fund, "Balance", "FundBalance", "AvailableAmount", "Amount")
            self.stdout.write(
                f"{index}. Id={fund_id or '-'} | {title or '-'} | empleado={employee or '-'} | saldo={balance or '-'}"
            )
        if funds:
            self.stdout.write("Campos getFunds primer fondo: " + ", ".join(sorted(funds[0].keys())))

    def _print_fund_detail(self, payload, options):
        self.stdout.write(self.style.SUCCESS("Detalle getFund recibido."))
        self.stdout.write("Campos raíz: " + ", ".join(sorted(payload.keys())))
        root = _fund_root(payload)
        if root is not payload:
            self.stdout.write("Campos fondo: " + ", ".join(sorted(root.keys())))
        self.stdout.write(
            "Identidad: "
            f"Id={_first_present(root, 'Id', 'id') or _first_present(payload, 'Id', 'id') or '-'} | "
            f"Nombre={_first_present(root, 'Title', 'Name', 'FundName', 'Description') or '-'}"
        )

        transactions = _transactions(payload)
        self.stdout.write(f"Movimientos detectados: {len(transactions)}")
        if transactions:
            self.stdout.write("Campos primer movimiento: " + ", ".join(sorted(transactions[0].keys())))
        for index, transaction in enumerate(transactions[: options["sample_transactions"]], start=1):
            amount = _first_present(transaction, "TransactionAmount", "Amount", "amount", "DepositAmount")
            date = _first_present(transaction, "TransactionDate", "CreatedAt", "Date", "date")
            detail = _first_present(transaction, "Detail", "Description", "Comment", "Note", "DepositComment")
            self.stdout.write(f"Movimiento {index}: fecha={date or '-'} | monto={amount or '-'} | detalle={detail or '-'}")
            self.stdout.write("  campos: " + ", ".join(sorted(transaction.keys())))

        text_hits = _text_key_hits(payload)
        if text_hits:
            self.stdout.write("Campos textuales tipo comentario/nota/detalle encontrados:")
            for path, value in text_hits[:50]:
                self.stdout.write(f"  {path}: {value}")
        else:
            self.stdout.write("No se encontraron campos con nombre comment/coment/note/nota/detail/description.")

        if options["full"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def _write_json(self, path, payload):
        with open(path, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(f"JSON escrito en {path}"))


def _fund_root(payload):
    for key in ("Fund", "fund", "ExpenseFund", "expenseFund"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _transactions(payload):
    candidates = []
    for key in (
        "Transactions",
        "transactions",
        "Movements",
        "movements",
        "FundTransactions",
        "fundTransactions",
        "Transaction",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            candidates.append(value)
    root = _fund_root(payload)
    if root is not payload:
        candidates.extend(_transactions(root))
    return candidates


def _first_present(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return ""


def _text_key_hits(value, path=""):
    hits = []
    if isinstance(value, dict):
        for key, child in value.items():
            next_path = f"{path}.{key}" if path else key
            normalized = key.lower()
            if any(token in normalized for token in ("comment", "coment", "note", "nota", "detail", "description")):
                if child not in (None, "", [], {}):
                    hits.append((next_path, _short_text(child)))
            hits.extend(_text_key_hits(child, next_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_text_key_hits(child, f"{path}[{index}]"))
    return hits


def _short_text(value):
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
    return text[:500]
