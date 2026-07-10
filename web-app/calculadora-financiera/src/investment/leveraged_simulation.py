# -*- coding: utf-8 -*-
"""
Simulación de historial "pre-inception" para ETFs apalancados (TQQQ 3x,
QLD 2x) que en la vida real no existen antes de 2010 y 2006.

Toda la data de esta app usa como fecha de referencia el 1999-03-10
(fecha real de listado de QQQ), así que el subyacente para simular el
tramo previo de TQQQ/QLD es siempre QQQ (no hace falta ningún otro
proxy: QQQ ya cubre exactamente desde esa fecha).

Metodología (aproximada, con supuestos ajustables) — fórmula típica de
un ETF apalancado con rebalanceo diario:

    retorno_diario_fondo = leverage * retorno_diario_QQQ
                           - (leverage - 1) * costo_financiamiento_diario
                           - gasto_anual_del_fondo_diario

La serie simulada se "ancla" (se escala) para que coincida exactamente
con el primer precio REAL del fondo en su fecha de listado real; desde
esa fecha en adelante se usa el precio REAL descargado de Yahoo.

Esto es una aproximación educativa del "decay" de los ETFs apalancados,
NO una réplica exacta de la mecánica de rebalanceo del fondo real.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from src.data.yahoo_client import get_daily_closes

FECHA_INCEPTION_GLOBAL = date(1999, 3, 10)

# El índice Nasdaq-100 (no el ETF) tiene historial en Yahoo Finance desde
# 1985-10-01 — mucho antes de que existiera QQQ — y se usa como proxy para
# simular el tramo previo de QQQ, capturando la subida y caída de la
# burbuja "puntocom" de finales de los 90 / inicios de los 2000.
NDX_TICKER = "^NDX"
FECHA_INICIO_NDX = date(1985, 10, 1)

LEVERAGE_CONFIG = {
    "TQQQ": {"leverage": 3.0},
    "QLD": {"leverage": 2.0},
}

DEFAULT_EXPENSE_RATIO_ANUAL_PCT = 0.95
DEFAULT_FINANCING_RATE_ANUAL_PCT = 4.5

TRADING_DAYS_PER_YEAR = 252


def construir_serie_diaria(
    ticker_apalancado: str,
    fecha_inicio: date,
    fecha_fin: date,
    expense_ratio_anual_pct: float = DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    financing_rate_anual_pct: float = DEFAULT_FINANCING_RATE_ANUAL_PCT,
    refresh: bool = False,
) -> tuple[pd.Series, date | None]:
    """
    Devuelve (serie_diaria_precio, fecha_inicio_datos_reales).

    Antes de `fecha_inicio_datos_reales` los precios son simulados sobre
    QQQ; desde esa fecha en adelante son precios reales de Yahoo Finance.
    """
    cfg = LEVERAGE_CONFIG.get(ticker_apalancado.upper())
    if cfg is None:
        raise ValueError(f"Ticker apalancado no soportado para simulación: {ticker_apalancado}")

    real = get_daily_closes(ticker_apalancado, refresh=refresh)
    real_inception_ts = real.index.min() if not real.empty else pd.Timestamp(fecha_fin)

    qqq = get_daily_closes("QQQ", refresh=refresh)
    retornos_qqq = qqq.pct_change().dropna()
    retornos_qqq = retornos_qqq[retornos_qqq.index <= real_inception_ts]

    if retornos_qqq.empty:
        return real, (real.index.min().date() if not real.empty else None)

    financing_diario = financing_rate_anual_pct / 100.0 / TRADING_DAYS_PER_YEAR
    expense_diario = expense_ratio_anual_pct / 100.0 / TRADING_DAYS_PER_YEAR
    leverage = cfg["leverage"]

    retorno_fondo_simulado = leverage * retornos_qqq - (leverage - 1) * financing_diario - expense_diario
    indice_simulado = (1.0 + retorno_fondo_simulado).cumprod()

    if not real.empty and not indice_simulado.empty:
        escala = float(real.iloc[0]) / float(indice_simulado.iloc[-1])
        simulado_escalado = indice_simulado * escala
        pre_inception = simulado_escalado[simulado_escalado.index < real_inception_ts]
        serie_final = pd.concat([pre_inception, real])
    else:
        serie_final = indice_simulado

    serie_final = serie_final[~serie_final.index.duplicated(keep="last")].sort_index()
    serie_final = serie_final[(serie_final.index.date >= fecha_inicio) & (serie_final.index.date <= fecha_fin)]
    serie_final.name = ticker_apalancado.upper()

    return serie_final, (real_inception_ts.date() if not real.empty else None)


def construir_serie_qqq_extendida(
    fecha_inicio: date,
    fecha_fin: date,
    refresh: bool = False,
) -> tuple[pd.Series, date | None]:
    """
    Extiende el precio de QQQ hacia atrás de su fecha real de listado
    (1999-03-10) usando el índice Nasdaq-100 (^NDX, disponible desde
    1985-10-01) como proxy, para capturar la subida y caída de la burbuja
    "puntocom". La serie simulada se ancla para calzar exactamente con el
    primer precio real de QQQ; desde su listado real en adelante se usa
    el precio real.

    Aviso: ^NDX es un índice de PRECIO (no incluye dividendos), así que
    el tramo simulado subestima levemente el retorno total real que
    QQQ (que sí reinvierte dividendos) hubiera tenido en esos años.
    """
    real = get_daily_closes("QQQ", refresh=refresh)
    real_inception_ts = real.index.min() if not real.empty else pd.Timestamp(fecha_fin)

    ndx = get_daily_closes(NDX_TICKER, refresh=refresh)
    retornos_ndx = ndx.pct_change().dropna()
    retornos_ndx = retornos_ndx[retornos_ndx.index <= real_inception_ts]

    if retornos_ndx.empty:
        real_filtrado = real[(real.index.date >= fecha_inicio) & (real.index.date <= fecha_fin)]
        return real_filtrado, (real.index.min().date() if not real.empty else None)

    indice_simulado = (1.0 + retornos_ndx).cumprod()

    if not real.empty and not indice_simulado.empty:
        escala = float(real.iloc[0]) / float(indice_simulado.iloc[-1])
        simulado_escalado = indice_simulado * escala
        pre_inception = simulado_escalado[simulado_escalado.index < real_inception_ts]
        serie_final = pd.concat([pre_inception, real])
    else:
        serie_final = indice_simulado

    serie_final = serie_final[~serie_final.index.duplicated(keep="last")].sort_index()
    serie_final = serie_final[(serie_final.index.date >= fecha_inicio) & (serie_final.index.date <= fecha_fin)]
    serie_final.name = "QQQ"

    return serie_final, (real_inception_ts.date() if not real.empty else None)
