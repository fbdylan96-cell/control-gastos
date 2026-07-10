"""Banco Promerica bank-specific parser."""

import re

from banks.utils import (
    normalize_number,
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
        subj = normalize_whitespace(subject or "")

        # "Aplicación de Transacción Exitosa" — solo la variante de pago de
        # servicios ("… de la compañía X …"); el pago de tarjeta de crédito
        # (titular + tarjeta enmascarada) se deja pasar a propósito para no
        # duplicar el débito de la cuenta.
        if re.search(r"aplicaci.{1,2}n\s+de\s+transacci.{1,2}n\s+exitosa", subj, re.IGNORECASE) \
                and re.search(r"de\s+la\s+compa[ñn][ií]a", t, re.IGNORECASE):
            parsed = self._parse_applied_service_payment(t)
            if parsed:
                return parsed

        # "Notificacion Pago de Servicio Automatico"
        if re.search(r"pago\s+de\s+servicio\s+autom[aá]tico", subj, re.IGNORECASE):
            parsed = self._parse_automatic_service_payment(t)
            if parsed:
                return parsed

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

    def _parse_applied_service_payment(self, t):
        '''
        Ejemplo:

        Estimado(a) Cliente: Le informamos que el pago RECIBOS DE ELECTRICIDAD CNFL
        de la compañía COMPANIA NACIONAL DE FUERZA Y LUZ #28487331 con el número de
        referencia 00000000000117604465 por un monto de 54,215.00 COLONES, ha sido
        aplicada con éxito.

        La moneda viene como palabra (COLONES / DOLARES ESTADOUNIDENSES) y el monto
        en formato mixto (54,215.00 / 142.613,09 / 99,87).
        '''
        m = re.search(
            r"de\s+la\s+compa[ñn][ií]a\s+(.+?)\s*(?:#\d|con\s+el\s+n[uú]mero)",
            t, re.IGNORECASE,
        )
        merchant = smart_title_case(m.group(1).strip()) if m else None

        m = re.search(
            r"por\s+un\s+monto\s+de\s*:?\s*([\d.,]+)\s+(COLONES|D[OÓ]LARES)",
            t, re.IGNORECASE,
        )
        if not m:
            return None
        amount = normalize_number(m.group(1))
        if amount is None:
            return None
        currency = "CRC" if m.group(2).lower().startswith("col") else "USD"
        desc = f"Pago de servicio: {merchant}" if merchant else "Pago de servicio"

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_automatic_service_payment(self, t):
        '''
        Ejemplo:

        28458235 Pago CNFL por un monto de: 69,710.00 ha sido realizado satisfactoriamente.
        3585201 Telecable Cable e Internet por un monto de: 29,532.56 ha sido realizado…

        No trae código de moneda: es implícitamente CRC.
        '''
        m = re.search(
            r"\b\d{5,}\s+(.+?)\s+por\s+un\s+monto\s+de\s*:\s*([\d.,]+)",
            t, re.IGNORECASE,
        )
        if not m:
            return None
        merchant_raw = re.sub(r"^pago\s+", "", m.group(1).strip(), flags=re.IGNORECASE)
        merchant = smart_title_case(merchant_raw) or None
        amount = normalize_number(m.group(2))
        if amount is None:
            return None
        desc = f"Pago de servicio: {merchant}" if merchant else "Pago de servicio"

        return {
            "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": "CRC",
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
