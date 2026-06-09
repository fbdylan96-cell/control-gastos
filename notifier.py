"""
Step 6: Email notification.

For each classified transaction whose notification hasn't been sent yet,
sends an HTML email to the client with transaction details and
signed reclassification buttons (one per valid category/subcategory pair).

Reclassification clicks are handled by the Flask web app (Step 7).
"""

import hashlib
import hmac
import html
import logging
import os
from urllib.parse import urlencode

import gmail_client
from db import (get_budget_and_spending, get_categories,
                get_pending_email_notifications, mark_email_sent)

log = logging.getLogger(__name__)

# Wording per transaction direction — emails previously said "gasto" even for credits.
_TYPE_WORD = {"debito": "gasto", "credito": "ingreso"}
_TYPE_WORD_DEFAULT = "movimiento"


def _type_word(notif: dict) -> str:
    return _TYPE_WORD.get(notif.get("transaction_type_guess"), _TYPE_WORD_DEFAULT)

# ---------------------------------------------------------------------------
# Signed URL helpers
# ---------------------------------------------------------------------------

def _get_secret() -> str:
    secret = os.environ.get("NOTIFICATION_SECRET", "")
    if not secret:
        raise RuntimeError("NOTIFICATION_SECRET env var is not set")
    return secret


def sign_reclassification(notification_id: str, category: str, subcategory: str) -> str:
    """Return HMAC-SHA256 signature for a reclassification action."""
    sub = subcategory or ""
    payload = f"{notification_id}|{category}|{sub}"
    sig = hmac.new(_get_secret().encode(), payload.encode(), hashlib.sha256).digest()
    import base64
    return base64.urlsafe_b64encode(sig).decode()


def verify_reclassification(notification_id: str, category: str, subcategory: str, sig: str) -> bool:
    """Verify a reclassification signature from an email click."""
    expected = sign_reclassification(notification_id, category, subcategory or "")
    return hmac.compare_digest(expected, sig)


def _build_reclassify_url(notification_id: str, category: str, subcategory: str) -> str:
    webapp_url = os.environ.get("WEBAPP_URL", "").rstrip("/")
    if not webapp_url:
        return "#"
    sig = sign_reclassification(notification_id, category, subcategory or "")
    params = urlencode({
        "nid": notification_id,
        "cat": category,
        "sub": subcategory or "",
        "sig": sig,
    })
    return f"{webapp_url}/reclassify?{params}"


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _fmt_amount(currency: str, amount) -> str:
    try:
        return f"{currency} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} {amount}"


def _fmt_date(local_date) -> str:
    if local_date is None:
        return ""
    try:
        return local_date.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(local_date)


def _build_email_html(
    notif: dict,
    categories: list[dict],
    monthly_budget: float | None = None,
    total_month_spending: float | None = None,
) -> str:
    tipo_word = _type_word(notif)
    merchant = html.escape(notif.get("merchant") or tipo_word.capitalize())
    desc = html.escape(notif.get("desc_guess") or "")
    date_str = html.escape(_fmt_date(notif.get("local_date")))
    amount_str = html.escape(_fmt_amount(
        notif.get("currency_guess") or "",
        notif.get("amount_guess"),
    ))
    local_amount_str = html.escape(_fmt_amount(
        notif.get("currency_local") or "CRC",
        notif.get("amount_local"),
    )) if notif.get("amount_local") is not None else ""
    cat = html.escape(notif.get("final_category") or "")
    sub = html.escape(notif.get("final_subcategory") or "")
    classification = f"{cat} / {sub}" if sub else cat

    notification_id = str(notif["notification_id"])

    # Reclassification buttons — one per valid pair
    buttons_html = ""
    for row in categories:
        c = row["category"] or ""
        s = row["subcategory"] or ""
        label = html.escape(f"{c} / {s}" if s else c)
        url = _build_reclassify_url(notification_id, c, s)
        buttons_html += f"""
        <a href="{url}"
           style="display:inline-block;margin:6px 6px 0 0;padding:9px 14px;
                  text-decoration:none;border-radius:10px;border:1px solid #ddd;
                  background:#fff;font-family:Arial,sans-serif;font-size:13px;color:#111;">
            {label}
        </a>"""

    if not buttons_html:
        buttons_html = "<i style='color:#888;'>No hay categorías disponibles.</i>"

    # Budget section (Type 2 only)
    budget_html = ""
    if monthly_budget is not None and total_month_spending is not None:
        pct = min(total_month_spending / monthly_budget * 100, 100) if monthly_budget > 0 else 0
        bar_color = "#e74c3c" if pct >= 100 else "#f39c12" if pct >= 80 else "#65fdb0"
        budget_html = f"""
  <div style="padding:12px;border:1px solid #eee;border-radius:12px;margin-bottom:14px;background:#fafafa;">
    <div style="font-weight:700;font-size:13px;margin-bottom:8px;">Presupuesto — {classification}</div>
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;">
      <span>Gastado este mes</span>
      <span style="font-weight:700;">CRC {total_month_spending:,.2f}</span>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px;">
      <span>Presupuesto mensual</span>
      <span>CRC {monthly_budget:,.2f}</span>
    </div>
    <div style="background:#e8e8e8;border-radius:6px;height:8px;overflow:hidden;">
      <div style="width:{pct:.1f}%;height:100%;background:{bar_color};border-radius:6px;"></div>
    </div>
    <div style="text-align:right;font-size:11px;color:#888;margin-top:4px;">{pct:.0f}% utilizado</div>
  </div>"""

    return f"""
<div style="font-family:Arial,sans-serif;line-height:1.4;color:#111;max-width:560px;">

  <div style="font-size:17px;font-weight:700;margin-bottom:6px;">
    Se registró un {tipo_word}
  </div>

  <div style="margin-bottom:12px;">
    <span style="font-size:15px;font-weight:700;">{merchant}</span><br>
    {"<span style='color:#555;font-size:13px;'>" + desc + "</span><br>" if desc else ""}
    {"<span style='color:#888;font-size:12px;'>" + date_str + "</span>" if date_str else ""}
  </div>

  <div style="padding:12px;border:1px solid #eee;border-radius:12px;margin-bottom:14px;">
    <div><b>Monto:</b> {amount_str}</div>
    {"<div style='margin-top:4px;'><b>Monto local:</b> " + local_amount_str + "</div>" if local_amount_str else ""}
    <div style="margin-top:4px;"><b>Clasificación:</b> {classification}</div>
  </div>

{budget_html}
  <div>
    <div style="font-weight:700;margin-bottom:6px;">¿Está bien clasificado?</div>
    <div style="color:#555;font-size:13px;margin-bottom:8px;">
      Si querés cambiar la categoría, tocá una opción:
    </div>
    <div>{buttons_html}</div>
  </div>

  <div style="margin-top:20px;color:#aaa;font-size:11px;">
    Este correo fue enviado automáticamente. No respondas a este mensaje.
  </div>

</div>
"""


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_notifications(conn, service) -> int:
    """
    Send pending email notifications for all qualifying clients.
    Returns the number of emails sent.
    """
    pending = get_pending_email_notifications(conn)
    log.info(f"Notifier: {len(pending)} notification(s) to send")
    sent = 0

    for notif in pending:
        notification_id = notif["notification_id"]
        to_email = notif.get("email_address") or ""
        if not to_email:
            log.warning(f"  notification_id={notification_id} — no email address, skipping")
            continue

        try:
            categories = get_categories(conn, notif["business_id"], notif["individual_id"])

            # Budget data only for business admins and individual clients
            is_individual = str(notif["business_id"]) == "00000000-0000-0000-0000-000000009999"
            budget_eligible = notif.get("business_admin") or is_individual
            monthly_budget, total_month_spending = (
                get_budget_and_spending(conn, notif) if budget_eligible else (None, None)
            )

            html_body = _build_email_html(notif, categories, monthly_budget, total_month_spending)

            tipo_word = _type_word(notif).capitalize()
            merchant = notif.get("merchant") or tipo_word
            cat = notif.get("final_category") or ""
            currency = notif.get("currency_guess") or ""
            amount = notif.get("amount_guess") or ""
            subject = f"{tipo_word}: {currency} {amount} | {cat} | {merchant}"[:140]

            gmail_client.send_email(service, to_email, subject, html_body)
            mark_email_sent(conn, notification_id)
            log.info(f"  Sent → {to_email} | {subject}")
            sent += 1

        except Exception as e:
            log.error(f"  Failed notification_id={notification_id}: {e}")

    log.info(f"Notifier complete: {sent}/{len(pending)} sent")
    return sent
