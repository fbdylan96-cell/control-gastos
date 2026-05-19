"""
Step 6 (WhatsApp channel).

For each classified transaction whose WhatsApp notification hasn't been sent yet,
sends an approved Meta template message with two quick-reply buttons:
  - 'Reclasificar' → opens an interactive list of categories (handled by webhook)
  - 'Ir a aplicación' → web app URL (handled by webhook)

Mirrors the email notifier (notifier.py): same eligibility rules for budget,
same dispatcher pattern, separate DB columns on transactions_notifications.
"""

import logging
import os

import whatsapp_client
from db import (
    get_budget_and_spending,
    get_pending_whatsapp_notifications,
    mark_whatsapp_sent,
)

log = logging.getLogger(__name__)

INDIVIDUAL_BIZ_ID = "00000000-0000-0000-0000-000000009999"

# Map enricher's transaction_type_guess → human Spanish word for {{1}}
_TYPE_LABEL = {
    "debito": "débito",
    "credito": "crédito",
}
_TYPE_LABEL_DEFAULT = "movimiento"


def _template_simple() -> str:
    return os.environ.get("META_WA_TEMPLATE_SIMPLE", "gasto_detectado_simple").strip()


def _template_budget() -> str:
    return os.environ.get("META_WA_TEMPLATE_BUDGET", "gasto_detectado_presupuesto").strip()


def _template_simple_lang() -> str:
    return os.environ.get("META_WA_TEMPLATE_SIMPLE_LANG", "es").strip()


def _template_budget_lang() -> str:
    return os.environ.get("META_WA_TEMPLATE_BUDGET_LANG", "es_ES").strip()


def _fmt_amount_value(amount) -> str:
    """Format a numeric amount for a WhatsApp template body variable."""
    if amount is None:
        return "—"
    try:
        return f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def _fmt_date(local_date) -> str:
    """Per the approved template screenshot: DD-MM-YYYY (no time)."""
    if local_date is None:
        return "—"
    try:
        return local_date.strftime("%d-%m-%Y")
    except Exception:
        return str(local_date)


def _placeholder(value) -> str:
    """Meta rejects empty template parameters — substitute a visible dash."""
    s = "" if value is None else str(value).strip()
    return s if s else "—"


def _build_simple_params(notif: dict) -> list[str]:
    """Ordered body parameters for gasto_detectado_simple ({{1}} through {{8}})."""
    type_label = _TYPE_LABEL.get(notif.get("transaction_type_guess"), _TYPE_LABEL_DEFAULT)
    return [
        type_label,                                              # {{1}} débito/crédito
        _placeholder(notif.get("currency_guess")),               # {{2}} USD
        _fmt_amount_value(notif.get("amount_guess")),            # {{3}} 20.00
        _fmt_amount_value(notif.get("amount_local")),            # {{4}} 10,000.00 (CRC)
        _placeholder(notif.get("merchant")),                     # {{5}} Shell
        _fmt_date(notif.get("local_date")),                      # {{6}} 17-05-2026
        _placeholder(notif.get("final_category")),               # {{7}} Transporte
        _placeholder(notif.get("final_subcategory")),            # {{8}} Gasolina
    ]


def _build_budget_extra_params(
    notif: dict, monthly_budget: float, total_month_spending: float
) -> list[str]:
    """Extra body parameters for the budget template ({{9}} through {{13}})."""
    pct = (total_month_spending / monthly_budget * 100) if monthly_budget > 0 else 0
    return [
        _placeholder(notif.get("final_category")),       # {{9}} Transporte
        _placeholder(notif.get("final_subcategory")),    # {{10}} Gasolina
        f"{total_month_spending:,.2f}",                  # {{11}} 38,500.00
        f"{monthly_budget:,.2f}",                        # {{12}} 50,000.00
        f"{pct:.0f}",                                    # {{13}} 77
    ]


def _button_payloads(notification_id: str) -> list[str]:
    """Ordered payloads for the two quick-reply buttons in both templates.

    Index 0 = Reclasificar; index 1 = Ir a aplicación. The webhook parses these.
    Format kept short (well under Meta's 256-char payload limit).
    """
    return [
        f"rc|nid={notification_id}",
        f"go|nid={notification_id}",
    ]


def run_whatsapp_notifications(conn) -> int:
    """Send pending WhatsApp messages for all eligible notifications.

    Returns the number of messages successfully sent.
    """
    pending = get_pending_whatsapp_notifications(conn)
    log.info(f"WA notifier: {len(pending)} notification(s) to send")
    sent = 0

    for notif in pending:
        notification_id = str(notif["notification_id"])
        raw_phone = notif.get("phone_number") or ""
        to = whatsapp_client.normalize_phone(raw_phone)
        if not to:
            log.warning(
                f"  notification_id={notification_id} — invalid phone_number "
                f"({raw_phone!r}), skipping"
            )
            continue

        try:
            is_individual = str(notif["business_id"]) == INDIVIDUAL_BIZ_ID
            budget_eligible = notif.get("business_admin") or is_individual
            monthly_budget, total_month_spending = (
                get_budget_and_spending(conn, notif) if budget_eligible else (None, None)
            )

            body_params = _build_simple_params(notif)
            payloads = _button_payloads(notification_id)

            if monthly_budget is not None and total_month_spending is not None:
                template_name = _template_budget()
                language_code = _template_budget_lang()
                body_params = body_params + _build_budget_extra_params(
                    notif, float(monthly_budget), float(total_month_spending)
                )
            else:
                template_name = _template_simple()
                language_code = _template_simple_lang()

            whatsapp_client.send_template(
                to=to,
                template_name=template_name,
                language_code=language_code,
                body_params=body_params,
                button_payloads=payloads,
            )
            mark_whatsapp_sent(conn, notification_id)
            log.info(
                f"  WA sent → {to} | template={template_name} | "
                f"merchant={notif.get('merchant')} | cat={notif.get('final_category')}"
            )
            sent += 1

        except Exception as e:
            log.error(f"  Failed WA notification_id={notification_id}: {e}")

    log.info(f"WA notifier complete: {sent}/{len(pending)} sent")
    return sent
