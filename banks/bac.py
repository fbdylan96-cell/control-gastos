"""BAC Credomatic bank-specific parser."""

import re

from banks.utils import (
    normalize_whitespace,
    parse_amount_currency,
    strip_city_country_suffix,
    smart_title_case,
)

# BAC sends from these domains
BAC_DOMAINS = ["notificacionesbaccr.com", "baccredomatic.com", "alerta@baccredomatic.com",
               "baccredomatic.cr"]  # nuevo remitente de tarjetas desde 2026-08


class BacParser:
    """Parses BAC Credomatic bank notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "bac"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        """
        Returns dict with merchant_guess, amount_guess, currency_guess, desc_guess.
        Any field not found is None / empty string.
        """
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")
        subj = normalize_whitespace(subject or "")

        # BAC COMPRA: has "Comercio:" and "Tipo de Transacción: COMPRA"
        is_compra = (
            re.search(r"Comercio:", t, re.IGNORECASE)
            and re.search(r"Tipo\s+de\s+Transacci[oó]n:\s*COMPRA", t, re.IGNORECASE)
        )
        if is_compra:
            return self._parse_compra(t)

        # BAC SINPE Móvil
        if re.search(r"sinpe", subj, re.IGNORECASE):
            return self._parse_sinpe(t)

        # BAC Notificación de Transferencia
        # ".{1,2}" tolerates the accented "ó" arriving as NFC (1 char), NFD
        # (o + combining accent, 2 chars) or mojibake "oì" from forwards (2 chars).
        if re.search(r"notificaci.{1,2}n\s+de\s+transferencia", subj, re.IGNORECASE):
            return self._parse_transfer_notification(t, subj)

        # BAC Transferencia entre cuentas: "Ha hecho una transferencia por CRC X"
        if re.search(
            r"ha\s+hecho\s+una\s+transferencia"
            r"|se\s+ha\s+realizado\s+una\s+transferencia\s+de\s+su\s+cuenta",
            t, re.IGNORECASE,
        ):
            return self._parse_transfer_simple(t)

        # Generic: any "Comercio:" line (e.g. insurance, other payments)
        if re.search(r"Comercio:", t, re.IGNORECASE):
            return self._parse_generic_comercio(t)

        # Fallback: try amount only
        currency, amount = parse_amount_currency(t)
        return {
            "merchant_guess": None,
            "amount_guess": amount,
            "currency_guess": currency or None,
            "desc_guess": None,
        }

    # ------------------------------------------------------------------

    def _parse_compra(self, t):
        merchant_raw = self._extract_comercio(t)
        merchant = smart_title_case(strip_city_country_suffix(merchant_raw))
        currency, amount = parse_amount_currency(t)
        desc = f"Compra en {merchant}" if merchant else None
        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency or "CRC",
            "desc_guess": desc,
        }

    def _parse_sinpe(self, t):
        currency, amount = parse_amount_currency(t)
        m = re.search(
            r"(?:enviaste|enviado a|destinatario)[:\s]+(.+?)(?=\s*(monto|por|CRC|USD|\n|$))",
            t, re.IGNORECASE,
        )
        payee = m.group(1).strip() if m else None
        merchant = smart_title_case(payee) if payee else "Transferencia BAC"
        return {
            "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": currency or "CRC",
            "desc_guess": f"SINPE Móvil: {merchant}" if payee else "SINPE Móvil BAC",
        }

    def _parse_transfer_notification(self, t, subj=""):
        currency, amount = parse_amount_currency(t)
        m = re.search(
            r"por\s+concepto\s+de:\s*(.+?)(?=\s*(el\s+n[uú]mero\s+de\s+referencia"
            r"|muchas\s+gracias|¿tiene\s+dudas|\bBAC\b|$))",
            t, re.IGNORECASE,
        )
        if m:
            concept = re.sub(r"_+", " ", m.group(1).strip())
            concept = re.sub(r"\s+", " ", concept).strip()
        else:
            concept = None
        # Forwarded subjects carry the counterparty:
        # "RV: Notificación de transferencia de: LUIS ADOLFO GARRO CARVAJAL"
        m = re.search(r"transferencia\s+de:\s*(.+)$", subj or "", re.IGNORECASE)
        sender = smart_title_case(m.group(1).strip()) if m else None
        merchant = sender or "Transferencia BAC"
        desc = f"Transferencia BAC: {concept}" if concept else "Transferencia BAC"
        return {
            "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": currency or "CRC",
            "desc_guess": desc,
        }

    def _parse_transfer_simple(self, t):
        """Account-to-account transfer: 'Ha hecho una transferencia por CRC X…'.

        This format carries no concept/description, only amount and account data.
        """
        currency, amount = parse_amount_currency(t)
        return {
            "merchant_guess": "Transferencia BAC",
            "amount_guess": amount,
            "currency_guess": currency or "CRC",
            "desc_guess": "Transferencia BAC",
        }

    def _parse_generic_comercio(self, t):
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
        m = re.search(r"Comercio:\s*(.+?)(?=\s*Ciudad\s+y\s+pa)", t, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(
            r"Comercio:\s*(.+?)(?=\s*(Fecha|Monto|VISA|Autorizaci[oó]n|Referencia"
            r"|Tipo\s+de\s+Transacci[oó]n)\b|$)",
            t, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        return ""
