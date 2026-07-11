# -*- coding: utf-8 -*-
"""
Módulo 8 — Momentum por cercanía al máximo de 52 semanas.

8A (selección de acciones, cross-sectional): sobre los constituyentes ACTUALES del Nasdaq-100
    (⚠️ sesgo de supervivencia — infla resultados; exploración direccional, no estimación fiel),
    ranquea por precio / máximo de 52 semanas y arma un top-15 igual-ponderado con rebalanceo
    mensual. Compara contra QQQ y contra el momentum 12-1 clásico sobre el mismo universo.

8B (timing del índice, robusto, SIN sesgo): al cierre de cada mes, si el índice está a menos
    del X% de su máximo de 52 semanas → invertido; si no → efectivo. Primo conceptual del
    SMA-10 (Faber). Incluye el análisis educativo de comprar en máximos vs. cualquier día.
"""
from __future__ import annotations

import io
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from src.data.yahoo_client import DATA_CACHE_DIR, get_daily_closes
from src.investment.backtest_v2 import COSTO_OPERACION

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_N100_CONST = Path(DATA_CACHE_DIR) / "NDX100_constituyentes.csv"
_N100_PRECIOS = Path(DATA_CACHE_DIR) / "NDX100_precios_mensual.csv"
VENTANA_52S = 252  # días hábiles


# ===========================================================================
# 8B — TIMING DEL ÍNDICE (robusto, sin sesgo)
# ===========================================================================
def _mensual(precio_diario: pd.Series) -> pd.Series:
    return precio_diario.resample("ME").last().dropna()


def timing_maximo(precio_diario: pd.Series, umbral_pct: float, costo: float = COSTO_OPERACION,
                  cash_m: pd.Series | None = None, ventana_dias: int = VENTANA_52S) -> dict:
    """
    Estrategia mensual: invertido si el índice está a menos de `umbral_pct`% de su máximo de las
    últimas `ventana_dias` (52 semanas por defecto) al cierre del mes; si no, a efectivo. Señal a
    fin de mes, se aplica el mes siguiente.
    """
    maximo = precio_diario.rolling(ventana_dias, min_periods=max(2, ventana_dias // 2)).max()
    dist_pct = (1.0 - precio_diario / maximo) * 100.0
    dist_m = dist_pct.resample("ME").last()
    pm = _mensual(precio_diario)
    idx = pm.pct_change().index
    dentro = (dist_m.reindex(idx) <= umbral_pct)
    dentro_aplicada = dentro.shift(1).fillna(True)
    ret_idx = pm.pct_change()
    if cash_m is None:
        cash_m = pd.Series(0.0, index=idx)
    ret = ret_idx.where(dentro_aplicada, cash_m.reindex(idx)).fillna(0.0)
    cambios = dentro_aplicada.astype(int).diff().abs().fillna(0)
    ret = (ret - cambios * costo).dropna()
    return {
        "ret_estrategia": ret, "ret_indice": ret_idx.reindex(ret.index),
        "posicion": dentro_aplicada.reindex(ret.index), "n_operaciones": int(cambios.sum()),
        "pct_invertido": float(dentro_aplicada.reindex(ret.index).mean()),
    }


def forward_desde_maximo(precio_diario: pd.Series, horizontes_dias: dict[str, int], tol: float = 0.001) -> pd.DataFrame:
    """
    Análisis educativo: retorno forward comprando en un MÁXIMO histórico (precio ≥ máximo previo)
    vs. comprando en un día cualquiera. Una fila por horizonte con ambos promedios y n.
    """
    p = precio_diario.dropna()
    ath = p.cummax()
    en_maximo = p >= ath * (1.0 - tol)
    filas = []
    for etiqueta, dias in horizontes_dias.items():
        if len(p) <= dias:
            continue
        fwd = p.shift(-dias) / p - 1.0
        fwd_valid = fwd.dropna()
        fila = {
            "Horizonte": etiqueta,
            "En máximo (%)": float(fwd_valid[en_maximo.reindex(fwd_valid.index, fill_value=False)].mean()) * 100.0,
            "Cualquier día (%)": float(fwd_valid.mean()) * 100.0,
            "n en máximo": int(en_maximo.reindex(fwd_valid.index, fill_value=False).sum()),
        }
        filas.append(fila)
    return pd.DataFrame(filas)


# ===========================================================================
# 8A — SELECCIÓN DE ACCIONES (Nasdaq-100, con sesgo de supervivencia)
# ===========================================================================
def constituyentes_nasdaq100(refresh: bool = False) -> list[str]:
    """Tickers actuales del Nasdaq-100 (Wikipedia), cacheados. Formato Yahoo."""
    if not refresh and _N100_CONST.exists():
        return pd.read_csv(_N100_CONST)["Ticker"].astype(str).tolist()
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        html = urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30).read().decode("utf-8", "ignore")
        tablas = pd.read_html(io.StringIO(html))
        comp = None
        for t in tablas:
            cols = [str(c).lower() for c in t.columns]
            if any("ticker" in c or "symbol" in c for c in cols):
                comp = t
                break
        if comp is None:
            return []
        col = [c for c in comp.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower()][0]
        tickers = [str(x).replace(".", "-").strip().upper() for x in comp[col].dropna()]
        tickers = [t for t in tickers if t and t != "NAN"]
        _N100_CONST.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"Ticker": tickers}).to_csv(_N100_CONST, index=False)
        return tickers
    except Exception:
        if _N100_CONST.exists():
            return pd.read_csv(_N100_CONST)["Ticker"].astype(str).tolist()
        return []


def descargar_precios_n100(tickers: list[str], refresh: bool = False, anios: int = 15) -> pd.DataFrame:
    """Cierre mensual ajustado de los tickers, limitado a los últimos `anios` años."""
    if not refresh and _N100_PRECIOS.exists():
        df = pd.read_csv(_N100_PRECIOS, index_col=0, parse_dates=[0])
        if not df.empty and df.index.max().date() >= date.today() - timedelta(days=7):
            return df
    inicio = (date.today() - timedelta(days=int(anios * 365.25 + 400)))
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        raw = yf.download(chunk, start=inicio.isoformat(), interval="1mo", auto_adjust=True, progress=False)
        if raw.empty:
            continue
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(columns={"Close": chunk[0]})
        frames.append(close)
    if not frames:
        return pd.DataFrame()
    precios = pd.concat(frames, axis=1)
    precios = precios.loc[:, ~precios.columns.duplicated()].dropna(how="all", axis=1).dropna(how="all").sort_index()
    _N100_PRECIOS.parent.mkdir(parents=True, exist_ok=True)
    precios.to_csv(_N100_PRECIOS)
    return precios


RET_MENSUAL_MAX = 1.5    # +150% en un mes → glitch de datos (excluir)
RET_MENSUAL_MIN = -0.90  # -90% en un mes → glitch/colapso (excluir)
PRECIO_MIN = 1.0         # precios < $1 en el universo S&P 500 → glitch (penny/dato malo)


def _sanitizar_precios(precios: pd.DataFrame) -> pd.DataFrame:
    """Marca como NaN los precios implausibles (< $1): Yahoo tiene glitches en delistadas del
    universo histórico (p.ej. 0.005 que luego 'salta' a 170). Un S&P 500 no cotiza bajo $1."""
    return precios.where(precios >= PRECIO_MIN)


def _backtest_topn(precios: pd.DataFrame, score: pd.DataFrame, top_n: int, costo: float,
                   mask: pd.DataFrame | None = None) -> dict:
    """
    Compra el top-N por `score` cada mes, igual peso, aplica el mes siguiente. Rotación incluida.
    Si `mask` (booleano mes × ticker de membresía point-in-time) se pasa, cada mes solo se rankean
    las acciones que ERAN miembros del índice esa fecha → SIN sesgo de supervivencia.
    Los retornos mensuales fuera de [-90%, +150%] se descartan como glitches de datos (una acción
    seleccionada con retorno implausible no se compra ese mes).
    """
    rets = precios.pct_change()
    rets = rets.where((rets <= RET_MENSUAL_MAX) & (rets >= RET_MENSUAL_MIN))
    if mask is not None:
        mask = mask.reindex(index=precios.index, columns=precios.columns).fillna(False)
    idx = precios.index
    strat = pd.Series(index=idx, dtype=float)
    turnover = pd.Series(index=idx, dtype=float)
    holdings: dict = {}          # mes de tenencia → lista de tickers
    contrib: dict = {}           # mes → Series(ticker → aporte al retorno de la cartera, EW)
    prev = set()
    conteo: dict = {}            # ticker → # de meses en el top
    for i in range(len(idx) - 1):
        f, f1 = idx[i], idx[i + 1]
        sc = score.loc[f].dropna()
        if mask is not None:
            sc = sc[mask.loc[f].reindex(sc.index).fillna(False).to_numpy()]
        r1 = rets.loc[f1]
        sc = sc[r1.reindex(sc.index).notna()]
        if len(sc) == 0:
            continue
        top = list(sc.sort_values(ascending=False).head(top_n).index)
        cur = set(top)
        n_ent = len(cur - prev)
        r1_top = r1.reindex(top)
        strat.loc[f1] = float(r1_top.mean()) - costo * 2.0 * n_ent / max(1, len(top))
        turnover.loc[f1] = n_ent / max(1, len(top))
        holdings[f1] = top
        contrib[f1] = (r1_top / len(top))  # aporte EW de cada acción al retorno del mes
        for _t in top:
            conteo[_t] = conteo.get(_t, 0) + 1
        prev = cur
    strat = strat.dropna()
    if mask is not None:
        ew_all = rets.where(mask.shift(1, fill_value=False)).mean(axis=1).reindex(strat.index)
    else:
        ew_all = rets.mean(axis=1).reindex(strat.index)
    ultimo_top = holdings[max(holdings)] if holdings else []
    _n_meses = max(1, len(holdings))
    mas_tenidas = pd.Series(conteo).sort_values(ascending=False) / _n_meses * 100.0  # % del tiempo en el top
    return {"ret": strat, "turnover_medio": float(turnover.reindex(strat.index).mean()), "ew_all": ew_all,
            "holdings": holdings, "ultimo_top": ultimo_top, "mas_tenidas": mas_tenidas, "contrib": contrib}


# ---------------------------------------------------------------------------
# Análisis de holdings y contribuciones (para explicar los picos de PnL de 8A)
# ---------------------------------------------------------------------------
def meses_extremos_con_driver(ret: pd.Series, contrib: dict, n: int = 12, peores: bool = False) -> pd.DataFrame:
    """
    Los `n` meses de mayor (o menor) retorno de la cartera, con la acción que MÁS aportó ese mes
    y el desglose de las TOP-3 acciones que explicaron el movimiento (para auditar que el PnL es
    real y no un glitch aislado).
    """
    r = ret.dropna().sort_values(ascending=peores).head(n)
    filas = []
    for f in r.index:
        c = contrib.get(f)
        if c is None or len(c.dropna()) == 0:
            drv, aporte, top3 = "—", float("nan"), "—"
        else:
            cc = c.dropna().sort_values(ascending=peores)  # peores → más negativas primero
            drv, aporte = cc.index[0], float(cc.iloc[0])
            top3 = " · ".join(f"{t} {v * 100:+.1f}" for t, v in cc.head(3).items())
        filas.append({"Mes": f.strftime("%b %Y"), "Retorno cartera (%)": float(r[f]) * 100.0,
                      "Acción #1": drv, "Su aporte (pp)": aporte * 100.0, "Top 3 aportantes (pp)": top3})
    return pd.DataFrame(filas)


def tabla_holdings(holdings: dict) -> pd.DataFrame:
    """Timeline de las tenencias: una fila por mes con la lista del top-K (más reciente arriba)."""
    if not holdings:
        return pd.DataFrame()
    filas = [{"Mes": f.strftime("%Y-%m"), "Top-K": ", ".join(v)} for f, v in sorted(holdings.items(), reverse=True)]
    return pd.DataFrame(filas)


def presencia_ticker(holdings: dict, ticker: str) -> dict:
    """Meses en que `ticker` estuvo en el top: cantidad, primer y último mes, y la lista."""
    meses = [f for f, v in sorted(holdings.items()) if ticker in v]
    return {
        "ticker": ticker, "n_meses": len(meses), "pct": (len(meses) / max(1, len(holdings)) * 100.0),
        "primero": (meses[0] if meses else None), "ultimo": (meses[-1] if meses else None), "meses": meses,
    }


def tickers_del_universo(holdings: dict) -> list[str]:
    """Todos los tickers que alguna vez estuvieron en el top (para el selector)."""
    return sorted({t for v in holdings.values() for t in v})


def contribuciones_totales(contrib: dict) -> pd.DataFrame:
    """
    Aporte TOTAL de cada acción al PnL de la cartera: suma de sus aportes mensuales (en pp),
    cuántos meses estuvo, y su mejor y peor mes. Atribución de primer orden (suma aritmética de
    aportes mensuales; ignora la composición, pero sirve para ver quién movió la aguja).
    """
    if not contrib:
        return pd.DataFrame()
    df = pd.DataFrame(contrib).T  # meses × tickers
    filas = []
    for t in df.columns:
        s = df[t].dropna()
        if s.empty:
            continue
        filas.append({"Acción": t, "Aporte total (pp)": float(s.sum()) * 100.0, "Meses en cartera": int(len(s)),
                      "Mejor mes (pp)": float(s.max()) * 100.0, "Peor mes (pp)": float(s.min()) * 100.0})
    out = pd.DataFrame(filas)
    return out.sort_values("Aporte total (pp)", ascending=False).reset_index(drop=True) if not out.empty else out


def senal_indice_mensual(index_diario: pd.Series, tipo: str = "maximo52", ventana_meses: int = 9,
                         x_pct: float = 12.0, lookback: int = 12, skip: int = 1) -> pd.Series:
    """
    Señal mensual booleana de RÉGIMEN sobre el ÍNDICE (para usar como filtro de la selección de
    acciones de 8A). Decisión a fin de mes; True = risk-on.
      • tipo="maximo52": invertido si el índice está a ≤ x_pct% de su máximo de `ventana_meses`.
      • tipo="momentum": invertido si el momentum `lookback`-`skip` del índice es positivo.
    """
    if index_diario is None or index_diario.empty:
        return pd.Series(dtype=bool)
    if tipo == "maximo52":
        vd = max(2, int(ventana_meses * 21))
        maximo = index_diario.rolling(vd, min_periods=vd // 2).max()
        dist = (1.0 - index_diario / maximo) * 100.0
        return (dist.resample("ME").last() <= x_pct)
    pm = index_diario.resample("ME").last()
    return ((pm.shift(skip) / pm.shift(lookback) - 1.0) > 0.0)


def filtrar_estrategia(ret_estrategia: pd.Series, senal_mensual: pd.Series, cash_m: pd.Series) -> dict:
    """
    Aplica el filtro de régimen del índice a los retornos de la estrategia de acciones (sin
    look-ahead: la señal de fin de mes t gatea el retorno del mes t+1). Fuera del régimen → efectivo.
    Devuelve {'ret': serie filtrada, 'pct_in': fracción de meses invertida}.
    """
    if ret_estrategia is None or ret_estrategia.empty or senal_mensual is None or senal_mensual.dropna().empty:
        return {"ret": ret_estrategia if ret_estrategia is not None else pd.Series(dtype=float), "pct_in": 1.0}
    r = ret_estrategia.dropna().copy()
    r.index = pd.to_datetime(r.index).to_period("M")
    r = r[~r.index.duplicated(keep="last")]
    s = senal_mensual.dropna().astype(float).copy()
    s.index = pd.to_datetime(s.index).to_period("M")
    s = s[~s.index.duplicated(keep="last")]
    pos = s.shift(1).reindex(r.index)          # señal de t-1 aplica a t (sin look-ahead)
    dentro = pos.fillna(1.0) > 0.5             # antes de tener señal → invertido
    c = cash_m.copy()
    c.index = pd.to_datetime(c.index).to_period("M")
    c = c[~c.index.duplicated(keep="last")].reindex(r.index).fillna(0.0)
    out = r.where(dentro, c)
    out.index = out.index.to_timestamp(how="end").normalize()
    return {"ret": out, "pct_in": float(dentro.mean())}


def sweep_filtro_indice(ret_estrategia: pd.Series, index_diario: pd.Series, cash_m: pd.Series,
                        ventanas_m: list[int], xs: list[float], ppa: int = 12) -> pd.DataFrame:
    """
    Barrido amplio del filtro de índice sobre una estrategia de acciones ya calculada: sin filtro,
    filtro por momentum 12-1 del índice, y grilla de máximo-52s (ventana meses × X%). Una fila por
    variante con CAGR/Sharpe/MaxDD y % del tiempo invertido.
    """
    from src.investment.backtest_v2 import cagr, max_drawdown, sharpe

    def _met(lbl, res, **extra):
        r = res["ret"].dropna()
        return {"Filtro": lbl, **extra, "CAGR (%)": cagr(r, ppa) * 100.0,
                "Sharpe": sharpe(r, cash_m.reindex(r.index), ppa),
                "Max DD (%)": max_drawdown(r) * 100.0, "% invertido": res["pct_in"] * 100.0}

    filas = [_met("Sin filtro", {"ret": ret_estrategia, "pct_in": 1.0}, Ventana="—", X="—")]
    s_mom = senal_indice_mensual(index_diario, "momentum")
    filas.append(_met("Momentum 12-1 (índice)", filtrar_estrategia(ret_estrategia, s_mom, cash_m), Ventana="mom", X="mom"))
    for v in ventanas_m:
        for x in xs:
            s = senal_indice_mensual(index_diario, "maximo52", v, x)
            filas.append(_met(f"Máx {v}m / {x:.0f}%", filtrar_estrategia(ret_estrategia, s, cash_m), Ventana=v, X=x))
    return pd.DataFrame(filas)


def conclusion_barrido_2d(grid: pd.DataFrame, metrica: str, bh_val: float | None = None) -> str:
    """
    Conclusión en texto (estilo analista) del barrido 2D ventana × X: mejor combinación, mejor
    ventana y mejor X en promedio, robustez (dispersión del cuartil superior) y comportamiento
    con X alto. `grid`: índice=ventana(meses), columnas=X%. `bh_val`: métrica del buy & hold.
    """
    if grid.empty:
        return "Sin datos para concluir."
    # Las tres métricas son 'mayor = mejor': Sharpe y CAGR obvio; el Max DD se reporta NEGATIVO,
    # así que menos negativo (mayor) también es mejor.
    mayor_mejor = True
    st_ = grid.stack()
    best = (st_.idxmax() if mayor_mejor else st_.idxmin())
    best_val = (st_.max() if mayor_mejor else st_.min())
    bv, bx = best  # (ventana, X)
    prom_v = grid.mean(axis=1)
    prom_x = grid.mean(axis=0)
    mejor_v = (prom_v.idxmax() if mayor_mejor else prom_v.idxmin())
    mejor_x = (prom_x.idxmax() if mayor_mejor else prom_x.idxmin())
    # robustez: dispersión del cuartil superior de celdas
    q = st_.quantile(0.75) if mayor_mejor else st_.quantile(0.25)
    top = st_[st_ >= q] if mayor_mejor else st_[st_ <= q]
    disp = float(top.std())
    # X alto: promedio de las 2 columnas más altas vs las 2 más bajas
    xs = sorted(grid.columns)
    x_bajo = grid[xs[:2]].mean().mean()
    x_alto = grid[xs[-2:]].mean().mean()

    partes = []
    partes.append(
        f"**Mejor combinación:** ventana **{bv} meses** × **X={bx}%** → {metrica} = **{best_val:.2f}**"
        + (f" (buy & hold: {bh_val:.2f})." if bh_val is not None else ".")
    )
    partes.append(
        f"**En promedio**, la mejor ventana fue **{mejor_v} meses** y el mejor umbral **X={mejor_x}%**; "
        f"la dispersión dentro del cuartil superior de celdas es {disp:.2f} "
        + ("(baja → resultado robusto, no depende de un valor mágico)." if disp < 0.08 else "(alta → sensible a los parámetros, cuidado con el sobreajuste).")
    )
    if mayor_mejor:
        tendencia = ("empeora" if x_alto < x_bajo - 0.02 else ("mejora" if x_alto > x_bajo + 0.02 else "cambia poco"))
        partes.append(
            f"**Con X alto** (dejar más margen antes de salir), la métrica {tendencia}: el filtro se activa "
            "menos y la estrategia converge al buy & hold (deja de proteger). El valor del timing está en "
            "los **X chicos-medios**, no en aflojar el umbral."
        )
    return "\n\n".join(partes)


def contribucion_ticker_serie(contrib: dict, ticker: str) -> pd.Series:
    """Serie temporal del aporte mensual (pp) de un ticker a la cartera (meses en que estuvo)."""
    datos = {f: c.get(ticker) for f, c in contrib.items() if hasattr(c, "get") and c.get(ticker) is not None}
    if not datos:
        return pd.Series(dtype=float)
    s = pd.Series(datos).sort_index() * 100.0
    return s.dropna()


def backtest_cercania_maximo(precios: pd.DataFrame, top_n: int = 15, costo: float = COSTO_OPERACION,
                             mask: pd.DataFrame | None = None) -> dict:
    """8A por cercanía al máximo de 52s: score = precio / máximo de 52s (más cerca de 1 = mejor)."""
    precios = _sanitizar_precios(precios)
    maximo = precios.rolling(12, min_periods=6).max()  # 12 meses ≈ 52 semanas (precios mensuales)
    score = precios / maximo
    return _backtest_topn(precios, score, top_n, costo, mask)


def backtest_momentum_12_1(precios: pd.DataFrame, top_n: int = 15, costo: float = COSTO_OPERACION,
                           mask: pd.DataFrame | None = None) -> dict:
    """8A por momentum 12-1 clásico: retorno de 12 meses excluyendo el último."""
    precios = _sanitizar_precios(precios)
    score = precios.shift(1) / precios.shift(12) - 1.0
    score = score.where(score <= 9.0)  # >900% en 11 meses → glitch de datos (excluir del ranking)
    return _backtest_topn(precios, score, top_n, costo, mask)


def backtest_52_acciones(
    precios: pd.DataFrame, ventana_meses: int = 12, x_entrada: float = 8.0, x_salida: float = 12.0,
    top_n: int = 15, costo: float = COSTO_OPERACION, mask: pd.DataFrame | None = None,
    cash_m: pd.Series | None = None,
) -> dict:
    """
    Estrategia de MÁXIMO DE 52s SOBRE ACCIONES con banda asimétrica por acción + top-K.

    Cada mes, para cada acción se mide su distancia (%) por debajo de su máximo rodante de
    `ventana_meses` meses. Una acción es ELEGIBLE si está a ≤ `x_entrada`% de su máximo (entrada);
    una acción ya en cartera se MANTIENE mientras siga a ≤ `x_salida`% (histéresis). Entre las
    elegibles se compra el top-K más cercano a su máximo, a peso 1/top_n; los huecos (si hay menos
    de K elegibles, típico en mercados débiles) van a EFECTIVO. Rebalanceo mensual, aplica el mes
    siguiente (sin look-ahead), con `mask` de membresía point-in-time (sin sesgo de supervivencia).
    """
    precios = _sanitizar_precios(precios)
    vd = max(2, int(round(ventana_meses)))  # precios MENSUALES → ventana en meses
    maximo = precios.rolling(vd, min_periods=max(2, vd // 2)).max()
    dist = (1.0 - precios / maximo) * 100.0  # % por debajo del máximo de N meses
    rets = precios.pct_change()
    rets = rets.where((rets <= RET_MENSUAL_MAX) & (rets >= RET_MENSUAL_MIN))
    if mask is not None:
        mask = mask.reindex(index=precios.index, columns=precios.columns).fillna(False)
    idx = precios.index
    if cash_m is None:
        cash_m = pd.Series(0.0, index=idx)
    w_stock = 1.0 / top_n

    strat = pd.Series(index=idx, dtype=float)
    turnover = pd.Series(index=idx, dtype=float)
    peso_inv = pd.Series(index=idx, dtype=float)
    holdings: dict = {}
    contrib: dict = {}
    conteo: dict = {}
    prev: set = set()
    for i in range(len(idx) - 1):
        f, f1 = idx[i], idx[i + 1]
        d = dist.loc[f].dropna()
        if mask is not None:
            d = d[mask.loc[f].reindex(d.index).fillna(False).to_numpy()]
        r1 = rets.loc[f1]
        d = d[r1.reindex(d.index).notna()]
        cash1 = float(cash_m.get(f1, 0.0))
        if len(d) == 0:
            strat.loc[f1] = cash1; turnover.loc[f1] = 0.0; peso_inv.loc[f1] = 0.0
            holdings[f1] = []; prev = set(); continue
        entrada = set(d[d <= x_entrada].index)
        mantener = set(d[d <= x_salida].index) & prev
        elegibles = entrada | mantener
        d_el = d[d.index.isin(elegibles)]
        top = list(d_el.sort_values(ascending=True).head(top_n).index)  # menor dist = más cerca del máximo
        cur = set(top)
        n_hold = len(top)
        n_ent = len(cur - prev)
        r1_top = r1.reindex(top) if top else pd.Series(dtype=float)
        w_cash = 1.0 - n_hold * w_stock
        strat.loc[f1] = (float(r1_top.sum()) * w_stock if n_hold else 0.0) + w_cash * cash1 - costo * 2.0 * n_ent / top_n
        turnover.loc[f1] = n_ent / top_n
        peso_inv.loc[f1] = n_hold * w_stock
        holdings[f1] = top
        contrib[f1] = (r1_top * w_stock) if n_hold else pd.Series(dtype=float)
        for _t in top:
            conteo[_t] = conteo.get(_t, 0) + 1
        prev = cur
    strat = strat.dropna()
    if mask is not None:
        ew_all = rets.where(mask.shift(1, fill_value=False)).mean(axis=1).reindex(strat.index)
    else:
        ew_all = rets.mean(axis=1).reindex(strat.index)
    ultimo_top = holdings[max(holdings)] if holdings else []
    _n_meses = max(1, len(holdings))
    mas_tenidas = pd.Series(conteo).sort_values(ascending=False) / _n_meses * 100.0 if conteo else pd.Series(dtype=float)
    return {"ret": strat, "turnover_medio": float(turnover.reindex(strat.index).mean()), "ew_all": ew_all,
            "holdings": holdings, "ultimo_top": ultimo_top, "mas_tenidas": mas_tenidas, "contrib": contrib,
            "pct_invertido": float(peso_inv.reindex(strat.index).mean())}


def barrido_52_acciones(
    precios: pd.DataFrame, ventana_meses: int, xs_entrada: list[float], xs_salida: list[float],
    top_n: int, cash_m: pd.Series, mask: pd.DataFrame | None = None, metrica: str = "Sharpe",
    costo: float = COSTO_OPERACION,
) -> pd.DataFrame:
    """Matriz X% entrada (filas) × X% salida (columnas) de la estrategia de 52s sobre acciones."""
    from src.investment.backtest_v2 import cagr, calmar, max_drawdown, sharpe

    grid = {}
    for xe in xs_entrada:
        fila = {}
        for xs in xs_salida:
            if xe > xs:
                fila[xs] = np.nan
                continue
            res = backtest_52_acciones(precios, ventana_meses, float(xe), float(xs), top_n,
                                       costo=costo, mask=mask, cash_m=cash_m)
            r = res["ret"].dropna()
            if r.empty:
                fila[xs] = np.nan
                continue
            ca = cash_m.reindex(r.index)
            if metrica == "Sharpe":
                fila[xs] = sharpe(r, ca, 12)
            elif metrica == "Calmar":
                fila[xs] = calmar(r, 12)
            elif metrica == "CAGR (%)":
                fila[xs] = cagr(r, 12) * 100.0
            elif metrica == "Max DD (%)":
                fila[xs] = max_drawdown(r) * 100.0
            else:
                fila[xs] = res["pct_invertido"] * 100.0
        grid[xe] = fila
    df = pd.DataFrame(grid).T
    df.index.name = "X entrada (%)"
    df.columns.name = "X salida (%)"
    return df
