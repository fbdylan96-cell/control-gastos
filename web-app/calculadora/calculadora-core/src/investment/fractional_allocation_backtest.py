# -*- coding: utf-8 -*-
"""
Buy the dip por FRACCIONES DE CAPITAL: a diferencia de la estrategia
escalonada (`tiered_leverage_backtest`), donde TODO el capital rota junto
a través de niveles de apalancamiento, acá el capital se divide en N
fracciones iguales desde el principio, y cada fracción tiene su propio
"gatillo" independiente:

- Una fracción puede quedarse SIEMPRE en el activo base (no se apalanca
  nunca), o
- Puede escalar hacia un activo apalancado propio la primera vez que el
  drawdown de la señal (desde su máximo de 52 semanas) cruza el umbral
  propio de esa fracción.

Salida por GANANCIA (no por tiempo): una vez que una fracción escala, se
queda invertida en su activo apalancado — nunca se vende solo porque el
mercado se recupere — y los aportes nuevos de esa fracción siguen
entrando ahí, promediando el precio si está en pérdida. Recién se vende
TODO cuando la posición alcanza su propio porcentaje de ganancia objetivo
(por eso conviene pedirle metas más ambiciosas a un activo más
apalancado). Al vender, el producto vuelve al activo base y la fracción
queda lista para volver a activarse en una futura caída — es un ciclo
que se puede repetir varias veces a lo largo del período.

Ejemplo: con 3 fracciones, se puede dejar 1 siempre en el activo base
(p.ej. QQQ), otra que escale a TQQQ si el drawdown pasa de -5% y venda al
llegar a +300%, y la tercera que escale a QLD si el drawdown pasa de
-10% y venda al llegar a +100% — cada fracción es independiente de las
demás.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import pandas as pd

from src.finance.metrics import max_drawdown, xirr
from src.investment.drawdown_recovery import VENTANA_52_SEMANAS_DIAS_HABILES, fechas_aportes_periodicos
from src.investment.tiered_leverage_backtest import alinear_precio


@dataclass
class ConfigFraccion:
    activo: str | None            # ticker al que escala; None = se queda siempre en el activo base
    umbral_pct: float | None      # drawdown (%, negativo) que dispara el escalamiento; None si activo es None
    ganancia_objetivo_pct: float | None = None  # % de ganancia al que se vende TODO y se vuelve al activo base


@dataclass
class TradeFraccion:
    fraccion_indice: int
    activo: str
    fecha_entrada: pd.Timestamp
    fecha_salida: pd.Timestamp
    monto_invertido: float
    valor_al_salir: float
    peor_drawdown_pct: float  # peor caída de pico a valle DURANTE este trade específico
    ganancia_objetivo_pct: float
    abierta_al_final: bool = False

    @property
    def dias_mantenida(self) -> int:
        return (self.fecha_salida - self.fecha_entrada).days

    @property
    def retorno_pct(self) -> float:
        if self.monto_invertido <= 0:
            return 0.0
        return (self.valor_al_salir / self.monto_invertido - 1.0) * 100.0


@dataclass
class FraccionResultado:
    indice: int
    activo: str
    umbral_pct: float | None
    ganancia_objetivo_pct: float | None
    trades: list[TradeFraccion] = field(default_factory=list)
    valor_final: float = 0.0
    max_drawdown_pct: float = 0.0  # peor caída de la SERIE de esta fracción en todo el período

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def ganancia_generada(self) -> float:
        return sum(t.valor_al_salir - t.monto_invertido for t in self.trades)

    @property
    def dias_promedio_por_trade(self) -> float:
        cerrados = [t for t in self.trades if not t.abierta_al_final]
        if not cerrados:
            return 0.0
        return sum(t.dias_mantenida for t in cerrados) / len(cerrados)


@dataclass
class FraccionesBacktestResultado:
    serie_valor: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    serie_aportado: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    fracciones: list[FraccionResultado] = field(default_factory=list)
    total_aportado: float = 0.0
    valor_final: float = 0.0
    retorno_anualizado_pct: float = 0.0
    max_drawdown_pct: float = 0.0


def backtest_fracciones_capital(
    precio_senal: pd.Series,
    precio_base: pd.Series,
    configs: list[ConfigFraccion],
    precios_por_activo: dict[str, pd.Series],
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
) -> FraccionesBacktestResultado:
    """
    `precios_por_activo` debe incluir una entrada para cada ticker usado en
    `configs` (los activos a los que escala alguna fracción) — NO necesita
    incluir el activo base, ese se pasa aparte en `precio_base`.
    """
    idx = precio_base.index
    n = len(idx)
    n_fracciones = len(configs)
    if n == 0 or n_fracciones == 0:
        return FraccionesBacktestResultado()

    v_senal = alinear_precio(precio_senal, idx)
    v_base = alinear_precio(precio_base, idx)
    precios_alineados = {t: alinear_precio(p, idx) for t, p in precios_por_activo.items()}

    maximo_movil = (
        pd.Series(v_senal, index=idx).rolling(window=VENTANA_52_SEMANAS_DIAS_HABILES, min_periods=1).max().to_numpy()
    )
    drawdown = [(v_senal[i] / maximo_movil[i] - 1.0) if maximo_movil[i] > 0 else 0.0 for i in range(n)]

    eventos_aporte = fechas_aportes_periodicos(precio_base, aporte_inicial, aporte_periodico, frecuencia)
    aportes_por_pos: dict[int, float] = {}
    for fecha, monto in eventos_aporte:
        pos = int(idx.searchsorted(fecha))
        if pos < n:
            aportes_por_pos[pos] = aportes_por_pos.get(pos, 0.0) + monto

    shares_base = [0.0] * n_fracciones
    shares_activo = [0.0] * n_fracciones
    activado = [False] * n_fracciones
    monto_invertido_actual = [0.0] * n_fracciones
    fecha_entrada_actual: list = [None] * n_fracciones
    pico_trade = [0.0] * n_fracciones
    peor_dd_trade = [0.0] * n_fracciones

    umbrales_frac = [c.umbral_pct / 100.0 if c.umbral_pct is not None else None for c in configs]
    escalable = [c.activo is not None and c.umbral_pct is not None for c in configs]

    total_aportado = 0.0
    aportado_acumulado = 0.0
    flujos_caja: list[tuple] = []
    serie_valor_vals: list[float] = []
    aportado_vals: list[float] = []
    trades: list[list[TradeFraccion]] = [[] for _ in range(n_fracciones)]
    serie_valor_frac_vals: list[list[float]] = [[] for _ in range(n_fracciones)]

    def _valor_fraccion(k: int, i: int) -> float:
        if activado[k]:
            return shares_activo[k] * precios_alineados[configs[k].activo][i]
        return shares_base[k] * v_base[i]

    def _valor_total(i: int) -> float:
        return sum(_valor_fraccion(k, i) for k in range(n_fracciones))

    for i in range(n):
        fecha = idx[i]

        if i in aportes_por_pos:
            monto = aportes_por_pos[i]
            total_aportado += monto
            aportado_acumulado += monto
            flujos_caja.append((fecha.date(), -monto))
            monto_por_fraccion = monto / n_fracciones
            for k in range(n_fracciones):
                if activado[k]:
                    precio_hoy = precios_alineados[configs[k].activo][i]
                    if precio_hoy > 0:
                        shares_activo[k] += monto_por_fraccion / precio_hoy
                    monto_invertido_actual[k] += monto_por_fraccion
                else:
                    if v_base[i] > 0:
                        shares_base[k] += monto_por_fraccion / v_base[i]

        for k in range(n_fracciones):
            if not escalable[k]:
                continue

            if not activado[k]:
                if drawdown[i] <= umbrales_frac[k]:
                    valor_a_rotar = shares_base[k] * v_base[i]
                    shares_base[k] = 0.0
                    precio_activo_hoy = precios_alineados[configs[k].activo][i]
                    if precio_activo_hoy > 0:
                        shares_activo[k] += valor_a_rotar / precio_activo_hoy
                    activado[k] = True
                    monto_invertido_actual[k] = valor_a_rotar
                    fecha_entrada_actual[k] = fecha
                    pico_trade[k] = valor_a_rotar
                    peor_dd_trade[k] = 0.0
            else:
                valor_actual = shares_activo[k] * precios_alineados[configs[k].activo][i]
                pico_trade[k] = max(pico_trade[k], valor_actual)
                if pico_trade[k] > 0:
                    dd_hoy = valor_actual / pico_trade[k] - 1.0
                    peor_dd_trade[k] = min(peor_dd_trade[k], dd_hoy)

                ganancia_actual_pct = (
                    (valor_actual / monto_invertido_actual[k] - 1.0) * 100.0
                    if monto_invertido_actual[k] > 0 else 0.0
                )
                objetivo = configs[k].ganancia_objetivo_pct
                if objetivo is not None and ganancia_actual_pct >= objetivo:
                    trades[k].append(
                        TradeFraccion(
                            fraccion_indice=k, activo=configs[k].activo,
                            fecha_entrada=fecha_entrada_actual[k], fecha_salida=fecha,
                            monto_invertido=monto_invertido_actual[k], valor_al_salir=valor_actual,
                            peor_drawdown_pct=peor_dd_trade[k] * 100.0,
                            ganancia_objetivo_pct=objetivo, abierta_al_final=False,
                        )
                    )
                    if v_base[i] > 0:
                        shares_base[k] += valor_actual / v_base[i]
                    shares_activo[k] = 0.0
                    activado[k] = False
                    monto_invertido_actual[k] = 0.0
                    fecha_entrada_actual[k] = None

        serie_valor_vals.append(_valor_total(i))
        aportado_vals.append(aportado_acumulado)
        for k in range(n_fracciones):
            serie_valor_frac_vals[k].append(_valor_fraccion(k, i))

    for k in range(n_fracciones):
        if escalable[k] and activado[k]:
            trades[k].append(
                TradeFraccion(
                    fraccion_indice=k, activo=configs[k].activo,
                    fecha_entrada=fecha_entrada_actual[k], fecha_salida=idx[-1],
                    monto_invertido=monto_invertido_actual[k], valor_al_salir=_valor_fraccion(k, n - 1),
                    peor_drawdown_pct=peor_dd_trade[k] * 100.0,
                    ganancia_objetivo_pct=configs[k].ganancia_objetivo_pct, abierta_al_final=True,
                )
            )

    valor_final = serie_valor_vals[-1] if serie_valor_vals else 0.0
    flujos_finales = list(flujos_caja)
    if flujos_finales:
        flujos_finales.append((idx[-1].date(), valor_final))
    retorno_anualizado_pct = xirr(flujos_finales) * 100.0 if len(flujos_finales) >= 2 else 0.0

    serie_valor = pd.Series(serie_valor_vals, index=idx)
    dd_pct = max_drawdown(serie_valor) * 100.0 if not serie_valor.empty else 0.0

    fracciones_resultado = []
    for k, c in enumerate(configs):
        serie_frac = pd.Series(serie_valor_frac_vals[k], index=idx)
        dd_frac = max_drawdown(serie_frac) * 100.0 if not serie_frac.empty else 0.0
        fracciones_resultado.append(
            FraccionResultado(
                indice=k,
                activo=c.activo if c.activo else "Base",
                umbral_pct=c.umbral_pct,
                ganancia_objetivo_pct=c.ganancia_objetivo_pct,
                trades=trades[k],
                valor_final=serie_valor_frac_vals[k][-1] if serie_valor_frac_vals[k] else 0.0,
                max_drawdown_pct=dd_frac,
            )
        )

    return FraccionesBacktestResultado(
        serie_valor=serie_valor,
        serie_aportado=pd.Series(aportado_vals, index=idx),
        fracciones=fracciones_resultado,
        total_aportado=total_aportado,
        valor_final=valor_final,
        retorno_anualizado_pct=retorno_anualizado_pct,
        max_drawdown_pct=dd_pct,
    )


def sweep_fracciones_capital(
    precio_senal: pd.Series,
    precio_base: pd.Series,
    configs_base: list[ConfigFraccion],
    precios_por_activo: dict[str, pd.Series],
    umbrales_por_fraccion: dict[int, list[float]],
    ganancias_por_fraccion: dict[int, list[float]],
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
    progreso_callback=None,
) -> pd.DataFrame:
    """
    Corre `backtest_fracciones_capital` para cada combinación de (umbral,
    porcentaje de ganancia objetivo) de las fracciones que escalan (las que
    no escalan quedan fijas). Como cada fracción es independiente de las
    demás, CUALQUIER combinación es válida.

    `umbrales_por_fraccion` y `ganancias_por_fraccion`: índice de fracción
    (0-based, solo las que escalan) -> lista de valores a probar.
    """
    indices_escalables = list(umbrales_por_fraccion.keys())
    listas_umbral = [umbrales_por_fraccion[k] for k in indices_escalables]
    listas_ganancia = [ganancias_por_fraccion.get(k, [configs_base[k].ganancia_objetivo_pct]) for k in indices_escalables]

    combos_umbral = list(product(*listas_umbral)) if listas_umbral else [()]
    combos_ganancia = list(product(*listas_ganancia)) if listas_ganancia else [()]

    total = len(combos_umbral) * len(combos_ganancia)
    filas = []
    contador = 0
    for combo_u in combos_umbral:
        for combo_g in combos_ganancia:
            configs = list(configs_base)
            for pos, idx_k in enumerate(indices_escalables):
                configs[idx_k] = ConfigFraccion(
                    activo=configs_base[idx_k].activo,
                    umbral_pct=combo_u[pos],
                    ganancia_objetivo_pct=combo_g[pos],
                )

            resultado = backtest_fracciones_capital(
                precio_senal, precio_base, configs, precios_por_activo,
                aporte_inicial, aporte_periodico, frecuencia,
            )
            fila = {}
            for pos, idx_k in enumerate(indices_escalables):
                fila[f"Umbral Fracción {idx_k + 1} (%)"] = combo_u[pos]
                fila[f"Ganancia obj. Fracción {idx_k + 1} (%)"] = combo_g[pos]
            fila.update(
                {
                    "Valor Final ($)": resultado.valor_final,
                    "Total Aportado ($)": resultado.total_aportado,
                    "Retorno anualizado XIRR (%)": resultado.retorno_anualizado_pct,
                    "Max drawdown (%)": resultado.max_drawdown_pct,
                }
            )
            filas.append(fila)
            contador += 1
            if progreso_callback is not None:
                progreso_callback(contador, total)

    return pd.DataFrame(filas)


def generar_conclusion_barrido(df: pd.DataFrame, columnas_variables: list[str]) -> str:
    """
    Análisis en texto, generado a partir de los datos reales del barrido (no
    una plantilla fija): mejor y peor combinación, y qué valores aparecen más
    seguido entre el 10% de mejores resultados — para ayudar a detectar
    "zonas dulces" de parámetros sin tener que leer la tabla entera.
    """
    if df.empty:
        return "El barrido no produjo resultados para analizar."

    df_ordenado = df.sort_values("Valor Final ($)", ascending=False).reset_index(drop=True)
    mejor = df_ordenado.iloc[0]
    peor = df_ordenado.iloc[-1]
    n_top = max(1, len(df_ordenado) // 10)
    top = df_ordenado.head(n_top)

    partes = []
    detalle_mejor = ", ".join(f"{c} = {mejor[c]:.0f}" for c in columnas_variables)
    partes.append(
        f"De **{len(df_ordenado):,}** combinaciones probadas, la mejor fue ({detalle_mejor}): terminó en "
        f"**${mejor['Valor Final ($)']:,.0f}** (XIRR {mejor['Retorno anualizado XIRR (%)']:.1f}%, max "
        f"drawdown {mejor['Max drawdown (%)']:.1f}%)."
    )

    if peor["Valor Final ($)"] > 0:
        multiplo = mejor["Valor Final ($)"] / peor["Valor Final ($)"]
        partes.append(
            f"La peor combinación probada terminó en ${peor['Valor Final ($)']:,.0f} — la mejor superó a "
            f"la peor por un factor de **{multiplo:.1f}x**, lo que muestra qué tan sensible es el "
            "resultado a estos parámetros: no cualquier combinación funciona igual de bien."
        )

    for c in columnas_variables:
        conteo = top[c].value_counts()
        if conteo.empty:
            continue
        valor_frecuente = conteo.index[0]
        veces = int(conteo.iloc[0])
        partes.append(
            f"Entre el {n_top} de mejores combinaciones (top 10%), el valor de **{c}** más frecuente fue "
            f"**{valor_frecuente:.0f}** (aparece en {veces} de {n_top})."
        )

    dd_top_promedio = top["Max drawdown (%)"].mean()
    dd_general_promedio = df_ordenado["Max drawdown (%)"].mean()
    if abs(dd_top_promedio - dd_general_promedio) < 5:
        comentario_dd = (
            "fue similar al del resto de combinaciones — buscar más retorno acá no pareció exigir más "
            "riesgo del ya inherente a la estrategia."
        )
    elif dd_top_promedio > dd_general_promedio:
        # menos negativo = caída menos profunda = menos riesgo
        comentario_dd = (
            f"fue MENOS PROFUNDO que el promedio general ({dd_general_promedio:.1f}%) — las mejores "
            "combinaciones no solo rindieron más, también sufrieron menos."
        )
    else:
        comentario_dd = (
            f"fue MÁS PROFUNDO que el promedio general ({dd_general_promedio:.1f}%) — el mejor retorno "
            "vino acompañado de más riesgo, no es gratis."
        )
    partes.append(f"El drawdown promedio del top 10% ({dd_top_promedio:.1f}%) {comentario_dd}")

    return "\n\n".join(partes)
