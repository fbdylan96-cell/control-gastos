"""Grupo Mutual bank-specific parser."""

import re

from banks.utils import (
    clean_merchant_key,
    normalize_number,
    normalize_whitespace,
    parse_amount_currency,
    smart_title_case,
)

# Grupo Mutual sends its notifications from these addresses (see also
# web-app/utils.BANK_NOTIFICATION_SENDERS, used by the Configuración tab).
GRUPOMUTUAL_SENDERS = [
    "MutualMovil@grupomutual.fi.cr",
    "MutualEnLinea@grupomutual.fi.cr",
    "comunicadostarjetadedebito@grupomutual.fi.cr",
]

# Grupo Mutual writes the currency as a Spanish word ("MONEDA : COLONES" or
# "6,000.00 Colones"), not an ISO code, so banks.utils.parse_amount_currency
# cannot recognize it. Keys are accent-free lowercase (looked up via
# clean_merchant_key, which strips accents).
_CURRENCY_WORDS = {
    "colones": "CRC",
    "colon": "CRC",
    "dolares": "USD",
    "dolar": "USD",
    "euros": "EUR",
    "euro": "EUR",
}


def _currency_code(word):
    return _CURRENCY_WORDS.get(clean_merchant_key(word or ""))


class GrupoMutualParser:
    """Parses Grupo Mutual notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "grupomutual"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        """Returns dict with merchant_guess, amount_guess, currency_guess, desc_guess."""
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")

        # Card transaction (débito / crédito): has the labelled "NOMBRE DEL COMERCIO" field.
        if re.search(r"NOMBRE\s+DEL\s+COMERCIO", t, re.IGNORECASE):
            return self._parse_card_transaction(t)

        # SINPE Móvil transfer: prose with "ha enviado una transferencia… por concepto de…".
        if re.search(r"\bsinpe\b|transferencia", t, re.IGNORECASE):
            return self._parse_transfer(t)

        # Fallback: amount/currency only (covers formats we haven't mapped yet).
        currency, amount = self._extract_amount_currency(t)
        return {
            "merchant_guess": None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": None,
        }

    # ------------------------------------------------------------------

    def _parse_card_transaction(self, t):
        '''
        Ejemplo (TRANSACCIONES CON TARJETAS DE DÉBITO):

        ... MONTO REBAJADO A SU CUENTA : 102,704.92 MONEDA : COLONES
        NOMBRE DEL COMERCIO : AIRBNB * HMNHKXW8HN 415-800-
        PAÍS ORIGEN DEL COMERCIO : Reino Unido de Gran Bretaña e Irlanda del Norte (el)
        NÚMERO DE REFERENCIA : 613960187562 ... TIPO DE TRANSACCIÓN : COMPRA
        TIPO DE TARJETA : TITULAR NOMBRE DEL TARJETAHABIENTE : ASTRIT CASTRO ALFARO
        '''
        # merchant: between "NOMBRE DEL COMERCIO :" and the next labelled field
        m = re.search(
            r"NOMBRE\s+DEL\s+COMERCIO\s*:?\s*(.+?)\s*"
            r"(?=PA[IÍ]S\s+ORIGEN|N[UÚ]MERO\s+DE\s+REFERENCIA"
            r"|TIPO\s+DE\s+TRANSACCI[OÓ]N|$)",
            t, re.IGNORECASE,
        )
        merchant_raw = m.group(1).strip() if m else None
        merchant = smart_title_case(merchant_raw) if merchant_raw else None

        currency, amount = self._extract_amount_currency(t)

        # transaction type word (COMPRA, RETIRO, …) for the description
        m = re.search(
            r"TIPO\s+DE\s+TRANSACCI[OÓ]N\s*:?\s*([A-Za-zÁÉÍÓÚÑáéíóúñ ]+?)\s*"
            r"(?=TIPO\s+DE\s+TARJETA|NOMBRE\s+DEL\s+TARJETAHABIENTE|$)",
            t, re.IGNORECASE,
        )
        ttype = m.group(1).strip().lower() if m else None

        if merchant:
            if ttype and "compra" not in ttype:
                desc = f"{smart_title_case(ttype)} en {merchant}"
            else:
                desc = f"Compra en {merchant}"
        else:
            desc = None

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_transfer(self, t):
        '''
        Ejemplo (Comprobante de Transferencia SINPE Móvil):

        Grupo Mutual le comunica que ha enviado una transferencia por medio de
        Sinpe Móvil Mutual por un monto de 6,000.00 Colones, por concepto de "uñas ".
        La cuenta origen … es 126-200-85297172-BCC a nombre de ASTRIT YOANKA CASTRO ALFARO
        y el destino de la transferencia es 63271321 a nombre de Soto Jennifer Maria,
        identificación: 02-0734-0451. La entidad financiera destino es BANCO NACIONAL …
        '''
        currency, amount = self._extract_amount_currency(t)

        # payee: "el destino de la transferencia es <cuenta> a nombre de <NOMBRE>"
        m = re.search(
            r"destino\s+de\s+la\s+transferencia\s+es\s+\S+\s+a\s+nombre\s+de\s+(.+?)\s*"
            r"(?=,?\s*identificaci[oó]n|\.\s|\bLa\s+entidad|\bEl\s+n[uú]mero|$)",
            t, re.IGNORECASE,
        )
        payee = smart_title_case(m.group(1).strip()) if m else None

        # concept: por concepto de "uñas"  (straight or curly quotes)
        m = re.search(
            r"concepto\s+de\s*[\"“”‘’]\s*(.+?)\s*[\"“”‘’]",
            t, re.IGNORECASE,
        )
        concepto = m.group(1).strip() if m else None

        merchant = payee or "Transferencia SINPE"
        if payee and concepto:
            desc = f"SINPE Móvil a {payee}: {concepto}"
        elif payee:
            desc = f"SINPE Móvil a {payee}"
        elif concepto:
            desc = f"SINPE Móvil: {concepto}"
        else:
            desc = "SINPE Móvil"

        return {
            "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _extract_amount_currency(self, t):
        """Return (currency, amount) — same order as banks.utils.parse_amount_currency.

        Grupo Mutual labels the amount ("MONTO REBAJADO A SU CUENTA : …" or
        "por un monto de …") and writes the currency as a word ("COLONES"),
        so the shared ISO-code/symbol parser does not apply; extract by label /
        currency word and fall back to it only for whatever is still missing.
        """
        m = re.search(
            r"MONTO\s+REBAJADO\s+A\s+SU\s+CUENTA\s*:?\s*([\d.,]+)", t, re.IGNORECASE
        )
        if not m:
            m = re.search(
                r"\bMONTO\b[^:\d]*:?\s*([\d]{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)",
                t, re.IGNORECASE,
            )
        amount = normalize_number(m.group(1)) if m else None

        # currency: explicit "MONEDA : COLONES" label, else a currency word in the text
        currency = None
        m = re.search(r"MONEDA\s*:?\s*([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", t, re.IGNORECASE)
        if m:
            currency = _currency_code(m.group(1))
        if currency is None:
            m = re.search(r"\b(colones|col[oó]n|d[oó]lar(?:es)?|euros?)\b", t, re.IGNORECASE)
            if m:
                currency = _currency_code(m.group(1))

        if amount is None or currency is None:
            fb_currency, fb_amount = parse_amount_currency(t)
            if amount is None:
                amount = fb_amount
            if currency is None:
                currency = fb_currency or None

        return currency, amount
