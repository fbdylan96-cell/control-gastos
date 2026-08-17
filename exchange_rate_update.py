"""Fetch de tipos de cambio.

Dos fuentes, en capas:

1. **Ministerio de Hacienda** (api.hacienda.go.cr/indicadores/tc) — fuente
   AUTORITATIVA para CRC y EUR. Publica el tipo de cambio de referencia del
   BCCR, que es el número que usan los bancos y la contabilidad en Costa Rica.
     * /dolar → venta.valor = CRC por USD (misma convención de rate_vs_usd)
     * /euro  → dolares = USD por EUR → rate_vs_usd[EUR] = 1/dolares
2. **open.er-api.com** — rellena las demás monedas y actúa de respaldo si
   Hacienda falla. Entrega las tasas con base USD, igual que rate_vs_usd, así
   que no hay conversiones de por medio. Sin API key.

Hacienda SIEMPRE gana sobre open.er-api para CRC y EUR: open.er-api es un
agregado de mercado y se parece mucho (verificado: diferencias por debajo del
0,15 %), pero para la moneda con la que se hace la mayoría de las
transacciones, "se parece" no es el estándar correcto. La fuente de mercado
solo entra donde no hay dato oficial, o cuando el oficial no responde — mejor
una tasa de mercado de hoy que una oficial de hace un mes.

Historia: hasta 2026-08-16 la fuente primaria era el Servicio Web legacy del
BCCR vía el paquete `bccr` (que NO es oficial del Banco Central: lo mantiene un
profesor de la UCR). Ese servicio responde "Service Unavailable" desde
2026-07-20 — el BCCR migró a sdd.bccr.fi.cr sin publicar un API equivalente —
así que llevaba un mes fallando en cada corrida y dejó 41 monedas congeladas.
Se retiró el intento: no aportaba y arrastraba pandas y Dash al importarse.
"""
import logging

import requests

log = logging.getLogger(__name__)

# Universo de monedas que seguimos. Viene del catálogo que publicaba el
# servicio del BCCR; se conserva tal cual para no cambiar lo que ya está en
# core.exchange_rates. VERIFICADO 2026-08-16: open.er-api cubre las 44.
TRACKED_CURRENCIES = (
    'USD', 'CRC', 'EUR', 'JPY', 'CHF', 'CAD', 'MXN', 'SEK', 'KRW', 'GTQ',
    'HNL', 'NIO', 'DKK', 'NOK', 'ARS', 'COP', 'BRL', 'DOP', 'HKD', 'TWD',
    'BOB', 'CLP', 'RUB', 'PEN', 'CNY', 'PLN', 'LKR', 'BDT', 'THB', 'IDR',
    'AED', 'MAD', 'ILS', 'INR', 'EGP', 'NZD', 'SGD', 'VND', 'ZAR', 'JOD',
    'MYR', 'BHD', 'VES', 'UYU',
)

# Monedas imprescindibles para el negocio: CRC convierte TODO a colones y EUR
# es la única otra divisa con uso real. rate_scheduler alerta por correo solo
# cuando una de ESTAS falta tras agotar las dos fuentes (las demás se loguean).
CRITICAL_CURRENCIES = ('CRC', 'EUR')

HACIENDA_TC_DOLAR = "https://api.hacienda.go.cr/indicadores/tc/dolar"
HACIENDA_TC_EURO = "https://api.hacienda.go.cr/indicadores/tc/euro"
OPEN_ER_LATEST_USD = "https://open.er-api.com/v6/latest/USD"


def _valid_rate(value):
    """Una tasa utilizable: número finito y estrictamente positivo.

    Nunca guardar 0 ni negativos: se propagarían como divisiones por cero o
    montos absurdos en amount_local, y peor, en silencio.
    """
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate != rate or rate in (float("inf"), float("-inf")) or rate <= 0:
        return None
    return rate


def _rates_from_hacienda() -> dict:
    """CRC y EUR desde el Ministerio de Hacienda (referencia oficial del BCCR)."""
    out = {}
    try:
        resp = requests.get(HACIENDA_TC_DOLAR, timeout=30)
        resp.raise_for_status()
        # venta.valor ya viene como CRC por 1 USD.
        rate = _valid_rate((resp.json().get("venta") or {}).get("valor"))
        if rate:
            out["CRC"] = rate
    except Exception as e:
        log.error(f"Hacienda TC dolar fetch failed: {e}")
    try:
        resp = requests.get(HACIENDA_TC_EURO, timeout=30)
        resp.raise_for_status()
        # 'dolares' son USD por 1 EUR: hay que invertirlo a EUR por 1 USD.
        usd_per_eur = _valid_rate(resp.json().get("dolares"))
        if usd_per_eur:
            out["EUR"] = 1.0 / usd_per_eur
    except Exception as e:
        log.error(f"Hacienda TC euro fetch failed: {e}")
    return out


def _rates_from_open_er() -> dict:
    """Todas las monedas seguidas desde open.er-api (base USD, sin API key).

    La respuesta trae `result: "success"`; un HTTP 200 con result distinto es un
    error de la API y no se debe leer como datos.
    """
    out = {}
    try:
        resp = requests.get(OPEN_ER_LATEST_USD, timeout=30,
                            headers={"User-Agent": "neto/1.0"})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("result") != "success":
            log.error(f"open.er-api devolvió result={payload.get('result')!r}: "
                      f"{payload.get('error-type')}")
            return out
        rates = payload.get("rates") or {}
        for code in TRACKED_CURRENCIES:
            rate = _valid_rate(rates.get(code))
            if rate:
                out[code] = rate
        log.info(f"open.er-api: {len(out)} de {len(TRACKED_CURRENCIES)} monedas "
                 f"(actualizado {payload.get('time_last_update_utc')})")
    except Exception as e:
        log.error(f"open.er-api fetch failed: {e}")
    return out


def get_latest_rates() -> dict:
    """Tasa más reciente de cada moneda de TRACKED_CURRENCIES.

    Devuelve {moneda: tasa} donde la tasa son unidades de esa moneda por 1 USD.
    USD siempre vale 1.0. El valor es None cuando ninguna fuente la entregó —
    rate_scheduler NO las inserta como NULL (incidente 2026-07-09/10): las
    conversiones usan la última fecha con datos válidos.
    """
    rates = {code: None for code in TRACKED_CURRENCIES}
    rates["USD"] = 1.0

    # 1. Oficial primero, para que nada lo pueda pisar después.
    for code, value in _rates_from_hacienda().items():
        rates[code] = value
        log.info(f"Rate for {code} from Hacienda (oficial): {value}")

    # 2. El resto, y respaldo de las oficiales si Hacienda no respondió.
    faltantes = [c for c in TRACKED_CURRENCIES if rates[c] is None]
    if faltantes:
        for code, value in _rates_from_open_er().items():
            if rates[code] is None:
                rates[code] = value
        recuperadas = [c for c in CRITICAL_CURRENCIES if c in faltantes
                       and rates[c] is not None]
        if recuperadas:
            log.warning("Hacienda no respondió; estas monedas críticas se "
                        f"llenaron con la tasa de mercado de open.er-api: "
                        f"{', '.join(recuperadas)}")

    return rates
