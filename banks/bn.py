"""BN (Banco Nacional de Costa Rica) bank-specific parser.

Cubre por ahora UN solo formato: el "Voucher Digital" de compra con tarjeta,
que es el único del que tenemos muestra real (2026-09-16). Todo lo demás
—SINPE, transferencias, pagos de servicios— devuelve campos vacíos a
propósito: el enricher rellena con `campo or ai.get(campo)`, así que lo que el
parser deja en None lo resuelve el fallback de IA igual que antes. Devolver un
valor EQUIVOCADO sí sería peor que devolver None, porque la IA ya no lo
corrige — de ahí que sólo se extraiga cuando la estructura calza exacta.
"""

import re

from banks.utils import (
    normalize_number,
    normalize_whitespace,
    parse_amount_currency,
    smart_title_case,
)

# "…comprobante de Compra realizada en <COMERCIO> el 16 de Septiembre de 2026…"
# El verbo concuerda con el sustantivo (Compra realizada / Retiro realizado),
# de ahí realizad[oa].
_VOUCHER_RE = re.compile(
    r"comprobante\s+de\s+(?P<tipo>[\wÁÉÍÓÚÜÑáéíóúüñ]+)\s+(?P<verbo>realizad[oa])\s+en\s+"
    r"(?P<merchant>.+?)\s+el\s+\d{1,2}\s+de\s+[\wÁÉÍÓÚÜÑáéíóúüñ]+\s+de\s+\d{4}",
    re.IGNORECASE,
)

# "TOTAL: CRC 6476,00" — el código de moneda va ANTES del monto.
_TOTAL_RE = re.compile(r"TOTAL:\s*([A-Z]{3})\s*([\d.,]+)", re.IGNORECASE)

# Sufijo de país que BN pega al final del comercio (" … SAN JOSE CR"). Es parte
# de la plantilla, así que también aplica a compras en el exterior (US, MX, ES).
# Ojo: la ciudad viene pegada al nombre sin separador ("CIRUJAN"+"SAN JOSE") y
# con una sola muestra no se puede saber dónde termina el comercio y empieza la
# ciudad, así que sólo se quita el país, que sí es inequívoco.
_COUNTRY_SUFFIX_RE = re.compile(r"\s+([A-Z]{2,3})\s*$")

_EMPTY = {
    "merchant_guess": None,
    "amount_guess": None,
    "currency_guess": None,
    "desc_guess": None,
}


class BnParser:
    """Parses Banco Nacional notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "bn"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")

        m = _VOUCHER_RE.search(t)
        if m:
            return self._parse_voucher_format(t, m)

        # Formato no reconocido: que lo resuelva la IA (ver docstring).
        return dict(_EMPTY)

    # ------------------------------------------------------------------

    def _parse_voucher_format(self, t, m):
        """
        Ejemplo (asunto "Voucher Digital"):

        Estimado señor(a): PARRA CACERES MARIANA PAOLA
        Reciba un cordial saludo de parte del Banco Nacional.
        Por este medio le hacemos llegar el comprobante de Compra realizada en
        COLEGIO MEDICOS Y CIRUJANSAN JOSE CR el 16 de Septiembre de 2026 a las
        10:56 a.m. AUT: 729857 REF: 625916796160 TOTAL: CRC 6476,00
        """
        merchant_raw = _COUNTRY_SUFFIX_RE.sub("", m.group("merchant").strip())
        merchant = smart_title_case(merchant_raw) if merchant_raw else None

        tipo = m.group("tipo").strip().lower()

        total = _TOTAL_RE.search(t)
        if total:
            currency = total.group(1).upper()
            amount = normalize_number(total.group(2))
        else:
            currency, amount = parse_amount_currency(t)
            currency = currency or None

        verbo = m.group("verbo").lower()
        desc = f"{tipo.capitalize()} {verbo} en {merchant}" if merchant else None

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }
