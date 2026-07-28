from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from .models import FuelSpecificTaxRate, TaxIndicatorValue
from .tax_indicators_sync import normalize_key


IVA_RATE = Decimal("0.19")
CLP = Decimal("1")


@dataclass
class InvoiceTaxCalculation:
    iva_amount: Decimal
    specific_tax_amount: Decimal
    source: str
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_autofill(self):
        return self.source == "auto" and not self.warnings


@dataclass
class FuelTaxRateSelection:
    fuel_key: str
    resulting_tax: Decimal
    unit: str
    effective_date: object
    metadata: dict = field(default_factory=dict)


def _round_clp(value):
    return value.quantize(CLP, rounding=ROUND_HALF_UP)


def _split_iva_from_taxable_gross(taxable_gross):
    rounded_taxable_gross = _round_clp(Decimal(taxable_gross))
    net = (rounded_taxable_gross / (Decimal("1") + IVA_RATE)).quantize(CLP, rounding=ROUND_DOWN)
    iva = rounded_taxable_gross - net
    return net, iva


def _is_invoice(document_type):
    return "factura" in (document_type or "").strip().casefold()


def _is_fuel_policy(policy_name):
    return (policy_name or "").strip().casefold() == "combustibles"


def infer_fuel_key(value):
    normalized = normalize_key(value or "")
    if not normalized:
        return ""
    if "diesel" in normalized or "petroleo" in normalized:
        return "petroleo_diesel"
    if "glp" in normalized or "gas_licuado" in normalized:
        return "gas_licuado_de_petroleo_de_consumo_vehicular"
    if "gnc" in normalized or "gas_natural" in normalized:
        return "gas_natural_comprimido_de_consumo_vehicular"
    if "97" in normalized:
        return "gasolina_automotriz_97"
    if "93" in normalized:
        return "gasolina_automotriz_93"
    if "95" in normalized:
        return "gasolina_automotriz_95_promedio_93_97"
    if "bencina" in normalized or "gasolina" in normalized:
        return ""
    return normalized


def _latest_rate_by_key(paid_at, fuel_key):
    if not paid_at or not fuel_key:
        return None
    return (
        FuelSpecificTaxRate.objects.filter(effective_date__lte=paid_at, fuel_key=fuel_key)
        .order_by("-effective_date")
        .first()
    )


def _resolve_fuel_tax_rate(paid_at, fuel_type):
    fuel_key = infer_fuel_key(fuel_type)
    if not fuel_key:
        return None, "No se pudo identificar el tipo de combustible."

    if fuel_key != "gasolina_automotriz_95_promedio_93_97":
        fuel_rate = _latest_rate_by_key(paid_at, fuel_key)
        if not fuel_rate:
            return None, "No hay tasa Mepco vigente para el tipo de combustible."
        return (
            FuelTaxRateSelection(
                fuel_key=fuel_rate.fuel_key,
                resulting_tax=fuel_rate.resulting_tax,
                unit=fuel_rate.unit,
                effective_date=fuel_rate.effective_date,
                metadata={
                    "fuel_key": fuel_rate.fuel_key,
                    "mepco_effective_date": fuel_rate.effective_date.isoformat(),
                    "mepco_resulting_tax": str(fuel_rate.resulting_tax),
                    "mepco_unit": fuel_rate.unit,
                    "rate_strategy": "sii_direct",
                },
            ),
            "",
        )

    rate_93 = _latest_rate_by_key(paid_at, "gasolina_automotriz_93")
    rate_97 = _latest_rate_by_key(paid_at, "gasolina_automotriz_97")
    if not rate_93 or not rate_97:
        return None, "Faltan tasas Mepco 93 y/o 97 para calcular bencina 95 o bencina generica."
    if rate_93.unit.upper() != rate_97.unit.upper():
        return None, "Las tasas Mepco 93 y 97 tienen unidades distintas; requiere revision manual."

    resulting_tax = (rate_93.resulting_tax + rate_97.resulting_tax) / Decimal("2")
    effective_date = min(rate_93.effective_date, rate_97.effective_date)
    return (
        FuelTaxRateSelection(
            fuel_key=fuel_key,
            resulting_tax=resulting_tax,
            unit=rate_93.unit,
            effective_date=effective_date,
            metadata={
                "fuel_key": fuel_key,
                "mepco_effective_date": effective_date.isoformat(),
                "mepco_resulting_tax": str(resulting_tax),
                "mepco_unit": rate_93.unit,
                "rate_strategy": "average_93_97",
                "mepco_93_effective_date": rate_93.effective_date.isoformat(),
                "mepco_93_resulting_tax": str(rate_93.resulting_tax),
                "mepco_97_effective_date": rate_97.effective_date.isoformat(),
                "mepco_97_resulting_tax": str(rate_97.resulting_tax),
                "rate_explanation": (
                    "Bencina 95/generica no tiene fila propia en Mepco SII; "
                    "se calcula usando el promedio simple de las tasas vigentes de gasolina 93 y 97."
                ),
            },
        ),
        "",
    )


def calculate_invoice_taxes(total, paid_at, document_type, policy, fuel_liters=None, fuel_type=""):
    if not _is_invoice(document_type):
        return InvoiceTaxCalculation(
            iva_amount=Decimal("0"),
            specific_tax_amount=Decimal("0"),
            source="none",
            metadata={"reason": "not_invoice"},
        )

    if total is None:
        return InvoiceTaxCalculation(
            iva_amount=Decimal("0"),
            specific_tax_amount=Decimal("0"),
            source="manual",
            warnings=["Falta monto para calcular impuestos."],
        )

    total = Decimal(total)
    if not _is_fuel_policy(policy):
        net, iva = _split_iva_from_taxable_gross(total)
        return InvoiceTaxCalculation(
            iva_amount=iva,
            specific_tax_amount=Decimal("0"),
            source="auto",
            metadata={
                "type": "invoice_non_fuel",
                "iva_rate": str(IVA_RATE),
                "taxable_gross": str(_round_clp(total)),
                "net_amount": str(net),
                "rounding_strategy": "net_round_down_iva_residual",
            },
        )

    warnings = []
    if not paid_at:
        warnings.append("Falta fecha del gasto para buscar tasa Mepco y UTM.")
    if fuel_liters is None:
        warnings.append("Faltan litros de combustible para calcular impuesto específico.")

    fuel_rate = None
    fuel_rate_warning = ""
    if paid_at:
        fuel_rate, fuel_rate_warning = _resolve_fuel_tax_rate(paid_at, fuel_type)
        if fuel_rate_warning:
            warnings.append(fuel_rate_warning)

    utm = TaxIndicatorValue.objects.filter(indicator="UTM", year=paid_at.year, month=paid_at.month).first() if paid_at else None
    if paid_at and not utm:
        warnings.append("No hay UTM sincronizada para el mes de la fecha del gasto.")

    if warnings:
        return InvoiceTaxCalculation(
            iva_amount=Decimal("0"),
            specific_tax_amount=Decimal("0"),
            source="manual",
            warnings=warnings,
            metadata={
                "type": "invoice_fuel",
                "fuel_type": fuel_type or "",
                "fuel_key": infer_fuel_key(fuel_type),
            },
        )

    if fuel_rate.unit.upper() != "UTM/M3":
        return InvoiceTaxCalculation(
            iva_amount=Decimal("0"),
            specific_tax_amount=Decimal("0"),
            source="manual",
            warnings=[f"Unidad Mepco no soportada automáticamente: {fuel_rate.unit}."],
            metadata={
                "type": "invoice_fuel",
                "fuel_type": fuel_type or "",
                "fuel_key": fuel_rate.fuel_key,
                "rate_unit": fuel_rate.unit,
            },
        )

    specific_tax = _round_clp((Decimal(fuel_liters) / Decimal("1000")) * fuel_rate.resulting_tax * utm.value)
    taxable_gross = total - specific_tax
    net, iva = _split_iva_from_taxable_gross(taxable_gross)
    return InvoiceTaxCalculation(
        iva_amount=iva,
        specific_tax_amount=specific_tax,
        source="auto",
        metadata={
            "type": "invoice_fuel",
            "iva_rate": str(IVA_RATE),
            "fuel_type": fuel_type or "",
            **fuel_rate.metadata,
            "taxable_gross": str(_round_clp(taxable_gross)),
            "net_amount": str(net),
            "rounding_strategy": "net_round_down_iva_residual",
            "utm_year": utm.year,
            "utm_month": utm.month,
            "utm_value": str(utm.value),
            "fuel_liters": str(fuel_liters),
        },
    )
