import logging
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

import requests
from django.utils import timezone

from .models import FuelSpecificTaxRate, TaxIndicatorValue


logger = logging.getLogger(__name__)

SII_MEPCO_URL = "https://www.sii.cl/valores_y_fechas/mepco/mepco{year}.htm"
SII_UTM_URL = "https://www.sii.cl/valores_y_fechas/utm/utm{year}.htm"

MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

FUEL_NAMES = [
    "Gasolina Automotriz 93",
    "Gasolina Automotriz 97",
    "Petróleo Diesel",
    "Petroleo Diesel",
    "Gas Licuado de Petróleo de Consumo Vehicular",
    "Gas Licuado de Petroleo de Consumo Vehicular",
    "Gas Natural Comprimido de Consumo Vehicular",
]


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = " ".join((data or "").split())
        if text:
            self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def _html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html or "")
    return parser.text()


def normalize_key(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    ascii_value = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_value).strip("_").lower()
    return ascii_value


def parse_chilean_decimal(value):
    raw = (value or "").strip()
    if not raw:
        raise InvalidOperation("Valor decimal vacío")
    normalized = raw.replace("\xa0", "").replace(" ", "")
    normalized = normalized.replace(".", "").replace(",", ".")
    return Decimal(normalized)


def _parse_date(value, fallback_year=None):
    raw = (value or "").strip().replace("/", "-")
    match = re.search(r"(\d{1,2})-(\d{1,2})-(\d{2,4})", raw)
    if match:
        day, month, year = match.groups()
        year_value = int(year)
        if year_value < 100:
            year_value += 2000
        return date(year_value, int(month), int(day))

    month_names = "|".join(MONTHS.keys())
    match = re.search(rf"(\d{{1,2}})\s+de\s+({month_names})(?:\s+de\s+(\d{{4}}))?", raw, re.IGNORECASE)
    if match:
        day, month_name, year = match.groups()
        year_value = int(year or fallback_year or timezone.localdate().year)
        return date(year_value, MONTHS[month_name.casefold()], int(day))

    raise ValueError(f"No se pudo parsear fecha SII: {value}")


class SiiTaxIndicatorSync:
    def __init__(self, http_get=None, timeout=20):
        self.http_get = http_get
        self.timeout = timeout

    def _fetch(self, url):
        if self.http_get:
            return self.http_get(url)
        response = requests.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    def parse_utm_values(self, html, source_url, year):
        text = _html_to_text(html)
        rows = []
        month_pattern = "|".join(MONTHS.keys())
        pattern = re.compile(
            rf"\b({month_pattern})\b\s+(\d{{1,3}}(?:\.\d{{3}})*(?:,\d+)?)",
            re.IGNORECASE,
        )
        seen = set()
        for match in pattern.finditer(text):
            month_name, value = match.groups()
            month = MONTHS[month_name.casefold()]
            if month in seen:
                continue
            seen.add(month)
            rows.append(
                {
                    "indicator": "UTM",
                    "year": year,
                    "month": month,
                    "value": parse_chilean_decimal(value),
                    "source_url": source_url,
                    "raw_payload": {"month": month_name, "value": value},
                }
            )
        return rows

    def parse_mepco_rates(self, html, source_url, year):
        text = _html_to_text(html)
        compact = re.sub(r"\s+", " ", text)
        date_pattern = re.compile(
            r"(?:vigencia|rige|desde)[^0-9]{0,40}(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
            re.IGNORECASE,
        )
        matches = list(date_pattern.finditer(compact))
        if matches:
            sections = [
                (
                    _parse_date(match.group(1), fallback_year=year),
                    match.end(),
                    matches[index + 1].start() if index + 1 < len(matches) else len(compact),
                )
                for index, match in enumerate(matches)
            ]
        else:
            sections = [(date(year, 1, 1), 0, len(compact))]

        rows = []
        for effective_date, section_start, section_end in sections:
            section = compact[section_start:section_end]

            for fuel_name in FUEL_NAMES:
                fuel_regex = re.compile(
                    rf"({re.escape(fuel_name)})(?P<body>.{{0,500}}?)(UTM\s*/\s*(?:1000)?m3|UTM\s*/\s*m3)",
                    re.IGNORECASE,
                )
                fuel_match = fuel_regex.search(section)
                if not fuel_match:
                    continue

                numbers = re.findall(r"-?\d+(?:[.,]\d+)+", fuel_match.group("body"))
                if len(numbers) < 3:
                    continue

                unit = re.sub(r"\s+", "", fuel_match.group(3)).upper()
                component_base = parse_chilean_decimal(numbers[-3])
                component_variable = parse_chilean_decimal(numbers[-2])
                resulting_tax = parse_chilean_decimal(numbers[-1])
                canonical_name = fuel_match.group(1).strip()
                rows.append(
                    {
                        "effective_date": effective_date,
                        "fuel_name": canonical_name,
                        "fuel_key": normalize_key(canonical_name),
                        "component_base": component_base,
                        "component_variable": component_variable,
                        "resulting_tax": resulting_tax,
                        "unit": unit,
                        "source_url": source_url,
                        "raw_payload": {
                            "fuel_name": canonical_name,
                            "numbers": numbers[-3:],
                            "unit": unit,
                        },
                    }
                )
        return rows

    def sync_year(self, year):
        year = int(year)
        now = timezone.now()
        utm_url = SII_UTM_URL.format(year=year)
        mepco_url = SII_MEPCO_URL.format(year=year)

        utm_rows = self.parse_utm_values(self._fetch(utm_url), utm_url, year)
        mepco_rows = self.parse_mepco_rates(self._fetch(mepco_url), mepco_url, year)

        for row in utm_rows:
            TaxIndicatorValue.objects.update_or_create(
                indicator=row["indicator"],
                year=row["year"],
                month=row["month"],
                defaults={
                    "value": row["value"],
                    "source_url": row["source_url"],
                    "last_synced_at": now,
                    "raw_payload": row["raw_payload"],
                },
            )

        for row in mepco_rows:
            FuelSpecificTaxRate.objects.update_or_create(
                effective_date=row["effective_date"],
                fuel_key=row["fuel_key"],
                unit=row["unit"],
                defaults={
                    "fuel_name": row["fuel_name"],
                    "component_base": row["component_base"],
                    "component_variable": row["component_variable"],
                    "resulting_tax": row["resulting_tax"],
                    "source_url": row["source_url"],
                    "last_synced_at": now,
                    "raw_payload": row["raw_payload"],
                },
            )

        stats = {"year": year, "utm_values": len(utm_rows), "fuel_rates": len(mepco_rows)}
        logger.info("Sincronización SII completada: %s", stats)
        return stats
