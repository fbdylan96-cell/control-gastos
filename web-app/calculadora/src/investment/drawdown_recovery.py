# -*- coding: utf-8 -*-
"""
Buy the dip con apalancamiento: usando un ticker de referencia (QQQ o SPY)
para generar la señal de caída desde el máximo histórico (ATH), se mide
qué tan bien le fue después a un ticker objetivo (posiblemente apalancado,
p.ej. TQQQ o QLD) — tanto en términos probabilísticos (retorno individual
de cada caída detectada, N tiempo después) como en un backtest concreto:
los aportes periódicos se acumulan como cash parqueado hasta que hay una
señal de compra Y no hay ya una posición abierta; ahí se despliega todo
el cash parqueado de una vez, se vende N tiempo después, y se vuelve a
esperar — modelando la realidad de un cliente que aporta con regularidad
pero solo invierte cuando hay una oportunidad de compra.

Los horizontes de tiempo se miden en días HÁBILES aproximados (21/mes,
252/año — convención estándar en análisis financiero), no en días
calendario, para no tener que lidiar con fines de semana/feriados: un
avance de N posiciones sobre la serie diaria real ya salta esos días
automáticamente, porque simplemente no están en el índice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.finance.annuity import FREQ_TO_MONTHS
from src.finance.metrics import xirr

HORIZONTES_DIAS_HABILES: list[tuple[str, int]] = [
    ("1 día", 1),
    ("15 días", 15),
    ("1 mes", 21),
    ("3 meses", 63),
    ("6 meses", 126),
    ("1 año", 252),
    ("2 años", 504),
    ("3 años", 756),
    ("4 años", 1008),
    ("5 años", 1260),
    ("6 años", 1512),
    ("7 años", 1764),
    ("8 años", 2016),
    ("9 años", 2268),
    ("10 años", 2520),
]


def fechas_disparo_caida(precios_referencia: pd.Series, umbral_pct: float) -> pd.DatetimeIndex:
    """
    Fechas en las que el drawdown desde el ATH CRUZA por primera vez `umbral_pct`
    (negativo, p.ej. -5.0) viniendo de un valor menos negativo — así cada caída
    cuenta como un solo evento, en vez de contar cada día que el precio se
    mantiene por debajo del umbral.
    """
    if precios_referencia.empty:
        return pd.DatetimeIndex([])
    drawdown = precios_referencia / precios_referencia.cummax() - 1.0
    umbral = umbral_pct / 100.0
    cruza = (drawdown <= umbral) & (drawdown.shift(1) > umbral)
    return precios_referencia.index[cruza.fillna(False)]


def retornos_forward_por_horizonte(precios: pd.Series) -> dict[str, pd.Series]:
    """Para cada horizonte, serie de retorno forward indexada por la fecha de inicio del cómputo."""
    resultado: dict[str, pd.Series] = {}
    valores = precios.to_numpy(dtype=float)
    n = len(valores)
    for etiqueta, dias_habiles in HORIZONTES_DIAS_HABILES:
        if n <= dias_habiles:
            resultado[etiqueta] = pd.Series(dtype=float)
            continue
        retorno = valores[dias_habiles:] / valores[:-dias_habiles] - 1.0
        resultado[etiqueta] = pd.Series(retorno, index=precios.index[:-dias_habiles])
    return resultado


def retornos_post_caida_vs_promedio(precios_ticker: pd.Series, fechas_disparo: pd.DatetimeIndex) -> list[dict]:
    """
    Por horizonte: retorno promedio del ticker N tiempo después de una fecha de
    disparo (caída de la referencia), retorno promedio incondicional (cualquier
    día) al mismo plazo, y cuántos eventos de disparo tenían dato disponible.
    """
    forward_por_horizonte = retornos_forward_por_horizonte(precios_ticker)

    filas = []
    for etiqueta, _ in HORIZONTES_DIAS_HABILES:
        serie_fwd = forward_por_horizonte[etiqueta]
        promedio_incondicional = float(serie_fwd.mean()) if not serie_fwd.empty else None

        fechas_validas = serie_fwd.index.intersection(fechas_disparo) if not serie_fwd.empty else pd.DatetimeIndex([])
        if len(fechas_validas) > 0:
            promedio_post_caida = float(serie_fwd.loc[fechas_validas].mean())
            n_eventos = int(len(fechas_validas))
        else:
            promedio_post_caida, n_eventos = None, 0

        filas.append(
            {
                "horizonte": etiqueta,
                "retorno_post_caida_pct": promedio_post_caida * 100.0 if promedio_post_caida is not None else None,
                "retorno_promedio_pct": promedio_incondicional * 100.0 if promedio_incondicional is not None else None,
                "n_eventos": n_eventos,
            }
        )
    return filas


def retornos_individuales_por_evento(precios_ticker: pd.Series, fechas_disparo: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Granular: una fila por cada fecha de caída detectada, con el retorno REAL
    (no promedio) de `precios_ticker` para cada horizonte a partir de esa fecha
    exacta. Celdas vacías (None) donde todavía no hay suficiente historia para
    ese plazo.
    """
    if precios_ticker.empty or len(fechas_disparo) == 0:
        return pd.DataFrame()

    idx = precios_ticker.index
    forward_por_horizonte = retornos_forward_por_horizonte(precios_ticker)

    fechas_alineadas = sorted(
        {idx[pos] for f in fechas_disparo if (pos := int(idx.searchsorted(f))) < len(idx)}
    )

    data: dict[str, list] = {}
    for etiqueta, _ in HORIZONTES_DIAS_HABILES:
        serie_fwd = forward_por_horizonte[etiqueta]
        data[etiqueta] = [
            float(serie_fwd.loc[f]) * 100.0 if f in serie_fwd.index else None for f in fechas_alineadas
        ]
    return pd.DataFrame(data, index=pd.DatetimeIndex(fechas_alineadas, name="Fecha de la señal"))


@dataclass
class OperacionDip:
    fecha_entrada: pd.Timestamp
    fecha_salida: pd.Timestamp
    precio_entrada: float
    precio_salida: float
    monto_invertido: float
    posicion_abierta_al_final: bool

    @property
    def retorno_pct(self) -> float:
        if self.precio_entrada <= 0:
            return 0.0
        return (self.precio_salida / self.precio_entrada - 1.0) * 100.0


@dataclass
class BacktestDipResultado:
    operaciones: list[OperacionDip] = field(default_factory=list)
    serie_valor: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    serie_aportado: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    total_aportado: float = 0.0
    valor_final: float = 0.0
    retorno_anualizado_pct: float = 0.0
    n_operaciones: int = 0
    pct_operaciones_ganadoras: float = 0.0


def _fechas_aportes_periodicos(
    precio_objetivo: pd.Series, aporte_inicial: float, aporte_periodico: float, frecuencia: str
) -> list[tuple[pd.Timestamp, float]]:
    """
    Mismo criterio de periodicidad que el resto de la simulación: el primer mes
    recibe el aporte inicial, y desde ahí cada `every_n` meses recibe el aporte
    periódico — aquí solo para saber CUÁNDO entra dinero nuevo a la cuenta.
    """
    if precio_objetivo.empty:
        return []
    periods = precio_objetivo.index.to_period("M")
    fechas_mensuales = sorted(precio_objetivo.groupby(periods).apply(lambda s: s.index.min()).tolist())
    every_n = FREQ_TO_MONTHS.get(frecuencia, 1)

    eventos: list[tuple[pd.Timestamp, float]] = []
    for i, fecha in enumerate(fechas_mensuales):
        monto = 0.0
        if i == 0 and aporte_inicial > 0:
            monto += aporte_inicial
        if i > 0 and aporte_periodico > 0 and (every_n <= 1 or i % every_n == 0):
            monto += aporte_periodico
        if monto > 0:
            eventos.append((pd.Timestamp(fecha), monto))
    return eventos


def backtest_buy_the_dip(
    precio_objetivo: pd.Series,
    fechas_disparo: pd.DatetimeIndex,
    horizonte_dias_habiles: int,
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
) -> BacktestDipResultado:
    """
    Simula la estrategia realista: los aportes (misma periodicidad y monto del
    plan) se acumulan como cash parqueado en la cuenta, SIN invertirse de
    inmediato. Cuando aparece una señal de caída Y no hay una posición abierta
    Y hay cash parqueado, se invierte TODO ese cash de una sola vez en el
    activo objetivo. La posición se vende automáticamente `horizonte_dias_habiles`
    después (o se marca a mercado al final de los datos si no alcanza), el
    producto de la venta vuelve a quedar parqueado, y se espera la próxima
    señal — nunca hay más de una posición abierta a la vez.
    """
    idx = precio_objetivo.index
    n = len(idx)
    if n == 0:
        return BacktestDipResultado()

    eventos_aporte = _fechas_aportes_periodicos(precio_objetivo, aporte_inicial, aporte_periodico, frecuencia)
    aportes_por_posicion: dict[int, float] = {}
    for fecha, monto in eventos_aporte:
        pos = int(idx.searchsorted(fecha))
        if pos < n:
            aportes_por_posicion[pos] = aportes_por_posicion.get(pos, 0.0) + monto

    set_posiciones_senal = {pos for f in fechas_disparo if (pos := int(idx.searchsorted(f))) < n}
    valores = precio_objetivo.to_numpy(dtype=float)

    operaciones: list[OperacionDip] = []
    en_posicion = False
    i_entrada: int | None = None
    precio_entrada: float | None = None
    monto_en_posicion: float = 0.0
    i_salida_objetivo: int | None = None

    cash_parqueado = 0.0
    total_aportado = 0.0
    serie_valor_vals: list[float] = []
    flujos_caja: list[tuple] = []
    aportado_vals: list[float] = []
    aportado_acumulado = 0.0

    for i in range(n):
        fecha = idx[i]
        precio = valores[i]

        if i in aportes_por_posicion:
            monto_aporte = aportes_por_posicion[i]
            cash_parqueado += monto_aporte
            total_aportado += monto_aporte
            aportado_acumulado += monto_aporte
            flujos_caja.append((fecha.date(), -monto_aporte))

        if not en_posicion and i in set_posiciones_senal and cash_parqueado > 0:
            en_posicion = True
            i_entrada = i
            precio_entrada = precio
            monto_en_posicion = cash_parqueado
            cash_parqueado = 0.0
            i_salida_objetivo = i + horizonte_dias_habiles

        if en_posicion and i_salida_objetivo is not None and i >= i_salida_objetivo:
            shares = monto_en_posicion / precio_entrada if precio_entrada > 0 else 0.0
            cash_parqueado += shares * precio
            operaciones.append(
                OperacionDip(idx[i_entrada], fecha, precio_entrada, precio, monto_en_posicion, False)
            )
            en_posicion = False

        if en_posicion:
            shares_abiertas = monto_en_posicion / precio_entrada if precio_entrada > 0 else 0.0
            valor_hoy = cash_parqueado + shares_abiertas * precio
        else:
            valor_hoy = cash_parqueado
        serie_valor_vals.append(valor_hoy)
        aportado_vals.append(aportado_acumulado)

    if en_posicion:
        operaciones.append(
            OperacionDip(idx[i_entrada], idx[-1], precio_entrada, valores[-1], monto_en_posicion, True)
        )

    valor_final = serie_valor_vals[-1] if serie_valor_vals else 0.0
    flujos_caja_finales = list(flujos_caja)
    if flujos_caja_finales:
        flujos_caja_finales.append((idx[-1].date(), valor_final))
    retorno_anualizado_pct = xirr(flujos_caja_finales) * 100.0 if len(flujos_caja_finales) >= 2 else 0.0

    ganadoras = sum(1 for op in operaciones if op.retorno_pct > 0)
    pct_ganadoras = (ganadoras / len(operaciones) * 100.0) if operaciones else 0.0

    return BacktestDipResultado(
        operaciones=operaciones,
        serie_valor=pd.Series(serie_valor_vals, index=idx),
        serie_aportado=pd.Series(aportado_vals, index=idx),
        total_aportado=total_aportado,
        valor_final=valor_final,
        retorno_anualizado_pct=retorno_anualizado_pct,
        n_operaciones=len(operaciones),
        pct_operaciones_ganadoras=pct_ganadoras,
    )
