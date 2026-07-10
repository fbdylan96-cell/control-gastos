# -*- coding: utf-8 -*-
"""
Simulación de la fase de retiro (decumulación): a partir del valor final
proyectado de la cartera, cada año se retira un monto fijo (definido como
% de retiro anual sobre el valor inicial del retiro) y el resto del
portafolio crece a una tasa constante asumida.

La tendencia real NO se puede inferir solo de si el portafolio toca cero
dentro del horizonte simulado: si el retiro supera apenas al crecimiento,
el portafolio puede estar decayendo sin llegar a cero dentro del
horizonte (y aun así no está "creciendo"). Por eso se compara
directamente `crecimiento_anual_pct` contra `tasa_retiro_anual_pct` para
clasificar la tendencia real:
  - "crece"   si crecimiento > retiro (el portafolio nunca se agota)
  - "estable" si crecimiento == retiro (el portafolio se mantiene flat)
  - "decrece" si crecimiento < retiro (el portafolio se agotará eventualmente,
              haya tocado cero o no dentro del horizonte graficado)
"""
from __future__ import annotations

from dataclasses import dataclass

_TOLERANCIA_PCT = 1e-9


@dataclass
class DecumulacionPunto:
    anio_index: int
    anio_calendario: int
    edad_cliente: int
    balance: float


@dataclass
class DecumulacionResultado:
    puntos: list[DecumulacionPunto]
    retiro_anual: float
    tendencia: str  # "crece" | "estable" | "decrece"
    anio_agotamiento: int | None  # años desde el inicio del retiro; None si no se agota dentro del horizonte graficado
    se_agota: bool  # True solo si se agota DENTRO del horizonte graficado


def simular_decumulacion(
    valor_inicial: float,
    tasa_retiro_anual_pct: float,
    crecimiento_anual_pct: float,
    edad_inicio: int,
    anio_calendario_inicio: int,
    horizonte_anios: int = 50,
) -> DecumulacionResultado:
    retiro_anual = valor_inicial * tasa_retiro_anual_pct / 100.0
    balance = valor_inicial

    puntos = [DecumulacionPunto(0, anio_calendario_inicio, edad_inicio, balance)]
    anio_agotamiento = None

    for anio in range(1, horizonte_anios + 1):
        balance = balance * (1.0 + crecimiento_anual_pct / 100.0) - retiro_anual
        if balance <= 0:
            puntos.append(DecumulacionPunto(anio, anio_calendario_inicio + anio, edad_inicio + anio, 0.0))
            anio_agotamiento = anio
            break
        puntos.append(DecumulacionPunto(anio, anio_calendario_inicio + anio, edad_inicio + anio, balance))

    diferencia = crecimiento_anual_pct - tasa_retiro_anual_pct
    if diferencia > _TOLERANCIA_PCT:
        tendencia = "crece"
    elif diferencia < -_TOLERANCIA_PCT:
        tendencia = "decrece"
    else:
        tendencia = "estable"

    return DecumulacionResultado(
        puntos=puntos,
        retiro_anual=retiro_anual,
        tendencia=tendencia,
        anio_agotamiento=anio_agotamiento,
        se_agota=anio_agotamiento is not None,
    )
