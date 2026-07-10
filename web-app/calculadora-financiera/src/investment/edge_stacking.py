# -*- coding: utf-8 -*-
"""
Stacking de edges: combina varias estrategias (cada una con su serie de retornos MENSUALES)
en una cartera, con asignación de capital por peso, y analiza —como un analista— cuáles aportan
valor PROPIO y cuáles son redundantes (una repetición de otra), a partir de:

  • la matriz de correlación de sus retornos mensuales (período común),
  • el Sharpe de cada una por separado, y
  • su aporte MARGINAL al Sharpe de la cartera igual-peso (¿sube o baja el Sharpe si la saco?).

Regla de lectura: una estrategia con correlación alta (> ~0.85) con otra y menor Sharpe es
probablemente redundante; una con correlación baja/negativa o con aporte marginal positivo
diversifica de verdad.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.investment.backtest_v2 import cagr, max_drawdown, sharpe, vol_anualizada


def alinear_mensual(series: dict[str, pd.Series]) -> pd.DataFrame:
    """
    Junta las series de retornos mensuales en un DataFrame (una columna por estrategia),
    normalizando el índice a PERÍODO MENSUAL para que alineen aunque unas vengan con fecha a
    inicio de mes y otras a fin de mes (así la correlación no sale NaN por desalineación).
    """
    limpio = {}
    for k, v in series.items():
        if v is None:
            continue
        s = v.dropna()
        if s.empty:
            continue
        s = s.copy()
        s.index = pd.to_datetime(s.index).to_period("M")
        s = s[~s.index.duplicated(keep="last")]
        limpio[k] = s
    if not limpio:
        return pd.DataFrame()
    df = pd.DataFrame(limpio).sort_index()
    df.index = df.index.to_timestamp(how="end").normalize()
    return df


def matriz_correlacion(rets: pd.DataFrame) -> pd.DataFrame:
    """Correlación de los retornos mensuales sobre el período COMÚN (donde todas tienen dato)."""
    comun = rets.dropna(how="any")
    if len(comun) < 12:
        comun = rets  # si el común es muy corto, usa correlación par a par
    return comun.corr()


def metricas_standalone(rets: pd.DataFrame, cash: pd.Series, ppa: int = 12) -> pd.DataFrame:
    """CAGR/Vol/Sharpe/MaxDD de cada estrategia por separado."""
    filas = []
    for c in rets.columns:
        s = rets[c].dropna()
        if s.empty:
            continue
        filas.append({
            "Estrategia": c, "Desde": f"{s.index.min():%b %Y}", "CAGR (%)": cagr(s, ppa) * 100.0,
            "Vol (%)": vol_anualizada(s, ppa) * 100.0, "Sharpe": sharpe(s, cash.reindex(s.index), ppa),
            "Max DD (%)": max_drawdown(s) * 100.0,
        })
    return pd.DataFrame(filas)


def combinar(rets: pd.DataFrame, pesos: dict[str, float]) -> pd.Series:
    """Retorno mensual de la cartera = suma ponderada (rebalanceo mensual a los pesos)."""
    w = pd.Series({k: v for k, v in pesos.items() if k in rets.columns and v > 0}, dtype=float)
    if w.empty:
        return pd.Series(dtype=float)
    w = w / w.sum()
    sub = rets[w.index]
    # cada mes, promedio ponderado de las estrategias con dato ese mes (renormalizando)
    wmat = sub.notna().mul(w, axis=1)
    wmat = wmat.div(wmat.sum(axis=1).replace(0, np.nan), axis=0)
    return (sub.fillna(0.0) * wmat).sum(axis=1).where(sub.notna().any(axis=1)).dropna()


def veredicto_redundancia(rets: pd.DataFrame, cash: pd.Series, ppa: int = 12,
                          umbral_redundante: float = 0.85) -> tuple[pd.DataFrame, float]:
    """
    Por estrategia: Sharpe propio, su correlación máxima con otra (y con cuál), y su APORTE
    marginal al Sharpe de la cartera igual-peso (Sharpe con todas − Sharpe sin ella). Veredicto:
      🔴 Redundante  — corr > umbral con otra y Sharpe menor que esa otra (repite y aporta menos).
      🟢 Aporta valor — baja correlación (< 0.5) o aporte marginal positivo claro.
      🟡 Marginal    — ni una cosa ni la otra.
    Devuelve (tabla, Sharpe de la cartera igual-peso).
    """
    df = rets.dropna(how="any")
    if df.shape[1] < 2 or len(df) < 12:
        return pd.DataFrame(), float("nan")
    corr = df.corr()
    cols = list(df.columns)
    ew_all = df.mean(axis=1)
    sh_all = sharpe(ew_all, cash.reindex(ew_all.index), ppa)
    sh_solo = {c: sharpe(df[c], cash.reindex(df.index), ppa) for c in cols}

    filas = []
    for c in cols:
        otras = [o for o in cols if o != c]
        cmax = max((corr.loc[c, o] for o in otras), default=0.0)
        quien = max(otras, key=lambda o: corr.loc[c, o]) if otras else "—"
        ew_sin = df[otras].mean(axis=1)
        sh_sin = sharpe(ew_sin, cash.reindex(ew_sin.index), ppa)
        marginal = sh_all - sh_sin  # cuánto sube el Sharpe combinado por incluirla
        # Veredicto por CORRELACIÓN (¿es una repetición de otra?):
        if cmax > umbral_redundante:
            ver = "🔴 Redundante (≈ copia)"
        elif cmax < 0.5:
            ver = "🟢 Diversifica (apuesta distinta)"
        else:
            ver = "🟡 Parcialmente solapada"
        filas.append({
            "Estrategia": c, "Sharpe solo": sh_solo[c], "Máx correlación": cmax, "…con": quien,
            "Aporte al Sharpe combinado": marginal, "Veredicto": ver,
        })
    tabla = pd.DataFrame(filas).sort_values("Aporte al Sharpe combinado", ascending=False).reset_index(drop=True)
    return tabla, sh_all
