# -*- coding: utf-8 -*-
"""
Descarga de índices de volatilidad directamente de los CSV oficiales de CBOE.

Se usa en vez de Yahoo Finance para VIX/VIX3M porque el ticker ^VIX3M de Yahoo
viene incompleto (a veces trae una sola fila). Los CSV de CBOE son gratuitos, no
requieren API key, y traen la historia completa:
  • VIX   desde 1990-01-02
  • VIX3M desde 2009-09-18 (el CDN de CBOE no incluye la era temprana "VXV"
    de 2007-2009; por eso el análisis de estructura de plazos arranca en ~2009).

Los cierres del VIX de CBOE coinciden con ^VIX de Yahoo con diferencia < 0.01.

Cache en disco: `data_cache/CBOE_<SIMBOLO>.csv`, igual que el resto de la data de
la app. Se re-descarga solo si el cache está desactualizado (más de 3 días).

Fallback (si el CDN de CBOE fallara en el futuro): la serie `VXVCLS` de FRED
(vía pandas_datareader) cubre el VIX3M — verificar hasta qué fecha está
actualizada antes de usarla, porque a veces va con rezago.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.data.yahoo_client import DATA_CACHE_DIR

CBOE_SIMBOLOS = {"VIX", "VIX3M", "VIX9D", "VVIX"}


def _cache_path(simbolo: str) -> Path:
    return Path(DATA_CACHE_DIR) / f"CBOE_{simbolo}.csv"


def _leer_cache(simbolo: str) -> pd.Series:
    path = _cache_path(simbolo)
    if not path.exists():
        return pd.Series(dtype=float, name=simbolo)
    s = pd.read_csv(path, index_col=0, parse_dates=[0]).iloc[:, 0]
    s.name = simbolo
    return s


def load_cboe_index(simbolo: str, refresh: bool = False) -> pd.Series:
    """
    Serie diaria de cierre de un índice de volatilidad de CBOE (VIX, VIX3M, ...).
    Usa cache local; re-descarga si está viejo o si `refresh=True`. Filtra nulos,
    valores <= 0 y fechas duplicadas.
    """
    cache = None if refresh else _leer_cache(simbolo)
    if cache is not None and not cache.empty:
        if cache.index.max().date() >= date.today() - timedelta(days=3):
            return cache

    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{simbolo}_History.csv"
    try:
        df = pd.read_csv(url, parse_dates=["DATE"]).set_index("DATE").sort_index()
        s = df["CLOSE"].rename(simbolo)
        s = s[(s > 0) & s.notna()]
        s = s[~s.index.duplicated(keep="last")]
        _cache_path(simbolo).parent.mkdir(parents=True, exist_ok=True)
        s.to_csv(_cache_path(simbolo))
        return s
    except Exception:
        # Si falla la descarga pero hay cache (aunque sea viejo), se usa.
        if cache is not None and not cache.empty:
            return cache
        return pd.Series(dtype=float, name=simbolo)
