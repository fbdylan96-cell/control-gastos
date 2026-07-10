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


def cash_diario(index_diario: pd.DatetimeIndex, tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL) -> pd.Series:
    """
    Retorno DIARIO del efectivo (T-bills), alineado a `index_diario`. Misma jerarquía que
    `cash_mensual`: (1) BIL (retorno total del ETF) donde exista; (2) TB3MS anualizado → diario;
    (3) tasa fija de respaldo. Pensado para backtests de frecuencia diaria.
    """
    tasa_prev_d = (1.0 + tasa_previa_anual) ** (1.0 / 252.0) - 1.0
    resultado = pd.Series(tasa_prev_d, index=index_diario, name="cash")

    tb3 = fred_series("TB3MS")
    if not tb3.empty:
        tb3_d = (1.0 + tb3 / 100.0) ** (1.0 / 252.0) - 1.0  # tasa anualizada (%) → retorno diario
        alineado = tb3_d.reindex(index_diario, method="ffill")
        resultado = alineado.fillna(resultado).rename("cash")

    bil = get_daily_closes("BIL")
    if not bil.empty:
        bil_ret = bil.reindex(index_diario).pct_change()
        resultado = bil_ret.fillna(resultado).rename("cash")

    return resultado


def particion_muestra(serie: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Divide una serie mensual en (in-sample, holdout) según FECHA_HOLDOUT."""
    return serie[serie.index < FECHA_HOLDOUT], serie[serie.index >= FECHA_HOLDOUT]


PERIODOS_POR_ANIO = {"ME": 12, "W": 52}


def precio_periodico(ticker: str, freq: str = "ME") -> pd.Series:
    """Cierre ajustado del último dato de cada período (mes 'ME' o semana 'W')."""
    diario = get_daily_closes(ticker)
    if diario.empty:
        return pd.Series(dtype=float, name=ticker)
    return diario.resample(freq).last().dropna().rename(ticker)


def cash_periodico(index_periodico: pd.DatetimeIndex, freq: str = "ME",
                   tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL) -> pd.Series:
    """Retorno del efectivo por período (mensual o semanal), alineado a `index_periodico`."""
    ppa = PERIODOS_POR_ANIO.get(freq, 12)
    tasa_prev = (1.0 + tasa_previa_anual) ** (1.0 / ppa) - 1.0
    resultado = pd.Series(tasa_prev, index=index_periodico, name="cash")
    tb3 = fred_series("TB3MS")
    if not tb3.empty:
        tb3_ret = ((1.0 + tb3 / 100.0) ** (1.0 / ppa) - 1.0).resample(freq).last()
        resultado = tb3_ret.reindex(index_periodico).fillna(resultado).rename("cash")
    bil = get_daily_closes("BIL")
    if not bil.empty:
        bil_ret = bil.resample(freq).last().pct_change().reindex(index_periodico)
        resultado = bil_ret.fillna(resultado).rename("cash")
    return resultado


# ==========================================================
# MÉTRICAS (módulo compartido)
# ==========================================================
def _curva_capital(retornos: pd.Series) -> pd.Series:
    return (1.0 + retornos.dropna()).cumprod()


def cagr(retornos: pd.Series, periodos_anio: int = MESES_ANIO) -> float:
    r = retornos.dropna()
    if len(r) == 0:
        return 0.0
    total = float((1.0 + r).prod())
    anios = len(r) / periodos_anio
    if anios <= 0 or total <= 0:
        return 0.0
    return total ** (1.0 / anios) - 1.0


def vol_anualizada(retornos: pd.Series, periodos_anio: int = MESES_ANIO) -> float:
    r = retornos.dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1)) * np.sqrt(periodos_anio)


def sharpe(retornos: pd.Series, cash: pd.Series, periodos_anio: int = MESES_ANIO) -> float:
    r = retornos.dropna()
    rf = cash.reindex(r.index).fillna(0.0)
    exceso = r - rf
    vol = float(exceso.std(ddof=1))
    if len(exceso) < 2 or vol == 0:
        return 0.0
    return float(exceso.mean()) * periodos_anio / (vol * np.sqrt(periodos_anio))


def sortino(retornos: pd.Series, cash: pd.Series, periodos_anio: int = MESES_ANIO) -> float:
    r = retornos.dropna()
    rf = cash.reindex(r.index).fillna(0.0)
    exceso = r - rf
    downside = exceso[exceso < 0]
    dd = float(np.sqrt((downside ** 2).mean())) if len(downside) else 0.0
    if len(exceso) < 2 or dd == 0:
        return 0.0
    return float(exceso.mean()) * periodos_anio / (dd * np.sqrt(periodos_anio))


def max_drawdown(retornos: pd.Series) -> float:
    eq = _curva_capital(retornos)
    if eq.empty:
        return 0.0
    return float((eq / eq.cummax() - 1.0).min())


def calmar(retornos: pd.Series, periodos_anio: int = MESES_ANIO) -> float:
    dd = abs(max_drawdown(retornos))
    return cagr(retornos, periodos_anio) / dd if dd > 0 else 0.0


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
    señal: pd.Series          # 1 invertido, 0 efectivo (calculada a fin de período)
    posicion: pd.Series       # señal.shift(1): lo que realmente se aplica cada período
    ret_estrategia: pd.Series
    ret_buy_hold: pd.Series
    cash: pd.Series
    operaciones: list[OperacionTrend] = field(default_factory=list)
    freq: str = "ME"


def backtest_sma_trend(
    ticker: str,
    ventana_meses: int = 10,
    tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
    freq: str = "ME",
) -> ResultadoTrend:
    """
    Modelo de Faber generalizado: al cierre de cada período (mes 'ME' o semana 'W'), si
    el precio está por encima de su SMA de `ventana_meses` períodos, se está 100%
    invertido; si no, 100% en efectivo. La posición se aplica el período SIGUIENTE
    (sin look-ahead).
    """
    precio_m = precio_periodico(ticker, freq)
    ret_m = precio_m.pct_change()
    sma = precio_m.rolling(ventana_meses).mean()

    señal = (precio_m > sma).astype(float).where(sma.notna())
    posicion = señal.shift(1)  # se ejecuta el período siguiente al cierre que generó la señal
    cash = cash_periodico(precio_m.index, freq, tasa_previa_anual)

    strat = ret_m.where(posicion == 1.0, cash)
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
        cash=cash_v, operaciones=operaciones, freq=freq,
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
    costo: float = COSTO_OPERACION, freq: str = "ME",
) -> pd.DataFrame:
    """
    Métricas de la estrategia para MUCHAS ventanas de SMA (para juzgar robustez vs
    sobreajuste). `freq`='ME' → ventana en meses; 'W' → ventana en semanas.
    """
    ppa = PERIODOS_POR_ANIO.get(freq, 12)
    etiqueta = "SMA (meses)" if freq == "ME" else "SMA (semanas)"
    # Precomputar precio/retorno/efectivo UNA vez y reusar en cada ventana (mucho más rápido).
    precio = precio_periodico(ticker, freq)
    ret = precio.pct_change()
    cash = cash_periodico(precio.index, freq, tasa_previa_anual)
    filas = []
    for v in ventanas:
        sma = precio.rolling(v).mean()
        pos = (precio > sma).astype(float).where(sma.notna()).shift(1)
        strat = ret.where(pos == 1.0, cash)
        cambio = pos.fillna(0.0).diff().abs() > 0
        strat = strat - cambio.astype(float) * costo
        valido = pos.notna() & ret.notna()
        strat = strat[valido]
        cash_v = cash[valido]
        pos_v = pos[valido]
        if len(strat) < ppa:
            continue
        n_ops = int(((pos_v == 1.0) & (pos_v.shift(1) != 1.0)).sum())
        filas.append(
            {
                etiqueta: v,
                "CAGR (%)": cagr(strat, ppa) * 100.0,
                "Vol. anual (%)": vol_anualizada(strat, ppa) * 100.0,
                "Sharpe": sharpe(strat, cash_v, ppa),
                "Max Drawdown (%)": max_drawdown(strat) * 100.0,
                "Calmar": calmar(strat, ppa),
                "N° operaciones": n_ops,
            }
        )
    return pd.DataFrame(filas)


def resumen_robustez_trend(df_sens: pd.DataFrame) -> dict:
    """
    A partir de la tabla de sensibilidad, resume qué tan ROBUSTA es la estrategia:
    promedio, desviación y rango de las métricas a través de todas las ventanas. Poca
    dispersión (bajo desvío, rango angosto) = el resultado no depende de un único número
    mágico. También reporta el coeficiente de variación del CAGR.
    """
    if df_sens is None or df_sens.empty:
        return {}
    out = {}
    for col in ["CAGR (%)", "Sharpe", "Max Drawdown (%)", "Calmar"]:
        s = df_sens[col].dropna()
        if s.empty:
            continue
        out[col] = {
            "prom": float(s.mean()), "desv": float(s.std(ddof=1)) if len(s) > 1 else 0.0,
            "min": float(s.min()), "max": float(s.max()),
        }
    cagr_s = df_sens["CAGR (%)"].dropna()
    cv = float(cagr_s.std(ddof=1) / abs(cagr_s.mean())) if len(cagr_s) > 1 and cagr_s.mean() != 0 else float("nan")
    out["cv_cagr"] = cv
    out["n_ventanas"] = int(len(df_sens))
    out["n_cagr_positivo"] = int((cagr_s > 0).sum())
    return out


# ==========================================================
# MÓDULO 2 — DUAL MOMENTUM CON ETFs (SPY / EFA / efectivo)
# ==========================================================
# Universo de ETFs disponible para el momentum (con la clase de activo y desde cuándo
# hay datos, para orientar al usuario). Momentum "dual" = relativo (elegir el mejor) +
# absoluto (si ni el mejor le gana al efectivo, irse a efectivo).
TICKERS_MOMENTUM_DISPONIBLES = {
    "SPY": "Acciones EE.UU. (S&P 500)",
    "QQQ": "Acciones EE.UU. tecnología (Nasdaq-100)",
    "EFA": "Acciones internacional desarrollado",
    "EEM": "Acciones mercados emergentes",
    "GLD": "Oro",
    "IEF": "Bonos del tesoro 7-10 años",
    "TLT": "Bonos del tesoro 20+ años",
    "VNQ": "Bienes raíces (REITs)",
    "DBC": "Materias primas",
}


@dataclass
class ResultadoDualMomentum:
    tickers: list
    lookback: int
    excluir_ultimo: bool
    ret_estrategia: pd.Series
    ret_spy: pd.Series
    ret_6040: pd.Series
    cash: pd.Series
    posicion: pd.Series          # activo tenido cada mes (o 'CASH'), ya aplicada (shift(1))
    n_rotaciones: int
    drag_costos_pct: float


def _lookback_return(precio_m: pd.Series, meses: int, excluir_ultimo: bool) -> pd.Series:
    """Retorno de `meses` meses; si excluir_ultimo, estilo '12-1' (excluye el mes más reciente)."""
    if excluir_ultimo:
        return precio_m.shift(1) / precio_m.shift(1 + meses) - 1.0
    return precio_m / precio_m.shift(meses) - 1.0


def backtest_dual_momentum(
    tickers: list = None,
    lookback: int = 12,
    excluir_ultimo: bool = False,
    tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
) -> ResultadoDualMomentum:
    """
    Dual momentum generalizado a un universo de N ETFs:
      • Momentum RELATIVO: cada fin de mes se elige el ETF con mayor retorno de
        `lookback` meses.
      • Momentum ABSOLUTO: si el retorno de ese ganador es MENOR al del efectivo
        (T-bills) en el mismo período, se va 100% a efectivo — es un filtro de
        "tendencia": si ni el mejor activo le gana a no arriesgar nada, mejor no
        arriesgar.
    La posición se aplica el mes siguiente (sin look-ahead); costo por cada cambio.
    """
    tickers = tickers or ["SPY", "EFA"]
    precios = {t: precio_mensual(t) for t in tickers}
    precios = {t: p for t, p in precios.items() if not p.empty}
    if len(precios) < 1:
        idx = pd.DatetimeIndex([])
    else:
        idx = None
        for p in precios.values():
            idx = p.index if idx is None else idx.intersection(p.index)
    precios = {t: p.reindex(idx) for t, p in precios.items()}
    cash = cash_mensual(idx, tasa_previa_anual)

    rets = {t: p.pct_change() for t, p in precios.items()}
    mom_df = pd.DataFrame({t: _lookback_return(p, lookback, excluir_ultimo) for t, p in precios.items()})
    cash_lb = (1.0 + cash).rolling(lookback).apply(np.prod, raw=True) - 1.0

    señal = pd.Series(index=idx, dtype=object)
    for f in idx:
        fila = mom_df.loc[f]
        if fila.isna().any() or pd.isna(cash_lb.loc[f]):
            continue
        ganador = fila.idxmax()
        señal.loc[f] = "CASH" if fila.max() < cash_lb.loc[f] else ganador

    posicion = señal.shift(1)
    ret_map = {**rets, "CASH": cash}
    strat = pd.Series(index=idx, dtype=float)
    for f in idx:
        p = posicion.loc[f]
        if isinstance(p, str) and p in ret_map:
            strat.loc[f] = ret_map[p].loc[f]

    cambio = posicion.ne(posicion.shift(1)) & posicion.notna() & posicion.shift(1).notna()
    strat = strat - cambio.astype(float) * costo

    valido = posicion.notna() & strat.notna()
    strat = strat[valido]
    posicion_v = posicion[valido]

    spy = precio_mensual("SPY")
    ief = precio_mensual("IEF")
    spy_bh = spy.pct_change().reindex(strat.index)
    ief_ret = ief.pct_change().reindex(strat.index)
    ret_6040 = (0.6 * spy_bh + 0.4 * ief_ret.fillna(cash.reindex(strat.index))).rename("60/40")

    n_rotaciones = int(cambio[valido].sum())
    drag = n_rotaciones * costo * 100.0

    return ResultadoDualMomentum(
        tickers=list(precios.keys()), lookback=lookback, excluir_ultimo=excluir_ultimo,
        ret_estrategia=strat, ret_spy=spy_bh, ret_6040=ret_6040, cash=cash.reindex(strat.index),
        posicion=posicion_v, n_rotaciones=n_rotaciones, drag_costos_pct=drag,
    )


def holdings_resumen(posicion: pd.Series) -> pd.DataFrame:
    """% del tiempo que la estrategia estuvo en cada activo (incluye efectivo)."""
    if posicion is None or posicion.empty:
        return pd.DataFrame()
    conteo = posicion.value_counts(normalize=True) * 100.0
    return pd.DataFrame({"Activo": conteo.index, "% del tiempo": conteo.values})


def sweep_momentum(tickers: list, lookbacks: list = None, incluir_12_1: bool = True) -> pd.DataFrame:
    """Itera varias ventanas de momentum sobre el mismo universo, para ver si el resultado es robusto."""
    lookbacks = lookbacks or [3, 6, 9, 12, 18, 24]
    filas = []

    def _fila(nombre, res):
        return {
            "Ventana momentum": nombre,
            "CAGR (%)": cagr(res.ret_estrategia) * 100.0,
            "Sharpe": sharpe(res.ret_estrategia, res.cash),
            "Max Drawdown (%)": max_drawdown(res.ret_estrategia) * 100.0,
            "Calmar": calmar(res.ret_estrategia),
            "Rotaciones": res.n_rotaciones,
        }

    for lb in lookbacks:
        filas.append(_fila(f"{lb} meses", backtest_dual_momentum(tickers, lb, False)))
    if incluir_12_1:
        filas.append(_fila("12-1", backtest_dual_momentum(tickers, 12, True)))
    return pd.DataFrame(filas)


# ==========================================================
# MOMENTUM PROFUNDO — tab dedicado (semanal/mensual, log de ranqueo, historia larga)
# ==========================================================
# Universo con historia larga donde se puede. QQQ se EXTIENDE con el índice Nasdaq-100
# (^NDX) hasta ~1985, así que no es el cuello de botella. Los ETFs internacionales / de
# oro / bonos son más nuevos (ver fechas reales en la UI) y suelen ser el cuello de botella.
MOMENTUM_UNIVERSO = {
    "SPY": "Acciones EE.UU. (S&P 500) — desde 1993",
    "QQQ": "Acciones EE.UU. tecnología (Nasdaq-100, extendido a ~1985)",
    "^GSPC": "Índice S&P 500 (precio, sin dividendos) — desde 1990",
    "^IXIC": "Índice Nasdaq Composite (precio) — desde 1990",
    "EFA": "Acciones internacional desarrollado — desde 2001",
    "EEM": "Acciones mercados emergentes — desde 2003",
    "GLD": "Oro — desde 2004",
    "IEF": "Bonos del tesoro 7-10 años — desde 2002",
    "TLT": "Bonos del tesoro 20+ años — desde 2002",
    "VNQ": "Bienes raíces (REITs) — desde 2004",
    "DBC": "Materias primas — desde 2006",
}

_FECHA_EXT_MIN = pd.Timestamp("1985-10-01")


def precio_periodico_ext(ticker: str, freq: str = "ME") -> pd.Series:
    """
    Precio por período con historia EXTENDIDA donde se puede: QQQ/QLD/TQQQ se
    construyen con la serie simulada/extendida de la app (QQQ sobre ^NDX hasta ~1985);
    el resto usa el dato real de Yahoo tal cual.
    """
    t = ticker.upper()
    if t in {"QQQ", "QLD", "TQQQ"}:
        from datetime import date as _date

        from src.investment.historical_dca import obtener_serie_diaria

        diaria = obtener_serie_diaria(ticker, _FECHA_EXT_MIN.date(), _date.today())
        return diaria.resample(freq).last().dropna().rename(ticker)
    return precio_periodico(ticker, freq)


def fecha_inicio_ticker(ticker: str, freq: str = "ME") -> pd.Timestamp | None:
    s = precio_periodico_ext(ticker, freq)
    return s.index.min() if not s.empty else None


@dataclass
class ResultadoMomentumProfundo:
    tickers: list
    freq: str
    lookback: int
    skip_last: bool
    sma_filtro: int
    ret_estrategia: pd.Series
    ret_benchmark: pd.Series
    cash: pd.Series
    posicion: pd.Series
    log: pd.DataFrame           # ranqueo período a período
    fecha_inicio: pd.Timestamp
    ticker_cuello: str          # el ETF que limita la fecha de inicio
    n_rotaciones: int


ETIQ_CASH_MOM = "Efectivo (T-bills)"


def _momentum_core(
    precios: dict, cash: pd.Series, lookback: int, skip_last: bool, sma_filtro: int, costo: float,
    solo_sma: bool = False,
) -> dict:
    """
    Núcleo VECTORIZADO del momentum: rankea activos + efectivo, elige el mayor retorno de
    lookback, aplica (opcional) un filtro de SMA sobre el ganador, y calcula los retornos.
    Sin bucles por período, para que los barridos de muchas combinaciones sean rápidos.

    Si `solo_sma=True` (y sma_filtro>0), se IGNORA el lookback: el ranqueo se hace por qué
    tan por encima de su SMA está cada activo (precio/SMA − 1), y el efectivo entra con 0.
    Así, si ningún activo está sobre su SMA, gana el efectivo → a efectivo. Para un solo
    activo es el clásico "estar dentro si está sobre la SMA, si no efectivo".
    """
    idx = None
    for p in precios.values():
        idx = p.index if idx is None else idx.intersection(p.index)
    if idx is None or len(idx) == 0:
        return {"strat": pd.Series(dtype=float), "posicion": pd.Series(dtype=object),
                "señal": pd.Series(dtype=object), "mom_df": pd.DataFrame(), "cash": pd.Series(dtype=float),
                "n_rotaciones": 0}
    precios = {t: p.reindex(idx) for t, p in precios.items()}
    cash = cash.reindex(idx)
    cash_eq = (1.0 + cash).cumprod()
    rets = {t: p.pct_change() for t, p in precios.items()}

    if solo_sma and sma_filtro and sma_filtro > 0:
        smas = {t: precios[t].rolling(sma_filtro).mean() for t in precios}
        mom = {t: (precios[t] / smas[t] - 1.0).where(smas[t].notna()) for t in precios}
        mom[ETIQ_CASH_MOM] = pd.Series(0.0, index=idx)
        mom_df = pd.DataFrame(mom)
        validos = mom_df.notna().all(axis=1)
        ganador = mom_df.idxmax(axis=1).where(validos)
        señal = ganador.map(lambda x: "CASH" if x == ETIQ_CASH_MOM else x)
    else:
        mom = {t: _lookback_return(p, lookback, skip_last) for t, p in precios.items()}
        mom[ETIQ_CASH_MOM] = _lookback_return(cash_eq, lookback, skip_last)
        mom_df = pd.DataFrame(mom)
        validos = mom_df.notna().all(axis=1)
        ganador = mom_df.idxmax(axis=1).where(validos)
        señal = ganador.map(lambda x: "CASH" if x == ETIQ_CASH_MOM else x)

        if sma_filtro and sma_filtro > 0:
            above = pd.DataFrame(
                {t: (precios[t] > precios[t].rolling(sma_filtro).mean()).where(precios[t].rolling(sma_filtro).mean().notna())
                 for t in precios}, index=idx,
            )
            above["CASH"] = True
            colpos = {c: i for i, c in enumerate(above.columns)}
            labels = señal.fillna("CASH").to_numpy()
            ci = np.array([colpos.get(l, colpos["CASH"]) for l in labels])
            wabove = above.to_numpy()[np.arange(len(idx)), ci]
            force_cash = pd.Series(wabove == False, index=idx) & señal.notna()  # noqa: E712
            señal = señal.mask(force_cash, "CASH")

    posicion = señal.shift(1)
    cols = list(precios.keys()) + ["CASH"]
    ret_df = pd.DataFrame({**rets, "CASH": cash}).reindex(columns=cols)
    colpos2 = {c: i for i, c in enumerate(cols)}
    posf = posicion.fillna("CASH").to_numpy()
    ci2 = np.array([colpos2.get(l, colpos2["CASH"]) for l in posf])
    strat_raw = ret_df.to_numpy()[np.arange(len(idx)), ci2]
    strat = pd.Series(strat_raw, index=idx)
    cambio = posicion.ne(posicion.shift(1)) & posicion.notna() & posicion.shift(1).notna()
    strat = strat - cambio.astype(float) * costo
    valido = posicion.notna() & pd.Series(np.isfinite(strat_raw), index=idx)
    return {
        "strat": strat[valido], "posicion": posicion[valido], "señal": señal, "mom_df": mom_df,
        "cash": cash.reindex(strat[valido].index), "n_rotaciones": int(cambio[valido].sum()),
    }


def backtest_momentum_profundo(
    tickers: list, lookback: int = 12, freq: str = "ME", skip_last: bool = False, sma_filtro: int = 0,
    ticker_benchmark: str = "SPY", solo_sma: bool = False, tasa_previa_anual: float = TASA_CASH_PREVIA_ANUAL,
    costo: float = COSTO_OPERACION,
) -> ResultadoMomentumProfundo:
    """
    Momentum dual con frecuencia configurable y filtro de SMA opcional (o modo 'solo SMA').
    Cada período se rankea por retorno de `lookback` (con el EFECTIVO compitiendo como un
    activo más) y, si `sma_filtro`>0, el ganador solo se compra si está sobre su SMA. Con
    `solo_sma=True` se ignora el lookback y se rankea únicamente por la SMA. `skip_last` = '12-1'.
    """
    precios = {t: precio_periodico_ext(t, freq) for t in tickers}
    precios = {t: p for t, p in precios.items() if not p.empty}
    ticker_cuello = max(precios, key=lambda t: precios[t].index.min()) if precios else ""
    idx = None
    for p in precios.values():
        idx = p.index if idx is None else idx.intersection(p.index)
    idx = idx if idx is not None else pd.DatetimeIndex([])
    cash = cash_periodico(idx, freq, tasa_previa_anual)

    core = _momentum_core(precios, cash, lookback, skip_last, sma_filtro, costo, solo_sma=solo_sma)
    strat = core["strat"]
    bench = precio_periodico_ext(ticker_benchmark, freq).pct_change().reindex(strat.index)

    return ResultadoMomentumProfundo(
        tickers=list(precios.keys()), freq=freq, lookback=lookback, skip_last=skip_last, sma_filtro=sma_filtro,
        ret_estrategia=strat, ret_benchmark=bench, cash=cash.reindex(strat.index),
        posicion=core["posicion"], log=pd.DataFrame(), fecha_inicio=(strat.index.min() if len(strat) else None),
        ticker_cuello=ticker_cuello, n_rotaciones=core["n_rotaciones"],
    )


def sweep_momentum_profundo(
    tickers: list, freq: str = "ME", skip_last: bool = False, sma_filtro: int = 0, lookbacks: list = None,
    solo_sma: bool = False,
) -> pd.DataFrame:
    """
    Itera muchas ventanas para juzgar robustez. En modo normal varía el LOOKBACK; en modo
    `solo_sma` la lista `lookbacks` se interpreta como ventanas de SMA a probar.
    """
    ppa = PERIODOS_POR_ANIO.get(freq, 12)
    if lookbacks is None:
        lookbacks = [4, 8, 13, 26, 39, 52] if freq == "W" else list(range(3, 19))
    unidad = "semanas" if freq == "W" else "meses"
    etiqueta = ("SMA" if solo_sma else "Lookback") + f" ({unidad})"
    precios_all = {t: precio_periodico_ext(t, freq) for t in tickers}
    precios_all = {t: p for t, p in precios_all.items() if not p.empty}
    union = None
    for p in precios_all.values():
        union = p.index if union is None else union.union(p.index)
    cash_master = cash_periodico(union, freq) if union is not None else pd.Series(dtype=float)
    filas = []
    for w in lookbacks:
        if solo_sma:
            core = _momentum_core(precios_all, cash_master, 0, skip_last, w, COSTO_OPERACION, solo_sma=True)
        else:
            core = _momentum_core(precios_all, cash_master, w, skip_last, sma_filtro, COSTO_OPERACION)
        strat = core["strat"]
        if len(strat) < ppa:
            continue
        filas.append({
            etiqueta: w,
            "CAGR (%)": cagr(strat, ppa) * 100.0,
            "Vol. anual (%)": vol_anualizada(strat, ppa) * 100.0,
            "Sharpe": sharpe(strat, core["cash"], ppa),
            "Max Drawdown (%)": max_drawdown(strat) * 100.0,
            "Calmar": calmar(strat, ppa),
            "Rotaciones": core["n_rotaciones"],
        })
    return pd.DataFrame(filas)


def sweep_momentum_combinaciones(
    pool: list, freq: str = "ME", lookbacks: list = None, skips: tuple = (False, True),
    sma_filtro: int = 0, tam_max: int = 4, max_combos: int = 1500, costo: float = COSTO_OPERACION,
) -> tuple[pd.DataFrame, int, bool]:
    """
    Barrido sobre COMBINACIONES de activos (subconjuntos del pool, tamaño 1..tam_max) ×
    ventanas de lookback × excluir-o-no el último período. Devuelve (tabla ordenada por
    Sharpe, nº total de combinaciones, si se topó el límite). Cada combinación corre sobre
    su propia historia disponible (columna 'Desde' para que sea comparable).
    """
    import itertools

    ppa = PERIODOS_POR_ANIO.get(freq, 12)
    if lookbacks is None:
        lookbacks = [13, 26, 39, 52] if freq == "W" else list(range(6, 16))  # step de 1 en meses
    precios_all = {t: precio_periodico_ext(t, freq) for t in pool}
    precios_all = {t: p for t, p in precios_all.items() if not p.empty}
    pool = list(precios_all.keys())
    if not pool:
        return pd.DataFrame(), 0, False
    union = None
    for p in precios_all.values():
        union = p.index if union is None else union.union(p.index)
    cash_master = cash_periodico(union, freq)

    subsets = []
    for k in range(1, min(len(pool), tam_max) + 1):
        subsets += list(itertools.combinations(pool, k))
    combos = [(sub, lb, sk) for sub in subsets for lb in lookbacks for sk in skips]
    total = len(combos)
    if total > max_combos:
        return pd.DataFrame(), total, True

    unidad = "sem" if freq == "W" else "m"
    filas = []
    for sub, lb, sk in combos:
        precios_sub = {t: precios_all[t] for t in sub}
        core = _momentum_core(precios_sub, cash_master, lb, sk, sma_filtro, costo)
        strat = core["strat"]
        if len(strat) < ppa:
            continue
        filas.append({
            "Activos": "+".join(sub),
            f"Lookback ({unidad})": lb,
            "Excluir último": "Sí" if sk else "No",
            "Desde": strat.index.min().strftime("%Y-%m"),
            "CAGR (%)": cagr(strat, ppa) * 100.0,
            "Sharpe": sharpe(strat, core["cash"], ppa),
            "Max DD (%)": max_drawdown(strat) * 100.0,
            "Calmar": calmar(strat, ppa),
            "Rotaciones": core["n_rotaciones"],
        })
    df = pd.DataFrame(filas)
    if not df.empty:
        df = df.sort_values("Sharpe", ascending=False).reset_index(drop=True)
    return df, total, False


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


TICKERS_VIX_DISPONIBLES = ["QQQ", "SPY", "QLD", "TQQQ"]


def _precio_activo_vix(ticker: str) -> pd.Series:
    """
    Precio diario del activo. Para QLD/TQQQ usa la serie apalancada simulada de la app
    (historia larga sobre QQQ), no el ETF real (que arranca en 2006/2010), para que el
    backtest tenga suficiente historia.
    """
    from datetime import date as _date

    from src.investment.historical_dca import obtener_serie_diaria
    from src.investment.leveraged_simulation import FECHA_INCEPTION_GLOBAL

    return obtener_serie_diaria(ticker, FECHA_INCEPTION_GLOBAL, _date.today()).dropna()


def _activo_vix_alineados(ticker: str = "QQQ") -> tuple[pd.Series, pd.Series]:
    """Precio del activo (retorno total) y VIX de CBOE alineados al calendario del activo."""
    precio = _precio_activo_vix(ticker)
    vix = vix_diario().reindex(precio.index).ffill()
    return precio, vix


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


def _fila_forward_vix(label: str, precio: pd.Series, fechas: list[pd.Timestamp]) -> dict:
    fwd = _forward_returns(precio, fechas, list(HORIZONTES_VIX_DIAS.values()))
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


_CACHE_SEÑALES: dict[str, dict] = {}


def señales_vix(ticker: str = "SPY") -> dict:
    """
    Devuelve las tres familias de señales de compra por VIX, ya alineadas al activo:
      A) nivel absoluto (VIX >= umbral)
      B) percentil rodante de 5 años (percentil >= umbral)
      C) estructura de plazos: backwardation, ratio VIX/VIX3M >= umbral
    El VIX/VIX3M son iguales para cualquier activo; lo que cambia es el precio sobre el
    que se miden los retornos forward.

    Se memoiza por ticker dentro del proceso porque el percentil rodante de 5 años es
    caro y se pide muchas veces por render (varias tablas + el backtest + el barrido).
    """
    if ticker in _CACHE_SEÑALES:
        return _CACHE_SEÑALES[ticker]
    precio, vix = _activo_vix_alineados(ticker)
    pct5 = percentil_rodante_vix(vix, 1260)
    vix3m = vix3m_diario().reindex(precio.index).ffill()
    ratio = (vix / vix3m).where(vix3m.notna())
    resultado = {"precio": precio, "vix": vix, "pct5": pct5, "vix3m": vix3m, "ratio": ratio}
    _CACHE_SEÑALES[ticker] = resultado
    return resultado


def _señal_booleana(d: dict, tipo: str, umbral: float) -> pd.Series:
    """Serie booleana diaria de la señal `tipo` ('A'/'B'/'C') con el umbral dado."""
    if tipo == "A":
        return d["vix"] >= umbral
    if tipo == "B":
        return d["pct5"] >= umbral
    return d["ratio"] >= umbral


def _fechas_incondicional(precio: pd.Series, señal_serie: pd.Series) -> list:
    """Días del activo desde que la señal existe (ventana justa para el 'incondicional')."""
    fecha0 = señal_serie.first_valid_index()
    if fecha0 is None:
        return list(precio.index)
    return [f for f in precio.index if f >= fecha0]


def resumen_señal_a(ticker: str = "QQQ", umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [20, 25, 30, 35, 40]
    d = señales_vix(ticker)
    precio, vix = d["precio"], d["vix"]
    filas = [_fila_forward_vix("Incondicional (cualquier día)", precio, _fechas_incondicional(precio, vix))]
    for u in umbrales:
        fechas = _episodios_desde_señal(vix >= u)
        filas.append(_fila_forward_vix(f"VIX ≥ {u:.0f}", precio, fechas))
    return pd.DataFrame(filas)


def resumen_señal_b(ticker: str = "QQQ", umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [80, 90, 95]
    d = señales_vix(ticker)
    precio, pct5 = d["precio"], d["pct5"]
    filas = [_fila_forward_vix("Incondicional (mismo período)", precio, _fechas_incondicional(precio, pct5))]
    for u in umbrales:
        fechas = _episodios_desde_señal(pct5 >= u)
        filas.append(_fila_forward_vix(f"Percentil 5a ≥ {u:.0f}", precio, fechas))
    return pd.DataFrame(filas)


def resumen_señal_b_ventanas(ticker: str = "QQQ", umbral: float = 90, ventanas_anios: list[int] = None) -> pd.DataFrame:
    """Sensibilidad de la señal B a la ventana del percentil (3/5/10 años)."""
    ventanas_anios = ventanas_anios or [3, 5, 10]
    precio, vix = _activo_vix_alineados(ticker)
    filas = []
    for va in ventanas_anios:
        pct = percentil_rodante_vix(vix, va * 252)
        fechas = _episodios_desde_señal(pct >= umbral)
        filas.append(_fila_forward_vix(f"Ventana {va} años", precio, fechas))
    return pd.DataFrame(filas)


def resumen_señal_c(ticker: str = "QQQ", umbrales: list[float] = None) -> pd.DataFrame:
    umbrales = umbrales or [0.95, 1.00, 1.05]
    d = señales_vix(ticker)
    precio, ratio = d["precio"], d["ratio"]
    filas = [_fila_forward_vix("Incondicional (mismo período, ~2009+)", precio, _fechas_incondicional(precio, ratio))]
    for u in umbrales:
        fechas = _episodios_desde_señal(ratio >= u)
        filas.append(_fila_forward_vix(f"VIX/VIX3M ≥ {u:.2f}", precio, fechas))
    return pd.DataFrame(filas)


def matriz_solapamiento_vix(ticker: str = "QQQ", umbral_a: float = 30, umbral_b: float = 90, umbral_c: float = 1.00) -> pd.DataFrame:
    """
    Matriz de coincidencia diaria entre las tres señales (solo fechas donde las tres
    tienen dato, es decir desde el inicio del VIX3M). Cada celda = P(columna activa |
    fila activa) en %.
    """
    d = señales_vix(ticker)
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


def episodios_para_grafico(ticker: str = "QQQ", umbral_a: float = 30, umbral_b: float = 90, umbral_c: float = 1.00) -> dict:
    """Fechas de episodios de cada señal, para marcarlas sobre la línea del activo."""
    d = señales_vix(ticker)
    return {
        "precio": d["precio"],
        "A": _episodios_desde_señal(d["vix"] >= umbral_a),
        "B": _episodios_desde_señal(d["pct5"] >= umbral_b),
        "C": _episodios_desde_señal(d["ratio"] >= umbral_c),
    }


# --- Backtest OPERABLE de entrada/salida por umbrales (dos umbrales: uno para
#     entrar, otro para salir). La comparación arranca cuando la señal ya existe,
#     para no comparar toda la historia del activo contra una señal más corta. ---
_ETIQUETA_TIPO = {"A": "Nivel VIX", "B": "Percentil 5a", "C": "Backwardation"}
# Muchos umbrales de ENTRADA (para explorar a fondo) y unos pocos más de SALIDA.
_RANGO_ENTRADA = {
    "A": [16, 18, 20, 22, 24, 26, 28, 30, 32, 35, 38, 40, 45, 50, 55],
    "B": [55, 60, 65, 70, 75, 80, 83, 85, 88, 90, 92, 95, 97],
    "C": [0.90, 0.925, 0.95, 0.975, 1.00, 1.025, 1.05, 1.075, 1.10, 1.15, 1.20],
}
_RANGO_SALIDA = {
    "A": [10, 12, 14, 15, 16, 18, 20, 25],
    "B": [15, 20, 25, 30, 40, 50, 60],
    "C": [0.80, 0.85, 0.90, 0.95, 1.00],
}


@dataclass
class ResultadoEntradaSalidaVix:
    ticker: str
    tipo: str
    umbral_entrada: float
    umbral_salida: float
    ret_estrategia: pd.Series      # mensual
    ret_buy_hold: pd.Series        # mensual, MISMO período que la estrategia
    cash: pd.Series
    exposicion_pct: float
    n_trades: int
    fecha_inicio: pd.Timestamp


def _serie_señal(d: dict, tipo: str) -> pd.Series:
    return {"A": d["vix"], "B": d["pct5"], "C": d["ratio"]}[tipo]


def _cash_diario(index: pd.DatetimeIndex) -> pd.Series:
    """Retorno diario del efectivo: BIL donde exista, TB3MS antes, y tasa fija de respaldo."""
    tasa_prev_d = (1.0 + TASA_CASH_PREVIA_ANUAL) ** (1.0 / 252.0) - 1.0
    out = pd.Series(tasa_prev_d, index=index)
    tb3 = fred_series("TB3MS")
    if not tb3.empty:
        tb3_d = ((1.0 + tb3 / 100.0) ** (1.0 / 252.0) - 1.0)
        out = tb3_d.reindex(index, method="ffill").fillna(out)
    bil = get_daily_closes("BIL")
    if not bil.empty:
        out = bil.pct_change().reindex(index).fillna(out)
    return out


def _correr_entrada_salida(
    df: pd.DataFrame, ret_d: pd.Series, cash_d: pd.Series,
    umbral_entrada: float, umbral_salida: float, costo: float,
) -> dict:
    """
    Máquina de estados con histéresis: entra cuando señal ≥ entrada, sale cuando
    señal ≤ salida, mantiene la posición en el medio. Vectorizado: marca cada día como
    'encender' (+1), 'apagar' (-1) o 'mantener' (NaN) y hace forward-fill del último
    estado (arranca en efectivo). Es equivalente al bucle día-a-día pero mucho más rápido.
    """
    s = df["señal"].to_numpy()
    fuerza = np.where(s >= umbral_entrada, 1.0, np.where(s <= umbral_salida, -1.0, np.nan))
    estado = pd.Series(fuerza, index=df.index).ffill()
    pos = (estado == 1.0).astype(float)
    pos_ap = pos.shift(1)
    strat_d = ret_d.where(pos_ap == 1.0, cash_d)
    cambio = pos_ap.fillna(0.0).ne(pos_ap.shift(1).fillna(0.0))
    strat_d = strat_d - cambio.astype(float) * costo

    valido = pos_ap.notna() & ret_d.notna()
    strat_d = strat_d[valido]
    bh_d = ret_d[valido]
    pos_v = pos_ap[valido]
    n_trades = int(((pos_v == 1.0) & (pos_v.shift(1) != 1.0)).sum())
    exposicion = float((pos_v == 1.0).mean() * 100.0)

    strat_m = (1.0 + strat_d).resample("ME").prod() - 1.0
    bh_m = (1.0 + bh_d).resample("ME").prod() - 1.0
    return {"strat_m": strat_m, "bh_m": bh_m, "n_trades": n_trades, "exposicion": exposicion}


def backtest_entrada_salida_vix(
    ticker: str, tipo: str, umbral_entrada: float, umbral_salida: float,
    costo: float = COSTO_OPERACION,
) -> ResultadoEntradaSalidaVix | None:
    """
    Estrategia de entrada/salida sobre `ticker`: en efectivo por defecto; ENTRA el día
    siguiente a que la señal cruza `umbral_entrada` y SALE el día siguiente a que cruza
    `umbral_salida`. El período arranca cuando la señal ya existe (p.ej. ~2009 para
    backwardation), y el buy & hold de comparación usa exactamente ESE mismo período.
    """
    d = señales_vix(ticker)
    df = pd.DataFrame({"precio": d["precio"], "señal": _serie_señal(d, tipo)}).dropna()
    if len(df) < 120:
        return None
    ret_d = df["precio"].pct_change()
    cash_d = _cash_diario(df.index)
    r = _correr_entrada_salida(df, ret_d, cash_d, umbral_entrada, umbral_salida, costo)
    cash_m = cash_mensual(r["strat_m"].index)
    return ResultadoEntradaSalidaVix(
        ticker=ticker, tipo=tipo, umbral_entrada=umbral_entrada, umbral_salida=umbral_salida,
        ret_estrategia=r["strat_m"], ret_buy_hold=r["bh_m"], cash=cash_m,
        exposicion_pct=r["exposicion"], n_trades=r["n_trades"], fecha_inicio=df.index.min(),
    )


def grilla_entrada_salida_vix(
    ticker: str, tipo: str, entradas: list[float] = None, salidas: list[float] = None,
    costo: float = COSTO_OPERACION,
) -> tuple[pd.DataFrame, float, pd.Timestamp]:
    """
    Corre un rango amplio de umbrales de entrada × salida (solo combos con entrada >
    salida) y devuelve (tabla de resultados, CAGR del buy & hold del mismo período,
    fecha de inicio). El buy & hold es idéntico para todos los combos (mismo período).
    """
    entradas = entradas or _RANGO_ENTRADA[tipo]
    salidas = salidas or _RANGO_SALIDA[tipo]
    d = señales_vix(ticker)
    df = pd.DataFrame({"precio": d["precio"], "señal": _serie_señal(d, tipo)}).dropna()
    if len(df) < 120:
        return pd.DataFrame(), float("nan"), None
    ret_d = df["precio"].pct_change()
    cash_d = _cash_diario(df.index)
    bh_m = (1.0 + ret_d.dropna()).resample("ME").prod() - 1.0
    cash_m = cash_mensual(bh_m.index)
    bh_cagr = cagr(bh_m) * 100.0
    bh_dd = max_drawdown(bh_m) * 100.0
    bh_calmar = calmar(bh_m)

    filas = []
    for ue in entradas:
        for us in salidas:
            if ue <= us:
                continue
            r = _correr_entrada_salida(df, ret_d, cash_d, ue, us, costo)
            c = cagr(r["strat_m"]) * 100.0
            dd = max_drawdown(r["strat_m"]) * 100.0
            filas.append({
                "Entrada ≥": ue,
                "Salida ≤": us,
                "CAGR (%)": c,
                "Δ CAGR vs B&H (pp)": c - bh_cagr,
                "Max DD (%)": dd,
                "DD más suave (pp)": abs(bh_dd) - abs(dd),
                "Calmar": calmar(r["strat_m"]),
                "Sharpe": sharpe(r["strat_m"], cash_m),
                "Exposición (%)": r["exposicion"],
                "N° trades": r["n_trades"],
            })
    df_res = pd.DataFrame(filas).sort_values("Δ CAGR vs B&H (pp)", ascending=False).reset_index(drop=True)
    df_res.attrs["bh_cagr"] = bh_cagr
    df_res.attrs["bh_dd"] = bh_dd
    df_res.attrs["bh_calmar"] = bh_calmar
    return df_res, bh_cagr, df.index.min()


def conclusion_grilla_vix(df: pd.DataFrame, ticker: str, tipo: str, bh_cagr: float, fecha_inicio) -> str:
    """Texto data-driven, enfocado en si alguna combinación MEJORA EL RETORNO (no solo el riesgo)."""
    if df is None or df.empty:
        return "No hay resultados que analizar todavía."
    fam = _ETIQUETA_TIPO.get(tipo, tipo)
    ini = fecha_inicio.strftime("%b %Y") if fecha_inicio is not None else "—"
    bh_dd = df.attrs.get("bh_dd", float("nan"))
    bh_calmar = df.attrs.get("bh_calmar", float("nan"))

    ganan_cagr = df[df["Δ CAGR vs B&H (pp)"] > 0].sort_values("Δ CAGR vs B&H (pp)", ascending=False)
    mejor_cagr = df.sort_values("CAGR (%)", ascending=False).iloc[0]
    mejor_calmar = df.sort_values("Calmar", ascending=False).iloc[0]

    partes = [
        f"### 🔎 Entrada/salida con **{fam}** sobre **{ticker}** (desde {ini})\n",
        f"Benchmark: comprar y mantener {ticker} en el mismo período → CAGR **{bh_cagr:.1f}%**, "
        f"Max DD **{bh_dd:.1f}%**, Calmar **{bh_calmar:.2f}**.",
    ]

    # El foco del usuario: ¿alguna MEJORA EL RETORNO?
    if not ganan_cagr.empty:
        n = len(ganan_cagr)
        top = ganan_cagr.iloc[0]
        partes.append(
            f"- ✅ **{n} de {len(df)} combinaciones superaron el CAGR del buy & hold.** La mejor en retorno: "
            f"entrar en {top['Entrada ≥']:g}, salir en {top['Salida ≤']:g} → CAGR **{top['CAGR (%)']:.1f}%** "
            f"(**{top['Δ CAGR vs B&H (pp)']:+.1f} pp**), Max DD {top['Max DD (%)']:.1f}% "
            f"({top['DD más suave (pp)']:+.1f} pp vs B&H), invertido {top['Exposición (%)']:.0f}% del tiempo."
        )
        # ¿Gana en retorno Y en drawdown a la vez?
        doble = ganan_cagr[ganan_cagr["DD más suave (pp)"] > 0]
        if not doble.empty:
            dd_top = doble.sort_values("Δ CAGR vs B&H (pp)", ascending=False).iloc[0]
            partes.append(
                f"- 🏆 **{len(doble)} combinaciones le ganan a la vez en retorno Y en drawdown** — el "
                f"'santo grial'. La mejor: entrar {dd_top['Entrada ≥']:g}, salir {dd_top['Salida ≤']:g} → "
                f"CAGR {dd_top['CAGR (%)']:.1f}% ({dd_top['Δ CAGR vs B&H (pp)']:+.1f} pp) con drawdown "
                f"{dd_top['DD más suave (pp)']:+.1f} pp más suave. Ojo: son pocas y elegidas mirando el "
                "pasado, así que hay que desconfiar del sobreajuste."
            )
        else:
            partes.append(
                "- Pero esas que ganan en retorno lo hacen **asumiendo más drawdown** que el buy & hold "
                "(entran más agresivo / están más tiempo dentro). No hay 'gratis': más retorno vino con "
                "más riesgo."
            )
    else:
        partes.append(
            "- ❌ **Ninguna combinación superó el CAGR del buy & hold.** Con este activo y esta señal, la "
            "estrategia **no mejora el retorno**: al estar en efectivo parte del tiempo, se pierde upside. "
            "Su único aporte es reducir el drawdown / la volatilidad."
        )

    partes.append(
        f"- **Mejor retorno/riesgo (Calmar):** entrar {mejor_calmar['Entrada ≥']:g}, salir "
        f"{mejor_calmar['Salida ≤']:g} → Calmar **{mejor_calmar['Calmar']:.2f}** vs {bh_calmar:.2f} del "
        f"buy & hold (CAGR {mejor_calmar['CAGR (%)']:.1f}%, Max DD {mejor_calmar['Max DD (%)']:.1f}%)."
    )
    partes.append(
        "- 💡 Para de verdad **subir el retorno** (no solo suavizarlo), la palanca más directa es aplicar "
        "la señal sobre un activo **apalancado** (QLD/TQQQ): comprar el rebote apalancado tras el pánico "
        "y salir al calmarse. Probalo cambiando el activo arriba — el apalancamiento amplifica el acierto "
        "de la señal (y también el error, así que mirá el drawdown)."
    )
    partes.append(
        "- ⚠️ Cuidado con el sobreajuste: confirmá que combinaciones **vecinas** den resultados parecidos "
        "(mirá el mapa de calor), no un único punto mágico aislado."
    )
    return "\n".join(partes)


# ==========================================================
# MÓDULO 7 — ESTRATEGIA COMBINADA (fracciones de capital)
# ==========================================================
def _precio_mensual_ext(ticker: str) -> pd.Series:
    """Precio mensual; para QLD/TQQQ usa la serie apalancada simulada (historia larga)."""
    if ticker.upper() in {"TQQQ", "QLD"}:
        return _precio_activo_vix(ticker).resample("ME").last().dropna().rename(ticker)
    return precio_mensual(ticker)


@dataclass
class ResultadoDip:
    ticker_señal: str
    ticker_compra: str
    umbral_pct: float
    ret_estrategia: pd.Series
    ret_buy_hold: pd.Series
    cash: pd.Series
    exposicion_pct: float


def backtest_dip_mensual(
    ticker_señal: str = "QQQ", ticker_compra: str = "QQQ", umbral_pct: float = -10.0,
    costo: float = COSTO_OPERACION,
) -> ResultadoDip:
    """
    'Comprar la caída' operable: en efectivo por defecto; cuando `ticker_señal` cae por
    debajo de `umbral_pct` desde su máximo (drawdown), se compra `ticker_compra` (puede
    ser el mismo o uno apalancado como TQQQ) y se mantiene hasta que el señal recupera un
    nuevo máximo. Mensual, con costos.
    """
    p_sig = _precio_mensual_ext(ticker_señal)
    p_buy = _precio_mensual_ext(ticker_compra)
    idx = p_sig.index.intersection(p_buy.index)
    p_sig = p_sig.reindex(idx)
    p_buy = p_buy.reindex(idx)
    dd = p_sig / p_sig.cummax() - 1.0
    fuerza = np.where(dd <= umbral_pct / 100.0, 1.0, np.where(dd >= 0.0, -1.0, np.nan))
    estado = pd.Series(fuerza, index=idx).ffill()
    pos = (estado == 1.0).astype(float).shift(1)
    ret_buy = p_buy.pct_change()
    cash = cash_mensual(idx)
    strat = ret_buy.where(pos == 1.0, cash)
    cambio = pos.fillna(0.0).diff().abs() > 0
    strat = strat - cambio.astype(float) * costo
    valido = pos.notna() & ret_buy.notna()
    strat = strat[valido]
    bh = ret_buy[valido]
    return ResultadoDip(
        ticker_señal=ticker_señal, ticker_compra=ticker_compra, umbral_pct=umbral_pct,
        ret_estrategia=strat, ret_buy_hold=bh, cash=cash.reindex(strat.index),
        exposicion_pct=float((pos[valido] == 1.0).mean() * 100.0),
    )


@dataclass
class ResultadoCombinado:
    ret_estrategia: pd.Series
    componentes: dict          # nombre -> retorno mensual (alineado)
    ret_benchmark: pd.Series
    cash: pd.Series
    fracciones: dict


def backtest_combinado(
    frac_sma: float, frac_mom: float, frac_dip: float,
    ticker_sma: str = "SPY", tickers_mom: list = None, lookback: int = 12,
    ticker_señal_dip: str = "QQQ", ticker_compra_dip: str = "QQQ", umbral_dip: float = -10.0,
    ticker_benchmark: str = "SPY", costo: float = COSTO_OPERACION,
) -> ResultadoCombinado:
    """
    Reparte el capital en tres 'motores' (fracciones que suman 1, se normalizan):
      • SMA trend (Faber) sobre `ticker_sma`,
      • Dual momentum sobre `tickers_mom`,
      • Comprar la caída (`ticker_compra_dip`, p.ej. QQQ o TQQQ).
    Rebalanceo mensual (suma ponderada de los retornos). Se compara contra comprar y
    mantener `ticker_benchmark` en el mismo período.
    """
    tickers_mom = tickers_mom or ["SPY", "EFA"]
    total = frac_sma + frac_mom + frac_dip
    total = total if total > 0 else 1.0
    w = {"SMA trend": frac_sma / total, "Momentum ETFs": frac_mom / total, "Comprar caídas": frac_dip / total}

    comp = {
        "SMA trend": backtest_sma_trend(ticker_sma).ret_estrategia,
        "Momentum ETFs": backtest_dual_momentum(tickers_mom, lookback).ret_estrategia,
        "Comprar caídas": backtest_dip_mensual(ticker_señal_dip, ticker_compra_dip, umbral_dip).ret_estrategia,
    }
    idx = None
    for s in comp.values():
        idx = s.index if idx is None else idx.intersection(s.index)
    comp = {k: v.reindex(idx) for k, v in comp.items()}
    combinado = sum(w[k] * comp[k] for k in comp)
    bench = _precio_mensual_ext(ticker_benchmark).pct_change().reindex(idx)
    cash = cash_mensual(idx)
    return ResultadoCombinado(
        ret_estrategia=combinado, componentes=comp, ret_benchmark=bench, cash=cash, fracciones=w,
    )
