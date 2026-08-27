"""
PayPal Subscriptions webhook endpoint (Fase 2 del motor de pagos).

Recibe los eventos del webhook registrado en el developer dashboard de PayPal
(PAYPAL_WEBHOOK_ID) y sincroniza core.client_subscriptions:

  BILLING.SUBSCRIPTION.ACTIVATED       → status 'active' (custom_id = clients.id)
  BILLING.SUBSCRIPTION.UPDATED         → refresca próximo cobro
  BILLING.SUBSCRIPTION.CANCELLED       → status 'cancelled'
  BILLING.SUBSCRIPTION.EXPIRED         → status 'cancelled'
  BILLING.SUBSCRIPTION.SUSPENDED       → status 'suspended'
  BILLING.SUBSCRIPTION.PAYMENT.FAILED  → status 'past_due'
  PAYMENT.SALE.COMPLETED               → status 'active' + próximo cobro (cobro
                                         recurrente exitoso; también recupera
                                         una cuenta que estaba en past_due)

Cada evento se verifica contra la API de PayPal (verify-webhook-signature)
antes de tocar la base. Patrón del endpoint: whatsapp_webhook.py.
"""

import logging

from flask import Blueprint, abort, request

import paypal_client
from db import (activate_subscription_from_paypal, get_connection,
                get_plan_by_paypal_plan_id, update_subscription_from_paypal)

log = logging.getLogger(__name__)

paypal_webhook_bp = Blueprint("paypal_webhook", __name__)

# resource.status de PayPal → status de core.client_subscriptions
_STATUS_MAP = {
    "CANCELLED": "cancelled",
    "EXPIRED": "cancelled",
    "SUSPENDED": "suspended",
}


def _next_billing_time(resource: dict) -> str | None:
    return (resource.get("billing_info") or {}).get("next_billing_time")


def _handle_activated(conn, resource: dict) -> None:
    client_id = (resource.get("custom_id") or "").strip()
    sub_id = resource.get("id")
    if not client_id or not sub_id:
        log.warning(f"PayPal ACTIVATED sin custom_id/id — ignorado: {resource.get('id')}")
        return
    plan = get_plan_by_paypal_plan_id(conn, resource.get("plan_id"))
    if not plan:
        log.warning(f"PayPal ACTIVATED con plan desconocido {resource.get('plan_id')!r} "
                    f"(sub={sub_id}) — se activa sin plan interno")
    activate_subscription_from_paypal(
        conn, client_id, plan["id"] if plan else None, sub_id,
        _next_billing_time(resource),
    )
    log.info(f"PayPal sub {sub_id} activada para cliente {client_id}"
             f" (plan={plan['name'] if plan else '?'})")


def _handle_sale_completed(conn, resource: dict) -> None:
    # En PAYMENT.SALE.COMPLETED la suscripción viene en billing_agreement_id.
    sub_id = resource.get("billing_agreement_id")
    if not sub_id:
        log.info("PayPal SALE.COMPLETED sin billing_agreement_id (pago no "
                 "recurrente) — ignorado")
        return
    # El evento no trae el próximo cobro: se consulta la suscripción. Si la
    # consulta falla igual se marca 'active' (el pago SÍ se completó).
    next_billing = None
    try:
        next_billing = _next_billing_time(paypal_client.get_subscription(sub_id))
    except Exception as e:
        log.error(f"PayPal: no se pudo leer la sub {sub_id} tras un pago: {e}")
    touched = update_subscription_from_paypal(conn, sub_id, "active", next_billing)
    if touched:
        log.info(f"PayPal pago completado sub {sub_id} — próximo cobro {next_billing}")
    else:
        log.warning(f"PayPal pago completado para sub desconocida {sub_id}")


def _process_event(conn, event: dict) -> None:
    event_type = event.get("event_type", "")
    resource = event.get("resource") or {}

    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        _handle_activated(conn, resource)

    elif event_type == "BILLING.SUBSCRIPTION.UPDATED":
        sub_id = resource.get("id")
        next_billing = _next_billing_time(resource)
        if sub_id and next_billing:
            update_subscription_from_paypal(conn, sub_id, "active", next_billing)
            log.info(f"PayPal sub {sub_id} actualizada — próximo cobro {next_billing}")

    elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED",
                        "BILLING.SUBSCRIPTION.EXPIRED",
                        "BILLING.SUBSCRIPTION.SUSPENDED"):
        sub_id = resource.get("id")
        status = _STATUS_MAP[event_type.rsplit(".", 1)[1]]
        touched = update_subscription_from_paypal(conn, sub_id, status)
        log.info(f"PayPal sub {sub_id} → {status} ({'ok' if touched else 'desconocida'})")

    elif event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        sub_id = resource.get("id")
        touched = update_subscription_from_paypal(conn, sub_id, "past_due")
        log.warning(f"PayPal PAGO FALLIDO sub {sub_id} → past_due "
                    f"({'ok' if touched else 'desconocida'})")

    elif event_type == "PAYMENT.SALE.COMPLETED":
        _handle_sale_completed(conn, resource)

    else:
        log.info(f"PayPal evento ignorado: {event_type}")


@paypal_webhook_bp.route("/webhook", methods=["POST"])
def receive():
    event = request.get_json(force=True, silent=True) or {}
    if not paypal_client.verify_webhook_signature(request.headers, event):
        log.warning("PayPal webhook: firma inválida, rechazado")
        abort(403)

    log.info(f"PayPal webhook: {event.get('event_type')} id={event.get('id')}")
    conn = get_connection()
    try:
        _process_event(conn, event)
    except Exception as e:
        # 200 igual: PayPal reintenta ante non-2xx y el error es nuestro, no del evento.
        log.error(f"PayPal webhook handler error: {e}")
    finally:
        conn.close()
    return "ok", 200
