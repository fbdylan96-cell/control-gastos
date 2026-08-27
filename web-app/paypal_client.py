"""
PayPal Subscriptions REST API wrapper (Fase 2 del motor de pagos).

Cobros SIEMPRE en USD: PayPal no soporta CRC (verificado 2026-06-25). La app
muestra ₡ (subscription_plans.amount_crc) y cobra amount_usd.

Flujo:
  1. paypal_bootstrap.py crea (una vez por ambiente) el Product y un Billing
     Plan por cada fila de core.subscription_plans → guarda paypal_plan_id.
  2. persona/facturacion crea la suscripción del cliente con
     custom_id = clients.id y redirige al link de aprobación de PayPal.
     Si el cliente está en prueba gratuita, start_time = trial_end: el primer
     cobro cae exactamente cuando termina la prueba (no usamos un ciclo TRIAL
     en el plan porque daría 30 días fijos sin importar cuánta prueba quede).
  3. paypal_webhook.py recibe los eventos firmados y actualiza
     core.client_subscriptions.

Env vars (ver .env.example):
  PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET — credenciales de la app REST.
  PAYPAL_MODE — 'sandbox' (default) o 'live'. Decide el host de la API.
  PAYPAL_WEBHOOK_ID — id del webhook registrado en el developer dashboard;
                      requerido para verificar firmas (fail-closed sin él).
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 20

# Cache del access token OAuth2 (client_credentials). PayPal los emite por
# ~9 horas; se renueva 60 s antes de expirar.
_TOKEN_CACHE = {"token": None, "exp": 0.0}


def is_configured() -> bool:
    return bool(os.environ.get("PAYPAL_CLIENT_ID", "").strip()
                and os.environ.get("PAYPAL_CLIENT_SECRET", "").strip())


def _base_url() -> str:
    mode = os.environ.get("PAYPAL_MODE", "sandbox").strip().lower()
    if mode == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def _access_token() -> str:
    if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["exp"]:
        return _TOKEN_CACHE["token"]

    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET no están configurados")

    resp = requests.post(
        f"{_base_url()}/v1/oauth2/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        timeout=_TIMEOUT,
    )
    if not resp.ok:
        log.error(f"PayPal OAuth falló [{resp.status_code}]: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    _TOKEN_CACHE["token"] = data["access_token"]
    _TOKEN_CACHE["exp"] = time.time() + int(data.get("expires_in", 0)) - 60
    return _TOKEN_CACHE["token"]


def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    resp = requests.request(
        method,
        f"{_base_url()}{path}",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=_TIMEOUT,
    )
    if not resp.ok:
        log.error(f"PayPal {method} {path} falló [{resp.status_code}]: {resp.text}")
        resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


# ── Catálogo (Products + Billing Plans) — usados por paypal_bootstrap.py ──────

# Id fijo del Product: hace idempotente el bootstrap (el segundo create con el
# mismo id devuelve DUPLICATE_RESOURCE_IDENTIFIER y se reutiliza).
PRODUCT_ID = "NETO-CONTROL-GASTOS"


def ensure_product() -> str:
    """Crea el Product del catálogo si no existe. Devuelve su id."""
    resp = requests.post(
        f"{_base_url()}/v1/catalogs/products",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json={
            "id": PRODUCT_ID,
            "name": "neto — control de gastos",
            "description": "Membresía de la aplicación neto (control de gastos personales).",
            "type": "SERVICE",
            "category": "SOFTWARE",
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code == 201:
        log.info(f"PayPal product creado: {PRODUCT_ID}")
        return PRODUCT_ID
    # 422 DUPLICATE_RESOURCE_IDENTIFIER → ya existía (bootstrap re-ejecutado)
    if resp.status_code == 422 and "DUPLICATE_RESOURCE_IDENTIFIER" in resp.text:
        log.info(f"PayPal product ya existía: {PRODUCT_ID}")
        return PRODUCT_ID
    log.error(f"PayPal create product falló [{resp.status_code}]: {resp.text}")
    resp.raise_for_status()
    return PRODUCT_ID  # unreachable


def create_plan(name: str, amount_usd, modality: str) -> str:
    """Crea un Billing Plan (un solo ciclo REGULAR infinito) y devuelve su id.

    modality: 'mensual' | 'anual' (mismos valores que subscription_plans).
    """
    interval_unit = "YEAR" if modality == "anual" else "MONTH"
    data = _request("POST", "/v1/billing/plans", {
        "product_id": PRODUCT_ID,
        "name": name,
        "billing_cycles": [{
            "frequency": {"interval_unit": interval_unit, "interval_count": 1},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,  # 0 = se renueva indefinidamente
            "pricing_scheme": {
                "fixed_price": {"value": f"{float(amount_usd):.2f}",
                                "currency_code": "USD"},
            },
        }],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "payment_failure_threshold": 2,
        },
    })
    return data["id"]


# ── Suscripciones por cliente ─────────────────────────────────────────────────

def create_subscription(paypal_plan_id: str, client_id: str, return_url: str,
                        cancel_url: str, start_time: str | None = None) -> tuple[str, str]:
    """Crea la suscripción y devuelve (subscription_id, approval_url).

    custom_id = client_id: es lo que une los webhooks con core.clients.
    start_time (RFC3339 UTC, futuro) difiere el primer cobro — se usa para
    respetar los días restantes de la prueba gratuita.
    """
    body = {
        "plan_id": paypal_plan_id,
        "custom_id": str(client_id),
        "application_context": {
            "brand_name": "neto",
            "locale": "es-CR",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    if start_time:
        body["start_time"] = start_time
    data = _request("POST", "/v1/billing/subscriptions", body)
    approval = next((l["href"] for l in data.get("links", []) if l.get("rel") == "approve"), None)
    if not approval:
        raise RuntimeError(f"PayPal no devolvió link de aprobación: {data}")
    return data["id"], approval


def get_subscription(subscription_id: str) -> dict:
    return _request("GET", f"/v1/billing/subscriptions/{subscription_id}")


def cancel_subscription(subscription_id: str, reason: str = "Cancelada por el cliente") -> None:
    _request("POST", f"/v1/billing/subscriptions/{subscription_id}/cancel",
             {"reason": reason})


# ── Verificación de firma de webhooks ─────────────────────────────────────────

def verify_webhook_signature(headers, event: dict) -> bool:
    """Valida un webhook contra /v1/notifications/verify-webhook-signature.

    A diferencia de Meta (HMAC local con app secret), PayPal verifica en su
    API con el PAYPAL_WEBHOOK_ID del webhook registrado. Fail-closed: sin
    webhook id o sin los 5 headers de transmisión → False.
    """
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        log.error("PAYPAL_WEBHOOK_ID no está configurado — webhook rechazado")
        return False
    required = ["Paypal-Auth-Algo", "Paypal-Cert-Url", "Paypal-Transmission-Id",
                "Paypal-Transmission-Sig", "Paypal-Transmission-Time"]
    values = {h: headers.get(h, "") for h in required}
    if not all(values.values()):
        log.warning("PayPal webhook sin headers de transmisión — rechazado")
        return False
    try:
        data = _request("POST", "/v1/notifications/verify-webhook-signature", {
            "auth_algo": values["Paypal-Auth-Algo"],
            "cert_url": values["Paypal-Cert-Url"],
            "transmission_id": values["Paypal-Transmission-Id"],
            "transmission_sig": values["Paypal-Transmission-Sig"],
            "transmission_time": values["Paypal-Transmission-Time"],
            "webhook_id": webhook_id,
            "webhook_event": event,
        })
    except Exception as e:
        log.error(f"PayPal verify-webhook-signature falló: {e}")
        return False
    return data.get("verification_status") == "SUCCESS"
