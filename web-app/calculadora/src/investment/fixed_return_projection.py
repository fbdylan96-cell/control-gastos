# -*- coding: utf-8 -*-
"""
Proyección teórica de una cartera con rendimiento fijo, aportes
periódicos variables (monto y frecuencia), y los costos del servicio de
inversión:
  - Fee de apertura (setup, único), descontado del aporte inicial.
  - Costo por transferencia (p.ej. comisión SWIFT), descontado de CADA
    transferencia (el aporte inicial y cada aporte periódico).
  - Management fee (% anual), cobrado sobre el saldo/AUM cada mes.

La simulación corre mes a mes internamente (para reflejar bien aportes
trimestrales/semestrales/anuales y el cobro mensual del management fee),
pero los puntos que se exponen para graficar son solo uno por año
(aniversario), cada uno etiquetado con el año calendario real y la edad
real del cliente en ese año.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.finance.annuity import FREQ_TO_MONTHS, monthly_rate_from_annual


@dataclass
class ProyeccionPunto:
    anio_index: int       # años desde hoy (0..N)
    anio_calendario: int  # año calendario real
    edad_cliente: int     # edad del cliente ese año
    aportado_bruto_cum: float
    balance: float


@dataclass
class ProyeccionFijaResultado:
    puntos: list[ProyeccionPunto]
    valor_final: float
    aportado_bruto_total: float
    costo_setup: float
    comisiones_swift_totales: float
    comisiones_manejo_totales: float
    costos_servicio_totales: float
    numero_transferencias: int
    rendimiento_generado: float


def proyectar_rendimiento_fijo(
    anios: int,
    rendimiento_anual_pct: float,
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
    edad_actual: int,
    setup_fee: float = 0.0,
    costo_swift: float = 0.0,
    management_fee_anual_pct: float = 0.0,
    anio_actual: int | None = None,
) -> ProyeccionFijaResultado:
    if anio_actual is None:
        anio_actual = date.today().year

    meses = max(1, anios * 12)
    every_n = FREQ_TO_MONTHS.get(frecuencia, 1)
    r_month = monthly_rate_from_annual(rendimiento_anual_pct / 100.0)
    fee_manejo_mensual = management_fee_anual_pct / 100.0 / 12.0

    costo_setup = max(0.0, setup_fee)
    comisiones_swift_cum = 0.0
    comisiones_manejo_cum = 0.0
    numero_transferencias = 0

    if aporte_inicial > 0:
        comisiones_swift_cum += costo_swift
        numero_transferencias += 1
        balance = max(0.0, aporte_inicial - costo_swift - costo_setup)
    else:
        balance = 0.0

    aportado_bruto_cum = aporte_inicial
    costos_servicio_cum = costo_setup + comisiones_swift_cum

    puntos = [ProyeccionPunto(0, anio_actual, edad_actual, aportado_bruto_cum, balance)]

    for m in range(1, meses + 1):
        balance *= (1.0 + r_month)

        fee_mes = balance * fee_manejo_mensual
        balance -= fee_mes
        comisiones_manejo_cum += fee_mes
        costos_servicio_cum += fee_mes

        if aporte_periodico > 0 and (every_n <= 1 or m % every_n == 0):
            monto_neto = max(0.0, aporte_periodico - costo_swift)
            balance += monto_neto
            aportado_bruto_cum += aporte_periodico
            comisiones_swift_cum += costo_swift
            costos_servicio_cum += costo_swift
            numero_transferencias += 1

        if m % 12 == 0:
            anio_idx = m // 12
            puntos.append(
                ProyeccionPunto(anio_idx, anio_actual + anio_idx, edad_actual + anio_idx, aportado_bruto_cum, balance)
            )

    valor_final = puntos[-1].balance
    return ProyeccionFijaResultado(
        puntos=puntos,
        valor_final=valor_final,
        aportado_bruto_total=aportado_bruto_cum,
        costo_setup=costo_setup,
        comisiones_swift_totales=comisiones_swift_cum,
        comisiones_manejo_totales=comisiones_manejo_cum,
        costos_servicio_totales=costos_servicio_cum,
        numero_transferencias=numero_transferencias,
        rendimiento_generado=valor_final - aportado_bruto_cum,
    )
