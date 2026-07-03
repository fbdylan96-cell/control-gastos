# -*- coding: utf-8 -*-
"""
Análisis de riesgo histórico de un ticker: peores caídas (drawdowns) y
mejores/peores años calendario.

Se calcula sobre el PRECIO del ticker (no sobre el saldo con aportes),
para que el análisis de riesgo no se mezcle con el efecto de cuándo se
metió dinero — esto responde "qué tan riesgoso es este activo", no "qué
tan bien me fue a mí con mi plan de aportes" (eso ya lo responde la
tabla resumen con el XIRR).
"""
from __future__ import annotations

import pandas as pd


def peores_drawdowns(precios: pd.Series, top_n: int = 10) -> list[dict]:
    """
    Identifica episodios de caída: tramos continuos donde el precio está
    por debajo de su máximo previo. Devuelve los `top_n` más profundos,
    con la fecha del pico, la fecha del valle y el % de caída.
    """
    if precios.empty:
        return []

    max_precio_actual = float(precios.iloc[0])
    fecha_pico_actual = precios.index[0]

    episodios: list[dict] = []
    en_caida = False
    peor_dd = 0.0
    fecha_valle = None
    fecha_pico_episodio = None

    for fecha, precio in precios.items():
        precio = float(precio)
        if precio >= max_precio_actual:
            if en_caida:
                episodios.append(
                    {"fecha_pico": fecha_pico_episodio, "fecha_valle": fecha_valle, "drawdown_pct": peor_dd * 100.0}
                )
                en_caida = False
            max_precio_actual = precio
            fecha_pico_actual = fecha
        else:
            dd = precio / max_precio_actual - 1.0
            if not en_caida:
                en_caida = True
                fecha_pico_episodio = fecha_pico_actual
                peor_dd = dd
                fecha_valle = fecha
            elif dd < peor_dd:
                peor_dd = dd
                fecha_valle = fecha

    if en_caida:
        episodios.append(
            {"fecha_pico": fecha_pico_episodio, "fecha_valle": fecha_valle, "drawdown_pct": peor_dd * 100.0}
        )

    episodios.sort(key=lambda e: e["drawdown_pct"])
    return episodios[:top_n]


def mejores_y_peores_anios(precios: pd.Series, top_n: int = 10) -> tuple[list[dict], list[dict]]:
    """Retorno % por año calendario (último precio del año vs. el del año anterior)."""
    if precios.empty:
        return [], []

    anual = precios.resample("YE").last()
    retornos = anual.pct_change().dropna()
    filas = [{"anio": fecha.year, "retorno_pct": float(val) * 100.0} for fecha, val in retornos.items()]

    mejores = sorted(filas, key=lambda f: f["retorno_pct"], reverse=True)[:top_n]
    peores = sorted(filas, key=lambda f: f["retorno_pct"])[:top_n]
    return mejores, peores
