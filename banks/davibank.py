"""DAVIbank bank-specific parser."""

import re

from banks.utils import (
    normalize_number,
    normalize_whitespace,
    parse_amount_currency,
    smart_title_case,
    strip_city_country_suffix,
    strip_cr_location_suffix,
)


class DavibankParser:
    """Parses DAVIbank notification emails."""

    def can_handle(self, bank: str) -> bool:
        return bank == "davibank"

    def parse(self, subject: str, body_text: str, body_condensed: str) -> dict:
        t = normalize_whitespace(f"{subject or ''}\n{body_text or ''}")
        bc = normalize_whitespace(body_condensed or "")

        # Format 1: full POS transaction — has "realizada en" or "Ciudad:" in condensed body
        # (also covers "Alerta Transacción Tarjeta de Crédito Titular" and
        # "Davivienda Autorizaciones", which share the "realizada en" phrasing)
        if re.search(r"realizada\s+en|Ciudad:", bc, re.IGNORECASE):
            return self._parse_card_expense_format(t)

        # Format: pago de servicios (Municipalidad, recargas, seguros…)
        # Must run before the débito/crédito branch — the security footer of these
        # emails mentions "tarjeta de débito o crédito" and would misroute them.
        if re.search(
            r"pago\s+de\s+servicios?\s+con\s+las\s+siguientes\s+caracter[ií]sticas",
            t, re.IGNORECASE,
        ):
            return self._parse_service_payment(t)

        # Formats 2 & 3: simple debit/credit notification (only amount + currency available)
        if re.search(r"\bd[eé]bito\b|\bcr[eé]dito\b", t, re.IGNORECASE):
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

    def _parse_card_expense_format(self, t):
        '''
        Ejemplo:

        Estimado cliente DAVIbank le notifica que se ha realizado una transacción de débito a su cuenta 
        bancaria de CRC terminada en (*******7889) 
        Detalle de la transacción : Transacción realizada en: CINEMARK COSTA RICA WEB 
        Ciudad: SAN JOSE País: CR Fecha y hora: 03 - 04 - 2026 12:19:19 
        Marca de su tarjeta: VISA Número de la tarjeta: *******8096 
        Número de autorización: 121919 Número de referencia: 609318908748 Moneda: CRC 
        Monto: 15120.00 DAVIbank tiene a su disposición este servicio de alertas para transacciones 
        realizadas en sus cuentas bancarias, esto pensando en su seguridad y en cumplimiento con la 
        legislación local vigente. ¡Gracias por preferirnos como su proveedor de servicios 
        financieros! DAVIbank de Costa Rica S.A.

        '''
        # merchant: between "Transacción realizada en:" and "Ciudad:"
        m = re.search(r"Transacci[oó]n\s+realizada\s+en:\s*(.+?)\s*Ciudad:", t, re.IGNORECASE)
        merchant_raw = m.group(1).strip() if m else None
        merchant = smart_title_case(strip_city_country_suffix(merchant_raw)) if merchant_raw else None

        # variants without "Ciudad:":
        #   "realizada en OSVALDO SEQUEIRA CARTAGO Costa Rica, el día 03/07/2026"
        #     (Alerta Transacción Tarjeta de Crédito Titular)
        #   "realizada en: FARMAVALUE ECOMMERCE (188SAN JOSE CR , el 21/06/2026"
        #     (Davivienda Autorizaciones)
        if not merchant:
            m = re.search(
                r"Transacci[oó]n\s+realizada\s+en:?\s*(.+?)"
                r"(?=\s*(?:\(\d|,?\s*el\s+d[ií]a\b|,\s*el\s+\d))",
                t, re.IGNORECASE,
            )
            if m:
                merchant = smart_title_case(strip_cr_location_suffix(m.group(1).strip())) or None

        # currency: between "Moneda:" and "Monto:"
        m = re.search(r"Moneda:\s*(CRC|USD|EUR)\s+Monto:", t, re.IGNORECASE)
        currency = m.group(1).upper() if m else None

        # amount: between "Monto:" and "DAVIbank"
        m = re.search(r"Monto:\s*([\d.,]+)\s+DAVIbank", t, re.IGNORECASE)
        amount = normalize_number(m.group(1)) if m else None

        # currency as a word (Davivienda): "en Colones por 134,819.09"
        if amount is None or currency is None:
            m = re.search(r"en\s+(Colones|D[oó]lares)\s+por\s+([\d.,]+)", t, re.IGNORECASE)
            if m:
                if currency is None:
                    currency = "CRC" if m.group(1).lower() == "colones" else "USD"
                if amount is None:
                    amount = normalize_number(m.group(2))

        # fallback if targeted extraction missed anything
        if amount is None or currency is None:
            fb_currency, fb_amount = parse_amount_currency(t)
            if amount is None:
                amount = fb_amount
            if currency is None:
                currency = fb_currency or None

        desc = f"Compra en {merchant}" if merchant else None

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_service_payment(self, t):
        '''
        Ejemplo:

        DAVIbank le notifica que nuestro cliente MORA FERNANDEZ JORGE ARM , ha realizado
        un pago de servicios con las siguientes características: MUNICIPALIDAD DE ALAJUELA
        PAGO COMPLETO DE DEUDA Nombre Carrillo Aguiluz Silvia Identificador 0701340583
        Fecha Vito. 30/06/2026 Monto ₡ 139,763 Por su seguridad: …

        El monto puede venir con "₡" o "¢" y sin decimales.
        '''
        m = re.search(r"caracter[ií]sticas:\s*(.+?)\s+Nombre\b", t, re.IGNORECASE)
        merchant = smart_title_case(m.group(1).strip()) if m else None

        m = re.search(r"Monto\s*[₡¢]\s*([\d.,]+)", t, re.IGNORECASE)
        amount = normalize_number(m.group(1)) if m else None
        currency = "CRC" if amount is not None else None

        if amount is None or currency is None:
            fb_currency, fb_amount = parse_amount_currency(t)
            if amount is None:
                amount = fb_amount
            if currency is None:
                currency = fb_currency or None

        desc = f"Pago de servicio: {merchant}" if merchant else "Pago de servicio"

        return {
            "merchant_guess": merchant or None,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
        }

    def _parse_sinpe_format(self, t):
        '''
        Ejemplo:

        Estimado(a) Cliente: Se realizó un débito a la cuenta bancaria en CRC terminada en *******7889 , 
        por un monto de CRC 3,250.00 . La transacción se realizó el día 23-03-2026 10:43:39 
        En cumplimiento con la legislación local vigente y pensando en su seguridad, DAVIbank 
        pone a su disposición el servicio de notificaciones para transacciones realizadas en sus 
        cuentas bancarias. Si usted no realizó esta transacción, favor comunicarse de inmediato con 
        nuestro Centro de Contacto al 8001-726842 Este es un mensaje autogenerado por un sistema, 
        le agradecemos no responder a esta dirección.rirnos como su proveedor de servicios 
        financieros! DAVIbank de Costa Rica S.A.

        '''
        currency, amount = parse_amount_currency(t)

        return {
            "merchant_guess": None,
            "amount_guess": amount,
            "currency_guess": currency or None,
            "desc_guess": "Transacción SINPE",
        }
