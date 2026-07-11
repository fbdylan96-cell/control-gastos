# -*- coding: utf-8 -*-
from datetime import date as _date

import pandas as pd


def xirr(flujos: list[tuple[_date, float]]) -> float:
    """
    Retorno anualizado ponderado por dinero (money-weighted rate of
    return), la forma correcta de calcular un "CAGR realizado" cuando
    hay varios aportes en fechas distintas — a diferencia de un CAGR
    simple (primer valor vs. último valor), esto SÍ toma en cuenta
    cuánto y cuándo se aportó cada vez.

    `flujos`: lista de (fecha, monto). Los aportes van en NEGATIVO (salen
    del bolsillo del cliente) y el valor final va en POSITIVO (como si
    se liquidara ese día). Se resuelve por bisección la tasa `r` que hace
    que el valor presente neto de todos los flujos sea cero.
    """
    if len(flujos) < 2:
        return 0.0

    flujos_ordenados = sorted(flujos, key=lambda f: f[0])
    fecha0 = flujos_ordenados[0][0]

    def van(tasa: float) -> float:
        total = 0.0
        for fecha, monto in flujos_ordenados:
            anios = (fecha - fecha0).days / 365.25
            total += monto / (1.0 + tasa) ** anios
        return total

    lo, hi = -0.9999, 20.0  # -99.99% a 2000% anual, rango amplio de sobra
    van_lo, van_hi = van(lo), van(hi)
    if van_lo * van_hi > 0:
        return 0.0  # no se pudo acotar una raíz (caso degenerado, p.ej. todo cero)

    for _ in range(200):
        mid = (lo + hi) / 2.0
        van_mid = van(mid)
        if abs(van_mid) < 1e-6:
            return mid
        if van_lo * van_mid < 0:
            hi = mid
        else:
            lo, van_lo = mid, van_mid

    return (lo + hi) / 2.0


def cagr(balance_series: pd.Series, date_series: pd.Series) -> float:
    """
    CAGR usando la primera fecha con saldo positivo y la última fecha.

    El saldo puede arrancar en $0 (p.ej. si el aporte inicial no alcanza
    a cubrir un fee de apertura), así que se busca el primer valor > 0
    en vez de dividir directamente entre balance_series.iloc[0].
    """
    if len(balance_series) < 2:
        return 0.0

    valores = balance_series.reset_index(drop=True)
    fechas = date_series.reset_index(drop=True)

    positivos = valores > 0
    if not positivos.any():
        return 0.0

    inicio_pos = positivos.idxmax()
    valor_inicial = valores.iloc[inicio_pos]
    valor_final = valores.iloc[-1]
    if valor_final <= 0:
        return 0.0

    years = (fechas.iloc[-1] - fechas.iloc[inicio_pos]).days / 365.25
    if years <= 0:
        return 0.0
    return (valor_final / valor_inicial) ** (1 / years) - 1


def max_drawdown(balance_series: pd.Series) -> float:
    """
    Max drawdown (como número negativo).
    """
    if balance_series.empty:
        return 0.0
    return (balance_series / balance_series.cummax() - 1).min()
