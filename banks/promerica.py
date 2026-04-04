"""Banco Promerica bank-specific parser."""

import re

from banks.utils import (
    normalize_whitespace,
    parse_amount_currency,
    strip_city_country_suffix,
    smart_title_case,
)


class PromericaParser:
    """Parses Banco Promerica notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "promerica"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")

        merchant_raw = self._extract_comercio(t)
        merchant = smart_title_case(strip_city_country_suffix(merchant_raw)) if merchant_raw else None
        currency, amount = parse_amount_currency(t)
        desc = f"Compra en {merchant}" if merchant else None

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency or None,
            "desc_guess": desc,
        }

    def _extract_comercio(self, t):
        # "Comercio: NAME Ciudad y País: ..."
        m = re.search(r"Comercio:\s*(.+?)(?=\s*Ciudad\s+y\s+pa)", t, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # "Comercio: NAME Fecha/hora: ..."
        m = re.search(
            r"Comercio[:\s]+(.+?)(?=\s+(Ciudad\/?Pa[ií]s|Ciudad\s+y\s+pa[ií]s"
            r"|Fecha\/?hora|Fecha|Monto|Importe|Total|N[uú]mero|Tarjeta|Referencia)\b|$)",
            t, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()

        # "Compra en NAME Monto: ..."
        m = re.search(
            r"Compra\s+en\s+(.+?)(?=\s+(Ciudad\s+y\s+pa|Ciudad\/?Pa[ií]s|Monto:|Monto|CRC|USD|EUR|$))",
            t, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()

        return ""
