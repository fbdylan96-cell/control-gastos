# -*- coding: utf-8 -*-
"""
Descarga y almacenamiento local de precios de mercado desde Yahoo Finance
(vía `yfinance`). No requiere llaves/API keys.

Los precios se piden con `auto_adjust=True`, es decir, ya vienen
ajustados por splits Y por dividendos reinvertidos (retorno total), no
solo por splits.

Cache en disco: cada ticker se guarda como CSV en `data_cache/<TICKER>.csv`
con todo el historial disponible desde `EARLIEST_REQUEST_DATE`. En
llamadas posteriores solo se descarga (y se agrega) la "cola" de días
nuevos, en vez de re-descargar todo el historial cada vez.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

EARLIEST_REQUEST_DATE = date(1990, 1, 1)

DATA_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data_cache"


def _cache_path(ticker: str) -> Path:
    return DATA_CACHE_DIR / f"{ticker.upper()}.csv"


def _load_cache(ticker: str) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
    except (pd.errors.EmptyDataError, pd.errors.ParserError, ValueError):
        # Cache corrupto o vacío (p.ej. una descarga que falló a medias): se ignora
        # y se vuelve a descargar desde cero.
        return None
    return df if not df.empty else None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_cache_path(ticker), index_label="Date")


def _fetch_from_yahoo(ticker: str, start: date, end: date) -> pd.DataFrame:
    if start > end:
        return pd.DataFrame(columns=["Close"]).rename_axis("Date")

    raw = yf.download(
        ticker,
        start=start,
        end=end + timedelta(days=1),
        auto_adjust=True,
        progress=False,
    )
    if raw.empty:
        return pd.DataFrame(columns=["Close"]).rename_axis("Date")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    df = raw[["Close"]].copy()
    df.index.name = "Date"
    return df


def download_and_cache(ticker: str, refresh: bool = False) -> pd.DataFrame:
    """
    Devuelve el historial diario completo (columna 'Close') de `ticker`,
    usando el cache local si existe y está al día. Si `refresh=True`,
    fuerza una descarga completa nueva.
    """
    today = date.today()
    cached = None if refresh else _load_cache(ticker)

    if cached is not None and not cached.empty:
        last_date = cached.index.max().date()
        if last_date >= today - timedelta(days=1):
            return cached
        nuevo = _fetch_from_yahoo(ticker, start=last_date + timedelta(days=1), end=today)
        if not nuevo.empty:
            combinado = pd.concat([cached, nuevo])
            combinado = combinado[~combinado.index.duplicated(keep="last")].sort_index()
            _save_cache(ticker, combinado)
            return combinado
        return cached

    completo = _fetch_from_yahoo(ticker, start=EARLIEST_REQUEST_DATE, end=today)
    if not completo.empty:
        _save_cache(ticker, completo)
    return completo


def get_daily_closes(ticker: str, refresh: bool = False) -> pd.Series:
    df = download_and_cache(ticker, refresh=refresh)
    if df.empty:
        return pd.Series(dtype=float, name=ticker)
    return df["Close"].dropna().rename(ticker)


# ---------------------------------------------------------------------------
# OHLC completo (Open/High/Low/Close) — para estrategias que necesitan la apertura
# y el rango del día (edge overnight/intraday, floor pivots). Cache aparte para no
# pisar el cache de solo-cierre.
# ---------------------------------------------------------------------------
_COLS_OHLC = ["Open", "High", "Low", "Close"]


def _ohlc_cache_path(ticker: str) -> Path:
    return DATA_CACHE_DIR / f"{ticker.upper()}_OHLC.csv"


def _fetch_ohlc_from_yahoo(ticker: str, start: date, end: date) -> pd.DataFrame:
    if start > end:
        return pd.DataFrame(columns=_COLS_OHLC).rename_axis("Date")
    raw = yf.download(ticker, start=start, end=end + timedelta(days=1), auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame(columns=_COLS_OHLC).rename_axis("Date")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)
    cols = [c for c in _COLS_OHLC if c in raw.columns]
    df = raw[cols].copy()
    df.index.name = "Date"
    return df


def get_daily_ohlc(ticker: str, refresh: bool = False) -> pd.DataFrame:
    """
    Historial diario con Open/High/Low/Close (ajustado por dividendos y splits,
    igual que `get_daily_closes`), con caché incremental propio. Devuelve un
    DataFrame vacío si no hay datos.
    """
    today = date.today()
    path = _ohlc_cache_path(ticker)
    cached = None
    if not refresh and path.exists():
        try:
            cached = pd.read_csv(path, index_col="Date", parse_dates=["Date"])
        except (pd.errors.EmptyDataError, pd.errors.ParserError, ValueError):
            cached = None

    if cached is not None and not cached.empty:
        last_date = cached.index.max().date()
        if last_date >= today - timedelta(days=1):
            return cached
        nuevo = _fetch_ohlc_from_yahoo(ticker, start=last_date + timedelta(days=1), end=today)
        if not nuevo.empty:
            combinado = pd.concat([cached, nuevo])
            combinado = combinado[~combinado.index.duplicated(keep="last")].sort_index()
            DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            combinado.to_csv(path, index_label="Date")
            return combinado
        return cached

    completo = _fetch_ohlc_from_yahoo(ticker, start=EARLIEST_REQUEST_DATE, end=today)
    if not completo.empty:
        DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        completo.to_csv(path, index_label="Date")
    return completo


