import base64
import hashlib
import hmac

from django.conf import settings


def expense_integration_code(expense_id):
    key = str(settings.SECRET_KEY).encode("utf-8")
    message = f"expense:{expense_id}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    token = base64.b32encode(digest[:5]).decode("ascii").rstrip("=")
    return f"OTZ-{token}"


def expense_integration_code_for_expense(expense):
    return expense.rindegastos_integration_code or expense_integration_code(expense.id)


def ensure_expense_integration_code(expense):
    if expense.rindegastos_integration_code:
        return expense.rindegastos_integration_code
    code = expense_integration_code(expense.id)
    type(expense).objects.filter(pk=expense.pk).update(rindegastos_integration_code=code)
    expense.rindegastos_integration_code = code
    return code
