# -*- coding: utf-8 -*-
"""
Módulo 9 — Spreads de crédito high yield como señal de compra/venta.

Serie base: ICE BofA US High Yield Option-Adjusted Spread (FRED `BAMLH0A0HYM2`),
diaria desde 1996-12-31. FRED la publica en PUNTOS PORCENTUALES (p.ej. 5.0 = 500 pbs);
acá se convierte a PUNTOS BASE (×100) y se documenta. Es dato de cierre de mercado, no
requiere rezago de publicación; para señales mensuales se usa el último día hábil del mes.

Reglas globales (heredadas del resto del tab): cero look-ahead (la señal se calcula con el
cierre de fin de mes y se aplica el mes siguiente), precios ajustados, costo 0.10% por
operación, holdout 2020+ reportado aparte, tablas de sensibilidad.

Tres señales independientes:
  9A — Nivel: percentil EXPANSIVO del spread (solo datos hasta la fecha, mínimo 5 años de
       historia → utilizable desde ~2002). Estados: pánico (>90), estrés (70-90),
       normal (30-70), complacencia (<30).
  9B — Dirección: cambio a 3 meses (63 días hábiles), en pbs. Estados: ensanchándose fuerte
       (>+100), ensanchándose (+25..+100), estable (-25..+25), comprimiéndose (<-25).
  9C — Distancia desde el mínimo de 52 semanas (252 días hábiles), en pbs. Umbral clave +300.
"""
from __future__ import annotations

import io
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.yahoo_client import DATA_CACHE_DIR

FRED_HY_CODE = "BAMLH0A0HYM2"
_HY_CACHE = Path(DATA_CACHE_DIR) / "HY_SPREAD_BPS.csv"
# Fallback local: si el usuario descarga el CSV de FRED (https://fred.stlouisfed.org/series/
# BAMLH0A0HYM2 → Download → CSV) y lo deja en assets/, se usa directo (historial completo).
_ASSETS_DIR = Path(DATA_CACHE_DIR).parent / "assets"
_HY_ASSETS = [_ASSETS_DIR / "BAMLH0A0HYM2.csv", _ASSETS_DIR / "HY_SPREAD.csv"]


def _leer_csv_fred_local(path: Path) -> pd.Series:
    """Lee un CSV descargado de FRED (columnas fecha + valor, valor en puntos porcentuales)."""
    df = pd.read_csv(path)
    df = df.dropna(axis=1, how="all")
    cols = list(df.columns)
    fecha_col = cols[0]
    val_col = next((c for c in cols[1:] if c.lower() not in ("date", "observation_date")), cols[-1])
    df[fecha_col] = pd.to_datetime(df[fecha_col], errors="coerce")
    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
    return df.dropna(subset=[fecha_col, val_col]).set_index(fecha_col)[val_col].sort_index()

# Estados (etiquetas y orden para tablas)
ESTADOS_NIVEL = ["Complacencia (<30)", "Normal (30-70)", "Estrés (70-90)", "Pánico (>90)"]
ESTADOS_DIR = ["Comprimiéndose (<-25)", "Estable (-25..+25)", "Ensanchándose (+25..+100)", "Ensanchándose fuerte (>+100)"]


# ---------------------------------------------------------------------------
# DATOS
# ---------------------------------------------------------------------------
def _fetch_fred_csv(codigo: str, start: str = "1996-12-31") -> pd.Series:
    """Descarga la serie completa desde el endpoint CSV de FRED (sin API key)."""
    url = (
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={codigo}"
        f"&cosd={start}&coed={date.today():%Y-%m-%d}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    df = pd.read_csv(io.StringIO(raw))
    df.columns = ["date", "val"]
    df["date"] = pd.to_datetime(df["date"])
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    return df.dropna().set_index("date")["val"].sort_index()


def serie_spread_hy(refresh: bool = False) -> pd.Series:
    """
    Spread HY diario en PUNTOS BASE (pbs), cacheado en disco. Intenta el CSV directo de FRED
    (historial completo); si falla, cae a `btv2.fred_series` (pandas_datareader). Devuelve una
    Serie vacía si no hay conexión ni caché.
    """
    # 1) Archivo local en assets/ (el usuario lo bajó de FRED) → prioridad, historial completo.
    for _ap in _HY_ASSETS:
        if _ap.exists():
            try:
                loc = _leer_csv_fred_local(_ap)
                if len(loc) > 500:
                    bps_loc = (loc * 100.0).rename("spread_bps")
                    _HY_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    bps_loc.to_csv(_HY_CACHE)
                    return bps_loc
            except Exception:
                pass

    # 2) Caché en disco (salvo refresh).
    if not refresh and _HY_CACHE.exists():
        try:
            s = pd.read_csv(_HY_CACHE, index_col=0, parse_dates=[0]).iloc[:, 0]
            if not s.empty:
                return s.rename("spread_bps")
        except Exception:
            pass

    # 3) Descarga directa del CSV de FRED (historial completo en máquinas con acceso normal).
    serie = pd.Series(dtype=float)
    try:
        serie = _fetch_fred_csv(FRED_HY_CODE)
    except Exception:
        serie = pd.Series(dtype=float)
    if serie.empty:
        try:
            from src.investment import backtest_v2 as btv2
            serie = btv2.fred_series(FRED_HY_CODE, refresh=refresh)
        except Exception:
            serie = pd.Series(dtype=float)

    if serie.empty:
        if _HY_CACHE.exists():
            return pd.read_csv(_HY_CACHE, index_col=0, parse_dates=[0]).iloc[:, 0].rename("spread_bps")
        return pd.Series(dtype=float, name="spread_bps")

    bps = (serie * 100.0).rename("spread_bps")  # puntos porcentuales → puntos base
    _HY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    bps.to_csv(_HY_CACHE)
    return bps


def sanity_spread(spread_bps: pd.Series) -> pd.DataFrame:
    """Tabla de verificación: máximos/mínimos de referencia por período de crisis/calma."""
    refs = {
        "2002-10 (puntocom)": ("2002-10-01", "2002-11-30", "max", 1100),
        "2008-11 (GFC)": ("2008-10-01", "2008-12-31", "max", 2000),
        "2020-03 (COVID)": ("2020-03-01", "2020-04-15", "max", 1100),
        "1997-10 (calma)": ("1997-06-01", "1997-12-31", "min", 300),
        "2007-06 (calma)": ("2007-01-01", "2007-06-30", "min", 250),
        "2021-06 (calma)": ("2021-01-01", "2021-12-31", "min", 300),
    }
    filas = []
    for nombre, (ini, fin, cual, esperado) in refs.items():
        sub = spread_bps[(spread_bps.index >= ini) & (spread_bps.index <= fin)]
        obs = (sub.max() if cual == "max" else sub.min()) if len(sub) else np.nan
        filas.append({"Período": nombre, "Tipo": cual, "Observado (pbs)": obs,
                      "Referencia (pbs)": esperado, "Datos": len(sub)})
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# SEÑALES
# ---------------------------------------------------------------------------
def spread_mensual(spread_bps: pd.Series) -> pd.Series:
    """Último valor de cada mes (fin de mes hábil)."""
    return spread_bps.resample("ME").last().dropna().rename("spread_bps")


def senal_nivel(spread_m: pd.Series, min_meses: int = 60) -> pd.DataFrame:
    """9A — percentil EXPANSIVO (solo pasado) + estado. Empieza a valer tras `min_meses`."""
    pct = spread_m.expanding(min_periods=min_meses).apply(lambda w: float((w <= w[-1]).mean()), raw=False)
    pct = pct * 100.0
    estado = pd.cut(pct, bins=[-0.1, 30, 70, 90, 100.1], labels=ESTADOS_NIVEL)
    return pd.DataFrame({"percentil": pct, "estado": estado})


def senal_direccion(spread_bps: pd.Series, dias: int = 63) -> pd.DataFrame:
    """9B — cambio a 3 meses (63 días hábiles) en pbs, muestreado a fin de mes, + estado."""
    cambio = (spread_bps - spread_bps.shift(dias)).resample("ME").last()
    estado = pd.cut(cambio, bins=[-1e9, -25, 25, 100, 1e9], labels=ESTADOS_DIR)
    return pd.DataFrame({"cambio_3m": cambio, "estado": estado}).dropna(subset=["cambio_3m"])


def senal_distancia_min(spread_bps: pd.Series, ventana: int = 252) -> pd.Series:
    """9C — distancia (pbs) del spread sobre su mínimo rodante de 52 semanas, a fin de mes."""
    minimo = spread_bps.rolling(ventana, min_periods=ventana // 2).min()
    return (spread_bps - minimo).resample("ME").last().rename("dist_min52")


# ---------------------------------------------------------------------------
# ANÁLISIS 1 — RETORNOS CONDICIONALES
# ---------------------------------------------------------------------------
def retornos_forward(precio_m: pd.Series, horizontes: list[int]) -> dict[int, pd.Series]:
    """Retorno forward del activo a `h` meses, indexado en la fecha de decisión."""
    return {h: (precio_m.shift(-h) / precio_m - 1.0) for h in horizontes}


def tabla_condicional(estados: pd.Series, fwd: dict[int, pd.Series], orden_estados: list[str], horizonte: int) -> pd.DataFrame:
    """Estadísticas del retorno forward a `horizonte` meses por estado + fila incondicional."""
    r = fwd[horizonte]
    filas = []
    for est in orden_estados:
        mask = (estados == est)
        vals = r[mask.reindex(r.index, fill_value=False)].dropna()
        filas.append(_fila_cond(est, vals))
    filas.append(_fila_cond("— Incondicional —", r.dropna()))
    return pd.DataFrame(filas)


def _fila_cond(nombre: str, vals: pd.Series) -> dict:
    if len(vals) == 0:
        return {"Estado": nombre, "n": 0, "Promedio (%)": np.nan, "Mediana (%)": np.nan, "% positivo": np.nan, "Peor (%)": np.nan}
    return {
        "Estado": nombre, "n": int(len(vals)),
        "Promedio (%)": float(vals.mean()) * 100.0, "Mediana (%)": float(vals.median()) * 100.0,
        "% positivo": float((vals > 0).mean()) * 100.0, "Peor (%)": float(vals.min()) * 100.0,
    }


def matriz_nivel_direccion(nivel: pd.DataFrame, direccion: pd.DataFrame, fwd12: pd.Series) -> pd.DataFrame:
    """Retorno forward a 12m promedio por celda (estado de nivel × estado de dirección)."""
    idx = nivel.index.intersection(direccion.index).intersection(fwd12.dropna().index)
    ne, de, rr = nivel["estado"].reindex(idx), direccion["estado"].reindex(idx), fwd12.reindex(idx)
    out = pd.DataFrame(index=ESTADOS_NIVEL, columns=ESTADOS_DIR, dtype=float)
    for en in ESTADOS_NIVEL:
        for ed in ESTADOS_DIR:
            m = (ne == en) & (de == ed)
            vals = rr[m].dropna()
            out.loc[en, ed] = float(vals.mean()) * 100.0 if len(vals) else np.nan
    return out


# ---------------------------------------------------------------------------
# ANÁLISIS 3 — ESTRATEGIA OPERABLE
# ---------------------------------------------------------------------------
def estrategia_credito(
    spy_m: pd.Series, spread_m: pd.Series, dist_min_m: pd.Series, cambio3m_m: pd.Series,
    umbral_salida: float = 300.0, confirm_reentrada: int = 2, costo: float = 0.001,
    cash_m: pd.Series | None = None, lag_ejecucion: int = 1,
) -> dict:
    """
    Mensual: parte 100% en SPY. SALE a efectivo cuando 9C > `umbral_salida` Y el spread sigue
    ensanchándose (cambio 3m > +25). REENTRA cuando el spread lleva `confirm_reentrada` meses
    consecutivos bajando (comprimiéndose). Costo por cada cambio de posición.

    `lag_ejecucion`:
      1 (default) → la señal de fin de mes se aplica al retorno del **mes siguiente** (SIN look-ahead,
                    tradeable). 0 → se aplica al retorno del **mismo mes** de la señal; ATENCIÓN: en un
                    backtest mensual esto usa el cierre de fin de mes para capturar el retorno de ese
                    mismo mes → introduce LOOK-AHEAD y sobreestima el resultado (solo sería realista si
                    de verdad se ejecuta intra-mes con el dato diario del spread).
    """
    ret_spy = spy_m.pct_change()
    idx = ret_spy.index
    dist = dist_min_m.reindex(idx)
    camb = cambio3m_m.reindex(idx)
    baja = spread_m.reindex(idx).diff() < 0  # mes con spread menor al anterior

    invertido = True
    pos = pd.Series(True, index=idx)
    for i in range(len(idx)):
        f = idx[i]
        if invertido:
            if pd.notna(dist.loc[f]) and pd.notna(camb.loc[f]) and dist.loc[f] > umbral_salida and camb.loc[f] > 25:
                invertido = False
        else:
            # reentra si los últimos `confirm` meses (incluido este) el spread bajó
            ventana = baja.iloc[max(0, i - confirm_reentrada + 1): i + 1]
            if len(ventana) >= confirm_reentrada and bool(ventana.all()):
                invertido = True
        pos.iloc[i] = invertido

    # lag=1 → opera el mes siguiente (sin look-ahead); lag=0 → mismo mes (look-ahead, ver docstring)
    pos_aplicada = pos.shift(lag_ejecucion, fill_value=True) if lag_ejecucion else pos.copy()
    if cash_m is None:
        cash_m = pd.Series(0.0, index=idx)
    ret_estrategia = ret_spy.where(pos_aplicada, cash_m.reindex(idx)).fillna(0.0)
    cambios = pos_aplicada.astype(int).diff().abs().fillna(0)
    ret_estrategia = ret_estrategia - cambios * costo

    return {
        "ret_estrategia": ret_estrategia.dropna(),
        "ret_spy": ret_spy.reindex(ret_estrategia.dropna().index),
        "posicion": pos_aplicada,
        "n_operaciones": int(cambios.sum()),
        "pct_invertido": float(pos_aplicada.mean()),
    }


# ---------------------------------------------------------------------------
# ANÁLISIS 3-diario — SEÑAL DIARIA con ejecución configurable (D / W / M)
# ---------------------------------------------------------------------------
_FREQ_PPA = {"D": 252, "W-FRI": 52, "W": 52, "ME": 12}


def estrategia_credito_diaria(
    precio_diario: pd.Series, spread_diario: pd.Series, umbral_salida: float = 300.0,
    confirm_meses: float = 2.0, costo: float = 0.001, cash_diario: pd.Series | None = None,
    frecuencia: str = "ME", ventana_min: int = 252, ventana_dir: int = 63,
) -> dict:
    """
    Versión de SEÑAL DIARIA de la estrategia de crédito, con ejecución (rebalanceo) configurable.

    Señal (diaria, el spread es dato same-day observable → no hay look-ahead al usar el valor del día):
      dist  = spread − mínimo rodante de 252 días hábiles (distancia sobre el mínimo de 52s, pbs)
      cambio = spread − spread de hace 63 días hábiles (~3 meses)
      SALE a efectivo cuando dist > `umbral_salida` Y cambio > +25 (ensanchándose).
      REENTRA cuando el spread comprimió: hoy < spread de hace `confirm_meses` meses (~21 días/mes).
      Máquina de estado diaria con histéresis.

    Ejecución: la posición OBJETIVO solo se refresca en los puntos de la cadencia (`frecuencia`):
      "D" cada día · "W-FRI" cada viernes · "ME" cada fin de mes; entre puntos se sostiene la última.
      Sin look-ahead: la decisión de un punto se aplica al retorno del día SIGUIENTE (shift 1).

    Devuelve dict con ret_estrategia (diario), ret_activo, posicion (aplicada), n_operaciones,
    pct_invertido y ppa (periodos/año, = 252 porque la serie de retornos es diaria).
    """
    px = precio_diario.dropna()
    sp = spread_diario.dropna()
    idx = px.index.intersection(sp.index)
    vacio = {"ret_estrategia": pd.Series(dtype=float), "ret_activo": pd.Series(dtype=float),
             "posicion": pd.Series(dtype=bool), "n_operaciones": 0, "pct_invertido": 0.0, "ppa": 252}
    if len(idx) < ventana_min:
        return vacio
    px = px.reindex(idx)
    sp = sp.reindex(idx)
    ret_px = px.pct_change()

    minimo = sp.rolling(ventana_min, min_periods=ventana_min // 2).min()
    dist = (sp - minimo).values
    cambio = (sp - sp.shift(ventana_dir)).values
    comp_lag = max(1, int(round(confirm_meses * 21)))
    baja = (sp < sp.shift(comp_lag)).values  # comprimió neto en ~confirm_meses meses

    invertido = True
    pos = np.empty(len(idx), dtype=bool)
    for i in range(len(idx)):
        if invertido:
            if np.isfinite(dist[i]) and np.isfinite(cambio[i]) and dist[i] > umbral_salida and cambio[i] > 25:
                invertido = False
        elif bool(baja[i]):
            invertido = True
        pos[i] = invertido
    pos = pd.Series(pos, index=idx)

    if frecuencia in ("D", None):
        pos_exec = pos
    else:
        pts = pos.resample(frecuencia).last()
        pos_exec = pts.reindex(idx, method="ffill").fillna(True).astype(bool)

    pos_aplicada = pos_exec.shift(1, fill_value=True)  # se opera el día siguiente a la decisión
    if cash_diario is None:
        from src.investment import backtest_v2 as btv2
        cash_diario = btv2.cash_diario(idx)
    cash_al = cash_diario.reindex(idx).fillna(0.0)
    ret_estrategia = ret_px.where(pos_aplicada, cash_al).fillna(0.0)
    cambios = pos_aplicada.astype(int).diff().abs().fillna(0)
    ret_estrategia = ret_estrategia - cambios * costo

    return {
        "ret_estrategia": ret_estrategia.dropna(),
        "ret_activo": ret_px.reindex(ret_estrategia.dropna().index),
        "posicion": pos_aplicada,
        "n_operaciones": int(cambios.sum()),
        "pct_invertido": float(pos_aplicada.mean()),
        "ppa": 252,
    }


def _dist_max_pct_diaria(precio_diario: pd.Series, ventana_dias: int) -> pd.Series:
    """% que el precio está POR DEBAJO de su máximo rodante (0 = en el máximo, 12 = 12% abajo)."""
    maximo = precio_diario.rolling(ventana_dias, min_periods=max(2, ventana_dias // 2)).max()
    return (1.0 - precio_diario / maximo) * 100.0


def _momentum_diario(serie: pd.Series, lb_dias: int, sk_dias: int) -> pd.Series:
    """Momentum 'lookback-skip' diario: P(t−skip)/P(t−lookback) − 1."""
    return serie.shift(sk_dias) / serie.shift(lb_dias) - 1.0


def senales_momentum_vs_tbill(index_diario: pd.Series, cash_diario: pd.Series,
                              lookback_meses: float = 9.0, skip_meses: float = 1.0) -> pd.DataFrame:
    """
    Momentum del índice y de las T-bills sobre la misma ventana (lookback−skip). La señal de
    momentum 12-1 es risk-on cuando el momentum del índice supera al del efectivo (T-bills).
    Devuelve DataFrame con columnas: mom_indice, mom_tbill, risk_on (bool). Diario.
    """
    idx = index_diario.dropna()
    lb = max(2, int(round(lookback_meses * 21)))
    sk = max(0, int(round(skip_meses * 21)))
    cidx = (1.0 + cash_diario.reindex(idx.index).fillna(0.0)).cumprod()
    mom_i = _momentum_diario(idx, lb, sk)
    mom_c = _momentum_diario(cidx, lb, sk)
    valido = mom_i.notna() & mom_c.notna()
    risk_on = (mom_i > mom_c).where(valido, True)  # antes de tener señal → invertido (warmup)
    return pd.DataFrame({"mom_indice": mom_i, "mom_tbill": mom_c, "risk_on": risk_on.astype(bool)})


def estrategia_combinada_diaria(
    precio_diario: pd.Series, spread_diario: pd.Series | None = None, index_diario: pd.Series | None = None,
    fuente_entrada: str = "max52", fuente_salida: str = "spread",
    ventana_meses: float = 9.0, x_entrada: float = 8.0, x_salida: float = 12.0,
    umbral_salida: float = 300.0, confirm_meses: float = 2.0,
    lookback_meses: float = 9.0, skip_meses: float = 1.0,
    costo: float = 0.001, cash_diario: pd.Series | None = None, frecuencia: str = "ME",
    ventana_min: int = 252, ventana_dir: int = 63, apertura_diaria: pd.Series | None = None,
) -> dict:
    """
    Máquina de estado diaria long/flat sobre `precio_diario`, donde la ENTRADA (compra) y la SALIDA
    (venta) provienen, de forma INDEPENDIENTE, de una de dos señales:

      • "spread": crédito HY (`spread_diario`).
            salida  = pánico: (spread − mín 252d) > `umbral_salida` Y spread ensanchándose (Δ63d > +25).
            entrada = comprimió: spread hoy < spread de hace `confirm_meses` meses (~21 d/mes).
      • "max52": máximo de 52s del índice (`index_diario`, o el propio activo si None), BANDA ASIMÉTRICA:
            salida  = el precio cae > `x_salida`% por debajo de su máximo rodante de `ventana_meses`.
            entrada = el precio vuelve a estar ≤ `x_entrada`% del máximo.

    El objetivo es medir si una señal sirve mejor para ENTRAR o para SALIR, y combinarlas.
    Ejecución D / W-FRI / ME (cadencia). Sin look-ahead (shift 1). Devuelve el mismo dict que
    `estrategia_credito_diaria` (+ ppa=252).
    """
    px = precio_diario.dropna()
    sig52 = (index_diario if index_diario is not None else precio_diario).dropna()
    usa_spread = (fuente_entrada == "spread") or (fuente_salida == "spread")
    usa_mom = (fuente_entrada == "momentum") or (fuente_salida == "momentum")
    idx = px.index.intersection(sig52.index)
    if usa_spread:
        sp = (spread_diario if spread_diario is not None else pd.Series(dtype=float)).dropna()
        idx = idx.intersection(sp.index)
    vacio = {"ret_estrategia": pd.Series(dtype=float), "ret_activo": pd.Series(dtype=float),
             "posicion": pd.Series(dtype=bool), "n_operaciones": 0, "pct_invertido": 0.0, "ppa": 252}
    if len(idx) < ventana_min:
        return vacio
    px = px.reindex(idx)
    ret_px = px.pct_change()
    if cash_diario is None:
        from src.investment import backtest_v2 as btv2
        cash_diario = btv2.cash_diario(idx)

    # --- La señal se evalúa sobre el CIERRE del período de ejecución ---
    # D → decisión diaria; W/ME → decisión con el cierre de la SEMANA/MES (no un estado intra-período).
    diario = frecuencia in ("D", None)
    if diario:
        S, Sidx = sig52.reindex(idx), idx
        w52 = max(2, int(round(ventana_meses * 21)))
        lb, sk = max(2, int(round(lookback_meses * 21))), max(0, int(round(skip_meses * 21)))
        wmin, wdir = ventana_min, ventana_dir
        comp = max(1, int(round(confirm_meses * 21)))
        Ssp = sp.reindex(idx) if usa_spread else None
        cidx = (1.0 + cash_diario.reindex(idx).fillna(0.0)).cumprod() if usa_mom else None
    else:
        _per = 52 if str(frecuencia).startswith("W") else 12
        S = sig52.resample(frecuencia).last().dropna()
        Sidx = S.index
        w52 = max(2, int(round(ventana_meses * _per / 12.0)))
        lb, sk = max(2, int(round(lookback_meses * _per / 12.0))), max(0, int(round(skip_meses * _per / 12.0)))
        wmin, wdir = max(4, _per), max(1, int(round(_per / 4.0)))
        comp = max(1, int(round(confirm_meses * _per / 12.0)))
        Ssp = sp.resample(frecuencia).last().dropna() if usa_spread else None
        cidx = (1.0 + cash_diario.reindex(idx).fillna(0.0)).cumprod().reindex(Sidx, method="ffill") if usa_mom else None

    # Señal de máximo de 52s (banda asimétrica) sobre el CIERRE del período
    distpct = ((1.0 - S / S.rolling(w52, min_periods=max(2, w52 // 2)).max()) * 100.0).values
    exit_52 = distpct > x_salida
    entry_52 = distpct <= x_entrada

    # Señal de crédito (si alguna la usa)
    if usa_spread:
        minimo = Ssp.rolling(wmin, min_periods=wmin // 2).min()
        dist_sp = (Ssp - minimo).values
        cambio = (Ssp - Ssp.shift(wdir)).values
        baja = (Ssp < Ssp.shift(comp)).values
        exit_sp = np.isfinite(dist_sp) & np.isfinite(cambio) & (dist_sp > umbral_salida) & (cambio > 25)
        entry_sp = baja
    else:
        exit_sp = entry_sp = None

    # Señal de momentum 12-1 vs T-bills (si alguna la usa) — sobre los cierres del período
    if usa_mom:
        mom_i = (S.shift(sk) / S.shift(lb) - 1.0).values
        mom_c = (cidx.shift(sk) / cidx.shift(lb) - 1.0).values
        _val = np.isfinite(mom_i) & np.isfinite(mom_c)
        risk_on = np.where(_val, mom_i > mom_c, True)
        entry_mom, exit_mom = risk_on, ~risk_on
    else:
        exit_mom = entry_mom = None

    _exit = {"spread": exit_sp, "momentum": exit_mom}.get(fuente_salida, exit_52)
    _entry = {"spread": entry_sp, "momentum": entry_mom}.get(fuente_entrada, entry_52)
    exit_sig, entry_sig = _exit, _entry

    invertido = True
    pos = np.empty(len(Sidx), dtype=bool)
    for i in range(len(Sidx)):
        if invertido:
            if bool(exit_sig[i]):
                invertido = False
        elif bool(entry_sig[i]):
            invertido = True
        pos[i] = invertido
    pos = pd.Series(pos, index=Sidx)

    # La decisión (cierre del período) se OPERA en la APERTURA de la barra siguiente ("next bar open").
    pos_exec = pos.reindex(idx, method="ffill").fillna(True).astype(bool)
    pos_aplicada = pos_exec.shift(1, fill_value=True)   # posición diurna: decisión previa, ejecutada en la apertura de hoy
    cash_al = cash_diario.reindex(idx).fillna(0.0)
    if apertura_diaria is not None:
        # Ejecución en la apertura: el gap overnight (cierre→apertura) lo tiene la posición PREVIA;
        # la sesión (apertura→cierre) la tiene la posición NUEVA (ya ejecutada en la apertura).
        # Donde NO hay apertura (p.ej. tramo SIMULADO de un apalancado antes de su listado), se cae
        # a ejecución close-to-close para no perder ese período de historia.
        op = apertura_diaria.reindex(idx)
        has_open = (op.notna() & px.shift(1).notna()).to_numpy()
        overnight = np.nan_to_num((op / px.shift(1) - 1.0).to_numpy())   # cierre_{t-1} → apertura_t
        intraday = np.nan_to_num((px / op - 1.0).to_numpy())            # apertura_t → cierre_t
        inv_in = pos_exec.shift(1, fill_value=True).to_numpy()          # posición durante la sesión de hoy
        inv_on = pos_exec.shift(2, fill_value=True).to_numpy()          # posición durante el gap overnight de hoy
        f_on = np.where(inv_on, 1.0 + overnight, 1.0)
        f_in = np.where(inv_in, 1.0 + intraday, 1.0 + cash_al.to_numpy())
        r_open = f_on * f_in - 1.0
        r_close = ret_px.where(pos_aplicada, cash_al).fillna(0.0).to_numpy()
        ret_estrategia = pd.Series(np.where(has_open, r_open, r_close), index=idx)
        cambios = pd.Series(inv_in.astype(int), index=idx).diff().abs().fillna(0)
    else:
        ret_estrategia = ret_px.where(pos_aplicada, cash_al).fillna(0.0)
        cambios = pos_aplicada.astype(int).diff().abs().fillna(0)
    ret_estrategia = ret_estrategia - cambios * costo

    return {
        "ret_estrategia": ret_estrategia.dropna(),
        "ret_activo": ret_px.reindex(ret_estrategia.dropna().index),
        "posicion": pos_aplicada,
        "n_operaciones": int(cambios.sum()),
        "pct_invertido": float(pos_aplicada.mean()),
        "ppa": 252,
    }


_FREQ_MAP_LBL = {"Diaria": "D", "Semanal": "W-FRI", "Mensual": "ME"}


def _pos_banda_cadencia(sig52: pd.Series, idx: pd.DatetimeIndex, ventana_meses: float,
                        x_entrada: float, x_salida: float, frecuencia: str) -> pd.Series:
    """
    Posición APLICADA (long/flat, shift 1 día) de la banda de 52s evaluada sobre el CIERRE del período
    de ejecución (D=diario, W/ME=cierre de semana/mes). La ventana se convierte a nº de períodos.
    """
    if frecuencia in ("D", None):
        S = sig52.dropna(); w = max(2, int(round(ventana_meses * 21)))
    else:
        _per = 52 if str(frecuencia).startswith("W") else 12
        S = sig52.resample(frecuencia).last().dropna(); w = max(2, int(round(ventana_meses * _per / 12.0)))
    dist = ((1.0 - S / S.rolling(w, min_periods=max(2, w // 2)).max()) * 100.0).values
    desired = np.where(dist <= x_entrada, 1.0, np.where(dist > x_salida, 0.0, np.nan))
    pos = pd.Series(desired, index=S.index).ffill().fillna(1.0) > 0.5
    pos = pos.reindex(idx, method="ffill").fillna(True).astype(bool)
    return pos.shift(1, fill_value=True)


def _pos_momentum(index_diario: pd.Series, cash_diario: pd.Series, lookback_meses: float,
                  skip_meses: float, frecuencia: str) -> pd.Series:
    """Posición APLICADA (shift 1 día) del momentum 12-1 vs T-bills, evaluado sobre el cierre del período."""
    idxd = index_diario.dropna()
    cidx_d = (1.0 + cash_diario.reindex(idxd.index).fillna(0.0)).cumprod()
    if frecuencia in ("D", None):
        S, C = idxd, cidx_d
        lb, sk = max(2, int(round(lookback_meses * 21))), max(0, int(round(skip_meses * 21)))
    else:
        _per = 52 if str(frecuencia).startswith("W") else 12
        S = idxd.resample(frecuencia).last().dropna()
        C = cidx_d.reindex(S.index, method="ffill")
        lb, sk = max(2, int(round(lookback_meses * _per / 12.0))), max(0, int(round(skip_meses * _per / 12.0)))
    mi = (S.shift(sk) / S.shift(lb) - 1.0)
    mc = (C.shift(sk) / C.shift(lb) - 1.0)
    val = mi.notna() & mc.notna()
    ron = (mi > mc).where(val, True)
    pos = ron.reindex(idxd.index, method="ffill").fillna(True).astype(bool)
    return pos.shift(1, fill_value=True)


def estudio_robustez_momentum(
    index_diario: pd.Series, freqs: list[str], lookbacks: list[float], skips: list[float],
    cash_diario: pd.Series | None = None, costo: float = 0.001,
) -> pd.DataFrame:
    """
    Barrido del momentum 12-1 vs T-bills: ejecución (D/W/M) × lookback (meses) × skip (meses).
    Solo skip < lookback. Vectorizado. Tabla 'tidy' con métricas anualizadas (252).
    """
    from src.investment import backtest_v2 as btv2

    idx0 = index_diario.dropna()
    if len(idx0) < 252:
        return pd.DataFrame()
    if cash_diario is None:
        cash_diario = btv2.cash_diario(idx0.index)
    ret_px = idx0.pct_change()
    cash_al = cash_diario.reindex(idx0.index).fillna(0.0)
    rows = []
    for flbl in freqs:
        f = _FREQ_MAP_LBL.get(flbl, flbl)
        for lb in lookbacks:
            for sk in skips:
                if sk >= lb:
                    continue
                pos_ap = _pos_momentum(idx0, cash_diario, lb, sk, f)
                ret = ret_px.where(pos_ap, cash_al).fillna(0.0)
                cambios = pos_ap.astype(int).diff().abs().fillna(0)
                r = (ret - cambios * costo).dropna()
                if r.empty:
                    continue
                ca = cash_al.reindex(r.index)
                rows.append({"Ejecución": flbl, "Lookback (m)": int(lb), "Skip (m)": int(sk),
                             "CAGR (%)": btv2.cagr(r, 252) * 100.0, "Sharpe": btv2.sharpe(r, ca, 252),
                             "Calmar": btv2.calmar(r, 252), "Max DD (%)": btv2.max_drawdown(r) * 100.0,
                             "% invertido": float(pos_ap.mean()) * 100.0, "Ops": int(cambios.sum())})
    return pd.DataFrame(rows)


def diagnostico_combo_momentum(
    index_diario: pd.Series, lookback_meses: float, skip_meses: float, frecuencia: str,
    cash_diario: pd.Series | None = None, costo: float = 0.001,
) -> dict:
    """Diagnóstico profundo del momentum 12-1: subperíodos, significancia (t-stat) y costo."""
    import math
    from src.investment import backtest_v2 as btv2

    idx0 = index_diario.dropna()
    if cash_diario is None:
        cash_diario = btv2.cash_diario(idx0.index)
    ret_px = idx0.pct_change()
    cash_al = cash_diario.reindex(idx0.index).fillna(0.0)

    def _run(c):
        pos = _pos_momentum(idx0, cash_diario, lookback_meses, skip_meses, frecuencia)
        ret = ret_px.where(pos, cash_al).fillna(0.0)
        cam = pos.astype(int).diff().abs().fillna(0)
        return (ret - cam * c).dropna()

    r = _run(costo)
    if r.empty or len(r) < 252:
        return {}
    bh = ret_px.reindex(r.index).fillna(0.0)
    cash = cash_al.reindex(r.index)

    mid = r.index[len(r) // 2]
    tramos = {"Todo": r.index, f"1ª mitad (→{mid:%Y})": r.index[r.index < mid],
              f"2ª mitad ({mid:%Y}→)": r.index[r.index >= mid], "Holdout 2015+": r.index[r.index >= pd.Timestamp("2015-01-01")]}
    filas = [{"Período": n, **_sub_metricas(r.reindex(s), bh.reindex(s), cash)} for n, s in tramos.items() if len(s) >= 60]
    df_sub = pd.DataFrame(filas)

    exc = (r - cash).dropna()
    n = len(exc); mu, sd = float(exc.mean()), float(exc.std(ddof=1))
    t_stat = mu / (sd / math.sqrt(n)) if sd > 0 and n > 2 else 0.0
    signif = {"sharpe_ann": (mu / sd * math.sqrt(252)) if sd > 0 else 0.0, "t_stat": t_stat,
              "p_val": math.erfc(abs(t_stat) / math.sqrt(2.0)), "n_dias": n, "años": n / 252.0}

    filas_c = []
    for c in [0.0, 0.0005, 0.001, 0.002, 0.005]:
        rc = _run(c); ca = cash.reindex(rc.index)
        filas_c.append({"Costo por op (bps)": c * 1e4, "CAGR (%)": btv2.cagr(rc, 252) * 100.0,
                        "Sharpe": btv2.sharpe(rc, ca, 252), "Calmar": btv2.calmar(rc, 252)})
    return {"subperiodos": df_sub, "significancia": signif, "costos": pd.DataFrame(filas_c),
            "sharpe_bh": btv2.sharpe(bh, cash, 252), "calmar_bh": btv2.calmar(bh, 252)}


def senal_banda_52_mensual(
    index_diario: pd.Series, ventana_meses: float, x_entrada: float, x_salida: float, frecuencia: str = "ME",
) -> pd.Series:
    """
    Régimen MENSUAL (booleano, risk-on) de la banda asimétrica del máximo de 52s sobre el índice, para
    usarlo como filtro de la selección de acciones. Estado a fin de mes (con la cadencia elegida), sin
    el shift de ejecución (de eso se encarga `filtrar_estrategia`). True = índice risk-on.
    """
    idx_s = index_diario.dropna()
    if idx_s.empty:
        return pd.Series(dtype=bool)
    if frecuencia in ("D", None):
        S = idx_s; w = max(2, int(round(ventana_meses * 21)))
    else:
        _per = 52 if str(frecuencia).startswith("W") else 12
        S = idx_s.resample(frecuencia).last().dropna(); w = max(2, int(round(ventana_meses * _per / 12.0)))
    dist = ((1.0 - S / S.rolling(w, min_periods=max(2, w // 2)).max()) * 100.0).values
    desired = np.where(dist <= x_entrada, 1.0, np.where(dist > x_salida, 0.0, np.nan))
    pos = pd.Series(desired, index=S.index).ffill().fillna(1.0) > 0.5
    return pos.resample("ME").last().dropna().astype(bool)


def estudio_robustez_52(
    precio_diario: pd.Series, freqs: list[str], ventanas_m: list[float],
    xs_entrada: list[float], xs_salida: list[float], index_diario: pd.Series | None = None,
    cash_diario: pd.Series | None = None, costo: float = 0.001,
) -> pd.DataFrame:
    """
    Barrido 4D COMPLETO de la banda de 52s: ejecución (D/W/M) × ventana (meses) × X entrada × X salida.
    Solo combinaciones con x_entrada ≤ x_salida (banda con histéresis válida). Vectorizado → rápido.
    Devuelve una tabla 'tidy' con una fila por combinación y sus métricas anualizadas (252).
    """
    from src.investment import backtest_v2 as btv2

    px = precio_diario.dropna()
    sig = (index_diario if index_diario is not None else precio_diario).dropna()
    idx = px.index.intersection(sig.index)
    if len(idx) < 252:
        return pd.DataFrame()
    px = px.reindex(idx)
    ret_px = px.pct_change()
    if cash_diario is None:
        cash_diario = btv2.cash_diario(idx)
    cash_al = cash_diario.reindex(idx).fillna(0.0)

    filas = []
    for flbl in freqs:
        f = _FREQ_MAP_LBL.get(flbl, flbl)
        for vm in ventanas_m:
            for xe in xs_entrada:
                for xs in xs_salida:
                    if xe > xs:
                        continue
                    pos_ap = _pos_banda_cadencia(sig, idx, float(vm), float(xe), float(xs), f)
                    ret = ret_px.where(pos_ap, cash_al).fillna(0.0)
                    cambios = pos_ap.astype(int).diff().abs().fillna(0)
                    r = (ret - cambios * costo).dropna()
                    if r.empty:
                        continue
                    ca = cash_al.reindex(r.index)
                    filas.append({
                        "Ejecución": flbl, "Ventana (m)": int(vm), "X entrada (%)": float(xe), "X salida (%)": float(xs),
                        "CAGR (%)": btv2.cagr(r, 252) * 100.0, "Sharpe": btv2.sharpe(r, ca, 252),
                        "Calmar": btv2.calmar(r, 252), "Max DD (%)": btv2.max_drawdown(r) * 100.0,
                        "% invertido": float(pos_ap.mean()) * 100.0, "Ops": int(cambios.sum()),
                    })
    return pd.DataFrame(filas)


def _sub_metricas(r: pd.Series, bh: pd.Series, cash: pd.Series) -> dict:
    from src.investment import backtest_v2 as btv2
    r = r.dropna(); bh = bh.reindex(r.index).dropna()
    ca = cash.reindex(r.index)
    return {
        "Años": round(len(r) / 252.0, 1),
        "CAGR estr. (%)": btv2.cagr(r, 252) * 100.0, "CAGR B&H (%)": btv2.cagr(bh, 252) * 100.0,
        "Sharpe estr.": btv2.sharpe(r, ca, 252), "Sharpe B&H": btv2.sharpe(bh, ca.reindex(bh.index), 252),
        "Max DD estr. (%)": btv2.max_drawdown(r) * 100.0, "Max DD B&H (%)": btv2.max_drawdown(bh) * 100.0,
    }


def diagnostico_combo_52(
    precio_diario: pd.Series, ventana_meses: float, x_entrada: float, x_salida: float, frecuencia: str,
    index_diario: pd.Series | None = None, cash_diario: pd.Series | None = None, costo: float = 0.001,
) -> dict:
    """
    Diagnóstico profundo de UNA combinación de la banda 52s: subperíodos (mitades + holdout reciente),
    significancia estadística (t-stat del retorno excedente diario) y sensibilidad al costo.
    """
    import math
    from src.investment import backtest_v2 as btv2

    res = estrategia_combinada_diaria(
        precio_diario, spread_diario=None, index_diario=index_diario, fuente_entrada="max52",
        fuente_salida="max52", ventana_meses=ventana_meses, x_entrada=x_entrada, x_salida=x_salida,
        costo=costo, cash_diario=cash_diario, frecuencia=frecuencia)
    r = res["ret_estrategia"].dropna()
    if r.empty or len(r) < 252:
        return {}
    bh = res["ret_activo"].reindex(r.index).fillna(0.0)
    if cash_diario is None:
        cash_diario = btv2.cash_diario(r.index)
    cash = cash_diario.reindex(r.index).fillna(0.0)

    # --- Subperíodos: full, 1ª mitad, 2ª mitad, holdout 2015+ ---
    mid = r.index[len(r) // 2]
    tramos = {
        "Todo": r.index,
        f"1ª mitad (→{mid:%Y})": r.index[r.index < mid],
        f"2ª mitad ({mid:%Y}→)": r.index[r.index >= mid],
        "Holdout 2015+": r.index[r.index >= pd.Timestamp("2015-01-01")],
    }
    filas = []
    for nombre, sub in tramos.items():
        if len(sub) < 60:
            continue
        m = _sub_metricas(r.reindex(sub), bh.reindex(sub), cash)
        m = {"Período": nombre, **m}
        filas.append(m)
    df_sub = pd.DataFrame(filas)

    # --- Significancia: t-stat del retorno excedente diario (aprox., sin corrección de autocorrelación) ---
    exc = (r - cash).dropna()
    n = len(exc)
    mu, sd = float(exc.mean()), float(exc.std(ddof=1))
    t_stat = mu / (sd / math.sqrt(n)) if sd > 0 and n > 2 else 0.0
    p_val = math.erfc(abs(t_stat) / math.sqrt(2.0))  # dos colas, aprox normal
    sharpe_ann = (mu / sd * math.sqrt(252)) if sd > 0 else 0.0
    signif = {"sharpe_ann": sharpe_ann, "t_stat": t_stat, "p_val": p_val, "n_dias": n, "años": n / 252.0}

    # --- Sensibilidad al costo por operación ---
    filas_c = []
    for c in [0.0, 0.0005, 0.001, 0.002, 0.005]:
        rc = estrategia_combinada_diaria(
            precio_diario, spread_diario=None, index_diario=index_diario, fuente_entrada="max52",
            fuente_salida="max52", ventana_meses=ventana_meses, x_entrada=x_entrada, x_salida=x_salida,
            costo=c, cash_diario=cash_diario, frecuencia=frecuencia)["ret_estrategia"].dropna()
        ca = cash.reindex(rc.index)
        filas_c.append({"Costo por op (bps)": c * 1e4, "CAGR (%)": btv2.cagr(rc, 252) * 100.0,
                        "Sharpe": btv2.sharpe(rc, ca, 252), "Calmar": btv2.calmar(rc, 252)})
    df_cost = pd.DataFrame(filas_c)

    return {"subperiodos": df_sub, "significancia": signif, "costos": df_cost,
            "sharpe_bh": btv2.sharpe(bh, cash, 252), "calmar_bh": btv2.calmar(bh, 252)}


def barrido_banda_52(
    precio_diario: pd.Series, xs_entrada: list[float], xs_salida: list[float],
    ventana_meses: float, frecuencia: str = "ME", cash_diario: pd.Series | None = None,
    index_diario: pd.Series | None = None, metrica: str = "Sharpe", costo: float = 0.001,
) -> pd.DataFrame:
    """
    Barrido 2D (con TODA la data) de la BANDA ASIMÉTRICA del máximo de 52s: X% de entrada × X% de salida,
    ambas fuentes = máximo de 52s (sin usar el spread). Devuelve una matriz (índice=X entrada, columnas=X
    salida) con la métrica pedida ("Sharpe" | "Calmar" | "CAGR (%)" | "Max DD (%)" | "% invertido").
    """
    from src.investment import backtest_v2 as btv2

    grid = {}
    for xe in xs_entrada:
        fila = {}
        for xs in xs_salida:
            res = estrategia_combinada_diaria(
                precio_diario, spread_diario=None, index_diario=index_diario,
                fuente_entrada="max52", fuente_salida="max52", ventana_meses=ventana_meses,
                x_entrada=float(xe), x_salida=float(xs), costo=costo, cash_diario=cash_diario, frecuencia=frecuencia)
            r = res["ret_estrategia"].dropna()
            if r.empty:
                fila[xs] = np.nan
                continue
            cash_al = cash_diario.reindex(r.index) if cash_diario is not None else pd.Series(0.0, index=r.index)
            if metrica == "Sharpe":
                fila[xs] = btv2.sharpe(r, cash_al, 252)
            elif metrica == "Calmar":
                fila[xs] = btv2.calmar(r, 252)
            elif metrica == "CAGR (%)":
                fila[xs] = btv2.cagr(r, 252) * 100.0
            elif metrica == "Max DD (%)":
                fila[xs] = btv2.max_drawdown(r) * 100.0
            else:
                fila[xs] = res["pct_invertido"] * 100.0
        grid[xe] = fila
    df = pd.DataFrame(grid).T
    df.index.name = "X entrada (%)"
    df.columns.name = "X salida (%)"
    return df


def comparar_frecuencias_diaria(
    precio_diario: pd.Series, spread_diario: pd.Series, umbral_salida: float, confirm_meses: float,
    cash_diario: pd.Series | None = None, costo: float = 0.001,
) -> pd.DataFrame:
    """Corre la señal diaria con ejecución D / W-FRI / ME y tabula CAGR/Sharpe/Calmar/MaxDD/%inv/Ops."""
    from src.investment import backtest_v2 as btv2

    filas = []
    for freq, lbl in [("D", "Diaria"), ("W-FRI", "Semanal"), ("ME", "Mensual")]:
        res = estrategia_credito_diaria(precio_diario, spread_diario, umbral_salida=umbral_salida,
                                        confirm_meses=confirm_meses, costo=costo,
                                        cash_diario=cash_diario, frecuencia=freq)
        r = res["ret_estrategia"].dropna()
        if r.empty:
            continue
        cash_al = cash_diario.reindex(r.index) if cash_diario is not None else pd.Series(0.0, index=r.index)
        filas.append({
            "Ejecución": lbl, "CAGR (%)": btv2.cagr(r, 252) * 100.0, "Sharpe": btv2.sharpe(r, cash_al, 252),
            "Calmar": btv2.calmar(r, 252), "Max DD (%)": btv2.max_drawdown(r) * 100.0,
            "% invertido": res["pct_invertido"] * 100.0, "Ops": res["n_operaciones"],
        })
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# ANÁLISIS 3b — BARRIDO 2D DE LA ESTRATEGIA (umbral de salida × meses de reentrada)
# ---------------------------------------------------------------------------
def barrido_estrategia(
    precio_m: pd.Series, spread_m: pd.Series, dist_min_m: pd.Series, cambio3m_m: pd.Series,
    umbrales: list[float], confirms: list[int], cash_m: pd.Series | None = None, costo: float = 0.001,
    lag_ejecucion: int = 1,
) -> pd.DataFrame:
    """
    Corre `estrategia_credito` para cada combinación (umbral de salida, meses comprimiendo para
    reentrar) y devuelve una tabla 'tidy' con CAGR/Sharpe/Calmar/Max DD/% invertido/nº ops por celda.
    Sirve para ver dónde hay un bloque robusto de parámetros (y no un pico aislado de overfitting).
    """
    from src.investment import backtest_v2 as btv2

    filas = []
    for u in umbrales:
        for c in confirms:
            res = estrategia_credito(precio_m, spread_m, dist_min_m, cambio3m_m,
                                     umbral_salida=float(u), confirm_reentrada=int(c),
                                     costo=costo, cash_m=cash_m, lag_ejecucion=lag_ejecucion)
            r = res["ret_estrategia"].dropna()
            if r.empty:
                continue
            cash_al = cash_m.reindex(r.index) if cash_m is not None else pd.Series(0.0, index=r.index)
            filas.append({
                "Umbral (pbs)": int(u), "Meses reentrada": int(c),
                "CAGR (%)": btv2.cagr(r, 12) * 100.0, "Sharpe": btv2.sharpe(r, cash_al, 12),
                "Calmar": btv2.calmar(r, 12), "Max DD (%)": btv2.max_drawdown(r) * 100.0,
                "% invertido": res["pct_invertido"] * 100.0, "Ops": res["n_operaciones"],
            })
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# ANÁLISIS 4 — SOLAPAMIENTO CON OTRAS SEÑALES
# ---------------------------------------------------------------------------
def solapamiento(pos_credito: pd.Series, otras: dict[str, pd.Series]) -> pd.DataFrame:
    """
    Correlación y coincidencia mensual entre la posición 'dentro/fuera' del crédito y otras
    señales binarias (dentro=1/fuera=0). Devuelve una fila por señal con correlación y % de
    meses en que ambas coinciden.
    """
    a = pos_credito.astype(float)
    filas = []
    for nombre, s in otras.items():
        b = s.reindex(a.index).astype(float)
        pareja = pd.concat([a, b], axis=1).dropna()
        if len(pareja) < 12:
            filas.append({"Señal": nombre, "Correlación": np.nan, "% coincidencia": np.nan, "n meses": len(pareja)})
            continue
        corr = float(pareja.iloc[:, 0].corr(pareja.iloc[:, 1]))
        coinc = float((pareja.iloc[:, 0] == pareja.iloc[:, 1]).mean()) * 100.0
        filas.append({"Señal": nombre, "Correlación": corr, "% coincidencia": coinc, "n meses": int(len(pareja))})
    return pd.DataFrame(filas)
