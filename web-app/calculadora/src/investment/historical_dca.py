# -*- coding: utf-8 -*-
"""
Simulación histórica real de aportes periódicos (DCA) sobre un ticker,
usando precios reales descargados y cacheados desde Yahoo Finance
(ajustados por splits Y dividendos reinvertidos).

Incluye los costos del servicio de inversión: un fee de apertura (setup,
único, descontado del aporte inicial) y un management fee (% anual,
cobrado mes a mes sobre el valor de la cartera, vendiendo una fracción
de las participaciones equivalente al cobro).

Toda la data de esta app arranca en FECHA_INCEPTION_GLOBAL (1999-03-10,
fecha real de listado de QQQ), salvo que se pida una fecha anterior para
QQQ, en cuyo caso se extiende con el índice Nasdaq-100 (ver
src/investment/leveraged_simulation.py). Para TQQQ y QLD, cuyo historial
real es más corto (desde 2010 y 2006), el tramo anterior a su fecha de
listado se completa con una simulación del ETF apalancado sobre QQQ.

El retorno anualizado (`retorno_anualizado_pct`) se calcula con XIRR
(retorno ponderado por dinero), NO con un CAGR simple de primer-valor
contra último-valor. Esto importa mucho en un DCA con varios aportes: un
CAGR simple ignora todo el dinero aportado después del primero, y puede
dar resultados sin sentido (p.ej. mostrar una ganancia grande cuando en
realidad el valor final es prácticamente igual a lo aportado, porque el
aporte inicial cayó justo antes de una crisis y se recuperó con dinero
metido después). El XIRR sí toma en cuenta cuánto y cuándo se aportó
cada vez.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from src.data.yahoo_client import get_daily_closes
from src.finance.annuity import FREQ_TO_MONTHS
from src.finance.metrics import max_drawdown, xirr
from src.investment.leveraged_simulation import (
    DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    DEFAULT_FINANCING_RATE_ANUAL_PCT,
    FECHA_INCEPTION_GLOBAL,
    construir_serie_diaria,
    construir_serie_qqq_extendida,
)

TICKERS_DISPONIBLES = ["QQQ", "QLD", "TQQQ", "SPY"]
TICKERS_SIMULADOS = {"TQQQ", "QLD"}


@dataclass
class SimulacionHistoricaResultado:
    ticker: str
    fecha_inicio_real: pd.Timestamp
    fecha_fin_real: pd.Timestamp
    fecha_inicio_datos_reales: Optional[date]  # None u.a.: desde cuándo el precio ya no es simulado
    serie_balance: pd.Series          # mensual
    serie_invertido_neto: pd.Series   # mensual
    serie_precio: pd.Series          # mensual — precio del ticker (sin aportes), para análisis de riesgo
    fechas_aportes: list               # meses en los que realmente hubo una transferencia
    valor_final: float
    aportado_bruto_total: float
    costo_setup: float
    comisiones_swift_totales: float
    comisiones_manejo_totales: float
    costos_servicio_totales: float
    numero_transferencias: int
    rendimiento_generado: float
    retorno_anualizado_pct: float  # XIRR: retorno ponderado por dinero, sí toma en cuenta cada aporte
    max_drawdown_pct: float


def _serie_diaria_para_ticker(
    ticker: str,
    fecha_inicio: date,
    fecha_fin: date,
    expense_ratio_anual_pct: float,
    financing_rate_anual_pct: float,
    refresh: bool = False,
) -> tuple[pd.Series, Optional[date]]:
    if ticker.upper() in TICKERS_SIMULADOS:
        diaria, fecha_inicio_real = construir_serie_diaria(
            ticker,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            expense_ratio_anual_pct=expense_ratio_anual_pct,
            financing_rate_anual_pct=financing_rate_anual_pct,
            refresh=refresh,
        )
    elif ticker.upper() == "QQQ" and fecha_inicio < FECHA_INCEPTION_GLOBAL:
        diaria, fecha_inicio_real = construir_serie_qqq_extendida(
            fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, refresh=refresh
        )
    else:
        diaria = get_daily_closes(ticker, refresh=refresh)
        diaria = diaria[(diaria.index.date >= fecha_inicio) & (diaria.index.date <= fecha_fin)]
        fecha_inicio_real = diaria.index.min().date() if not diaria.empty else None

    return diaria, fecha_inicio_real


def obtener_serie_diaria(
    ticker: str,
    fecha_inicio: date,
    fecha_fin: date,
    expense_ratio_anual_pct: float = DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    financing_rate_anual_pct: float = DEFAULT_FINANCING_RATE_ANUAL_PCT,
    refresh: bool = False,
) -> pd.Series:
    """Precio diario (no mensual) de un ticker, con la misma lógica de simulación/extensión que el resto del módulo."""
    diaria, _ = _serie_diaria_para_ticker(
        ticker, fecha_inicio, fecha_fin, expense_ratio_anual_pct, financing_rate_anual_pct, refresh=refresh
    )
    return diaria


def _serie_mensual_para_ticker(
    ticker: str,
    fecha_inicio: date,
    fecha_fin: date,
    expense_ratio_anual_pct: float,
    financing_rate_anual_pct: float,
    refresh: bool = False,
) -> tuple[pd.Series, Optional[date]]:
    diaria, fecha_inicio_real = _serie_diaria_para_ticker(
        ticker, fecha_inicio, fecha_fin, expense_ratio_anual_pct, financing_rate_anual_pct, refresh=refresh
    )
    if diaria.empty:
        return pd.Series(dtype=float), fecha_inicio_real

    periods = diaria.index.to_period("M")
    last_dates = diaria.groupby(periods).apply(lambda s: s.index.max())
    mensual = diaria.loc[last_dates.values].sort_index()
    return mensual, fecha_inicio_real


def simular_dca_historico(
    tickers: List[str],
    fecha_fin: date,
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
    fecha_inicio: date = FECHA_INCEPTION_GLOBAL,
    setup_fee: float = 0.0,
    costo_swift: float = 0.0,
    management_fee_anual_pct: float = 0.0,
    expense_ratio_anual_pct: float = DEFAULT_EXPENSE_RATIO_ANUAL_PCT,
    financing_rate_anual_pct: float = DEFAULT_FINANCING_RATE_ANUAL_PCT,
    refresh: bool = False,
) -> Dict[str, SimulacionHistoricaResultado]:
    if not tickers:
        return {}

    every_n = FREQ_TO_MONTHS.get(frecuencia, 1)
    fee_manejo_mensual = management_fee_anual_pct / 100.0 / 12.0
    resultados: Dict[str, SimulacionHistoricaResultado] = {}

    for ticker in tickers:
        mensual, fecha_inicio_datos_reales = _serie_mensual_para_ticker(
            ticker, fecha_inicio, fecha_fin, expense_ratio_anual_pct, financing_rate_anual_pct, refresh=refresh
        )
        if mensual.empty:
            continue

        shares = 0.0
        aportado_bruto_cum = 0.0
        costos_servicio_cum = 0.0
        comisiones_swift_cum = 0.0
        comisiones_manejo_cum = 0.0
        numero_transferencias = 0

        fechas: list = []
        precios: list = []
        balances: list = []
        aportado_serie: list = []
        fechas_aportes: list = []
        flujos_caja: list = []  # (fecha, monto) — negativo = aporte, para el XIRR

        for i, (fecha, precio) in enumerate(mensual.items()):
            precio = float(precio)

            if i == 0 and aporte_inicial > 0:
                monto_neto_inicial = max(0.0, aporte_inicial - costo_swift - setup_fee)
                if precio > 0:
                    shares += monto_neto_inicial / precio
                aportado_bruto_cum += aporte_inicial
                costos_servicio_cum += setup_fee + costo_swift
                comisiones_swift_cum += costo_swift
                numero_transferencias += 1
                fechas_aportes.append(fecha)
                flujos_caja.append((fecha.date(), -aporte_inicial))
            elif i > 0 and aporte_periodico > 0 and (every_n <= 1 or i % every_n == 0):
                monto_neto = max(0.0, aporte_periodico - costo_swift)
                if precio > 0:
                    shares += monto_neto / precio
                aportado_bruto_cum += aporte_periodico
                costos_servicio_cum += costo_swift
                comisiones_swift_cum += costo_swift
                numero_transferencias += 1
                fechas_aportes.append(fecha)
                flujos_caja.append((fecha.date(), -aporte_periodico))

            valor_actual = shares * precio
            fee_mes = valor_actual * fee_manejo_mensual
            if precio > 0 and fee_mes > 0:
                shares -= fee_mes / precio
            comisiones_manejo_cum += fee_mes
            costos_servicio_cum += fee_mes

            fechas.append(fecha)
            precios.append(precio)
            balances.append(shares * precio)
            aportado_serie.append(aportado_bruto_cum - costos_servicio_cum)

        if not fechas:
            continue

        serie_balance = pd.Series(balances, index=fechas, name=ticker)
        serie_invertido = pd.Series(aportado_serie, index=fechas, name=ticker)
        serie_precio = pd.Series(precios, index=fechas, name=ticker)

        valor_final = balances[-1]

        # Retorno anualizado real (XIRR): a diferencia de un CAGR simple
        # (primer valor vs. último valor), esto sí pondera cada aporte por
        # su fecha y monto — indispensable en un DCA con muchos aportes,
        # donde un CAGR simple puede dar cifras sin sentido (ver docstring
        # del módulo).
        flujos_caja.append((fechas[-1].date(), valor_final))
        retorno_anualizado_pct = xirr(flujos_caja)

        dd = max_drawdown(serie_balance.reset_index(drop=True))

        resultados[ticker] = SimulacionHistoricaResultado(
            ticker=ticker,
            fecha_inicio_real=fechas[0],
            fecha_fin_real=fechas[-1],
            fecha_inicio_datos_reales=fecha_inicio_datos_reales,
            serie_balance=serie_balance,
            serie_invertido_neto=serie_invertido,
            serie_precio=serie_precio,
            fechas_aportes=fechas_aportes,
            valor_final=valor_final,
            aportado_bruto_total=aportado_bruto_cum,
            costo_setup=setup_fee,
            comisiones_swift_totales=comisiones_swift_cum,
            comisiones_manejo_totales=comisiones_manejo_cum,
            costos_servicio_totales=costos_servicio_cum,
            numero_transferencias=numero_transferencias,
            rendimiento_generado=valor_final - aportado_bruto_cum,
            retorno_anualizado_pct=retorno_anualizado_pct,
            max_drawdown_pct=dd,
        )

    return resultados
