# -*- coding: utf-8 -*-
"""
Buy the dip con apalancamiento ESCALONADO: en vez de un solo salto
(desapalancado -> un apalancado), acá hay 1 o 2 niveles apalancados —
p.ej. QQQ -> QLD (2x), o QQQ -> QLD (2x) -> TQQQ (3x) — que se activan
según qué tan profunda es la caída, medida desde el máximo móvil de 52
semanas de un ticker de señal (QQQ o SPY, independiente de los tickers
que realmente se compran, y también independiente de cuáles tickers se
usan en cada nivel apalancado).

Reglas (confirmadas con el cliente antes de programar esto):

- Nivel 0 (el activo base, típicamente QQQ): mientras el drawdown de la
  señal sea menos profundo que el primer umbral. Cada aporte nuevo entra
  al nivel 0.
- Nivel 1 (primer apalancado, p.ej. QLD 2x): se activa la PRIMERA vez que
  el drawdown cruza por debajo del primer umbral — ahí se rota TODO lo
  que había en el nivel 0 hacia el nivel 1. Mientras el drawdown siga en
  ese rango, los aportes nuevos entran ahí.
- Nivel 2 (segundo apalancado, opcional, p.ej. TQQQ 3x): solo existe si
  se configuran 2 niveles. Se activa si el drawdown cruza por debajo del
  segundo umbral — se rota TODO lo que había en el nivel 1 hacia el nivel
  2. Aportes nuevos entran ahí mientras el drawdown siga tan profundo.
- La recuperación es ASIMÉTRICA: si el drawdown mejora, solo cambia a
  DÓNDE van los aportes NUEVOS (de vuelta hacia un nivel más bajo según
  el drawdown actual) — el capital que ya escaló de nivel se queda ahí,
  no se vende automáticamente solo porque el drawdown mejoró.
- Qué hacer con las posiciones ya escaladas es una VARIABLE
  (`mantener_apalancamiento`):
    - False (por defecto): el "reloj" de salida (horizonte N) arranca en
      la PRIMERA caída que disparó el episodio — no se reinicia si se
      escala de un nivel a otro dentro del mismo episodio ("la primera
      caída lleva la batuta del conteo"). Al cumplirse ese plazo, TODO se
      vende y se consolida de vuelta en el nivel 0, terminando el
      episodio. Si el mercado sigue en caída ese mismo día, empieza un
      episodio nuevo de inmediato (nunca hay más de un episodio de
      escalamiento activo a la vez).
    - True: nunca se vende nada por tiempo — una vez que el dinero
      escaló de nivel, se queda ahí para siempre (ride it out). Solo se
      sigue invirtiendo cada aporte nuevo según el nivel que corresponda
      al drawdown de ese momento. Útil para ver qué tan bien (o mal) va
      simplemente sostener el apalancamiento sin vender nunca.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import pandas as pd

from src.finance.metrics import max_drawdown, xirr
from src.investment.drawdown_recovery import (
    HORIZONTES_DIAS_HABILES,
    VENTANA_52_SEMANAS_DIAS_HABILES,
    fechas_aportes_periodicos,
)

TIER_NOMBRES = {0: "QQQ", 1: "QLD", 2: "TQQQ"}  # default informativo — la UI arma el propio con los tickers elegidos


@dataclass
class EpisodioTiered:
    fecha_inicio: pd.Timestamp
    fecha_fin: pd.Timestamp
    tier_maximo: int  # nivel más alto que alcanzó este episodio (1, 2, ...)
    valor_al_inicio: float
    valor_al_final: float
    abierto_al_final: bool = False

    @property
    def retorno_pct(self) -> float:
        if self.valor_al_inicio <= 0:
            return 0.0
        return (self.valor_al_final / self.valor_al_inicio - 1.0) * 100.0


@dataclass
class TieredBacktestResultado:
    serie_valor: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    serie_aportado: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    serie_tier: pd.Series = field(default_factory=lambda: pd.Series(dtype=int))  # nivel más profundo con saldo > 0
    episodios: list[EpisodioTiered] = field(default_factory=list)
    total_aportado: float = 0.0
    valor_final: float = 0.0
    retorno_anualizado_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    n_episodios: int = 0


def alinear_precio(precio: pd.Series, idx_maestro: pd.DatetimeIndex) -> list[float]:
    """Reindexa `precio` a `idx_maestro` por fecha más cercana disponible (<=)."""
    idx_o = precio.index
    valores = precio.to_numpy(dtype=float)
    n_o = len(idx_o)
    out = []
    for f in idx_maestro:
        pos = int(idx_o.searchsorted(f))
        if pos >= n_o:
            pos = n_o - 1
        out.append(float(valores[pos]))
    return out


def backtest_tiered_leverage(
    precio_senal: pd.Series,
    precio_tier0: pd.Series,
    precios_apalancados: list[pd.Series],
    umbrales_pct: list[float],
    horizonte_dias_habiles: int | None,
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
    mantener_apalancamiento: bool = False,
) -> TieredBacktestResultado:
    """
    `precios_apalancados` y `umbrales_pct` deben tener la misma longitud (1 o 2):
    un ticker y un umbral de drawdown (más negativo = más profundo) por cada
    nivel apalancado, en orden creciente de profundidad (umbrales_pct[0] es el
    menos profundo). Si `mantener_apalancamiento=True`, `horizonte_dias_habiles`
    se ignora por completo (nunca se liquida por tiempo).
    """
    idx = precio_tier0.index
    n = len(idx)
    n_niveles = len(precios_apalancados)
    if n == 0 or n_niveles == 0 or len(umbrales_pct) != n_niveles:
        return TieredBacktestResultado()

    v_senal = alinear_precio(precio_senal, idx)
    precios_por_tier = [alinear_precio(precio_tier0, idx)] + [
        alinear_precio(p, idx) for p in precios_apalancados
    ]

    maximo_movil = (
        pd.Series(v_senal, index=idx).rolling(window=VENTANA_52_SEMANAS_DIAS_HABILES, min_periods=1).max().to_numpy()
    )
    drawdown = [
        (v_senal[i] / maximo_movil[i] - 1.0) if maximo_movil[i] > 0 else 0.0 for i in range(n)
    ]

    umbrales_frac = [u / 100.0 for u in umbrales_pct]

    def _tier_objetivo(dd: float) -> int:
        tier = 0
        for u in umbrales_frac:
            if dd <= u:
                tier += 1
            else:
                break
        return tier

    eventos_aporte = fechas_aportes_periodicos(precio_tier0, aporte_inicial, aporte_periodico, frecuencia)
    aportes_por_pos: dict[int, float] = {}
    for fecha, monto in eventos_aporte:
        pos = int(idx.searchsorted(fecha))
        if pos < n:
            aportes_por_pos[pos] = aportes_por_pos.get(pos, 0.0) + monto

    n_tiers = n_niveles + 1
    shares = [0.0] * n_tiers
    en_episodio = False
    i_entrada_episodio: int | None = None
    i_salida_objetivo: int | None = None
    tier_maximo_episodio = 0
    valor_al_inicio_episodio = 0.0

    total_aportado = 0.0
    aportado_acumulado = 0.0
    flujos_caja: list[tuple] = []
    serie_valor_vals: list[float] = []
    aportado_vals: list[float] = []
    tier_actual_vals: list[int] = []
    episodios: list[EpisodioTiered] = []

    def _valor_total(i: int) -> float:
        return sum(shares[t] * precios_por_tier[t][i] for t in range(n_tiers))

    def _tier_mas_profundo_con_saldo() -> int:
        for t in range(n_tiers - 1, -1, -1):
            if shares[t] > 0:
                return t
        return 0

    for i in range(n):
        fecha = idx[i]
        monto = 0.0
        if i in aportes_por_pos:
            monto = aportes_por_pos[i]
            total_aportado += monto
            aportado_acumulado += monto
            flujos_caja.append((fecha.date(), -monto))

        tier_obj = _tier_objetivo(drawdown[i])

        if not en_episodio:
            if tier_obj > 0:
                if shares[0] > 0:
                    valor_a_rotar = shares[0] * precios_por_tier[0][i]
                    shares[0] = 0.0
                    if precios_por_tier[tier_obj][i] > 0:
                        shares[tier_obj] += valor_a_rotar / precios_por_tier[tier_obj][i]
                en_episodio = True
                i_entrada_episodio = i
                i_salida_objetivo = i + horizonte_dias_habiles if horizonte_dias_habiles is not None else None
                tier_maximo_episodio = tier_obj
                valor_al_inicio_episodio = _valor_total(i)
            if monto > 0 and precios_por_tier[tier_obj][i] > 0:
                shares[tier_obj] += monto / precios_por_tier[tier_obj][i]
        else:
            if tier_obj > tier_maximo_episodio:
                tier_ocupado = tier_maximo_episodio
                if shares[tier_ocupado] > 0:
                    valor_a_rotar = shares[tier_ocupado] * precios_por_tier[tier_ocupado][i]
                    shares[tier_ocupado] = 0.0
                    if precios_por_tier[tier_obj][i] > 0:
                        shares[tier_obj] += valor_a_rotar / precios_por_tier[tier_obj][i]
                tier_maximo_episodio = tier_obj

            if monto > 0 and precios_por_tier[tier_obj][i] > 0:
                shares[tier_obj] += monto / precios_por_tier[tier_obj][i]

            if not mantener_apalancamiento and i_salida_objetivo is not None and i >= i_salida_objetivo:
                valor_liquidacion = sum(shares[t] * precios_por_tier[t][i] for t in range(1, n_tiers))
                if precios_por_tier[0][i] > 0:
                    shares[0] += valor_liquidacion / precios_por_tier[0][i]
                for t in range(1, n_tiers):
                    shares[t] = 0.0
                episodios.append(
                    EpisodioTiered(
                        idx[i_entrada_episodio], fecha, tier_maximo_episodio,
                        valor_al_inicio_episodio, _valor_total(i),
                    )
                )
                en_episodio = False
                i_entrada_episodio = None
                i_salida_objetivo = None
                tier_maximo_episodio = 0

        serie_valor_vals.append(_valor_total(i))
        aportado_vals.append(aportado_acumulado)
        tier_actual_vals.append(_tier_mas_profundo_con_saldo())

    if en_episodio and i_entrada_episodio is not None:
        episodios.append(
            EpisodioTiered(
                idx[i_entrada_episodio], idx[-1], tier_maximo_episodio,
                valor_al_inicio_episodio, _valor_total(n - 1), abierto_al_final=True,
            )
        )

    valor_final = serie_valor_vals[-1] if serie_valor_vals else 0.0
    flujos_finales = list(flujos_caja)
    if flujos_finales:
        flujos_finales.append((idx[-1].date(), valor_final))
    retorno_anualizado_pct = xirr(flujos_finales) * 100.0 if len(flujos_finales) >= 2 else 0.0

    serie_valor = pd.Series(serie_valor_vals, index=idx)
    dd_pct = max_drawdown(serie_valor) * 100.0 if not serie_valor.empty else 0.0

    return TieredBacktestResultado(
        serie_valor=serie_valor,
        serie_aportado=pd.Series(aportado_vals, index=idx),
        serie_tier=pd.Series(tier_actual_vals, index=idx),
        episodios=episodios,
        total_aportado=total_aportado,
        valor_final=valor_final,
        retorno_anualizado_pct=retorno_anualizado_pct,
        max_drawdown_pct=dd_pct,
        n_episodios=len(episodios),
    )


def sweep_tiered_leverage(
    precio_senal: pd.Series,
    precio_tier0: pd.Series,
    precios_apalancados: list[pd.Series],
    umbrales_pct_por_nivel: list[list[float]],
    horizontes: list[tuple[str, int]],
    aporte_inicial: float,
    aporte_periodico: float,
    frecuencia: str,
    mantener_apalancamiento: bool = False,
    progreso_callback=None,
) -> pd.DataFrame:
    """
    Corre `backtest_tiered_leverage` para cada combinación válida de umbrales
    (estrictamente más profundos en cada nivel siguiente) y horizontes de la
    grilla, manteniendo fijos los tickers y el modo de salida. Si
    `mantener_apalancamiento=True`, los horizontes se ignoran (solo se corre
    una vez por combinación de umbrales).

    `umbrales_pct_por_nivel` es una lista de listas: una lista de valores a
    probar por cada nivel apalancado (1 o 2 niveles).
    """
    n_niveles = len(precios_apalancados)
    combos_umbrales = list(product(*umbrales_pct_por_nivel))
    combos_umbrales_validos = [
        u for u in combos_umbrales if all(u[k + 1] < u[k] for k in range(n_niveles - 1))
    ]
    horizontes_a_usar = horizontes if not mantener_apalancamiento else [("N/A", 0)]

    total = len(combos_umbrales_validos) * len(horizontes_a_usar)
    filas = []
    contador = 0
    for umbrales in combos_umbrales_validos:
        for h_lbl, h_dias in horizontes_a_usar:
            resultado = backtest_tiered_leverage(
                precio_senal, precio_tier0, precios_apalancados, list(umbrales),
                None if mantener_apalancamiento else h_dias,
                aporte_inicial, aporte_periodico, frecuencia,
                mantener_apalancamiento=mantener_apalancamiento,
            )
            fila = {f"Umbral Nivel {k + 1} (%)": umbrales[k] for k in range(n_niveles)}
            if not mantener_apalancamiento:
                fila["Horizonte"] = h_lbl
            fila.update(
                {
                    "Valor Final ($)": resultado.valor_final,
                    "Total Aportado ($)": resultado.total_aportado,
                    "Retorno anualizado XIRR (%)": resultado.retorno_anualizado_pct,
                    "Max drawdown (%)": resultado.max_drawdown_pct,
                    "N° episodios": resultado.n_episodios,
                }
            )
            filas.append(fila)
            contador += 1
            if progreso_callback is not None:
                progreso_callback(contador, total)

    return pd.DataFrame(filas)
