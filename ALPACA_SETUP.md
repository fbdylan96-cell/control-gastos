# Alpaca Investments (OAuth, read-only) — Setup & Security Guide

End-to-end checklist for enabling the **Portafolio de Inversión** feature on the
server hosting `control-gastos`.

Everything here assumes:
- The Flask web app is running at `https://gastos.empoweredinvestor.trade`.
- The feature is **read-only**: clients connect their own Alpaca accounts via
  OAuth and the app only *reads* their portfolio. It can never place orders.

---

## 0. What you already have in this repo

| File | What it does |
|---|---|
| `create.sql` (updated) | New `core.client_investment` table: `enabled` gate flag + the client's Alpaca OAuth token encrypted at rest |
| `web-app/crypto.py` | AES-256-GCM encrypt/decrypt for the token; master key from `ENCRYPTION_KEY`, the client id bound as AAD |
| `web-app/alpaca_client.py` | OAuth authorize/exchange + read-only Trading API reads (account, positions, portfolio history). Scope omits `trading` |
| `web-app/db.py` (updated) | Helpers: `get_investment`, `set_investment_enabled`, `store_broker_token`, `revoke_broker_token`, `touch_broker_token_used` |
| `web-app/persona/__init__.py` & `web-app/empresa/__init__.py` (updated) | `/inversion`, `/inversion/conectar`, `/inversion/callback`, `/inversion/desconectar` routes |
| `web-app/administracion/__init__.py` (updated) | "Cliente de inversión" checkbox on client creation + Inversión toggle column |
| `web-app/templates/_inversion_panel.html` | Shared 3-state tab (placeholder / connect / portfolio) used by persona and empresa |

Public OAuth callback URLs once deployed:
```
https://gastos.empoweredinvestor.trade/persona/inversion/callback
https://gastos.empoweredinvestor.trade/empresa/inversion/callback
```

---

## 1. The host account (your firm's Alpaca account)

You need **one** Alpaca account that owns the OAuth app — this is the *host
account*. It is **not** a custody account:

- ✅ It only identifies your app in the OAuth flow (holds `CLIENT_ID` / `CLIENT_SECRET`).
- ❌ It does **not** hold client funds — each client connects their **own** Alpaca account.
- ❌ It does **not** store client tokens — those live encrypted in your database.
- ❌ It cannot trade client accounts — the read-only scope forbids order execution.

> This is the OAuth / Connect model, **not** the Broker API (correspondent)
> model. We are not custodying client money.

---

## 2. Register the OAuth app

1. Log in to the host account at <https://app.alpaca.markets/connect>.
2. Complete the **Alpaca Connect Application** form. Disclose that this is a
   **commercial** app (it serves paying clients) — commercial apps must be
   disclosed and get written approval.
3. Set the **redirect URIs** to the exact callback URLs above (HTTPS, no
   wildcards).
4. On approval you receive `CLIENT_ID` and `CLIENT_SECRET`.

**Review note:** the heavyweight *live-trading* compliance review is triggered
by apps that execute trades. Because this app is **read-only** (no `trading`
scope), it is not subject to that review — only the standard registration plus
the commercial disclosure.

⚠️ Confirm the exact **read-only scope string** in Alpaca's docs before going
live: <https://docs.alpaca.markets/docs/using-oauth2-and-trading-api>. The code
defaults to `account:write` (which grants read access without `trading`) and is
overridable via `ALPACA_SCOPE`.

---

## 3. Environment variables

Add to `.env` on the server (see `.env.example`):

| Value | Env var | Notes |
|---|---|---|
| Encryption master key | `ENCRYPTION_KEY` | Generate once: `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`. **Never** commit it or store it in the DB. |
| OAuth client id | `ALPACA_CLIENT_ID` | From the registration |
| OAuth client secret | `ALPACA_CLIENT_SECRET` | The crown jewel — protect it (see §5) |
| Callback URL | `ALPACA_REDIRECT_URI` | Must match a registered redirect URI exactly |
| OAuth scope | `ALPACA_SCOPE` | Default `account:write` (read-only). Do **not** add `trading`. |
| API base | `ALPACA_API_BASE` | Default `https://api.alpaca.markets` |

---

## 4. Apply the schema and roll out

1. Run the new table from `create.sql` (or just the `core.client_investment`
   block) against Postgres.
2. `pip install -r web-app/requirements.txt` (adds `requests`, `cryptography`).
3. Restart the web app.

---

## 5. Hardening the host account

Although the host account holds no client money, protect it:

| Asset | Risk if leaked | Mitigation |
|---|---|---|
| `CLIENT_SECRET` | Lets an attacker impersonate your app in the OAuth flow | Keep in `.env` / secrets manager, never in git. Rotate on suspicion. |
| Host account login | Could change the redirect URI, regenerate the secret, or view connected apps | **2FA on**, strong password, access limited to 1–2 trusted people. |
| `redirect_uri` allowlist | Tokens are only ever redirected to registered URIs | Register only exact HTTPS callback URLs. No wildcards. |
| `ENCRYPTION_KEY` | Decrypts every stored client token | Separate from the DB; rotate via `key_version` if needed. |

**Blast radius.** With read-only OAuth, even a stolen token cannot trade or move
money — only read a portfolio. A compromised host account does not directly
expose client funds or accounts, because each client's account stays separate.
The two things that genuinely must be blinded are the **`CLIENT_SECRET`** and
**2FA access** to the host account.

---

## 6. How the client experience works

1. Admin marks a client as a *Cliente de inversión* in `administracion`
   (checkbox at creation, or the Inversión toggle in the client list).
2. The client opens the **Portafolio de Inversión** tab and clicks
   **Conectar con Alpaca**.
3. They log in and consent **on Alpaca** (we never see their Alpaca password or
   API keys), then return to the app.
4. The tab shows portfolio value, positions, and the weekly / monthly / yearly
   percentage change — all read-only.
5. They can **Desconectar** at any time (clears and revokes the stored token).

The consent is long-lived: Alpaca OAuth tokens do not auto-expire — they remain
valid until the client revokes access (from Alpaca or via Desconectar).
