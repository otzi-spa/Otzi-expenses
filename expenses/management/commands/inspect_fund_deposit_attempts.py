import json

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from expenses.models import FundDepositInjectionAttempt


class Command(BaseCommand):
    help = "Inspecciona intentos de inyección de abonos de remesas hacia fondos Rindegastos."

    def add_arguments(self, parser):
        parser.add_argument("--remesa", default="", help="Filtra por ID de remesa, por ejemplo REMESA-6408.")
        parser.add_argument("--fund-id", default="", help="Filtra por ID de fondo Rindegastos.")
        parser.add_argument("--status", default="", help="Filtra por estado interno del intento.")
        parser.add_argument("--limit", type=int, default=10, help="Cantidad máxima de intentos a mostrar.")
        parser.add_argument("--show-json", action="store_true", help="Imprime payloads JSON completos guardados.")

    def handle(self, *args, **options):
        attempts = FundDepositInjectionAttempt.objects.select_related("notion_log", "actor").order_by("-started_at")
        remesa = options["remesa"].strip()
        if remesa:
            attempts = attempts.filter(
                Q(internal_note__iexact=remesa) | Q(notion_log__notion_record_id__iexact=remesa)
            )
        fund_id = options["fund_id"].strip()
        if fund_id:
            attempts = attempts.filter(rindegastos_fund_id=fund_id)
        status = options["status"].strip()
        if status:
            attempts = attempts.filter(status=status)

        attempts = list(attempts[: max(1, options["limit"])])
        if not attempts:
            self.stdout.write(self.style.WARNING("No hay intentos que calcen con el filtro."))
            return

        self.stdout.write(self.style.SUCCESS(f"{len(attempts)} intento(s) encontrado(s)."))
        for attempt in attempts:
            self._print_attempt(attempt, show_json=options["show_json"])

    def _print_attempt(self, attempt, show_json=False):
        log = attempt.notion_log
        actor = attempt.actor.email if attempt.actor_id and attempt.actor else "-"
        before_count = _snapshot_count(attempt.before_fund_payload)
        after_count = _snapshot_count(attempt.after_fund_payload)
        before_balance = _snapshot_value(attempt.before_fund_payload, "balance")
        after_balance = _snapshot_value(attempt.after_fund_payload, "balance")
        before_deposits = _snapshot_value(attempt.before_fund_payload, "deposits")
        after_deposits = _snapshot_value(attempt.after_fund_payload, "deposits")
        response_summary = _response_summary(attempt.response_payload)

        self.stdout.write("")
        self.stdout.write(f"Intento #{attempt.id} | {attempt.internal_note or log.notion_record_id or '-'}")
        self.stdout.write(
            "  Estado: "
            f"{attempt.status} | inicio={_fmt_dt(attempt.started_at)} | fin={_fmt_dt(attempt.completed_at)}"
        )
        self.stdout.write(
            "  Remesa: "
            f"notion={log.notion_record_id or '-'} | estado_notion={log.notion_status or '-'} | "
            f"sync_local={log.local_status}"
        )
        self.stdout.write(
            "  Abono solicitado: "
            f"fondo={attempt.rindegastos_fund_id or '-'} | admin={attempt.rindegastos_admin_id or '-'} | "
            f"monto={attempt.amount or '-'} {attempt.currency or ''} | fecha_notion={attempt.requested_payment_date or '-'} | "
            f"actor={actor}"
        )
        self.stdout.write(f"  Nota interna: {attempt.internal_note or '-'}")
        self.stdout.write(
            "  Rindegastos snapshots: "
            f"movimientos {before_count} -> {after_count} | "
            f"depositos {before_deposits} -> {after_deposits} | saldo {before_balance} -> {after_balance}"
        )
        self.stdout.write(f"  Respuesta Rindegastos: {response_summary}")
        self.stdout.write(f"  Movimiento detectado: {attempt.detected_transaction_reference or '-'}")
        if attempt.anomaly:
            self.stdout.write(self.style.WARNING(f"  Anomalía: {attempt.anomaly}"))
        if attempt.error:
            self.stdout.write(self.style.ERROR(f"  Error: {attempt.error}"))
        if log.last_error:
            self.stdout.write(self.style.WARNING(f"  Último error log: {log.last_error}"))

        if show_json:
            payloads = {
                "request_payload": attempt.request_payload,
                "response_payload": attempt.response_payload,
                "before_fund_payload": attempt.before_fund_payload,
                "after_fund_payload": attempt.after_fund_payload,
                "detected_transaction": attempt.detected_transaction,
                "notion_log_response_payload": log.rindegastos_response_payload,
            }
            self.stdout.write(json.dumps(payloads, ensure_ascii=False, indent=2, default=str))


def _snapshot_count(snapshot):
    if not isinstance(snapshot, dict):
        return "-"
    return snapshot.get("transactions_count", "-")


def _snapshot_value(snapshot, key):
    if not isinstance(snapshot, dict):
        return "-"
    value = snapshot.get(key)
    return value if value not in (None, "") else "-"


def _response_summary(payload):
    if not isinstance(payload, dict) or not payload:
        return "-"
    preferred = []
    for key in ("Status", "status", "statusCode", "Code", "code", "Message", "message", "Error", "error"):
        if key in payload:
            preferred.append(f"{key}={payload.get(key)}")
    if preferred:
        return " | ".join(preferred)
    return "campos=" + ", ".join(sorted(payload.keys()))


def _fmt_dt(value):
    if not value:
        return "-"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")
