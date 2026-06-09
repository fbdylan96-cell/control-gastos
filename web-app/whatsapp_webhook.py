"""
Meta WhatsApp Cloud API webhook endpoint.

Receives all incoming events from Meta:
  - Initial GET verification handshake (hub.challenge)
  - POST callbacks for inbound user messages, button taps, list replies, delivery status

Routes user interactions back into our notification lifecycle:
  - Tap 'Reclasificar' → reply with a List Message of categories
  - Tap 'Ir a aplicación' → reply with the web app URL for the client's app
  - Pick a category from the list → update transactions_notifications and confirm
"""

import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path

from flask import Blueprint, abort, request

from db import get_connection

# Allow importing whatsapp_client from the parent control-gastos directory
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))
import whatsapp_client  # noqa: E402

log = logging.getLogger(__name__)

whatsapp_webhook_bp = Blueprint("whatsapp_webhook", __name__)

INDIVIDUAL_BIZ_ID = "00000000-0000-0000-0000-000000009999"

WEBAPP_BASE_URL = "https://gastos.empoweredinvestor.trade"
URL_PERSONA = f"{WEBAPP_BASE_URL}/persona/"
URL_EMPRESA = f"{WEBAPP_BASE_URL}/empresa/"

ROW_TITLE_MAX = 20  # per PROMPT_requests.md
SECTION_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
# Meta caps an interactive list at 10 rows TOTAL across all sections (not 100).
# Sending more returns (#131009) "Total row count exceed max allowed count: 10"
# and the whole list fails, so the user gets nothing back. Businesses with more
# than 10 category rows have their list truncated to the first 10.
MAX_ROWS_TOTAL = 10
MAX_ROWS_PER_SECTION = 10
MAX_SECTIONS = 10


# ---------------------------------------------------------------------------
# Payload encoding / decoding for button + list-row ids
# ---------------------------------------------------------------------------

def _enc(value: str) -> str:
    return urllib.parse.quote(value or "", safe="")


def _dec(value: str) -> str:
    return urllib.parse.unquote(value or "")


def _make_row_id(notification_id: str, category: str, subcategory: str | None) -> str:
    return f"rc|nid={notification_id}|c={_enc(category)}|s={_enc(subcategory or '')}"


def _parse_row_id(raw: str) -> dict | None:
    """Parse a row id produced by _make_row_id. Returns None if it doesn't match."""
    parts = dict(p.split("=", 1) for p in raw.split("|") if "=" in p)
    nid = parts.get("nid", "").strip()
    if not raw.startswith("rc|") or not nid:
        return None
    return {
        "notification_id": nid,
        "category": _dec(parts.get("c", "")),
        "subcategory": _dec(parts.get("s", "")) or None,
    }


def _parse_button_payload(payload: str) -> dict | None:
    """Parse a quick-reply button payload set by whatsapp_notifier._button_payloads.

    Returns {"action": "rc"|"go", "notification_id": "..."} or None on mismatch.
    """
    if not payload:
        return None
    parts = payload.split("|")
    if len(parts) < 2:
        return None
    action = parts[0]
    if action not in ("rc", "go"):
        return None
    nid = ""
    for p in parts[1:]:
        if p.startswith("nid="):
            nid = p[4:].strip()
            break
    if not nid:
        return None
    return {"action": action, "notification_id": nid}


# ---------------------------------------------------------------------------
# DB helpers — local to keep the webhook self-contained
# ---------------------------------------------------------------------------

def _fetch_notification_for_reclassify(conn, notification_id: str) -> dict | None:
    """Pull what we need to render the category list message for a notification."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                n.id              AS notification_id,
                n.individual_id,
                n.business_id,
                e.merchant_guess,
                e.amount_guess,
                e.currency_guess,
                e.desc_guess,
                r.local_date
            FROM core.transactions_notifications n
            JOIN core.transactions_classified cl ON cl.id = n.classified_id
            JOIN core.transactions_enriched   e  ON e.raw_id = cl.raw_id
            JOIN core.transactions_raw        r  ON r.id = cl.raw_id
            WHERE n.id = %s
            """,
            (notification_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def _fetch_client_phone_and_business(conn, notification_id: str) -> tuple[str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.phone_number, c.business_id
            FROM core.transactions_notifications n
            JOIN core.clients c ON c.id = n.individual_id
            WHERE n.id = %s
            """,
            (notification_id,),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        return row[0], str(row[1])


def _fetch_categories(conn, business_id: str, individual_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, subcategory
            FROM core.categories
            WHERE business_id = %s
              AND (individual_id = %s OR individual_id IS NULL)
            ORDER BY category, subcategory NULLS FIRST
            """,
            (business_id, individual_id),
        )
        return [{"category": r[0], "subcategory": r[1]} for r in cur.fetchall()]


def _update_whatsapp_action(conn, notification_id: str, action_value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET whatsapp_action_at = now(),
                whatsapp_action_value = %s
            WHERE id = %s
            """,
            (action_value, notification_id),
        )
    conn.commit()


def _apply_reclassification(conn, notification_id: str, category: str, subcategory: str | None) -> None:
    action_value = f"{category} / {subcategory}" if subcategory else category
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET final_category     = %s,
                final_subcategory  = %s,
                reclassified_by    = 'user',
                reclassified_at    = now(),
                whatsapp_action_at = now(),
                whatsapp_action_value = %s
            WHERE id = %s
            """,
            (category, subcategory, action_value, notification_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Building outgoing replies
# ---------------------------------------------------------------------------

def _fmt_amount(currency: str | None, amount) -> str:
    if amount is None:
        return ""
    try:
        return f"{currency or ''} {float(amount):,.2f}".strip()
    except (TypeError, ValueError):
        return f"{currency or ''} {amount}".strip()


def _fmt_date(local_date) -> str:
    if local_date is None:
        return ""
    try:
        return local_date.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return str(local_date)


def _build_reclassify_sections(categories: list[dict], notification_id: str) -> list[dict]:
    """Convert flat (category, subcategory) rows into Meta List Message sections.

    - Each distinct category becomes a section (max 10 sections).
    - Each subcategory becomes a row under it (max 10 rows per section, 100 total).
    - When a category has no subcategories, a single row with the category name is added
      (per the layout requested in PROMPT_requests.md).
    - When over the 100-row cap, the overflow is truncated and a warning is logged.
    """
    grouped: dict[str, list[str | None]] = {}
    order: list[str] = []
    for c in categories:
        cat = c["category"] or "Otros"
        sub = c["subcategory"]
        if cat not in grouped:
            grouped[cat] = []
            order.append(cat)
        grouped[cat].append(sub)

    sections: list[dict] = []
    total_rows = 0
    truncated = False

    for cat in order[:MAX_SECTIONS]:
        subs = grouped[cat]
        rows: list[dict] = []
        has_real_sub = any(s for s in subs)

        if not has_real_sub:
            # No subcategories — show the category itself as the single row
            if total_rows >= MAX_ROWS_TOTAL:
                truncated = True
                break
            rows.append({
                "id": _make_row_id(notification_id, cat, None),
                "title": cat[:ROW_TITLE_MAX],
            })
            total_rows += 1
        else:
            for sub in subs:
                if sub is None:
                    continue
                if len(rows) >= MAX_ROWS_PER_SECTION or total_rows >= MAX_ROWS_TOTAL:
                    truncated = True
                    break
                rows.append({
                    "id": _make_row_id(notification_id, cat, sub),
                    "title": sub[:ROW_TITLE_MAX],
                    "description": cat[:ROW_DESCRIPTION_MAX],
                })
                total_rows += 1

        if rows:
            sections.append({"title": cat[:SECTION_TITLE_MAX], "rows": rows})
        if truncated:
            break

    if truncated or len(order) > MAX_SECTIONS:
        log.warning(
            f"  notification_id={notification_id} — category list truncated to fit Meta limits "
            f"(sections sent={len(sections)}, rows sent={total_rows}, source categories={len(order)})"
        )

    return sections


def _send_reclassify_list(conn, to: str, notification_id: str) -> None:
    notif = _fetch_notification_for_reclassify(conn, notification_id)
    if not notif:
        log.warning(f"  notification_id={notification_id} — not found, ignoring Reclasificar tap")
        return

    categories = _fetch_categories(conn, str(notif["business_id"]), str(notif["individual_id"]))
    if not categories:
        log.warning(f"  notification_id={notification_id} — no categories available")
        whatsapp_client.send_text(
            to, "No hay categorías disponibles para reclasificar."
        )
        return

    sections = _build_reclassify_sections(categories, notification_id)
    if not sections:
        whatsapp_client.send_text(to, "No hay categorías disponibles para reclasificar.")
        return

    amount_str = _fmt_amount(notif.get("currency_guess"), notif.get("amount_guess"))
    desc = notif.get("desc_guess") or notif.get("merchant_guess") or "Transacción"
    date_str = _fmt_date(notif.get("local_date"))

    body_lines = [f"*{desc}*"]
    if amount_str:
        body_lines.append(f"Monto: {amount_str}")
    if date_str:
        body_lines.append(f"Fecha: {date_str}")
    body_text = "\n".join(body_lines)

    whatsapp_client.send_list_message(
        to=to,
        header_text="Seleccionar categoría correcta",
        body_text=body_text,
        button_label="Ver opciones",
        sections=sections,
    )


def _send_webapp_url(conn, to: str, notification_id: str) -> None:
    _phone, business_id = _fetch_client_phone_and_business(conn, notification_id)
    url = URL_PERSONA if business_id == INDIVIDUAL_BIZ_ID else URL_EMPRESA
    whatsapp_client.send_text(to, url)


# ---------------------------------------------------------------------------
# Webhook routes
# ---------------------------------------------------------------------------

@whatsapp_webhook_bp.route("/webhook", methods=["GET"])
def verify():
    """Meta's initial webhook verification handshake."""
    expected_token = os.environ.get("META_WA_VERIFY_TOKEN", "").strip()
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and expected_token and token == expected_token:
        return challenge, 200
    return "forbidden", 403


@whatsapp_webhook_bp.route("/webhook", methods=["POST"])
def receive():
    raw_body = request.get_data()  # cache=True (default) — body must be readable by get_json() afterwards
    signature = request.headers.get("X-Hub-Signature-256", "")
    log.info(f"WA POST received: {len(raw_body)} bytes, sig={'yes' if signature else 'no'}")
    if not whatsapp_client.verify_webhook_signature(raw_body, signature):
        log.warning("WA webhook: invalid signature, rejecting")
        abort(403)

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    try:
        _process_payload(payload)
    except Exception as e:
        # Always 200 to Meta to prevent retry storms; log internally.
        log.error(f"WA webhook handler error: {e}")

    return "ok", 200


def _log_status(status: dict) -> None:
    """Log a Meta delivery-status callback (sent / delivered / read / failed).

    Failures carry an errors[] array — surfacing code/title/details here means a
    problem like 131042 (payment method) shows up directly in journalctl instead
    of only in Meta's dashboard.
    """
    state = status.get("status")
    recipient = status.get("recipient_id")
    wamid = status.get("id")
    errors = status.get("errors") or []
    if state == "failed" or errors:
        for err in errors or [{}]:
            details = (err.get("error_data") or {}).get("details")
            log.error(
                f"  WA status=failed → {recipient} | wamid={wamid} | "
                f"code={err.get('code')} | title={err.get('title')!r}"
                + (f" | details={details!r}" if details else "")
            )
    else:
        log.info(f"  WA status={state} → {recipient} | wamid={wamid}")


def _process_payload(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}
            messages = value.get("messages") or []
            statuses = value.get("statuses") or []
            log.info(f"WA payload: {len(messages)} message(s), {len(statuses)} status(es)")
            for s in statuses:
                _log_status(s)
            from_phone = None
            for m in messages:
                from_phone = m.get("from") or from_phone
                _handle_message(m, from_phone)


def _handle_message(msg: dict, from_phone: str | None) -> None:
    msg_type = msg.get("type")
    if not from_phone:
        from_phone = msg.get("from")
    if not from_phone:
        return

    conn = get_connection()
    try:
        if msg_type == "button":
            # Template quick-reply tap (Reclasificar / Ir a aplicación)
            payload = (msg.get("button") or {}).get("payload", "")
            text = (msg.get("button") or {}).get("text", "")
            parsed = _parse_button_payload(payload)
            if not parsed:
                log.info(f"  Unrecognized button payload: {payload!r}")
                return
            nid = parsed["notification_id"]
            _update_whatsapp_action(conn, nid, text or parsed["action"])
            if parsed["action"] == "rc":
                _send_reclassify_list(conn, from_phone, nid)
            elif parsed["action"] == "go":
                _send_webapp_url(conn, from_phone, nid)

        elif msg_type == "interactive":
            interactive = msg.get("interactive") or {}
            itype = interactive.get("type")
            if itype == "list_reply":
                row_id = (interactive.get("list_reply") or {}).get("id", "")
                parsed = _parse_row_id(row_id)
                if not parsed:
                    log.info(f"  Unrecognized list_reply id: {row_id!r}")
                    return
                _apply_reclassification(
                    conn,
                    parsed["notification_id"],
                    parsed["category"],
                    parsed["subcategory"],
                )
                pretty = (
                    f"{parsed['category']} / {parsed['subcategory']}"
                    if parsed["subcategory"]
                    else parsed["category"]
                )
                whatsapp_client.send_text(
                    from_phone,
                    f"✅ Reclasificación guardada: {pretty}",
                    preview_url=False,
                )
            elif itype == "button_reply":
                # Free-form interactive button reply (not used today, but tolerate it)
                btn = interactive.get("button_reply") or {}
                payload = btn.get("id", "")
                parsed = _parse_button_payload(payload)
                if parsed:
                    nid = parsed["notification_id"]
                    _update_whatsapp_action(conn, nid, btn.get("title") or parsed["action"])
                    if parsed["action"] == "rc":
                        _send_reclassify_list(conn, from_phone, nid)
                    elif parsed["action"] == "go":
                        _send_webapp_url(conn, from_phone, nid)
        else:
            # Ignore other inbound types (text/image/etc.) — out of scope for now
            log.info(f"  Ignoring inbound WA message type={msg_type}")
    finally:
        conn.close()
