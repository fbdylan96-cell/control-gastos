"""Fetch de tipos de cambio.

Fuente primaria: Servicio Web legacy del BCCR (paquete `bccr`, 44 monedas).
⚠ Ese servicio (gee.bccr.fi.cr/...wsindicadoreseconomicos.asmx) responde
"Service Unavailable" desde 2026-07-20 — el BCCR migró su sitio de
indicadores a sdd.bccr.fi.cr y no publica (aún) un API equivalente. Se
mantiene el intento por si el servicio se restablece, en cuyo caso las 44
monedas vuelven solas.

Fallback para las monedas CRÍTICAS (CRC, EUR): API pública del Ministerio
de Hacienda (api.hacienda.go.cr/indicadores/tc), que publica el tipo de
cambio de referencia del BCCR en JSON:
  * /dolar → venta.valor = CRC por USD (misma convención de rate_vs_usd)
  * /euro  → dolares = USD por EUR → rate_vs_usd[EUR] = 1/dolares
Las demás monedas quedan en None mientras el BCCR no ofrezca fuente; las
conversiones usan la última fecha con datos válidos (get_fx_conversion).
"""
import logging
from datetime import date, timedelta

import requests
from bccr import SW

log = logging.getLogger(__name__)

code_list = {
    'EUR': 333,
    'JPY': 325,
    'CHF': 326,
    'CAD': 328,
    'MXN': 332,
    'SEK': 335,
    'KRW': 337,
    'GTQ': 338,
    'HNL': 339,
    'NIO': 340,
    'DKK': 342,
    'NOK': 343,
    'ARS': 344,
    'COP': 345,
    'BRL': 346,
    'DOP': 3043,
    'HKD': 3052,
    'TWD': 3053,
    'BOB': 3054,
    'CLP': 3055,
    'RUB': 3056,
    'PEN': 3057,
    'CNY': 3364,
    'PLN': 3430,
    'LKR': 20873,
    'BDT': 21251,
    'THB': 21262,
    'IDR': 21263,
    'AED': 21264,
    'MAD': 21265,
    'ILS': 21266,
    'INR': 21267,
    'EGP': 21268,
    'NZD': 21269,
    'SGD': 21270,
    'VND': 21766,
    'ZAR': 21881,
    'JOD': 22204,
    'MYR': 25067,
    'BHD': 41438,
    'VES': 60246,
    'UYU': 84857,
    'CRC': 318,
}

# BCCR quotes these indicators as USD per unit of currency (e.g. EUR 333 ≈ 1.16
# USD per EUR), the inverse of our rate_vs_usd convention (units per 1 USD), so
# their fetched values must be inverted before storing.
inverted_codes = {'EUR'}

# Monedas imprescindibles para el negocio: CRC convierte TODO a colones y EUR
# es la única otra divisa con uso real. Si alguna falta tras el fetch primario,
# se intenta el fallback de Hacienda; rate_scheduler alerta por correo solo
# cuando una de ESTAS sigue faltando (las 41 restantes solo se loguean).
CRITICAL_CURRENCIES = ('CRC', 'EUR')

HACIENDA_TC_DOLAR = "https://api.hacienda.go.cr/indicadores/tc/dolar"
HACIENDA_TC_EURO = "https://api.hacienda.go.cr/indicadores/tc/euro"


def _rates_from_bccr(days_back: int) -> dict:
    """Legacy BCCR web service fetch (all 44 currencies). May yield all-None."""
    today = date.today()
    start = today - timedelta(days=days_back)

    exc_rate_daily = SW(
        **{code: indicator for code, indicator in code_list.items()},
        FechaInicio=str(start.strftime("%Y-%m-%d"))
    )

    latest_rates = {'USD': 1.0}
    for code in code_list:
        if code not in exc_rate_daily.columns:
            latest_rates[code] = None
        else:
            col = exc_rate_daily[code].dropna()
            value = float(col.iloc[-1]) if not col.empty else None
            if value is not None and code in inverted_codes:
                value = (1.0 / value) if value != 0 else None
            latest_rates[code] = value

    return latest_rates


def _rates_from_hacienda() -> dict:
    """Fallback for the critical currencies via the Hacienda JSON API."""
    out = {}
    try:
        resp = requests.get(HACIENDA_TC_DOLAR, timeout=30)
        resp.raise_for_status()
        venta = (resp.json().get("venta") or {}).get("valor")
        if venta:
            out["CRC"] = float(venta)
    except Exception as e:
        log.error(f"Hacienda TC dolar fetch failed: {e}")
    try:
        resp = requests.get(HACIENDA_TC_EURO, timeout=30)
        resp.raise_for_status()
        usd_per_eur = resp.json().get("dolares")
        if usd_per_eur:
            out["EUR"] = 1.0 / float(usd_per_eur)
    except Exception as e:
        log.error(f"Hacienda TC euro fetch failed: {e}")
    return out


def get_latest_rates(days_back: int = 15) -> dict:
    """Fetch the latest non-null rate for every currency in code_list.

    Returns a dict mapping currency code → rate (units of `currency` per 1 USD).
    USD is always included with value 1.0. A currency's value is None when no
    source could provide it.
    """
    try:
        latest_rates = _rates_from_bccr(days_back)
    except Exception as e:
        log.error(f"BCCR SW fetch failed entirely: {e}")
        latest_rates = {code: None for code in code_list}
        latest_rates['USD'] = 1.0

    if any(latest_rates.get(c) is None for c in CRITICAL_CURRENCIES):
        fallback = _rates_from_hacienda()
        for code, value in fallback.items():
            if latest_rates.get(code) is None:
                latest_rates[code] = value
                log.info(f"Rate for {code} filled from Hacienda fallback: {value}")

    return latest_rates
