# -*- coding: utf-8 -*-
"""
Momentum de acciones del S&P 500: cada mes se rankean las acciones del índice por
momentum (retorno de N meses) o por tamaño (market cap), se compra el TOP-K con igual
peso, y se rebalancea mensualmente. Se compara contra el índice (SPY) y contra la
cartera de igual peso de todas las acciones.

SESGO DE SUPERVIVENCIA — dos modos:
  • Modo simple (lista ACTUAL de Wikipedia): el backtest solo mira empresas que HOY
    siguen en el índice; ignora todas las que quebraron o fueron sacadas. Infla mucho
    los resultados. Sirve para comparar estrategias entre sí, no como promesa de retorno.
  • Modo SIN SESGO (membresía histórica point-in-time): usa el CSV de assets con la
    composición exacta del índice en cada fecha (1996→hoy). Se descarga el universo
    COMPLETO de acciones que alguna vez estuvieron en el S&P 500 (~1200, no solo las
    ~500 de hoy), y cada mes el ranking solo considera las que realmente eran miembros
    esa fecha. Esto elimina el sesgo de inclusión (comprar hoy-ganadores en el pasado).
    Limitación residual: Yahoo no siempre tiene precios de tickers ya delistados
    (bancarrotas con sufijo 'Q', etc.), así que la cobertura de perdedoras es parcial.

Datos:
  • Constituyentes actuales: tabla de Wikipedia (con User-Agent), cacheada.
  • Membresía histórica: CSV en assets/ (fecha → lista de tickers de ese día).
  • Precios: cierre mensual ajustado (yfinance, interval='1mo'), en un solo CSV.
  • Acciones en circulación / market cap: fast_info de yfinance (solo si se pide el
    criterio de market cap), cacheado.
"""
from __future__ import annotations

import io
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from src.data.yahoo_client import DATA_CACHE_DIR
from src.investment.backtest_v2 import COSTO_OPERACION

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TOPS_DEFAULT = [1, 2, 3, 4, 5, 10, 20, 30]

_CONST_PATH = Path(DATA_CACHE_DIR) / "SP500_constituyentes.csv"
_PRECIOS_PATH = Path(DATA_CACHE_DIR) / "SP500_precios_mensual.csv"
_SHARES_PATH = Path(DATA_CACHE_DIR) / "SP500_shares.csv"

# Membresía histórica point-in-time (CSV en assets/) y precios del universo completo.
_HIST_CSV_PATH = Path(__file__).resolve().parents[2] / "assets" / "S&P 500 Historical Components & Changes (Updated).csv"
_PRECIOS_HIST_PATH = Path(DATA_CACHE_DIR) / "SP500_precios_mensual_hist.csv"
_membresia_cache: pd.DataFrame | None = None


def _to_yahoo(sym: str) -> str:
    return str(sym).replace(".", "-").strip().upper()


def constituyentes_sp500(refresh: bool = False) -> pd.DataFrame:
    """Lista actual del S&P 500 (Ticker, Nombre, Sector), cacheada en disco."""
    if not refresh and _CONST_PATH.exists():
        return pd.read_csv(_CONST_PATH)
    try:
        req = urllib.request.Request(WIKI_URL, headers=_UA)
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        df = pd.read_html(io.StringIO(html))[0]
        out = pd.DataFrame({
            "Ticker": df["Symbol"].astype(str).map(_to_yahoo),
            "Nombre": df["Security"].astype(str),
            "Sector": df["GICS Sector"].astype(str),
        }).drop_duplicates("Ticker").reset_index(drop=True)
        _CONST_PATH.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(_CONST_PATH, index=False)
        return out
    except Exception:
        if _CONST_PATH.exists():
            return pd.read_csv(_CONST_PATH)
        return pd.DataFrame(columns=["Ticker", "Nombre", "Sector"])


def _bajar_precios(lista: list, path: Path, refresh: bool) -> pd.DataFrame:
    """
    Cierre mensual ajustado de una lista de tickers en un DataFrame (fechas × tickers).
    Descarga por lotes de 100 y cachea el resultado en `path`. Los tickers sin datos en
    Yahoo (p. ej. delistados) simplemente no aparecen como columnas.
    """
    if not refresh and path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=[0])
        if not df.empty and df.index.max().date() >= date.today() - timedelta(days=7):
            return df

    lista = list(dict.fromkeys(lista))
    frames = []
    for i in range(0, len(lista), 100):
        chunk = lista[i:i + 100]
        raw = yf.download(chunk, start="1990-01-01", interval="1mo", auto_adjust=True, progress=False)
        if raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            close = raw[["Close"]].rename(columns={"Close": chunk[0]})
        frames.append(close)
    if not frames:
        return pd.DataFrame()
    precios = pd.concat(frames, axis=1)
    precios = precios.loc[:, ~precios.columns.duplicated()]
    # descartar columnas 100% vacías: yfinance devuelve el ticker (all-NaN) aunque no tenga
    # datos (delistados/fallidos). Si no, contarían como "cobertura" y no aportan nada.
    precios = precios.dropna(how="all", axis=1).dropna(how="all", axis=0).sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    precios.to_csv(path)
    return precios


def descargar_precios_sp500(tickers: list, refresh: bool = False, incluir_spy: bool = True) -> pd.DataFrame:
    """Precios de la lista ACTUAL de constituyentes (+ SPY). Modo con sesgo de supervivencia."""
    lista = list(tickers) + (["SPY"] if incluir_spy else [])
    return _bajar_precios(lista, _PRECIOS_PATH, refresh)


def descargar_precios_historico(refresh: bool = False, incluir_spy: bool = True) -> pd.DataFrame:
    """
    Precios del universo COMPLETO de acciones que alguna vez estuvieron en el S&P 500
    (según el CSV de membresía histórica) + SPY. Base del modo SIN sesgo de supervivencia.
    Son ~1200 tickers, así que la descarga tarda bastante más que la lista actual.
    """
    lista = universo_historico() + (["SPY"] if incluir_spy else [])
    return _bajar_precios(lista, _PRECIOS_HIST_PATH, refresh)


def precios_cacheados() -> pd.DataFrame | None:
    """Los precios (lista actual) ya descargados (o None si no existen)."""
    if _PRECIOS_PATH.exists():
        return pd.read_csv(_PRECIOS_PATH, index_col=0, parse_dates=[0])
    return None


def precios_historico_cacheados() -> pd.DataFrame | None:
    """Los precios del universo histórico ya descargados (o None si no existen)."""
    if _PRECIOS_HIST_PATH.exists():
        return pd.read_csv(_PRECIOS_HIST_PATH, index_col=0, parse_dates=[0])
    return None


def membresia_historica(refresh: bool = False) -> pd.DataFrame:
    """
    Membresía point-in-time del S&P 500 desde el CSV de assets. Devuelve un DataFrame
    booleano (fechas de snapshot × tickers en formato Yahoo); True = ese ticker era
    miembro del índice esa fecha. Se cachea en memoria para no re-parsear el CSV.
    """
    global _membresia_cache
    if _membresia_cache is not None and not refresh:
        return _membresia_cache
    df = pd.read_csv(_HIST_CSV_PATH)
    fechas = pd.to_datetime(df["date"])
    filas = [{_to_yahoo(t) for t in str(tk).split(",") if t} for tk in df["tickers"]]
    universo = sorted(set().union(*filas)) if filas else []
    mat = pd.DataFrame(False, index=fechas.values, columns=universo)
    col_idx = {c: j for j, c in enumerate(universo)}
    arr = mat.to_numpy()
    for i, miembros in enumerate(filas):
        for t in miembros:
            arr[i, col_idx[t]] = True
    mat = pd.DataFrame(arr, index=pd.DatetimeIndex(fechas.values), columns=universo).sort_index()
    _membresia_cache = mat
    return mat


def universo_historico() -> list:
    """Lista (en formato Yahoo) de todas las acciones que alguna vez estuvieron en el S&P 500."""
    return list(membresia_historica().columns)


def mascara_mensual(membresia: pd.DataFrame, index_mensual: pd.DatetimeIndex, columnas: list) -> pd.DataFrame:
    """
    Reindexa la membresía diaria a una grilla mensual (as-of: el último snapshot de cada
    mes) y la alinea a las `columnas` de precios. Devuelve un booleano (mes × ticker);
    True = ese mes ese ticker era miembro del índice. Los meses/ tickers sin dato quedan
    en False.
    """
    m = membresia.sort_index()
    m_mes = m.groupby(m.index.to_period("M")).last().astype("int8")  # último snapshot del mes
    per = index_mensual.to_period("M")
    mask = m_mes.reindex(per, method="ffill")                 # as-of por mes calendario
    mask.index = index_mensual
    mask = mask.reindex(columns=columnas, fill_value=0).fillna(0)
    return mask.astype(bool)


def shares_cacheadas() -> pd.Series | None:
    """Las acciones en circulación ya descargadas (o None si no existen)."""
    if _SHARES_PATH.exists():
        return pd.read_csv(_SHARES_PATH, index_col=0).iloc[:, 0]
    return None


def preparar_datos(refresh: bool = False, con_shares: bool = False) -> dict:
    """Descarga/actualiza todo lo necesario: constituyentes, precios y (opcional) acciones."""
    const = constituyentes_sp500(refresh=refresh)
    tickers = const["Ticker"].tolist()
    precios = descargar_precios_sp500(tickers, refresh=refresh)
    shares = shares_sp500(tickers, refresh=refresh) if con_shares else shares_cacheadas()
    return {"constituyentes": const, "precios": precios, "shares": shares}


def preparar_datos_historico(refresh: bool = False) -> dict:
    """
    Prepara el modo SIN sesgo: descarga los precios del universo histórico completo
    (~1200 tickers) y arma la máscara de membresía point-in-time alineada a esos precios.
    """
    precios = descargar_precios_historico(refresh=refresh)
    membresia = membresia_historica()
    cols = [c for c in precios.columns if c != "SPY"]
    mask = mascara_mensual(membresia, precios.index, cols) if not precios.empty else None
    return {"precios": precios, "membresia": membresia, "mask": mask}


def shares_sp500(tickers: list, refresh: bool = False) -> pd.Series:
    """Acciones en circulación (actuales) por ticker, para el proxy de market cap."""
    if not refresh and _SHARES_PATH.exists():
        s = pd.read_csv(_SHARES_PATH, index_col=0).iloc[:, 0]
        if set(tickers).issubset(set(s.index)):
            return s
    data = {}
    for tk in tickers:
        try:
            fi = yf.Ticker(tk).fast_info
            val = fi["shares"]
            data[tk] = float(val) if val else np.nan
        except Exception:
            data[tk] = np.nan
    s = pd.Series(data, name="shares")
    _SHARES_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.to_csv(_SHARES_PATH)
    return s


def _puntajes(precios: pd.Series | pd.DataFrame, criterio: str, lookback_meses: int, shares: pd.Series | None):
    """Matriz de puntaje por mes × acción según el criterio de ranqueo."""
    if criterio == "mcap" and shares is not None:
        # Proxy de market cap = acciones (actuales) × precio del mes. Asume acciones
        # constantes (ignora recompras/emisiones); sirve para ORDENAR por tamaño.
        return precios.mul(shares.reindex(precios.columns), axis=1)
    # Momentum: retorno de los últimos `lookback_meses` meses.
    return precios / precios.shift(lookback_meses) - 1.0


def backtest_top_n(
    precios: pd.DataFrame, criterio: str = "momentum", top_n: int = 10, lookback_meses: int = 12,
    shares: pd.Series | None = None, costo: float = COSTO_OPERACION, mask: pd.DataFrame | None = None,
) -> dict:
    """
    Cada mes: rankea las acciones por `criterio`, compra el TOP-`top_n` con igual peso,
    lo aplica el mes siguiente (sin look-ahead), con costo por rotación. Devuelve la
    serie de retornos de la estrategia, del SPY y de la cartera de igual peso de las
    acciones elegibles, más estadísticas de tenencia.

    Si `mask` (booleano mes × ticker de membresía point-in-time) se pasa, cada mes solo
    se rankean/compran las acciones que ERAN miembros del índice esa fecha: así se elimina
    el sesgo de supervivencia por inclusión (comprar hoy-ganadores en el pasado).
    """
    universo = [c for c in precios.columns if c != "SPY"]
    precios_u = precios[universo].sort_index()
    # fill_method=None: NO arrastrar el último precio sobre gaps (acciones delistadas o con
    # trading suspendido). El pad-fill inventaría retornos de 0% y ocultaría las pérdidas.
    rets = precios_u.pct_change(fill_method=None)
    score = _puntajes(precios_u, criterio, lookback_meses, shares)
    if mask is not None:
        mask = mask.reindex(index=precios_u.index, columns=universo).fillna(False)

    idx = precios_u.index
    strat = pd.Series(index=idx, dtype=float)
    turnover = pd.Series(index=idx, dtype=float)
    prev = set()
    for i in range(len(idx) - 1):
        f, f1 = idx[i], idx[i + 1]
        sc = score.loc[f].dropna()
        if mask is not None:  # solo miembros del índice ESA fecha
            sc = sc[mask.loc[f].reindex(sc.index).fillna(False).to_numpy()]
        r1 = rets.loc[f1]
        sc = sc[r1.reindex(sc.index).notna()]  # solo acciones con retorno el mes siguiente
        if len(sc) == 0:
            continue
        top = list(sc.sort_values(ascending=False).head(top_n).index)
        cur = set(top)
        n_ent = len(cur - prev)
        cost = costo * 2.0 * n_ent / max(1, len(top))
        strat.loc[f1] = float(r1.reindex(top).mean()) - cost
        turnover.loc[f1] = n_ent / max(1, len(top))
        prev = cur

    strat = strat.dropna()
    spy = precios["SPY"].pct_change(fill_method=None).reindex(strat.index) if "SPY" in precios.columns else pd.Series(dtype=float)
    if mask is not None:
        # igual peso solo de los miembros del índice cada mes (el retorno de f se decide
        # con la membresía del mes anterior, sin look-ahead)
        ew_all = rets.where(mask.shift(1, fill_value=False)).mean(axis=1).reindex(strat.index)
    else:
        ew_all = rets.mean(axis=1).reindex(strat.index)
    return {
        "strat": strat, "spy": spy, "ew_all": ew_all,
        "turnover": turnover.reindex(strat.index), "n_universo": len(universo),
    }


def sweep_top_n(
    precios: pd.DataFrame, criterio: str = "momentum", lookback_meses: int = 12,
    shares: pd.Series | None = None, tops: list = None, costo: float = COSTO_OPERACION,
    mask: pd.DataFrame | None = None,
):
    """
    Corre el backtest para varios TOP-K y devuelve una tabla comparativa (+ las filas
    de referencia SPY y cartera de igual peso de las acciones elegibles). Métricas
    mensuales. `mask` (opcional) activa la membresía point-in-time (sin sesgo).
    """
    from src.investment.backtest_v2 import cagr, calmar, max_drawdown, sharpe, vol_anualizada

    tops = tops or TOPS_DEFAULT
    # cash mensual para el Sharpe, alineado
    base = backtest_top_n(precios, criterio, tops[0], lookback_meses, shares, costo, mask)
    from src.investment.backtest_v2 import cash_mensual
    cash = cash_mensual(base["strat"].index)

    def _fila(nombre, serie):
        return {
            "Cartera": nombre,
            "CAGR (%)": cagr(serie, 12) * 100.0,
            "Vol. anual (%)": vol_anualizada(serie, 12) * 100.0,
            "Sharpe": sharpe(serie, cash, 12),
            "Max Drawdown (%)": max_drawdown(serie) * 100.0,
            "Calmar": calmar(serie, 12),
        }

    filas = []
    for k in tops:
        res = backtest_top_n(precios, criterio, k, lookback_meses, shares, costo, mask)
        fila = _fila(f"Top {k}", res["strat"])
        fila["Rotación media (%)"] = float(res["turnover"].mean() * 100.0)
        filas.append(fila)
    ref = []
    ref.append({**_fila("SPY (índice)", base["spy"]), "Rotación media (%)": float("nan")})
    ref.append({**_fila("Igual peso (todas)", base["ew_all"]), "Rotación media (%)": float("nan")})
    df = pd.DataFrame(filas + ref)
    return df, base["spy"], base["ew_all"], base["n_universo"]


def sweep_lookback_topn(
    precios: pd.DataFrame, lookbacks: list = None, tops: list = None, costo: float = COSTO_OPERACION,
    mask: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Barrido MOMENTUM sobre lookback (N meses de retorno) × Top-K. Devuelve una tabla larga
    con CAGR/Sharpe/Max DD/Calmar por combinación, para ver qué ventana × cuántas acciones
    funciona mejor. (El market cap no usa lookback, así que este barrido es solo momentum.)
    """
    from src.investment.backtest_v2 import cagr, calmar, cash_mensual, max_drawdown, sharpe

    lookbacks = lookbacks or [1, 3, 6, 9, 12, 15, 18]
    tops = tops or TOPS_DEFAULT
    cash_master = cash_mensual(precios.index)  # una sola vez; se reindexa por combo
    filas = []
    for lb in lookbacks:
        for k in tops:
            res = backtest_top_n(precios, "momentum", k, lb, None, costo, mask)
            strat = res["strat"]
            if len(strat) < 12:
                continue
            cash = cash_master.reindex(strat.index)
            filas.append({
                "Lookback (m)": lb, "Top-K": k,
                "CAGR (%)": cagr(strat, 12) * 100.0,
                "Sharpe": sharpe(strat, cash, 12),
                "Max DD (%)": max_drawdown(strat) * 100.0,
                "Calmar": calmar(strat, 12),
            })
    return pd.DataFrame(filas)
