"""Minimal Alpaca OAuth + read-only Trading API client.

Scope is intentionally read-only: by default NO scope is requested, which in
Alpaca's OAuth grants read access to account/positions/portfolio. We never
request `trading` (order execution) nor `account:write` (write access to
account configurations and watchlists) — see
https://docs.alpaca.markets/docs/using-oauth2-and-trading-api

All credentials come from the environment (see .env.example):
    ALPACA_CLIENT_ID, ALPACA_CLIENT_SECRET
    ALPACA_SCOPE       (default: '' — read-only; leave empty)
    ALPACA_API_BASE    (default: https://api.alpaca.markets)

Callback URLs are built per blueprint from WEBAPP_URL via `callback_url()`
(e.g. https://host/persona/inversion/callback), so persona and empresa each
return to their own /inversion/callback route. BOTH URLs must be registered
as redirect URIs in the Alpaca OAuth app.
"""

import os
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://app.alpaca.markets/oauth/authorize"
TOKEN_URL = "https://api.alpaca.markets/oauth/token"

_TIMEOUT = 15


class AlpacaConfigError(RuntimeError):
    """A required ALPACA_* environment variable is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AlpacaConfigError(
            f"{name} no está configurada. Registrá la app OAuth en Alpaca y "
            f"cargá las credenciales (ver .env.example)."
        )
    return value


def _scope() -> str:
    # Read-only by default: an EMPTY scope grants read access in Alpaca's OAuth.
    # 'trading' would allow order execution; 'account:write' would allow writing
    # account configurations/watchlists — neither is read-only, so neither is
    # requested unless deliberately set via ALPACA_SCOPE.
    return os.environ.get("ALPACA_SCOPE", "").strip()


def _api_base() -> str:
    return os.environ.get("ALPACA_API_BASE", "https://api.alpaca.markets").rstrip("/")


# ── OAuth ─────────────────────────────────────────────────────────────────────

def callback_url(blueprint: str) -> str:
    """Exact redirect URI for a blueprint ('persona' or 'empresa').

    Built from WEBAPP_URL (the public HTTPS base) instead of request headers,
    so it always matches the URLs registered in Alpaca even behind a proxy.
    The same value must be passed to build_authorize_url() and exchange_code()
    — OAuth requires the token exchange to repeat the authorize redirect_uri.
    """
    base = _require("WEBAPP_URL").rstrip("/")
    return f"{base}/{blueprint}/inversion/callback"


def build_authorize_url(state: str, redirect_uri: str) -> str:
    """URL to redirect the client to for Alpaca login + consent."""
    params = {
        "response_type": "code",
        "client_id": _require("ALPACA_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    scope = _scope()
    if scope:  # omit the parameter entirely for the read-only default
        params["scope"] = scope
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> str:
    """Exchange an authorization code for an access token. Returns the token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _require("ALPACA_CLIENT_ID"),
            "client_secret": _require("ALPACA_CLIENT_SECRET"),
            "redirect_uri": redirect_uri,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("access_token")
    if not token:
        raise RuntimeError("Alpaca no devolvió un access_token.")
    return token


# ── Read-only data ────────────────────────────────────────────────────────────

def _get(token: str, path: str, params: dict | None = None) -> dict | list:
    resp = requests.get(
        f"{_api_base()}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_account(token: str) -> dict:
    return _get(token, "/v2/account")


def get_positions(token: str) -> list:
    return _get(token, "/v2/positions")


def get_portfolio_history(token: str, period: str, timeframe: str = "1D") -> dict:
    return _get(
        token,
        "/v2/account/portfolio/history",
        {"period": period, "timeframe": timeframe},
    )


def get_portfolio_summary(token: str) -> dict:
    """Aggregate everything the Inversión tab needs: current value, positions,
    and percentage change over the last week, month and year."""
    account = get_account(token)
    positions = get_positions(token)

    changes = {}
    for label, period in (("week", "1W"), ("month", "1M"), ("year", "1A")):
        try:
            hist = get_portfolio_history(token, period)
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
