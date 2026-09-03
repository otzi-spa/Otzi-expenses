import json

from django.core.management.base import BaseCommand, CommandError

from expenses.rindegastos_client import RindegastosAPIError, RindegastosClient


class Command(BaseCommand):
    help = "Inspecciona usuarios devueltos por Rindegastos getUsers."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", default="", help="ID de usuario Rindegastos a buscar.")
        parser.add_argument("--email", default="", help="Email de usuario Rindegastos a buscar.")
        parser.add_argument("--limit", type=int, default=20, help="Cantidad máxima de usuarios a mostrar.")
        parser.add_argument("--show-json", action="store_true", help="Imprime JSON completo de usuarios encontrados.")

    def handle(self, *args, **options):
        try:
            users = RindegastosClient().get_users()
        except RindegastosAPIError as exc:
            raise CommandError(str(exc)) from exc

        user_id = options["user_id"].strip()
        email = options["email"].strip().casefold()
        filtered = []
        for user in users:
            if user_id and str(user.get("Id") or user.get("id") or "") != user_id:
                continue
            if email and str(user.get("Email") or user.get("email") or "").casefold() != email:
                continue
            filtered.append(user)

        shown = filtered if (user_id or email) else users[: max(1, options["limit"])]
        if not shown:
            self.stdout.write(self.style.WARNING(f"getUsers devolvió {len(users)} usuarios, pero ninguno calzó."))
            return

        self.stdout.write(self.style.SUCCESS(f"getUsers devolvió {len(users)} usuarios. Mostrando {len(shown)}."))
        for user in shown:
            self._print_user(user)
            if options["show_json"]:
                self.stdout.write(json.dumps(user, ensure_ascii=False, indent=2, default=str))

    def _print_user(self, user):
        user_id = _first_present(user, "Id", "id")
        email = _first_present(user, "Email", "email")
        first_name = _first_present(user, "FirstName", "firstName")
        last_name = _first_present(user, "LastName", "Surname", "lastName", "surname")
        is_active = _first_present(user, "IsActive", "isActive", "Status", "status")
        fields = ", ".join(sorted(user.keys()))
        self.stdout.write(
            f"Id={user_id or '-'} | email={email or '-'} | nombre={(first_name or '')} {(last_name or '')}".strip()
        )
        self.stdout.write(f"  estado/activo={is_active if is_active not in (None, '') else '-'}")
        self.stdout.write(f"  campos={fields}")


def _first_present(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return ""
