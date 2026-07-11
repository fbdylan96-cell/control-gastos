# -*- coding: utf-8 -*-
"""
Laboratorio de ROTACIÓN DE SECTORES por momentum dual (Gary Antonacci).

Idea:
  • Momentum RELATIVO: cada fin de mes se rankean los 11 sectores del S&P 500 (ETFs Select Sector
    SPDR) por su momentum "lookback-skip" (p.ej. 12-1 = retorno de los últimos 12 meses saltando
    el más reciente) y se compra el/los mejor(es).
  • Momentum ABSOLUTO (la "lógica de las tasas del tesoro"): un sector solo se compra si su momentum
    supera el del efectivo (T-bills) sobre la misma ventana; si no, ese tramo va a efectivo. Así se
    evita estar invertido cuando hasta el mejor sector va peor que la letra del tesoro.

Reglas: mensual, sin look-ahead (la señal de fin de mes t se opera el mes t+1), costo por rotación.
Los ETFs arrancan en fechas distintas (los 9 originales en 1998-12; XLRE 2015-10; XLC 2018-06);
cada mes se rankean solo los sectores con historia suficiente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ETFs Select Sector SPDR del S&P 500 (ticker -> nombre)
SECTORES = {
    "XLK": "Tecnología",
    "XLF": "Financiero",
    "XLV": "Salud",
    "XLY": "Consumo discrecional",
    "XLP": "Consumo básico",
    "XLE": "Energía",
    "XLI": "Industrial",
    "XLB": "Materiales",
    "XLU": "Utilities",
    "XLRE": "Inmobiliario",
    "XLC": "Comunicaciones",
}


def _mensual(precio_diario: pd.Series) -> pd.Series:
    return precio_diario.resample("ME").last().dropna()


def precios_sectores_mensuales(cargar_fn, tickers: list[str] | None = None) -> pd.DataFrame:
    """
    DataFrame de cierres MENSUALES por sector. `cargar_fn` es la función que baja el cierre diario
    de un ticker (p.ej. `get_daily_closes`); se pasa desde la app para reutilizar su caché.
    """
    tickers = tickers or list(SECTORES.keys())
    series = {}
    for tk in tickers:
        try:
            px = cargar_fn(tk)
        except Exception:
            px = None
        if px is None or len(px) == 0:
            continue
        m = _mensual(px)
        if len(m) >= 13:
            series[tk] = m
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series).sort_index()
    return df


def momentum_score(precios_m: pd.DataFrame, lookback: int, skip: int) -> pd.DataFrame:
    """Momentum 'lookback-skip' por sector y mes: P(t-skip)/P(t-lookback) − 1 (conocido a fin de t)."""
    return precios_m.shift(skip) / precios_m.shift(lookback) - 1.0


def _momentum_cash(cash_m: pd.Series, lookback: int, skip: int) -> pd.Series:
    """Momentum del efectivo sobre la misma ventana (para el filtro de momentum absoluto)."""
    idx_cash = (1.0 + cash_m.fillna(0.0)).cumprod()
    return idx_cash.shift(skip) / idx_cash.shift(lookback) - 1.0


def backtest_rotacion(
    precios_m: pd.DataFrame, lookback: int = 12, skip: int = 1, top_k: int = 1,
    cash_m: pd.Series | None = None, momentum_absoluto: bool = True, costo: float = 0.001,
) -> dict:
    """
    Rotación mensual: compra el top-K de sectores por momentum; con `momentum_absoluto`, cada tramo
    cuyo sector no supere al efectivo se va a efectivo. Devuelve retornos, tenencias, % invertido, etc.
    """
    if precios_m is None or precios_m.empty:
        return {"ret": pd.Series(dtype=float), "holdings": {}, "pct_invertido": 0.0,
                "n_rotaciones": 0, "ret_ew": pd.Series(dtype=float)}
    ret_m = precios_m.pct_change()
    mom = momentum_score(precios_m, lookback, skip)
    idx = precios_m.index
    if cash_m is None:
        cash_m = pd.Series(0.0, index=idx)
    cash_al = cash_m.reindex(idx).fillna(0.0)
    mom_cash = _momentum_cash(cash_al, lookback, skip).reindex(idx)

    pesos = pd.DataFrame(0.0, index=idx, columns=precios_m.columns)
    holdings = {}
    for f in idx:
        fila = mom.loc[f].dropna()
        if momentum_absoluto and pd.notna(mom_cash.loc[f]):
            fila = fila[fila > mom_cash.loc[f]]  # solo sectores que le ganan a las T-bills
        elegidos = list(fila.sort_values(ascending=False).head(top_k).index)
        holdings[f] = elegidos
        if elegidos:
            pesos.loc[f, elegidos] = 1.0 / len(elegidos)

    aplicado = pesos.shift(1).fillna(0.0)            # se opera el mes siguiente a la señal
    peso_cash = (1.0 - aplicado.sum(axis=1)).clip(lower=0.0)
    ret_estrategia = (ret_m.reindex(idx) * aplicado).sum(axis=1) + peso_cash * cash_al
    rotacion = 0.5 * (aplicado - aplicado.shift(1).fillna(0.0)).abs().sum(axis=1)
    ret_estrategia = ret_estrategia - rotacion * costo

    # descartar el arranque sin momentum (primeros `lookback` meses)
    valido = mom.dropna(how="all").index
    if len(valido):
        ret_estrategia = ret_estrategia.loc[ret_estrategia.index >= valido.min()]
    ret_estrategia = ret_estrategia.iloc[1:].dropna()  # el 1er mes no tiene posición previa

    ret_ew = ret_m.mean(axis=1).reindex(ret_estrategia.index)
    n_invertido = float((aplicado.sum(axis=1) > 0).reindex(ret_estrategia.index).mean())
    n_rot = int((rotacion.reindex(ret_estrategia.index) > 1e-9).sum())
    return {"ret": ret_estrategia, "holdings": holdings, "pct_invertido": n_invertido,
            "n_rotaciones": n_rot, "ret_ew": ret_ew}


def tabla_holdings(holdings: dict, meses: int = 24) -> pd.DataFrame:
    """Últimos `meses` de tenencias (qué sector(es) se mantuvo cada mes)."""
    filas = []
    for f in sorted(holdings.keys())[-meses:]:
        secs = holdings[f]
        etiqueta = ", ".join(f"{s} ({SECTORES.get(s, s)})" for s in secs) if secs else "— Efectivo —"
        filas.append({"Mes": f.strftime("%Y-%m"), "Sector(es) en cartera": etiqueta})
    return pd.DataFrame(filas)


def barrido_ventanas(
    precios_m: pd.DataFrame, lookbacks: list[int], skips: list[int], top_k: int,
    cash_m: pd.Series, momentum_absoluto: bool, metrica: str = "Sharpe", costo: float = 0.001,
) -> pd.DataFrame:
    """
    Matriz lookback (meses, filas) × skip (meses, columnas) de la métrica pedida
    ("Sharpe" | "Calmar" | "CAGR (%)" | "Max DD (%)" | "% invertido"). Solo skip < lookback.
    """
    from src.investment import backtest_v2 as btv2

    grid = {}
    for lb in lookbacks:
        fila = {}
        for sk in skips:
            if sk >= lb:
                fila[sk] = np.nan
                continue
            res = backtest_rotacion(precios_m, lookback=lb, skip=sk, top_k=top_k, cash_m=cash_m,
                                    momentum_absoluto=momentum_absoluto, costo=costo)
            r = res["ret"].dropna()
            if r.empty:
                fila[sk] = np.nan
                continue
            ca = cash_m.reindex(r.index)
            if metrica == "Sharpe":
                fila[sk] = btv2.sharpe(r, ca, 12)
            elif metrica == "Calmar":
                fila[sk] = btv2.calmar(r, 12)
            elif metrica == "CAGR (%)":
                fila[sk] = btv2.cagr(r, 12) * 100.0
            elif metrica == "Max DD (%)":
                fila[sk] = btv2.max_drawdown(r) * 100.0
            else:
                fila[sk] = res["pct_invertido"] * 100.0
        grid[lb] = fila
    df = pd.DataFrame(grid).T
    df.index.name = "Lookback (m)"
    df.columns.name = "Skip (m)"
    return df
