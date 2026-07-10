"""MUCAP (Mutual Cartago de Ahorro y Préstamo) bank-specific parser."""

import re

from banks.utils import (
    normalize_number,
    normalize_whitespace,
    parse_amount_currency,
    smart_title_case,
)


def _currency_word(word):
    """'Colones' → CRC, 'Dólares' → USD. None/desconocida → CRC (MUCAP opera en CRC)."""
    return "USD" if word and word.lower().startswith("d") else "CRC"


class MucapParser:
    """Parses MUCAP notification emails (info@mucap.fi.cr)."""

    def can_handle(self, bank: str) -> bool:
        return bank == "mucap"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        """Returns dict with merchant_guess, amount_guess, currency_guess, desc_guess."""
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")

        # Formato A: pago de recibo/servicio.
        # "Mucap le informa que el recibo 239696 por concepto de PAGO LUZ de
        #  ICE ELECTRICO por un valor de 7,210.00 Colones ha sido cancelado
        #  exitosamente."  (la palabra "Colones" a veces falta → CRC implícito)
        if re.search(r"recibo\s+\d+\s+por\s+concepto\s+de", t, re.IGNORECASE):
            return self._parse_receipt_payment(t)

        # Formato B / B′: transferencia SINPE sobre la cuenta, en ambas direcciones.
        # "se acreditó a su cuenta IBAN CR74… mediante tranferencia SINPE con el
        #  número de referencia … un monto de 750,000.00 Colones"
        # "se debitó de su cuenta IBAN CR74… mediante tranferencia SINPE de X …"
        # (sic: "tranferencia" viene mal escrito en el correo original)
        m = re.search(r"se\s+(acredit[oó]|debit[oó])\s+(?:a|de)\s+su\s+cuenta", t, re.IGNORECASE)
        if m:
            incoming = m.group(1).lower().startswith("acredit")
            return self._parse_account_sinpe(t, incoming)

        # Formato C: débito SINPE enviado (DTR).
        # "débito en tiempo real (DTR) la siguiente transferencia:
        #  Monto ¢ 750,000.00 … Detalle BETRIZ MONTO DE LA CASA JULIO 26"
        if re.search(r"d[eé]bito\s+en\s+tiempo\s+real|\(DTR\)", t, re.IGNORECASE):
            return self._parse_sinpe_debit(t)

        # Formato D: SINPE Móvil enviado.
        # "Teléfono acreditado: 84610878 … Monto: CRC 59,800.00
        #  Detalle: semana al 26 … Esta transferencia fue enviada el …"
        if re.search(r"tel[eé]fono\s+acreditado|sinpe\s+m[oó]vil", t, re.IGNORECASE):
            return self._parse_sinpe_movil(t)

        # Fallback: monto solamente (cubre formatos aún no mapeados y los
        # "Estado de cuenta" que llegan con cuerpo vacío)
        currency, amount = parse_amount_currency(t)
        return {
            "merchant_guess": None,
            "amount_guess": amount,
            "currency_guess": currency or None,
            "desc_guess": None,
        }

    # ------------------------------------------------------------------

    def _parse_receipt_payment(self, t):
        m = re.search(
            r"por\s+concepto\s+de\s+(.+?)\s+de\s+(.+?)\s+por\s+un\s+valor\s+de\s+"
            r"([\d.,]+)\s*(Colones|D[oó]lares)?",
            t, re.IGNORECASE,
        )
        if not m:
            currency, amount = parse_amount_currency(t)
            return {
                "merchant_guess": None,
                "amount_guess": amount,
                "currency_guess": currency or "CRC",
                "desc_guess": "Pago de servicio",
            }
        concept = smart_title_case(m.group(1).strip())
        merchant = smart_title_case(m.group(2).strip())
        amount = normalize_number(m.group(3))
        currency = _currency_word(m.group(4))
        desc = f"Pago de servicio: {merchant} ({concept})" if concept else f"Pago de servicio: {merchant}"
        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_account_sinpe(self, t, incoming):
        m = re.search(r"un\s+monto\s+de\s+([\d.,]+)\s*(Colones|D[oó]lares)?", t, re.IGNORECASE)
        if m:
            amount = normalize_number(m.group(1))
            currency = _currency_word(m.group(2))
        else:
            currency, amount = parse_amount_currency(t)
            currency = currency or "CRC"
        desc = "Transferencia SINPE recibida" if incoming else "Transferencia SINPE enviada"
        return {
            "merchant_guess": "Transferencia SINPE",
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_sinpe_movil(self, t):
        m = re.search(r"Monto:\s*(CRC|USD|EUR)\s*([\d.,]+)", t, re.IGNORECASE)
        if m:
            currency = m.group(1).upper()
            amount = normalize_number(m.group(2))
        else:
            currency, amount = parse_amount_currency(t)
            currency = currency or "CRC"

        m = re.search(r"Detalle:\s*(.+?)\s+Esta\s+transferencia", t, re.IGNORECASE)
        detail = m.group(1).strip() if m else None
        desc = f"SINPE Móvil: {detail}" if detail else "SINPE Móvil"
        return {
            "merchant_guess": "Transferencia SINPE",
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_sinpe_debit(self, t):
        m = re.search(r"Monto\s*[₡¢]\s*([\d.,]+)", t, re.IGNORECASE)
        amount = normalize_number(m.group(1)) if m else None
        currency = "CRC" if amount is not None else None
        if amount is None:
            fb_currency, fb_amount = parse_amount_currency(t)
            amount = fb_amount
            currency = fb_currency or "CRC"

        m = re.search(r"Detalle\s+(.+?)\s+Este\s+d[eé]bito", t, re.IGNORECASE)
        detail = smart_title_case(m.group(1).strip()) if m else None
        desc = f"Transferencia SINPE: {detail}" if detail else "Transferencia SINPE enviada"
        return {
            "merchant_guess": "Transferencia SINPE",
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }
