# -*- coding: utf-8 -*-
"""
Estimación de la pensión por vejez del régimen IVM (CCSS), siguiendo la
normativa vigente descrita en el Reglamento del Seguro de IVM (arts. 5,
6, 23, 24, 25, 27 y 29):

  1. Salario promedio de referencia: se calcula (fuera de esta función)
     como el promedio de los mejores 300 salarios mensuales cotizados,
     ya actualizados por inflación. Aquí se recibe como dato de entrada.
  2. Cuantía básica: % del salario promedio según el tramo salarial,
     medido contra el salario mínimo legal de un trabajador en ocupación
     no calificada (tabla del art. 24).
  3. Cuantía adicional: +0.0833% del salario promedio por cada mes
     cotizado por encima de las primeras 300 cuotas.
  4. Vejez proporcional: con 180 a 299 cuotas a la edad de retiro, se
     calcula la pensión "completa" (solo con cuantía básica, sin
     cuotas extra) y se multiplica por cuotas_totales / 300.
  5. Postergación: +0.1333% del salario promedio por cada mes que la
     persona sigue cotizando después de cumplir los requisitos, con
     tope de que ordinaria + postergación no exceda 125% del salario
     promedio (solo aplica a la vejez ordinaria, no a la proporcional).
  6. El monto final se ajusta a un piso (cuantía mínima) y un techo
     (tope máximo) fijados periódicamente por la Junta Directiva de la
     CCSS.

Esta es una guía de proyección educativa, no un cálculo oficial ni una
certificación de la CCSS.
"""
from __future__ import annotations

from dataclasses import dataclass

CUOTAS_MINIMAS_ORDINARIA = 300
CUOTAS_MINIMAS_PROPORCIONAL = 180
INCREMENTO_PCT_POR_CUOTA_EXTRA = 0.0833
INCREMENTO_PCT_POR_MES_POSTERGACION = 0.1333
TOPE_PCT_CON_POSTERGACION = 125.0

# Tabla de cuantía básica (art. 24): (límite superior del tramo en múltiplos
# del salario mínimo, cuantía básica %). El último tramo (None) es "8 o más".
TABLA_CUANTIA_BASICA = [
    (2.0, 52.5),
    (3.0, 51.0),
    (4.0, 49.4),
    (5.0, 47.8),
    (6.0, 46.2),
    (8.0, 44.6),
    (None, 43.0),
]

# Valores de referencia 2026 (fuente: referencia_ivm_2026.md / acuerdos CCSS 2026):
#   • Pensión mínima 2026:            ₡162,295
#   • Pensión máxima sin postergación: ₡1,765,859  (actualizado 2026; antes ₡1,666,062)
# El tope máximo se aplica SIEMPRE al final del cálculo (ver calcular_pension_ivm:
# monto_mensual = max(mínimo, min(bruto, máximo))).
MONTO_MINIMO_DEFAULT = 162_295.0
MONTO_MAXIMO_DEFAULT = 1_765_859.0


@dataclass
class IVMResultado:
    cumple_requisitos: bool
    motivo: str
    es_proporcional: bool
    cuotas_totales: int
    cuantia_basica_pct: float
    cuantia_adicional_pct: float
    incremento_postergacion_pct: float
    porcentaje_reconocido: float
    factor_proporcional: float
    monto_bruto: float
    monto_mensual: float


def cuantia_basica_pct(salario_promedio: float, salario_minimo_legal: float) -> float:
    if salario_minimo_legal <= 0:
        return TABLA_CUANTIA_BASICA[-1][1]
    ratio = salario_promedio / salario_minimo_legal
    for limite, pct in TABLA_CUANTIA_BASICA:
        if limite is None or ratio < limite:
            return pct
    return TABLA_CUANTIA_BASICA[-1][1]


def calcular_pension_ivm(
    salario_promedio_referencia: float,
    cuotas_ivm_hoy: int,
    anios_restantes: int,
    salario_minimo_legal: float,
    meses_postergacion: int = 0,
    monto_minimo: float = MONTO_MINIMO_DEFAULT,
    monto_maximo: float = MONTO_MAXIMO_DEFAULT,
) -> IVMResultado:
    meses_futuros = max(0, anios_restantes) * 12
    cuotas_totales = int(cuotas_ivm_hoy + meses_futuros)

    basica_pct = cuantia_basica_pct(salario_promedio_referencia, salario_minimo_legal)

    if cuotas_totales < CUOTAS_MINIMAS_PROPORCIONAL:
        return IVMResultado(
            cumple_requisitos=False,
            motivo=(
                f"Con {cuotas_totales} cuotas no se alcanza el mínimo de "
                f"{CUOTAS_MINIMAS_PROPORCIONAL} cuotas para una vejez proporcional."
            ),
            es_proporcional=False,
            cuotas_totales=cuotas_totales,
            cuantia_basica_pct=basica_pct,
            cuantia_adicional_pct=0.0,
            incremento_postergacion_pct=0.0,
            porcentaje_reconocido=0.0,
            factor_proporcional=0.0,
            monto_bruto=0.0,
            monto_mensual=0.0,
        )

    if cuotas_totales >= CUOTAS_MINIMAS_ORDINARIA:
        cuotas_extra = cuotas_totales - CUOTAS_MINIMAS_ORDINARIA
        adicional_pct = cuotas_extra * INCREMENTO_PCT_POR_CUOTA_EXTRA
        incremento_postergacion_pct = meses_postergacion * INCREMENTO_PCT_POR_MES_POSTERGACION
        porcentaje_reconocido = min(
            basica_pct + adicional_pct + incremento_postergacion_pct, TOPE_PCT_CON_POSTERGACION
        )
        monto_bruto = salario_promedio_referencia * porcentaje_reconocido / 100.0
        factor_proporcional = 1.0
        es_proporcional = False
        motivo = "Cumple los requisitos de vejez ordinaria (300 cuotas o más)."
    else:
        adicional_pct = 0.0
        incremento_postergacion_pct = 0.0
        porcentaje_reconocido = basica_pct
        factor_proporcional = cuotas_totales / CUOTAS_MINIMAS_ORDINARIA
        pension_completa = salario_promedio_referencia * basica_pct / 100.0
        monto_bruto = pension_completa * factor_proporcional
        es_proporcional = True
        motivo = (
            f"Vejez proporcional: {cuotas_totales} cuotas ({factor_proporcional * 100:.0f}% "
            "de la pensión completa)."
        )

    monto_mensual = max(monto_minimo, min(monto_bruto, monto_maximo))

    return IVMResultado(
        cumple_requisitos=True,
        motivo=motivo,
        es_proporcional=es_proporcional,
        cuotas_totales=cuotas_totales,
        cuantia_basica_pct=basica_pct,
        cuantia_adicional_pct=adicional_pct,
        incremento_postergacion_pct=incremento_postergacion_pct,
        porcentaje_reconocido=porcentaje_reconocido,
        factor_proporcional=factor_proporcional,
        monto_bruto=monto_bruto,
        monto_mensual=monto_mensual,
    )
