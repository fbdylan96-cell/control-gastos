"""Minimal Alpaca OAuth + read-only Trading API client.

Scope is intentionally read-only: the authorize URL does NOT request the
`trading` scope, so tokens issued through this flow can read account and
portfolio data but can never place, cancel, or modify orders.

All credentials come from the environment (see .env.example):
    ALPACA_CLIENT_ID, ALPACA_CLIENT_SECRET, ALPACA_REDIRECT_URI
    ALPACA_SCOPE       (default: 'account:write' — read access, no trading)
    ALPACA_API_BASE    (default: https://api.alpaca.markets)

NOTE: Alpaca's OAuth scopes are coarse. `account:write` is what grants access
to read account/positions/portfolio; `trading` (deliberately omitted here) is
what would allow order execution. Confirm the exact read-only scope string
against the current docs:
https://docs.alpaca.markets/docs/using-oauth2-and-trading-api
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
    # Read-only by default: no 'trading' scope -> token cannot execute orders.
    return os.environ.get("ALPACA_SCOPE", "account:write").strip()


def _api_base() -> str:
    return os.environ.get("ALPACA_API_BASE", "https://api.alpaca.markets").rstrip("/")


# ── OAuth ─────────────────────────────────────────────────────────────────────

def build_authorize_url(state: str) -> str:
    """URL to redirect the client to for Alpaca login + consent."""
    params = {
        "response_type": "code",
        "client_id": _require("ALPACA_CLIENT_ID"),
        "redirect_uri": _require("ALPACA_REDIRECT_URI"),
        "scope": _scope(),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> str:
    """Exchange an authorization code for an access token. Returns the token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _require("ALPACA_CLIENT_ID"),
            "client_secret": _require("ALPACA_CLIENT_SECRET"),
            "redirect_uri": _require("ALPACA_REDIRECT_URI"),
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
