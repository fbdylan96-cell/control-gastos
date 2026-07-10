# -*- coding: utf-8 -*-
"""
Proyección del ingreso mensual del ROP (Régimen Obligatorio de Pensiones
Complementarias), siguiendo la Ley de Protección al Trabajador (Ley 7983,
arts. 13, 20 y 22) y el Reglamento de Beneficios del Régimen de
Capitalización Individual (SUPEN/CONASSIF).

El ROP es de capitalización individual: se nutre de aportes equivalentes
al 4.25% del salario reportado a la CCSS. La forma de convertir el saldo
en renta mensual depende de la fecha de retiro:

  - Retiro entre 2021-01-01 y 2030-02-18 (régimen transitorio): se puede
    retirar en rentas temporales por un plazo equivalente a la cantidad
    de cuotas aportadas al ROP. Aproximación:
        ROP_mensual ≈ saldo_proyectado / cuotas_totales_al_retiro
  - Retiro a partir de 2030-02-19: aplican las modalidades no aceleradas
    (retiro programado, renta permanente, renta temporal hasta
    expectativa de vida). Se aproxima aquí como una anualidad sobre un
    plazo de pago definido:
        FV = saldo_actual*(1+i)^n + aporte_mensual*((1+i)^n - 1)/i
        ROP_mensual ≈ FV * j / (1 - (1+j)^-m)

Todos los cálculos de esta función usan la tasa de rendimiento REAL
(nominal ajustada por inflación), de modo que el resultado queda
expresado en colones de hoy (poder de compra actual), igual que en los
ejemplos de la guía de referencia.

Ninguna prestación del ROP puede ser inferior al 20% de la pensión
mínima del IVM; si el cálculo da menos, se ajusta a ese piso.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

APORTE_ROP_PCT = 4.25
FECHA_CORTE_TRANSITORIO = date(2030, 2, 19)
PISO_PCT_SOBRE_PENSION_MINIMA_IVM = 20.0


def tasa_real_anual_pct(rentabilidad_nominal_pct: float, inflacion_pct: float) -> float:
    return ((1.0 + rentabilidad_nominal_pct / 100.0) / (1.0 + inflacion_pct / 100.0) - 1.0) * 100.0


def _valor_futuro(saldo_actual: float, aporte_mensual: float, tasa_mensual: float, meses: int) -> float:
    if meses <= 0:
        return saldo_actual
    if tasa_mensual == 0:
        return saldo_actual + aporte_mensual * meses
    crecimiento = (1.0 + tasa_mensual) ** meses
    return saldo_actual * crecimiento + aporte_mensual * (crecimiento - 1.0) / tasa_mensual


def _pago_anualidad(valor_futuro: float, tasa_mensual: float, meses: int) -> float:
    if meses <= 0:
        return 0.0
    if tasa_mensual == 0:
        return valor_futuro / meses
    return valor_futuro * tasa_mensual / (1.0 - (1.0 + tasa_mensual) ** (-meses))


@dataclass
class ROPResultado:
    modalidad_aplicable: str  # "transitoria" | "anualidad"
    fecha_retiro_estimada: date
    saldo_proyectado: float
    aporte_mensual: float
    rentabilidad_real_anual_pct: float
    cuotas_rop_al_retiro: int
    ingreso_mensual_transitorio: float
    ingreso_mensual_anualidad: float
    ingreso_mensual_aplicable: float
    piso_pension_minima: float


def proyectar_rop(
    salario_bruto_actual: float,
    saldo_actual_rop: float,
    cuotas_rop_hoy: int,
    anios_restantes: int,
    rentabilidad_nominal_pct: float,
    inflacion_pct: float,
    plazo_pago_anios: float,
    monto_minimo_ivm: float,
    fecha_retiro_estimada: date | None = None,
) -> ROPResultado:
    if fecha_retiro_estimada is None:
        fecha_retiro_estimada = date(date.today().year + max(0, anios_restantes), date.today().month, 1)

    meses_futuros = max(0, anios_restantes) * 12
    cuotas_rop_al_retiro = int(cuotas_rop_hoy + meses_futuros)

    aporte_mensual = salario_bruto_actual * APORTE_ROP_PCT / 100.0
    rentabilidad_real_pct = tasa_real_anual_pct(rentabilidad_nominal_pct, inflacion_pct)
    tasa_mensual = rentabilidad_real_pct / 100.0 / 12.0

    saldo_proyectado = _valor_futuro(saldo_actual_rop, aporte_mensual, tasa_mensual, meses_futuros)

    ingreso_transitorio = (
        saldo_proyectado / cuotas_rop_al_retiro if cuotas_rop_al_retiro > 0 else 0.0
    )

    meses_pago = max(1, round(plazo_pago_anios * 12))
    ingreso_anualidad = _pago_anualidad(saldo_proyectado, tasa_mensual, meses_pago)

    modalidad_aplicable = "transitoria" if fecha_retiro_estimada < FECHA_CORTE_TRANSITORIO else "anualidad"
    ingreso_aplicable = ingreso_transitorio if modalidad_aplicable == "transitoria" else ingreso_anualidad

    piso = monto_minimo_ivm * PISO_PCT_SOBRE_PENSION_MINIMA_IVM / 100.0
    ingreso_aplicable = max(ingreso_aplicable, piso)

    return ROPResultado(
        modalidad_aplicable=modalidad_aplicable,
        fecha_retiro_estimada=fecha_retiro_estimada,
        saldo_proyectado=saldo_proyectado,
        aporte_mensual=aporte_mensual,
        rentabilidad_real_anual_pct=rentabilidad_real_pct,
        cuotas_rop_al_retiro=cuotas_rop_al_retiro,
        ingreso_mensual_transitorio=ingreso_transitorio,
        ingreso_mensual_anualidad=ingreso_anualidad,
        ingreso_mensual_aplicable=ingreso_aplicable,
        piso_pension_minima=piso,
    )
