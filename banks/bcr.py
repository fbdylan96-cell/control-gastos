"""BCR (Banco de Costa Rica) bank-specific parser."""

import re

from banks.utils import (
    normalize_number,
    normalize_whitespace,
    parse_amount_currency,
    smart_title_case,
    strip_city_country_suffix,
)


class BcrParser:
    """Parses BCR notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "bcr"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")

        # Format 1: SINPE Móvil transaction
        if re.search(r"\bsinpe\b", t, re.IGNORECASE):
            return self._parse_sinpe_format(t)

        # Final fallback: unknown format — extract amount only
        currency, amount = parse_amount_currency(t)
        return {
            "merchant_guess": None,
            "amount_guess": amount,
            "currency_guess": currency or None,
            "desc_guess": None,
        }

    # ------------------------------------------------------------------

    def _parse_sinpe_format(self, t):
        '''
        Ejemplo:

        Transacción SINPE MÓVIL Estimado (a): NOMBRE
        Le informamos que se le ha debitado de su cuenta BCR la siguiente transacción SINPE Móvil:
        Número de referencia: 2026040215283009227060421
        Teléfono Destino: 88035092
        Nombre cliente Destino: DYLAN JOSUE MOSQUERA CASTRO
        Entidad Destino: Banco Scotiabank S.A.
        Monto: 58,000.00
        Motivo: Transferencia SINPE
        Esta transacción fue realizada el 02/04/2026 a las 8:48 PM

        '''
        # merchant: between "Nombre cliente Destino:" and "Entidad Destino:"
        m = re.search(r"Nombre\s+cliente\s+Destino:\s*(.+?)\s*Entidad\s+Destino:", t, re.IGNORECASE)
        merchant_raw = m.group(1).strip() if m else None
        merchant = smart_title_case(strip_city_country_suffix(merchant_raw)) if merchant_raw else None

        # amount: between "Monto:" and "Motivo:"
        m = re.search(r"Monto:\s*([\d.,]+)\s+Motivo:", t, re.IGNORECASE)
        amount = normalize_number(m.group(1)) if m else None

        # currency: always CRC for SINPE transactions
        currency = "CRC"

        # fallback if amount wasn't captured
        if amount is None:
            _, amount = parse_amount_currency(t)

        desc = f"SINPE Móvil a {merchant}" if merchant else "SINPE Móvil BCR"

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }
