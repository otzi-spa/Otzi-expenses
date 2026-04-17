from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django import template
from django.conf import settings

register = template.Library()


def _format_thousands(integer: str) -> str:
    """Add dot thousand separators to a numeric string."""
    if not integer:
        return "0"
    parts = []
    while integer:
        parts.append(integer[-3:])
        integer = integer[:-3]
    return ".".join(reversed(parts))


@register.filter
def cl_currency(value):
    """
    Format a number following the common Chilean currency format.
    Thousands are separated with dots and decimals (if any) use a comma.
    """
    if value in (None, ""):
        return ""

    try:
        dec_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value

    is_negative = dec_value < 0
    dec_value = abs(dec_value)

    quantize_target = Decimal("0.01") if dec_value % 1 else Decimal("1")
    dec_value = dec_value.quantize(quantize_target, rounding=ROUND_HALF_UP)

    number_str = f"{dec_value:f}"
    integer_part, dot, fraction_part = number_str.partition(".")

    formatted_integer = _format_thousands(integer_part)
    formatted_fraction = ""

    if fraction_part and any(ch != "0" for ch in fraction_part):
        formatted_fraction = "," + fraction_part

    formatted = formatted_integer + formatted_fraction
    return f"-{formatted}" if is_negative else formatted


@register.filter
def secure_media_url(url: str | None) -> str | None:
    """
    Enforce HTTPS when serving media from a custom domain (e.g. ngrok tunnel).
    Prevents mixed-content warnings when the base site runs over HTTPS.
    """
    if not url:
        return url
    custom_domain = getattr(settings, "AZURE_CUSTOM_DOMAIN", None)
    if custom_domain and url.startswith("http://"):
        return url.replace("http://", "https://", 1)
    return url
