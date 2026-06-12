"""Minimal read-only Alpaca Trading API client using per-client API keys.

Each investment client's API key pair (key id + secret) is loaded by the
administrator in the Administración panel, encrypted at rest with AES-256-GCM
(web-app/crypto.py), and decrypted only here at request time.

SECURITY CONTRACT — READ THIS BEFORE ADDING ANY FUNCTION:
Alpaca API keys are FULL-PERMISSION credentials: unlike the old OAuth tokens
(empty scope, provably unable to trade), these keys CAN place orders. The
app's read-only promise to clients is now enforced purely by code discipline:
this module must only ever issue GET requests. Never add a POST/PUT/PATCH/
DELETE call against the Alpaca API, and never let a key leave this process
(no logging, no error messages containing credentials, no returning them
through any endpoint).

Environment:
    ALPACA_API_BASE   (default: https://api.alpaca.markets; paper:
                       https://paper-api.alpaca.markets). Global for all
                       clients — paper keys only work against the paper base
                       and live keys against the live base.
"""

import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

# Short timeout: with 2 sync gunicorn workers, a slow Alpaca must fail fast
# rather than hold a worker (and with it the whole web app) for 15s per call.
_TIMEOUT = 5


def _api_base() -> str:
    return os.environ.get("ALPACA_API_BASE", "https://api.alpaca.markets").rstrip("/")


# ── Read-only data ────────────────────────────────────────────────────────────

def _get(key_id: str, secret: str, path: str, params: dict | None = None) -> dict | list:
    resp = requests.get(
        f"{_api_base()}{path}",
        headers={
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
        },
        params=params,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_account(key_id: str, secret: str) -> dict:
    return _get(key_id, secret, "/v2/account")


def get_positions(key_id: str, secret: str) -> list:
    return _get(key_id, secret, "/v2/positions")


def get_portfolio_history(key_id: str, secret: str, period: str, timeframe: str = "1D") -> dict:
    return _get(
        key_id, secret,
        "/v2/account/portfolio/history",
        {"period": period, "timeframe": timeframe},
    )


def get_portfolio_summary(key_id: str, secret: str) -> dict:
    """Aggregate everything the Inversión tab needs: current value, positions,
    and percentage change over the last week, month and year."""
    account = get_account(key_id, secret)
    positions = get_positions(key_id, secret)

    changes = {}
    for label, period in (("week", "1W"), ("month", "1M"), ("year", "1A")):
        try:
            hist = get_portfolio_history(key_id, secret, period)
            pct = [p for p in (hist.get("profit_loss_pct") or []) if p is not None]
            # Alpaca returns a fraction (0.023 == 2.3%); last value is cumulative.
            changes[label] = round(pct[-1] * 100, 2) if pct else None
        except Exception:  # noqa: BLE001 - a missing period shouldn't break the page
            changes[label] = None

    return {
        "equity": float(account.get("equity") or 0),
        "currency": account.get("currency") or "USD",
        "positions": [
            {
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "market_value": float(p.get("market_value") or 0),
                "unrealized_plpc": round(float(p.get("unrealized_plpc") or 0) * 100, 2),
            }
            for p in (positions or [])
        ],
        "changes": changes,
    }


# ── Per-client cache ──────────────────────────────────────────────────────────
#
# get_portfolio_summary makes 5 serial Alpaca calls. The cache keeps page
# refreshes from re-hitting Alpaca and, more importantly, keeps a slow Alpaca
# from tying up a gunicorn worker on every view. Per-process (each worker has
# its own copy), bounded by the number of configured investment clients.

_CACHE_TTL_SECONDS = int(os.environ.get("ALPACA_CACHE_TTL_SECONDS", "180"))
_cache_lock = threading.Lock()
_portfolio_cache: dict[str, tuple[float, dict]] = {}


def get_portfolio_summary_cached(key_id: str, secret: str, cache_key: str) -> dict:
    """get_portfolio_summary with a short TTL cache, keyed by client id."""
    now = time.monotonic()
    with _cache_lock:
        hit = _portfolio_cache.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    summary = get_portfolio_summary(key_id, secret)
    with _cache_lock:
        _portfolio_cache[cache_key] = (time.monotonic(), summary)
    return summary


def invalidate_portfolio_cache(cache_key: str) -> None:
    """Drop a client's cached summary (call when credentials change)."""
    with _cache_lock:
        _portfolio_cache.pop(cache_key, None)
