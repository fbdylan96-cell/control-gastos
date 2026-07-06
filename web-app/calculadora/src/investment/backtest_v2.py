# -*- coding: utf-8 -*-
"""
Infraestructura común para el tab "Backtesting Avanzado V2".

Filosofía (reglas globales, no negociables — inspiradas en el brief del asesor):

  • CERO look-ahead. Las señales se calculan con el cierre de FIN DE MES y la
    posición se aplica al mes SIGUIENTE (se hace `señal.shift(1)`). Así nunca
    se opera con información que no existía al momento de decidir.

  • Precios de retorno TOTAL. Se usa el `Close` ya ajustado por dividendos y
    splits (`auto_adjust=True` en yahoo_client). Para índices puros de precio
    (^GSPC, ^IXIC) se documenta que NO incluyen dividendos.

  • Efectivo (cash). Se usa el retorno mensual de BIL (T-bills, desde 2007).
    Antes de 2007 no hay una serie de T-bills disponible sin FRED, así que ese
    tramo se aproxima con una tasa anual fija configurable (por defecto 3%).
    Está documentado y es conservador.

  • Costos de transacción: 0.10% por operación (una vía). Siempre incluidos.

  • Validación out-of-sample: el período hasta 2019-12-31 es "in-sample" y
    2020-01-01 a hoy es "holdout" (nunca se ajustan parámetros mirando el
    holdout). Las métricas se reportan por separado.

Todo lo de este módulo trabaja sobre SERIES DE RETORNOS MENSUALES (no precios),
salvo las funciones que explícitamente reciben precios diarios.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.cboe_client import load_cboe_index
from src.data.yahoo_client import DATA_CACHE_DIR, get_daily_closes

# --- Constantes globales del framework V2 ---
COSTO_OPERACION = 0.001          # 0.10% por operación (una vía)
FECHA_HOLDOUT = pd.Timestamp("2020-01-01")   # in-sample < esta fecha; holdout >=
TASA_CASH_PREVIA_ANUAL = 0.03    # aproximación de T-bills antes de que exista BIL (2007)
MESES_ANIO = 12


# ==========================================================
# DATOS
# ==========================================================
def fred_series(codigo: str, refresh: bool = False) -> pd.Series:
    """
    Serie mensual de FRED (p.ej. UNRATE, TB3MS) con caché local en data_cache/.
    Devuelve una Serie vacía si no hay conexión ni caché.
    """
    ruta = Path(DATA_CACHE_DIR) / f"FRED_{codigo}.csv"
    if not refresh and ruta.exists():
        s = pd.read_csv(ruta, index_col=0, parse_dates=[0]).iloc[:, 0]
        s.name = codigo
        return s
    try:
        import pandas_datareader.data as web

        df = web.DataReader(codigo, "fred", date(1934, 1, 1), date.today())
        s = df.iloc[:, 0].dropna()
        s.name = codigo
        ruta.parent.mkdir(parents=True, exist_ok=True)
        s.to_csv(ruta)
        return s
    except Exception:
        if ruta.exists():
            s = pd.read_csv(ruta, index_col=0, parse_dates=[0]).iloc[:, 0]
            s.name = codigo
            return s
        return pd.Series(dtype=float, name=codigo)


def hay_datos_fred() -> bool:
    """True si UNRATE está disponible (FRED en línea o en caché)."""
    return not fred_series("UNRATE").empty


def precio_mensual(ticker: str) -> pd.Series:
    """Cierre ajustado de fin de mes (retorno total) para un ticker."""
    diario = get_daily_closes(ticker)
    if diario.empty:
        return pd.Series(dtype=float, name=ticker)
    return diario.resample("ME").last().dropna().rename(ticker)


def retornos_mensuales(ticker: str) -> pd.Series:
    """Retorno mensual (pct_change) del cierre ajustado de fin de mes."""
    return precio_mensual(ticker).pct_change().rename(ticker)


def cash_mensual(index_mensual: pd.DatetimeIndex, tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL) -> pd.Series:
    """
    Retorno mensual del efectivo (T-bills), alineado a `index_mensual`.
    Prioridad: (1) BIL desde 2007 (retorno total real del ETF); (2) TB3MS de
    FRED antes de 2007 (tasa de la letra a 3 meses, convertida a retorno
    mensual); (3) si nada está disponible, una tasa anual fija de respaldo.
    """
    tasa_prev_mensual = (1.0 + tasa_previa_anual) ** (1.0 / 12.0) - 1.0
    resultado = pd.Series(tasa_prev_mensual, index=index_mensual, name="cash")

    # Capa base: TB3MS (anualizado en %) -> retorno mensual, alineado por fin de mes.
    tb3 = fred_series("TB3MS")
    if not tb3.empty:
        tb3_ret = ((1.0 + tb3 / 100.0) ** (1.0 / 12.0) - 1.0).resample("ME").last()
        alineado = tb3_ret.reindex(index_mensual)
        resultado = alineado.fillna(resultado).rename("cash")

    # Capa preferida: BIL (retorno total) donde exista.
    bil = get_daily_closes("BIL")
    if not bil.empty:
        bil_ret = bil.resample("ME").last().pct_change().reindex(index_mensual)
        resultado = bil_ret.fillna(resultado).rename("cash")

    return resultado


def particion_muestra(serie: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Divide una serie mensual en (in-sample, holdout) según FECHA_HOLDOUT."""
    return serie[serie.index < FECHA_HOLDOUT], serie[serie.index >= FECHA_HOLDOUT]


# ==========================================================
# MÉTRICAS (módulo compartido)
# ==========================================================
def _curva_capital(retornos: pd.Series) -> pd.Series:
    return (1.0 + retornos.dropna()).cumprod()


def cagr(retornos: pd.Series) -> float:
    r = retornos.dropna()
    if len(r) == 0:
        return 0.0
    total = float((1.0 + r).prod())
    anios = len(r) / MESES_ANIO
    if anios <= 0 or total <= 0:
        return 0.0
    return total ** (1.0 / anios) - 1.0


def vol_anualizada(retornos: pd.Series) -> float:
    r = retornos.dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1)) * np.sqrt(MESES_ANIO)


def sharpe(retornos: pd.Series, cash: pd.Series) -> float:
    r = retornos.dropna()
    rf = cash.reindex(r.index).fillna(0.0)
    exceso = r - rf
    vol = float(exceso.std(ddof=1))
    if len(exceso) < 2 or vol == 0:
        return 0.0
    return float(exceso.mean()) * MESES_ANIO / (vol * np.sqrt(MESES_ANIO))


def sortino(retornos: pd.Series, cash: pd.Series) -> float:
    r = retornos.dropna()
    rf = cash.reindex(r.index).fillna(0.0)
    exceso = r - rf
    downside = exceso[exceso < 0]
    dd = float(np.sqrt((downside ** 2).mean())) if len(downside) else 0.0
    if len(exceso) < 2 or dd == 0:
        return 0.0
    return float(exceso.mean()) * MESES_ANIO / (dd * np.sqrt(MESES_ANIO))


def max_drawdown(retornos: pd.Series) -> float:
    eq = _curva_capital(retornos)
    if eq.empty:
        return 0.0
    return float((eq / eq.cummax() - 1.0).min())


def calmar(retornos: pd.Series) -> float:
    dd = abs(max_drawdown(retornos))
    return cagr(retornos) / dd if dd > 0 else 0.0


def meses_bajo_agua(retornos: pd.Series) -> int:
    """Peor racha (en meses) sin alcanzar un nuevo máximo de la curva de capital."""
    eq = _curva_capital(retornos)
    if eq.empty:
        return 0
    pico = eq.cummax()
    bajo_agua = eq < pico
    peor = actual = 0
    for v in bajo_agua:
        actual = actual + 1 if v else 0
        peor = max(peor, actual)
    return int(peor)


def pct_meses_positivos(retornos: pd.Series) -> float:
    r = retornos.dropna()
    return float((r > 0).mean() * 100.0) if len(r) else 0.0


def rolling_12m_percentiles(retornos: pd.Series) -> dict:
    r = retornos.dropna()
    if len(r) < 12:
        return {"p5": float("nan"), "p50": float("nan"), "p95": float("nan")}
    roll = (1.0 + r).rolling(12).apply(np.prod, raw=True) - 1.0
    roll = roll.dropna()
    return {
        "p5": float(np.percentile(roll, 5)),
        "p50": float(np.percentile(roll, 50)),
        "p95": float(np.percentile(roll, 95)),
    }


def resumen_metricas(retornos: pd.Series, cash: pd.Series) -> dict:
    """Todas las métricas estándar para una serie de retornos mensuales."""
    roll = rolling_12m_percentiles(retornos)
    return {
        "CAGR (%)": cagr(retornos) * 100.0,
        "Vol. anual (%)": vol_anualizada(retornos) * 100.0,
        "Sharpe": sharpe(retornos, cash),
        "Sortino": sortino(retornos, cash),
        "Max Drawdown (%)": max_drawdown(retornos) * 100.0,
        "Calmar": calmar(retornos),
        "Meses bajo agua": meses_bajo_agua(retornos),
        "% meses positivos": pct_meses_positivos(retornos),
        "Rolling 12m p5 (%)": roll["p5"] * 100.0,
        "Rolling 12m p50 (%)": roll["p50"] * 100.0,
        "Rolling 12m p95 (%)": roll["p95"] * 100.0,
    }


# ==========================================================
# MÓDULO 1 — TREND FOLLOWING SMA DE 10 MESES (Faber)
# ==========================================================
@dataclass
class OperacionTrend:
    fecha_entrada: pd.Timestamp
    fecha_salida: pd.Timestamp | None   # None si sigue abierta al final
    meses: int
    retorno_pct: float
    abierta_al_final: bool = False


@dataclass
class ResultadoTrend:
    ticker: str
    ventana_meses: int
    precio_m: pd.Series
    sma: pd.Series
    señal: pd.Series          # 1 invertido, 0 efectivo (calculada a fin de mes)
    posicion: pd.Series       # señal.shift(1): lo que realmente se aplica cada mes
    ret_estrategia: pd.Series
    ret_buy_hold: pd.Series
    cash: pd.Series
    operaciones: list[OperacionTrend] = field(default_factory=list)


def backtest_sma_trend(
    ticker: str,
    ventana_meses: int = 10,
    tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
) -> ResultadoTrend:
    """
    Modelo de Faber: al cierre de cada mes, si el precio está por encima de su
    SMA de `ventana_meses`, se está 100% invertido en el activo; si no, 100% en
    efectivo. La posición se aplica el mes SIGUIENTE (sin look-ahead).
    """
    precio_m = precio_mensual(ticker)
    ret_m = precio_m.pct_change()
    sma = precio_m.rolling(ventana_meses).mean()

    señal = (precio_m > sma).astype(float).where(sma.notna())
    posicion = señal.shift(1)  # se ejecuta el mes siguiente al cierre que generó la señal
    cash = cash_mensual(precio_m.index, tasa_previa_anual)

    # Retorno de la estrategia: activo si estaba invertido, efectivo si no.
    strat = ret_m.where(posicion == 1.0, cash)
    # Costo cuando la posición cambia (entrar o salir).
    cambio = posicion.fillna(0.0).diff().abs() > 0
    strat = strat - cambio.astype(float) * costo

    valido = posicion.notna() & ret_m.notna()
    strat = strat[valido]
    bh = ret_m[valido]
    cash_v = cash[valido]

    operaciones = _extraer_operaciones_trend(posicion, ret_m, costo)

    return ResultadoTrend(
        ticker=ticker, ventana_meses=ventana_meses, precio_m=precio_m, sma=sma,
        señal=señal, posicion=posicion, ret_estrategia=strat, ret_buy_hold=bh,
        cash=cash_v, operaciones=operaciones,
    )


def _extraer_operaciones_trend(posicion: pd.Series, ret_m: pd.Series, costo: float) -> list[OperacionTrend]:
    ops: list[OperacionTrend] = []
    en_pos = False
    fecha_ent = None
    comp = 1.0
    nmeses = 0
    for fecha in posicion.index:
        p = posicion.loc[fecha]
        if p == 1.0:
            if not en_pos:
                en_pos = True
                fecha_ent = fecha
                comp = 1.0
                nmeses = 0
            r = ret_m.loc[fecha]
            if pd.notna(r):
                comp *= (1.0 + r)
                nmeses += 1
        elif en_pos:
            # salió este mes
            ret_op = comp * (1.0 - costo) * (1.0 - costo) - 1.0
            ops.append(OperacionTrend(fecha_ent, fecha, nmeses, ret_op * 100.0))
            en_pos = False
    if en_pos:
        ret_op = comp * (1.0 - costo) - 1.0
        ops.append(OperacionTrend(fecha_ent, None, nmeses, ret_op * 100.0, abierta_al_final=True))
    return ops


def operaciones_por_anio(operaciones: list[OperacionTrend]) -> pd.Series:
    """Cuántas operaciones (entradas) hubo por año calendario."""
    anios = [op.fecha_entrada.year for op in operaciones]
    if not anios:
        return pd.Series(dtype=int)
    return pd.Series(anios).value_counts().sort_index()


def tabla_sensibilidad_trend(
    ticker: str, ventanas: list[int], tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
) -> pd.DataFrame:
    """Métricas clave de la estrategia para varias ventanas de SMA (para detectar sobreajuste)."""
    filas = []
    for v in ventanas:
        res = backtest_sma_trend(ticker, ventana_meses=v, tasa_previa_anual=tasa_previa_anual, costo=costo)
        filas.append(
            {
                "SMA (meses)": v,
                "CAGR (%)": cagr(res.ret_estrategia) * 100.0,
                "Vol. anual (%)": vol_anualizada(res.ret_estrategia) * 100.0,
                "Sharpe": sharpe(res.ret_estrategia, res.cash),
                "Max Drawdown (%)": max_drawdown(res.ret_estrategia) * 100.0,
                "Calmar": calmar(res.ret_estrategia),
                "N° operaciones": len(res.operaciones),
            }
        )
    return pd.DataFrame(filas)


# ==========================================================
# MÓDULO 2 — DUAL MOMENTUM CON ETFs (SPY / EFA / efectivo)
# ==========================================================
@dataclass
class ResultadoDualMomentum:
    lookback: int
    excluir_ultimo: bool
    ret_estrategia: pd.Series
    ret_spy: pd.Series
    ret_6040: pd.Series
    cash: pd.Series
    posicion: pd.Series          # 'SPY' | 'EFA' | 'CASH' por mes (ya aplicada, shift(1))
    n_rotaciones: int
    drag_costos_pct: float        # costo total acumulado en % (suma de operaciones × costo)


def _lookback_return(precio_m: pd.Series, meses: int, excluir_ultimo: bool) -> pd.Series:
    """Retorno de `meses` meses; si excluir_ultimo, estilo '12-1' (excluye el mes más reciente)."""
    if excluir_ultimo:
        return precio_m.shift(1) / precio_m.shift(1 + meses) - 1.0
    return precio_m / precio_m.shift(meses) - 1.0


def backtest_dual_momentum(
    lookback: int = 12,
    excluir_ultimo: bool = False,
    tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
) -> ResultadoDualMomentum:
    """
    Dual momentum (Antonacci) ejecutable sin stock-picking:
      • Momentum relativo: cada fin de mes, entre SPY (EE.UU.) y EFA (internacional),
        se elige el de mayor retorno de `lookback` meses.
      • Momentum absoluto: si el retorno del ganador es menor al del efectivo (T-bills)
        en el mismo período, se va 100% a efectivo.
    La posición se aplica el mes siguiente (sin look-ahead). Rebalanceo solo si cambia
    la señal; costo por cada cambio de posición.
    """
    spy = precio_mensual("SPY")
    efa = precio_mensual("EFA")
    ief = precio_mensual("IEF")
    idx = spy.index.intersection(efa.index)
    spy = spy.reindex(idx)
    efa = efa.reindex(idx)
    cash = cash_mensual(idx, tasa_previa_anual)

    spy_ret = spy.pct_change()
    efa_ret = efa.pct_change()
    mom_spy = _lookback_return(spy, lookback, excluir_ultimo)
    mom_efa = _lookback_return(efa, lookback, excluir_ultimo)
    cash_lb = (1.0 + cash).rolling(lookback).apply(np.prod, raw=True) - 1.0

    señal = pd.Series(index=idx, dtype=object)
    for f in idx:
        ms, me, cl = mom_spy.loc[f], mom_efa.loc[f], cash_lb.loc[f]
        if pd.isna(ms) or pd.isna(me) or pd.isna(cl):
            continue
        if ms >= me:
            ganador_mom, ganador = ms, "SPY"
        else:
            ganador_mom, ganador = me, "EFA"
        señal.loc[f] = "CASH" if ganador_mom < cl else ganador

    posicion = señal.shift(1)

    ret_map = {"SPY": spy_ret, "EFA": efa_ret, "CASH": cash}
    strat = pd.Series(index=idx, dtype=float)
    for f in idx:
        p = posicion.loc[f]
        if p in ret_map:
            strat.loc[f] = ret_map[p].loc[f]

    cambio = posicion.ne(posicion.shift(1)) & posicion.notna() & posicion.shift(1).notna()
    strat = strat - cambio.astype(float) * costo

    valido = posicion.notna() & strat.notna()
    strat = strat[valido]
    posicion_v = posicion[valido]

    spy_bh = spy_ret[valido]
    ief_ret = ief.pct_change().reindex(strat.index)
    ret_6040 = (0.6 * spy_bh + 0.4 * ief_ret.fillna(cash[valido])).rename("60/40")

    n_rotaciones = int(cambio[valido].sum())
    drag = n_rotaciones * costo * 100.0

    return ResultadoDualMomentum(
        lookback=lookback, excluir_ultimo=excluir_ultimo, ret_estrategia=strat,
        ret_spy=spy_bh, ret_6040=ret_6040, cash=cash[valido], posicion=posicion_v,
        n_rotaciones=n_rotaciones, drag_costos_pct=drag,
    )


def regimen_vix_momentum(ret_estrategia: pd.Series) -> pd.DataFrame:
    """
    Retorno mensual promedio de la estrategia según el tercil del VIX del mes ANTERIOR
    (bajo / medio / alto). Verifica si el momentum se debilita en alta volatilidad.
    """
    vix = precio_mensual("^VIX")  # nivel de cierre de fin de mes
    vix_prev = vix.shift(1).reindex(ret_estrategia.index)
    df = pd.DataFrame({"ret": ret_estrategia, "vix_prev": vix_prev}).dropna()
    if df.empty:
        return pd.DataFrame()
    df["tercil"] = pd.qcut(df["vix_prev"], 3, labels=["VIX bajo", "VIX medio", "VIX alto"])
    agg = df.groupby("tercil", observed=True)["ret"].agg(
        **{"Retorno mensual prom. (%)": lambda s: s.mean() * 100.0,
           "% meses positivos": lambda s: (s > 0).mean() * 100.0,
           "N° meses": "count"}
    )
    return agg.reset_index().rename(columns={"tercil": "Régimen (VIX mes previo)"})


# ==========================================================
# MÓDULO 5 — RETORNOS FORWARD SEGÚN PROFUNDIDAD DE CAÍDA
# ==========================================================
UMBRALES_CAIDA_DEFAULT = [10, 15, 20, 30, 40, 50]
HORIZONTES_ANIOS_DEFAULT = [1, 3, 5, 10]


@dataclass
class EventoCaida:
    fecha_cruce: pd.Timestamp
    caida_adicional_pct: float               # cuánto MÁS cayó desde el cruce (peor punto del ciclo)
    dias_a_recuperar: float | None           # días hasta recuperar el máximo previo (None si no recuperó)
    forward: dict = field(default_factory=dict)   # {años: retorno_pct or nan}
    peak_idx: int = 0


def analizar_caidas(
    precio_diario: pd.Series,
    umbrales: list[int] = None,
    horizontes_anios: list[int] = None,
) -> dict[int, list[EventoCaida]]:
    """
    Para cada umbral de caída (%), detecta el PRIMER cruce dentro de cada ciclo de
    drawdown (no se vuelve a contar hasta recuperar el máximo histórico previo) y
    calcula, desde ese cruce: retornos forward a varios horizontes, la caída adicional
    máxima, y el tiempo hasta recuperar el máximo previo.
    """
    umbrales = umbrales or UMBRALES_CAIDA_DEFAULT
    horizontes_anios = horizontes_anios or HORIZONTES_ANIOS_DEFAULT
    precio = precio_diario.dropna()
    if precio.empty:
        return {u: [] for u in umbrales}

    vals = precio.values.astype(float)
    idx = precio.index
    n = len(vals)

    eventos: dict[int, list[EventoCaida]] = {u: [] for u in umbrales}
    peak_val = vals[0]
    peak_idx = 0
    crossed = {u: False for u in umbrales}

    for i in range(n):
        if vals[i] >= peak_val:
            peak_val = vals[i]
            peak_idx = i
            crossed = {u: False for u in umbrales}
            continue
        cur_dd = vals[i] / peak_val - 1.0
        for u in umbrales:
            if not crossed[u] and cur_dd <= -u / 100.0:
                crossed[u] = True
                # recuperación del máximo previo y caída adicional dentro del ciclo
                rec_dias = None
                min_post = vals[i]
                j = i
                while j < n and vals[j] < peak_val:
                    if vals[j] < min_post:
                        min_post = vals[j]
                    j += 1
                if j < n:  # recuperó el máximo previo en idx[j]
                    rec_dias = float((idx[j] - idx[i]).days)
                caida_adic = min_post / vals[i] - 1.0

                fwd = {}
                for h in horizontes_anios:
                    objetivo = idx[i] + pd.Timedelta(days=int(round(h * 365.25)))
                    pos = int(idx.searchsorted(objetivo))
                    fwd[h] = (vals[pos] / vals[i] - 1.0) * 100.0 if pos < n else float("nan")

                eventos[u].append(
                    EventoCaida(
                        fecha_cruce=idx[i], caida_adicional_pct=caida_adic * 100.0,
                        dias_a_recuperar=rec_dias, forward=fwd, peak_idx=peak_idx,
                    )
                )
    return eventos


def resumen_caidas(
    eventos: dict[int, list[EventoCaida]], horizontes_anios: list[int] = None,
) -> pd.DataFrame:
    """Tabla resumen por umbral: nº eventos, forward promedio/mediano/mín/%positivo, riesgo."""
    horizontes_anios = horizontes_anios or HORIZONTES_ANIOS_DEFAULT
    filas = []
    for u, evs in eventos.items():
        fila = {"Umbral": f"-{u}%", "N° eventos": len(evs)}
        for h in horizontes_anios:
            vals = [e.forward[h] for e in evs if h in e.forward and pd.notna(e.forward[h])]
            if vals:
                arr = np.array(vals)
                fila[f"Fwd {h}a prom. (%)"] = float(arr.mean())
                fila[f"Fwd {h}a mediana (%)"] = float(np.median(arr))
                fila[f"Fwd {h}a mín. (%)"] = float(arr.min())
                fila[f"Fwd {h}a % positivo"] = float((arr > 0).mean() * 100.0)
            else:
                for suf in ["prom. (%)", "mediana (%)", "mín. (%)", "% positivo"]:
                    fila[f"Fwd {h}a {suf}"] = float("nan")
        caidas_adic = [e.caida_adicional_pct for e in evs]
        recs = [e.dias_a_recuperar / 365.25 for e in evs if e.dias_a_recuperar is not None]
        fila["Caída adic. peor (%)"] = float(min(caidas_adic)) if caidas_adic else float("nan")
        fila["Años recuperar (prom.)"] = float(np.mean(recs)) if recs else float("nan")
        fila["Años recuperar (peor)"] = float(max(recs)) if recs else float("nan")
        filas.append(fila)
    return pd.DataFrame(filas)


def prob_escalada_caida(eventos: dict[int, list[EventoCaida]], desde: int, hasta: int) -> float:
    """Probabilidad histórica de que un ciclo que cruzó -`desde`% también cruzara -`hasta`%."""
    ciclos_desde = {e.peak_idx for e in eventos.get(desde, [])}
    ciclos_hasta = {e.peak_idx for e in eventos.get(hasta, [])}
    if not ciclos_desde:
        return float("nan")
    return len(ciclos_desde & ciclos_hasta) / len(ciclos_desde) * 100.0


# ==========================================================
# MÓDULO 3 — DESEMPLEO COMO INDICADOR CONTRARIO (descriptivo)
# ==========================================================
def _unrate_mensual_con_rezago() -> pd.Series:
    """
    UNRATE de FRED con rezago de PUBLICACIÓN: el dato de un mes se publica el mes
    siguiente, así que se aplica shift(+1). Indexado a fin de mes.
    """
    un = fred_series("UNRATE")
    if un.empty:
        return un
    un = un.shift(1)  # rezago de publicación de 1 mes
    return un.resample("ME").last().dropna().rename("UNRATE")


def _sp500_mensual() -> pd.Series:
    """S&P 500 (^GSPC, índice de precio sin dividendos) mensual."""
    return precio_mensual("^GSPC").rename("SP500")


def tabla_desempleo_forward(usar_percentil_expansivo: bool = False) -> pd.DataFrame:
    """
    Retorno forward del S&P (12/24/36m) por quintil de desempleo.
      • usar_percentil_expansivo=False: quintiles de MUESTRA COMPLETA (⚠️ look-ahead,
        solo descriptivo).
      • True: percentil con ventana EXPANSIVA (solo datos hasta esa fecha, mín. 20 años),
        operable sin look-ahead.
    """
    un = _unrate_mensual_con_rezago()
    sp = _sp500_mensual()
    if un.empty or sp.empty:
        return pd.DataFrame()
    idx = un.index.intersection(sp.index)
    un = un.reindex(idx)
    sp = sp.reindex(idx)

    fwd = {h: sp.shift(-h) / sp - 1.0 for h in (12, 24, 36)}

    if usar_percentil_expansivo:
        min_obs = 240  # 20 años
        pct = pd.Series(index=idx, dtype=float)
        arr = un.values
        for i in range(len(idx)):
            if i + 1 >= min_obs:
                ventana = arr[: i + 1]
                pct.iloc[i] = (ventana <= arr[i]).mean() * 100.0
        grupo = pd.cut(pct, [0, 20, 40, 60, 80, 100], labels=["Q1 (bajo)", "Q2", "Q3", "Q4", "Q5 (alto)"])
    else:
        grupo = pd.qcut(un, 5, labels=["Q1 (bajo)", "Q2", "Q3", "Q4", "Q5 (alto)"])

    filas = []
    for q in ["Q1 (bajo)", "Q2", "Q3", "Q4", "Q5 (alto)"]:
        mask = grupo == q
        fila = {"Quintil desempleo": q, "N° meses": int(mask.sum())}
        for h in (12, 24, 36):
            serie = fwd[h][mask].dropna()
            fila[f"Fwd {h}m prom. (%)"] = float(serie.mean() * 100.0) if len(serie) else float("nan")
            fila[f"Fwd {h}m mediana (%)"] = float(serie.median() * 100.0) if len(serie) else float("nan")
            fila[f"Fwd {h}m peor (%)"] = float(serie.min() * 100.0) if len(serie) else float("nan")
        filas.append(fila)
    return pd.DataFrame(filas)


def matriz_nivel_direccion_desempleo() -> pd.DataFrame:
    """
    Matriz 2×2: retorno forward 12m del S&P según nivel de desempleo (alto/bajo vs su
    mediana histórica) × dirección (subiendo/bajando vs su media móvil de 12 meses).
    """
    un = _unrate_mensual_con_rezago()
    sp = _sp500_mensual()
    if un.empty or sp.empty:
        return pd.DataFrame()
    idx = un.index.intersection(sp.index)
    un = un.reindex(idx)
    sp = sp.reindex(idx)
    fwd12 = sp.shift(-12) / sp - 1.0
    media12 = un.rolling(12).mean()
    mediana_hist = un.median()

    nivel = np.where(un >= mediana_hist, "Desempleo alto", "Desempleo bajo")
    direccion = np.where(un > media12, "subiendo", "bajando")
    df = pd.DataFrame({"nivel": nivel, "direccion": direccion, "fwd12": fwd12.values}, index=idx).dropna()

    filas = []
    for niv in ["Desempleo alto", "Desempleo bajo"]:
        fila = {"Nivel": niv}
        for dire in ["subiendo", "bajando"]:
            serie = df[(df["nivel"] == niv) & (df["direccion"] == dire)]["fwd12"]
            fila[f"Fwd 12m {dire} (%)"] = float(serie.mean() * 100.0) if len(serie) else float("nan")
        filas.append(fila)
    return pd.DataFrame(filas)


# ==========================================================
# MÓDULO 6 — SEÑAL COMPUESTA (score 0-4 -> exposición)
# ==========================================================
@dataclass
class ResultadoCompuesta:
    señales: pd.DataFrame        # columnas: tendencia, momentum, vol, macro (0/1 por mes)
    score: pd.Series
    exposicion: pd.Series        # % acciones aplicado cada mes (ya shift(1))
    ret_estrategia: pd.Series
    ret_spy: pd.Series
    ret_6040: pd.Series
    cash: pd.Series


def _señales_compuestas() -> pd.DataFrame:
    """
    Las 4 señales mensuales (0/1) del score compuesto, alineadas a fin de mes:
      1. Tendencia: SPY > su SMA de 10 meses.
      2. Momentum absoluto: retorno 12m de SPY > retorno 12m del efectivo.
      3. Régimen de volatilidad: percentil rodante 5 años del VIX < 80 (régimen tranquilo).
      4. Macro contrario: percentil expansivo de UNRATE > 80 Y desempleo bajo su media 12m.
    """
    spy = precio_mensual("SPY")
    cash = cash_mensual(spy.index)
    vix = precio_mensual("^VIX")

    sma10 = spy.rolling(10).mean()
    tendencia = (spy > sma10).astype(float).where(sma10.notna())

    spy_12m = spy / spy.shift(12) - 1.0
    cash_12m = (1.0 + cash).rolling(12).apply(np.prod, raw=True) - 1.0
    momentum = (spy_12m > cash_12m).astype(float).where(spy_12m.notna() & cash_12m.notna())

    vix_al = vix.reindex(spy.index)
    pct_vix = vix_al.rolling(60).apply(lambda w: (w <= w[-1]).mean() * 100.0, raw=True)
    vol_ok = (pct_vix < 80).astype(float).where(pct_vix.notna())

    un = _unrate_mensual_con_rezago().reindex(spy.index)
    media12_un = un.rolling(12).mean()
    if un.notna().any():
        pct_un = pd.Series(index=spy.index, dtype=float)
        arr = un.values
        vistos = 0
        for i in range(len(spy.index)):
            if pd.notna(arr[i]):
                vistos += 1
                if vistos >= 240:
                    hist = arr[: i + 1]
                    hist = hist[~np.isnan(hist)]
                    pct_un.iloc[i] = (hist <= arr[i]).mean() * 100.0
        macro = ((pct_un > 80) & (un < media12_un)).astype(float).where(pct_un.notna())
    else:
        macro = pd.Series(0.0, index=spy.index)

    return pd.DataFrame({
        "Tendencia": tendencia, "Momentum": momentum, "Volatilidad": vol_ok, "Macro": macro,
    })


def _correr_compuesta(
    señales: pd.DataFrame, mapeo: str, costo: float, n_max_score: int,
) -> ResultadoCompuesta:
    """Núcleo del módulo 6: convierte un set de señales (0/1) en score, exposición y retornos."""
    spy = precio_mensual("SPY")
    ief = precio_mensual("IEF")
    idx = señales.dropna().index
    señales = señales.reindex(idx)
    cash = cash_mensual(idx)
    spy_ret = spy.pct_change().reindex(idx)

    score = señales.sum(axis=1)
    if mapeo == "lineal":
        tope = min(3, n_max_score)
        exposicion = np.minimum(score, tope) / float(tope)
    else:
        mapa = {0: 0.0, 1: 0.4, 2: 0.7, 3: 1.0, 4: 1.0}
        exposicion = score.map(mapa).clip(upper=1.0)

    exp_aplicada = exposicion.shift(1)
    strat = exp_aplicada * spy_ret + (1.0 - exp_aplicada) * cash
    delta_exp = (exp_aplicada - exp_aplicada.shift(1)).abs()
    strat = strat - delta_exp.fillna(0.0) * costo

    valido = exp_aplicada.notna() & strat.notna()
    strat = strat[valido]
    ief_ret = ief.pct_change().reindex(strat.index)
    ret_6040 = 0.6 * spy_ret[valido] + 0.4 * ief_ret.fillna(cash[valido])

    return ResultadoCompuesta(
        señales=señales.reindex(strat.index), score=score.reindex(strat.index),
        exposicion=exp_aplicada[valido], ret_estrategia=strat, ret_spy=spy_ret[valido],
        ret_6040=ret_6040, cash=cash[valido],
    )


def backtest_senal_compuesta(mapeo: str = "escalonado", costo: float = COSTO_OPERACION) -> ResultadoCompuesta:
    """
    Score 0-4 (suma de las 4 señales) -> exposición a acciones (resto en efectivo).
      • mapeo 'escalonado': 0→0%, 1→40%, 2→70%, 3→100%, 4→100%.
      • mapeo 'lineal': min(score,3)/3 → exposición proporcional (el punto macro solo confirma).
    """
    señales = _señales_compuestas().dropna(how="all")
    return _correr_compuesta(señales, mapeo, costo, n_max_score=4)


def contribucion_señales(mapeo: str = "escalonado", costo: float = COSTO_OPERACION) -> pd.DataFrame:
    """Backtest quitando una señal a la vez, para ver cuál aporta y cuál sobra."""
    señales = _señales_compuestas().dropna(how="all")
    filas = []

    def _fila(nombre, res):
        return {
            "Configuración": nombre,
            "CAGR (%)": cagr(res.ret_estrategia) * 100.0,
            "Sharpe": sharpe(res.ret_estrategia, res.cash),
            "Max Drawdown (%)": max_drawdown(res.ret_estrategia) * 100.0,
            "Calmar": calmar(res.ret_estrategia),
        }

    filas.append(_fila("Completa (4 señales)", _correr_compuesta(señales, mapeo, costo, 4)))
    for col in señales.columns:
        restantes = [c for c in señales.columns if c != col]
        res = _correr_compuesta(señales[restantes], mapeo, costo, len(restantes))
        filas.append(_fila(f"Sin '{col}'", res))
    return pd.DataFrame(filas)


# ==========================================================
# MÓDULO 4 — SEÑALES DE VIX (nivel, percentil rodante, estructura de plazos)
# ==========================================================
HORIZONTES_VIX_DIAS = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}


def vix_diario() -> pd.Series:
    """VIX diario de CBOE (desde 1990)."""
    return load_cboe_index("VIX")


def vix3m_diario() -> pd.Series:
    """VIX3M diario de CBOE (desde ~2009)."""
    return load_cboe_index("VIX3M")


def _spy_vix_alineados() -> tuple[pd.Series, pd.Series]:
    """SPY (retorno total) y VIX de CBOE alineados al calendario de SPY."""
    spy = get_daily_closes("SPY").dropna()
    vix = vix_diario().reindex(spy.index).ffill()
    return spy, vix


def _episodios_desde_señal(señal_bool: pd.Series, sep_min_dias: int = 21) -> list[pd.Timestamp]:
    """
    Primeras fechas de cada episodio. Días consecutivos en señal cuentan como UN
    episodio; dos episodios se separan si hay al menos `sep_min_dias` días hábiles
    sin señal entre medias.
    """
    idx = señal_bool.index
    activos = señal_bool.fillna(False).values.astype(bool)
    episodios: list[pd.Timestamp] = []
    dias_false = sep_min_dias  # para que el primer True cuente como episodio nuevo
    for i in range(len(activos)):
        if activos[i]:
            if dias_false >= sep_min_dias:
                episodios.append(idx[i])
            dias_false = 0
        else:
            dias_false += 1
    return episodios


def _forward_returns(precio: pd.Series, fechas: list[pd.Timestamp], horizontes_dias: list[int]) -> dict:
    idx = precio.index
    vals = precio.values.astype(float)
    n = len(vals)
    res = {h: [] for h in horizontes_dias}
    for f in fechas:
        pos0 = int(idx.searchsorted(f))
        if pos0 >= n:
            continue
        for h in horizontes_dias:
            posh = int(idx.searchsorted(f + pd.Timedelta(days=h)))
            if posh < n:
                res[h].append(vals[posh] / vals[pos0] - 1.0)
    return res


def _fila_forward_vix(label: str, spy: pd.Series, fechas: list[pd.Timestamp]) -> dict:
    fwd = _forward_returns(spy, fechas, list(HORIZONTES_VIX_DIAS.values()))
    fila = {"Señal": label, "N° episodios": len(fechas)}
    for nombre, h in HORIZONTES_VIX_DIAS.items():
        arr = np.array(fwd[h])
        if len(arr):
            fila[f"{nombre} prom (%)"] = float(arr.mean() * 100.0)
            fila[f"{nombre} mediana (%)"] = float(np.median(arr) * 100.0)
            fila[f"{nombre} % pos"] = float((arr > 0).mean() * 100.0)
            fila[f"{nombre} peor (%)"] = float(arr.min() * 100.0)
        else:
            for suf in ["prom (%)", "mediana (%)", "% pos", "peor (%)"]:
                fila[f"{nombre} {suf}"] = float("nan")
    return fila


def percentil_rodante_vix(vix: pd.Series, ventana_dias: int = 1260) -> pd.Series:
    """Percentil (0-100) del VIX de hoy dentro de su ventana rodante de `ventana_dias`."""
    return vix.rolling(ventana_dias).apply(lambda w: (w <= w[-1]).mean() * 100.0, raw=True)


def señales_vix() -> dict:
    """
    Devuelve las tres familias de señales de compra por VIX, ya alineadas a SPY:
      A) nivel absoluto (VIX >= umbral)
      B) percentil rodante de 5 años (percentil >= umbral)
      C) estructura de plazos: backwardation, ratio VIX/VIX3M >= umbral
    """
    spy, vix = _spy_vix_alineados()
    pct5 = percentil_rodante_vix(vix, 1260)
    vix3m = vix3m_diario().reindex(spy.index).ffill()
    ratio = (vix / vix3m).where(vix3m.notna())
    return {"spy": spy, "vix": vix, "pct5": pct5, "vix3m": vix3m, "ratio": ratio}


def resumen_señal_a(umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [20, 25, 30, 35, 40]
    d = señales_vix()
    spy, vix = d["spy"], d["vix"]
    filas = [_fila_forward_vix("Incondicional (cualquier día)", spy, list(spy.index))]
    for u in umbrales:
        fechas = _episodios_desde_señal(vix >= u)
        filas.append(_fila_forward_vix(f"VIX ≥ {u:.0f}", spy, fechas))
    return pd.DataFrame(filas)


def resumen_señal_b(umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [80, 90, 95]
    d = señales_vix()
    spy, pct5 = d["spy"], d["pct5"]
    filas = [_fila_forward_vix("Incondicional (cualquier día)", spy, list(spy.index))]
    for u in umbrales:
        fechas = _episodios_desde_señal(pct5 >= u)
        filas.append(_fila_forward_vix(f"Percentil 5a ≥ {u:.0f}", spy, fechas))
    return pd.DataFrame(filas)


def resumen_señal_b_ventanas(umbral: float = 90, ventanas_anios: list[int] = None) -> pd.DataFrame:
    """Sensibilidad de la señal B a la ventana del percentil (3/5/10 años)."""
    ventanas_anios = ventanas_anios or [3, 5, 10]
    spy, vix = _spy_vix_alineados()
    filas = []
    for va in ventanas_anios:
        pct = percentil_rodante_vix(vix, va * 252)
        fechas = _episodios_desde_señal(pct >= umbral)
        fila = _fila_forward_vix(f"Ventana {va} años", spy, fechas)
        filas.append(fila)
    return pd.DataFrame(filas)


def resumen_señal_c(umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [0.95, 1.00, 1.05]
    d = señales_vix()
    spy, ratio = d["spy"], d["ratio"]
    filas = [_fila_forward_vix("Incondicional (cualquier día)", spy, list(spy.index))]
    for u in umbrales:
        fechas = _episodios_desde_señal(ratio >= u)
        filas.append(_fila_forward_vix(f"VIX/VIX3M ≥ {u:.2f}", spy, fechas))
    return pd.DataFrame(filas)


def matriz_solapamiento_vix(umbral_a: float = 30, umbral_b: float = 90, umbral_c: float = 1.00) -> pd.DataFrame:
    """
    Matriz de coincidencia diaria entre las tres señales (solo fechas donde las tres
    tienen dato, es decir desde el inicio del VIX3M). Cada celda = P(columna activa |
    fila activa) en %.
    """
    d = señales_vix()
    df = pd.DataFrame({
        f"A: VIX≥{umbral_a:.0f}": d["vix"] >= umbral_a,
        f"B: pct5≥{umbral_b:.0f}": d["pct5"] >= umbral_b,
        f"C: ratio≥{umbral_c:.2f}": d["ratio"] >= umbral_c,
    }).dropna()
    cols = df.columns
    mat = pd.DataFrame(index=cols, columns=cols, dtype=float)
    for a in cols:
        base = df[df[a]]
        for b in cols:
            mat.loc[a, b] = (base[b].mean() * 100.0) if len(base) else float("nan")
    return mat


def episodios_para_grafico(umbral_a: float = 30, umbral_b: float = 90, umbral_c: float = 1.00) -> dict:
    """Fechas de episodios de cada señal, para marcarlas sobre la línea del SPY."""
    d = señales_vix()
    return {
        "spy": d["spy"],
        "A": _episodios_desde_señal(d["vix"] >= umbral_a),
        "B": _episodios_desde_señal(d["pct5"] >= umbral_b),
        "C": _episodios_desde_señal(d["ratio"] >= umbral_c),
    }
