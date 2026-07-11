# -*- coding: utf-8 -*-
"""
Módulo 7 — Shareholder yield (recompras + dividendos).

Restricción de datos (define el diseño): los datos de recompras por empresa vienen de los
cash flow statements (Compustat/FactSet, de pago). Con datos GRATIS no se puede reconstruir
honestamente un ranking histórico de buyback yield por acción sin sesgo. Por eso este módulo
NO replica el factor a nivel de acción ni usa las recompras como señal de timing del mercado.
Lo que SÍ se puede medir con rigor:

  7A — El factor vía ETFs: PKW (Buyback Achievers, 2006+) y SYLD (Cambria Shareholder Yield,
       2013+) contra SPY y contra RSP (S&P 500 equal weight — control clave: ¿el exceso es
       'shareholder yield' o sólo 'equal weight / tamaño'?). Métricas, rolling 3 años, capture.

  7B — Verificación a nivel empresa (piloto Dow 30, universo chico y estable): reducción % de
       acciones en circulación en ventanas de 12 meses (buyback yield efectivo, neto de
       emisiones) vía yfinance `get_shares_full`. Cada año: mitad reductora vs mitad diluidora,
       retorno del año siguiente. Muestra chica → no sobrevender la conclusión.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.investment import backtest_v2 as btv2
from src.data.yahoo_client import get_daily_closes

ETFS_7A = ["PKW", "SYLD", "SPY", "RSP"]

# Dow 30 actual (lista chica y estable; minimiza el sesgo de supervivencia).
DOW30 = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]


# ===========================================================================
# 7A — EL FACTOR VÍA ETFs
# ===========================================================================
def retornos_7a() -> pd.DataFrame:
    """Retornos mensuales de PKW, SYLD, SPY, RSP (los que tengan datos)."""
    datos = {}
    for t in ETFS_7A:
        r = btv2.precio_mensual(t).pct_change().dropna()
        if not r.empty:
            datos[t] = r
    return pd.DataFrame(datos)


def capture_ratios(estrategia: pd.Series, benchmark: pd.Series) -> dict:
    """Up/down capture: cuánto captura la estrategia de los meses de subida y de bajada del bench."""
    par = pd.concat([estrategia, benchmark], axis=1).dropna()
    par.columns = ["e", "b"]
    up = par[par["b"] > 0]
    dn = par[par["b"] < 0]
    up_cap = (up["e"].mean() / up["b"].mean()) if len(up) and up["b"].mean() != 0 else np.nan
    dn_cap = (dn["e"].mean() / dn["b"].mean()) if len(dn) and dn["b"].mean() != 0 else np.nan
    return {"Up capture": up_cap, "Down capture": dn_cap}


def rolling_3y(ret: pd.Series) -> pd.Series:
    """Retorno anualizado rodante de 3 años (36 meses)."""
    return (1.0 + ret).rolling(36).apply(np.prod, raw=True) ** (12.0 / 36.0) - 1.0


# ===========================================================================
# 7B — VERIFICACIÓN A NIVEL EMPRESA (piloto Dow 30)
# ===========================================================================
def acciones_en_circulacion(ticker: str) -> pd.Series:
    """Historia de acciones en circulación (get_shares_full de yfinance). Serie anual (fin de año)."""
    try:
        import yfinance as yf
        s = yf.Ticker(ticker).get_shares_full(start="2000-01-01")
        if s is None or len(s) == 0:
            return pd.Series(dtype=float)
        s = pd.Series(s).dropna()
        s.index = pd.to_datetime(s.index)
        return s.resample("YE").last().dropna().rename(ticker)
    except Exception:
        return pd.Series(dtype=float)


def buyback_yield_dow30(tickers: list[str] | None = None) -> pd.DataFrame:
    """
    Reducción % de acciones en circulación año contra año (buyback yield efectivo, neto de
    emisiones) por empresa. Devuelve un DataFrame (años × tickers) con el % de reducción
    (positivo = recompró/redujo acciones).
    """
    tickers = tickers or DOW30
    series = {}
    for t in tickers:
        s = acciones_en_circulacion(t)
        if len(s) >= 3:
            series[t] = s
    if not series:
        return pd.DataFrame()
    shares = pd.DataFrame(series)
    shares.index = shares.index.year
    shares = shares[~shares.index.duplicated(keep="last")]
    reduccion = -(shares.pct_change()) * 100.0  # reducción positiva = menos acciones
    return reduccion


def test_reductores_vs_diluidores(reduccion: pd.DataFrame) -> pd.DataFrame:
    """
    Cada año: separar en mitad reductora vs mitad diluidora (por buyback yield del año), y comparar
    el retorno del AÑO SIGUIENTE de cada mitad (igual peso). Una fila por año con ambos retornos.
    """
    if reduccion.empty:
        return pd.DataFrame()
    # retornos anuales por ticker (calendario)
    px = {t: get_daily_closes(t) for t in reduccion.columns}
    ret_anual = {}
    for t, s in px.items():
        if s is None or s.empty:
            continue
        anual = s.resample("YE").last().pct_change()
        anual.index = anual.index.year
        ret_anual[t] = anual
    ret_df = pd.DataFrame(ret_anual)
    filas = []
    for anio in reduccion.index:
        fila_red = reduccion.loc[anio].dropna()
        if len(fila_red) < 6 or (anio + 1) not in ret_df.index:
            continue
        ret_sig = ret_df.loc[anio + 1]
        med = fila_red.median()
        reductores = fila_red[fila_red >= med].index
        diluidores = fila_red[fila_red < med].index
        r_red = ret_sig.reindex(reductores).dropna()
        r_dil = ret_sig.reindex(diluidores).dropna()
        if len(r_red) == 0 or len(r_dil) == 0:
            continue
        filas.append({"Año señal": int(anio), "Reductores (%)": float(r_red.mean()) * 100.0,
                      "Diluidores (%)": float(r_dil.mean()) * 100.0,
                      "Diferencia (pp)": (float(r_red.mean()) - float(r_dil.mean())) * 100.0,
                      "n": len(fila_red)})
    return pd.DataFrame(filas)
