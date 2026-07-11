# -*- coding: utf-8 -*-
"""
Estrategias de "edge" de corto plazo sobre índices (SPY, QQQ), a partir de datos
diarios OHLC:

  • Trade base — qué tramo del día se captura cada vez que se está "dentro":
      - overnight   : comprar al cierre, vender a la apertura del día siguiente.
      - intraday    : comprar a la apertura, vender al cierre (del día siguiente al
                      de decisión, para no mirar el futuro).
      - close_close : comprar al cierre, vender al cierre del día siguiente (mantener).

  • Filtros de posición (largo/flat, sin cortos) que se combinan por AND:
      - Floor trader pivots (P, R1, S1) calculados con el día ANTERIOR y comparados
        contra el Close (u Open) del día — NO como stop intradía, solo como nivel.
      - RSI stacking: RSI de varios períodos; largo si TODOS > umbral, flat si TODOS
        < umbral (sostiene el estado en el medio).

Regla anti-look-ahead: la posición del día t se decide con información disponible al
cierre de t (precio_t y pivot_t, que se calculó con t−1), y el retorno capturado es el
del período siguiente (t → t+1). Así, combinar "Close > R1 → tomar el overnight" es
honesto: se decide al cierre y se opera el tramo siguiente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.finance.metrics import max_drawdown

MODOS_BASE = ["overnight", "intraday", "close_close"]
NIVELES_PIVOT = ["P", "R1", "S1"]


def retornos_base(ohlc: pd.DataFrame, modo: str) -> pd.Series:
    """
    Retorno del trade base indexado en la fecha de DECISIÓN t (se realiza en t→t+1):
      overnight   = Open_{t+1} / Close_t − 1
      intraday    = Close_{t+1} / Open_{t+1} − 1
      close_close = Close_{t+1} / Close_t − 1
    """
    o, c = ohlc["Open"], ohlc["Close"]
    if modo == "overnight":
        r = o.shift(-1) / c - 1.0
    elif modo == "intraday":
        r = c.shift(-1) / o.shift(-1) - 1.0
    else:  # close_close
        r = c.shift(-1) / c - 1.0
    return r.rename("base")


def pivots_diarios(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Floor trader pivots para el día t, calculados con el H/L/C de t−1:
      P = (H+L+C)/3 · R1 = 2P − L · S1 = 2P − H
    """
    prev = ohlc.shift(1)
    p = (prev["High"] + prev["Low"] + prev["Close"]) / 3.0
    r1 = 2.0 * p - prev["Low"]
    s1 = 2.0 * p - prev["High"]
    return pd.DataFrame({"P": p, "R1": r1, "S1": s1})


def rsi(close: pd.Series, periodo: int) -> pd.Series:
    """RSI de Wilder (suavizado exponencial con alpha = 1/periodo)."""
    delta = close.diff()
    ganancia = delta.clip(lower=0.0)
    perdida = (-delta).clip(lower=0.0)
    avg_g = ganancia.ewm(alpha=1.0 / periodo, adjust=False, min_periods=periodo).mean()
    avg_p = perdida.ewm(alpha=1.0 / periodo, adjust=False, min_periods=periodo).mean()
    rs = avg_g / avg_p.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[avg_p == 0.0] = 100.0  # sin pérdidas → RSI 100
    return out.rename(f"RSI{periodo}")


def senal_pivote(
    precio_decision: pd.Series, pivots: pd.DataFrame, nivel_entrada: str, nivel_salida: str
) -> pd.Series:
    """
    Señal largo/flat CON HISTÉRESIS: entra cuando `precio_decision` sube por encima del
    `nivel_entrada` (P/R1/S1) y sale cuando cae por debajo del `nivel_salida`. Entre
    ambos, sostiene el estado. Booleano indexado como el precio.
    """
    p = precio_decision.to_numpy(dtype=float)
    ent = pivots[nivel_entrada].to_numpy(dtype=float)
    sal = pivots[nivel_salida].to_numpy(dtype=float)
    n = len(p)
    out = np.zeros(n, dtype=bool)
    dentro = False
    for i in range(n):
        if not dentro:
            if not np.isnan(ent[i]) and p[i] > ent[i]:
                dentro = True
        else:
            if not np.isnan(sal[i]) and p[i] < sal[i]:
                dentro = False
        out[i] = dentro
    return pd.Series(out, index=precio_decision.index, name="senal_pivote")


def sma(close: pd.Series, periodo: int) -> pd.Series:
    """Promedio móvil simple de `periodo` días."""
    return close.rolling(periodo, min_periods=periodo).mean()


def senal_triple(
    close: pd.Series, sma_corta: int, sma_larga: int, rsi_periodo: int, umbral: float = 50.0
) -> pd.Series:
    """
    Señal largo/flat de TRIPLE CONFIRMACIÓN: largo cuando SE CUMPLEN LAS TRES a la vez —
    Close > SMA(sma_corta), Close > SMA(sma_larga) y RSI(rsi_periodo) > umbral. Si CUALQUIERA
    falla → flat (a efectivo). Booleano indexado como el cierre.
    """
    sc = sma(close, sma_corta)
    sl = sma(close, sma_larga)
    r = rsi(close, rsi_periodo)
    cond = (close > sc) & (close > sl) & (r > umbral)
    return cond.fillna(False).rename("senal_triple")


def senal_momentum_edge(
    close: pd.Series, high: pd.Series, mom_periodo: int = 5, sma_periodo: int = 200, candle_lookback: int = 1
) -> pd.Series:
    """
    Suma de tres edges (todas deben cumplirse → largo; si cualquiera falla → flat):
      • Momentum: Close > Close de hace `mom_periodo` barras (indicador de momentum clásico).
      • Tendencia: Close > SMA(`sma_periodo`).
      • Candela: Close > High de hace `candle_lookback` barras (ruptura alcista).
    Booleano indexado como el cierre.
    """
    cond_mom = close > close.shift(mom_periodo)
    cond_sma = close > sma(close, sma_periodo)
    cond_candle = close > high.shift(candle_lookback)
    return (cond_mom & cond_sma & cond_candle).fillna(False).rename("senal_momedge")


def senal_rsi_stack(close: pd.Series, periodos: list[int], umbral: float = 50.0) -> pd.Series:
    """
    Señal largo/flat por 'stacking' de RSI: largo cuando TODOS los RSI de `periodos`
    están por encima de `umbral`, flat cuando TODOS están por debajo; en el medio,
    sostiene el estado anterior. Booleano indexado como el cierre.
    """
    rsis = pd.DataFrame({f"r{p}": rsi(close, p) for p in periodos})
    todos_arriba = (rsis > umbral).all(axis=1)
    todos_abajo = (rsis < umbral).all(axis=1)
    validos = rsis.notna().all(axis=1)
    ta = todos_arriba.to_numpy()
    tb = todos_abajo.to_numpy()
    ok = validos.to_numpy()
    out = np.zeros(len(close), dtype=bool)
    dentro = False
    for i in range(len(close)):
        if ok[i]:
            if ta[i]:
                dentro = True
            elif tb[i]:
                dentro = False
        out[i] = dentro
    return pd.Series(out, index=close.index, name="senal_rsi")


def backtest_edge(
    ohlc: pd.DataFrame,
    modo_base: str = "overnight",
    precio_decision: str = "Close",
    usar_pivote: bool = False,
    nivel_entrada: str = "R1",
    nivel_salida: str = "S1",
    usar_rsi: bool = False,
    periodos_rsi: list[int] | None = None,
    umbral_rsi: float = 50.0,
    usar_triple: bool = False,
    sma_corta: int = 10,
    sma_larga: int = 100,
    rsi_triple_periodo: int = 14,
    umbral_triple: float = 50.0,
    usar_momedge: bool = False,
    mom_periodo: int = 5,
    sma_edge_periodo: int = 200,
    candle_lookback: int = 1,
) -> dict:
    """
    Combina el trade base con los filtros habilitados (AND): largo solo si todas las
    condiciones activas dicen largo; si no, a efectivo (0%). Devuelve un dict con:
      ret_estrategia : retornos diarios de la estrategia (indexados en la fecha de decisión)
      ret_bh         : retornos diarios de comprar y mantener el activo (close→close)
      posicion       : serie booleana largo/flat
      pct_invertido  : fracción de días con posición
    """
    if ohlc.empty or len(ohlc) < 3:
        return {"ret_estrategia": pd.Series(dtype=float), "ret_bh": pd.Series(dtype=float),
                "posicion": pd.Series(dtype=bool), "pct_invertido": 0.0}

    base = retornos_base(ohlc, modo_base)
    posicion = pd.Series(True, index=ohlc.index)

    if usar_pivote:
        precio_dec = ohlc[precio_decision] if precio_decision in ohlc.columns else ohlc["Close"]
        posicion &= senal_pivote(precio_dec, pivots_diarios(ohlc), nivel_entrada, nivel_salida)
    if usar_rsi:
        posicion &= senal_rsi_stack(ohlc["Close"], periodos_rsi or [10, 30, 60, 80], umbral_rsi)
    if usar_triple:
        posicion &= senal_triple(ohlc["Close"], sma_corta, sma_larga, rsi_triple_periodo, umbral_triple)
    if usar_momedge:
        posicion &= senal_momentum_edge(ohlc["Close"], ohlc["High"], mom_periodo, sma_edge_periodo, candle_lookback)

    ret_estrategia = (base.where(posicion, 0.0)).rename("estrategia")
    ret_bh = retornos_base(ohlc, "close_close").rename("buy_hold")

    # alinear al tramo con dato realizado (el último día no tiene t+1)
    validos = base.notna()
    ret_estrategia = ret_estrategia[validos].dropna()
    ret_bh = ret_bh.reindex(ret_estrategia.index)
    posicion = posicion.reindex(ret_estrategia.index).fillna(False)

    return {
        "ret_estrategia": ret_estrategia,
        "ret_bh": ret_bh,
        "posicion": posicion,
        "pct_invertido": float(posicion.mean()) if len(posicion) else 0.0,
    }


def _metricas_diarias(ret: pd.Series) -> dict:
    """CAGR/Vol/Sharpe/Max DD anualizados (252) de una serie de retornos diarios."""
    r = ret.dropna()
    if len(r) < 20:
        return {"CAGR (%)": float("nan"), "Vol (%)": float("nan"), "Sharpe": float("nan"), "Max DD (%)": float("nan")}
    eq = (1.0 + r).cumprod()
    n = len(r)
    cagr = (eq.iloc[-1] ** (252.0 / n) - 1.0) * 100.0 if eq.iloc[-1] > 0 else float("nan")
    vol = float(r.std()) * (252 ** 0.5) * 100.0
    sharpe = (float(r.mean()) / float(r.std()) * (252 ** 0.5)) if r.std() > 0 else float("nan")
    return {"CAGR (%)": cagr, "Vol (%)": vol, "Sharpe": sharpe, "Max DD (%)": max_drawdown(eq) * 100.0}


def sweep_momedge(
    ohlc: pd.DataFrame,
    modo_base: str,
    precio_decision: str,
    mom_periodos: list[int],
    sma_periodos: list[int],
    candle_lookbacks: list[int],
) -> pd.DataFrame:
    """
    Barrido de la suma Momentum + Tendencia (SMA) + Candela sobre cada combinación de
    (barras de momentum, período de SMA, lookback de la candela). Una fila por combinación.
    """
    filas = []
    for mp in mom_periodos:
        for sp in sma_periodos:
            for cl in candle_lookbacks:
                res = backtest_edge(
                    ohlc, modo_base, precio_decision, usar_momedge=True,
                    mom_periodo=mp, sma_edge_periodo=sp, candle_lookback=cl,
                )
                ret = res["ret_estrategia"]
                if len(ret) < 60:
                    continue
                fila = {"Momentum (barras)": mp, "SMA": sp, "Candela (High −N)": cl}
                fila.update(_metricas_diarias(ret))
                fila["% invertido"] = res["pct_invertido"] * 100.0
                filas.append(fila)
    return pd.DataFrame(filas)


def sweep_rsi_stack(
    ohlc: pd.DataFrame,
    modo_base: str,
    precio_decision: str,
    sets_periodos: list[list[int]],
    umbrales: list[float],
) -> pd.DataFrame:
    """
    Barrido del RSI stacking sobre cada combinación de (conjunto de períodos, umbral). Una
    fila por combinación con las métricas anualizadas y el % de días invertido.
    """
    filas = []
    for ps in sets_periodos:
        if not ps:
            continue
        for um in umbrales:
            res = backtest_edge(ohlc, modo_base, precio_decision, usar_rsi=True, periodos_rsi=list(ps), umbral_rsi=um)
            ret = res["ret_estrategia"]
            if len(ret) < 60:
                continue
            fila = {"Períodos": ",".join(str(p) for p in ps), "Umbral": um}
            fila.update(_metricas_diarias(ret))
            fila["% invertido"] = res["pct_invertido"] * 100.0
            filas.append(fila)
    return pd.DataFrame(filas)


def sweep_triple(
    ohlc: pd.DataFrame,
    modo_base: str,
    precio_decision: str,
    smas_cortas: list[int],
    smas_largas: list[int],
    rsi_periodos: list[int],
    umbrales: list[float],
) -> pd.DataFrame:
    """
    Barrido del edge de triple confirmación sobre todas las combinaciones de (SMA corta,
    SMA larga, RSI período, umbral). Una fila por combinación con las métricas y el % de
    días invertido. Solo se incluyen combinaciones con SMA corta < SMA larga.
    """
    filas = []
    for sc in smas_cortas:
        for sl in smas_largas:
            if sc >= sl:
                continue
            for rp in rsi_periodos:
                for um in umbrales:
                    res = backtest_edge(
                        ohlc, modo_base, precio_decision, usar_triple=True,
                        sma_corta=sc, sma_larga=sl, rsi_triple_periodo=rp, umbral_triple=um,
                    )
                    ret = res["ret_estrategia"]
                    if len(ret) < 60:
                        continue
                    fila = {"SMA corta": sc, "SMA larga": sl, "RSI período": rp, "Umbral": um}
                    fila.update(_metricas_diarias(ret))
                    fila["% invertido"] = res["pct_invertido"] * 100.0
                    filas.append(fila)
    return pd.DataFrame(filas)
