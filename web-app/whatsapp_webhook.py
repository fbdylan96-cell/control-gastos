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
import uuid
from datetime import datetime
from pathlib import Path

import pytz

from flask import Blueprint, abort, request

from db import get_connection

# Allow importing whatsapp_client from the parent control-gastos directory
_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))
import whatsapp_client  # noqa: E402
from tools.finance import get_income_expense_summary, get_top_spending  # noqa: E402

log = logging.getLogger(__name__)

whatsapp_webhook_bp = Blueprint("whatsapp_webhook", __name__)

INDIVIDUAL_BIZ_ID = "00000000-0000-0000-0000-000000009999"

WEBAPP_BASE_URL = "https://gastos.empoweredinvestor.trade"
URL_PERSONA = f"{WEBAPP_BASE_URL}/persona/"
URL_EMPRESA = f"{WEBAPP_BASE_URL}/empresa/"

CR_TZ = pytz.timezone("America/Costa_Rica")
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
# Advisory weekly summary → 'Ver componentes del gasto': cap on breakdown rows
# so the reply stays readable; the remainder is aggregated into one line.
BREAKDOWN_MAX_ROWS = 12

# Consultation chat: max user messages accepted per client per hour. Bounds
# Anthropic API spend per client; the worker is the one paying the LLM cost.
CHAT_RATE_LIMIT_PER_HOUR = 20
CHAT_RATE_LIMIT_REPLY = (
    "Has alcanzado el límite de consultas por hora. Intenta de nuevo más tarde."
)

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
    """Parse a quick-reply button payload.

    Transaction templates (whatsapp_notifier._button_payloads) use
    'rc|nid=...' / 'go|nid=...' → {"action", "notification_id"}.
    The advisory weekly summary (advisory_scheduler) uses 'ad|cid=...'
    → {"action": "ad", "client_id": "..."}. Returns None on mismatch.
    """
    if not payload:
        return None
    parts = payload.split("|")
    if len(parts) < 2:
        return None
    action = parts[0]
    if action not in ("rc", "go", "ad"):
        return None
    fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    if action == "ad":
        cid = fields.get("cid", "").strip()
        if not cid:
            return None
        return {"action": "ad", "client_id": cid}
    nid = fields.get("nid", "").strip()
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


def _fetch_client_by_phone(conn, phone_digits: str) -> dict | None:
    """Match a Meta sender (digits only, no '+') to an active client.

    phone_number is stored in display form ('+506 8888 7777'), so compare on
    digits. If two clients ever share a phone, the oldest one wins.
    """
    with conn.cursor() as cur:
        cur.execute(
            r"""
            SELECT id, business_id, business_admin, client_name
            FROM core.clients
            WHERE active = TRUE
              AND phone_number IS NOT NULL
              AND regexp_replace(phone_number, '\D', '', 'g') = %s
            ORDER BY created_at
            LIMIT 1
            """,
            (phone_digits,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def _count_recent_chat_messages(conn, client_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM core.whatsapp_chat_messages
            WHERE client_id = %s AND role = 'user'
              AND created_at > now() - interval '1 hour'
            """,
            (client_id,),
        )
        return cur.fetchone()[0]


def _enqueue_chat_message(conn, client_id: str, phone: str, wamid: str | None,
                          body: str, media_id: str | None = None) -> bool:
    """Queue an inbound consultation message for whatsapp_agent_worker.py.

    media_id: id del audio en la Cloud API cuando el mensaje es una nota de
    voz — el worker lo transcribe y reemplaza body (que llega como
    placeholder). Returns False when the wamid was already seen (Meta
    redelivery) — the UNIQUE constraint turns the retry into a no-op so the
    user never gets a duplicate answer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.whatsapp_chat_messages
                (id, client_id, phone, role, content, media_id, wamid, status)
            VALUES (%s, %s, %s, 'user', %s, %s, %s, 'pending')
            ON CONFLICT (wamid) DO NOTHING
            RETURNING id
            """,
            (str(uuid.uuid4()), client_id, phone, body, media_id, wamid),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


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


def _fmt_crc(amount) -> str:
    return f"₡{float(amount):,.0f}"


def _handle_breakdown_tap(conn, from_phone: str, client_id: str) -> None:
    """Advisory weekly summary → 'Ver componentes del gasto' (Fase 5).

    The payload's client_id is only honored when the tapping phone belongs to
    that client — scope is enforced server-side, same discipline as the
    consultation chat. The reply is a deterministic free-text message inside
    the 24 h window (no template, no LLM cost); afterwards the client can keep
    asking in the AI chat.
    """
    with conn.cursor() as cur:
        cur.execute(
            r"""
            SELECT id FROM core.clients
            WHERE id = %s AND active = TRUE
              AND phone_number IS NOT NULL
              AND regexp_replace(phone_number, '\D', '', 'g') = %s
            """,
            (client_id, re.sub(r"\D", "", from_phone or "")),
        )
        if cur.fetchone() is None:
            log.warning(f"  Breakdown tap: phone {from_phone} does not match "
                        f"client {client_id} — ignoring")
            return

    today = datetime.now(CR_TZ).date()
    month_start = today.replace(day=1)
    rows = get_top_spending(
        conn, individual_id=client_id,
        date_from=month_start, date_to=today, limit=BREAKDOWN_MAX_ROWS,
    )
    if not rows:
        whatsapp_client.send_text(
            from_phone, "Aún no hay gastos registrados este mes.", preview_url=False
        )
        return

    total = get_income_expense_summary(
        conn, individual_id=client_id, date_from=month_start, date_to=today,
    )["gastos"]

    lines = [f"📊 *Componentes del gasto — {MESES_ES[today.month - 1]}*", ""]
    listed = 0.0
    for r in rows:
        label = (f"{r['category']} / {r['subcategory']}"
                 if r["subcategory"] else (r["category"] or "Sin categoría"))
        lines.append(f"• {label}: {_fmt_crc(r['total'])} ({r['share_pct']:.0f}%)")
        listed += r["total"]
    resto = total - listed
    if resto > 0.5:  # more categories than the cap — aggregate the tail
        lines.append(f"• Resto: {_fmt_crc(resto)}")
    lines += ["", f"Total del mes: {_fmt_crc(total)}"]

    whatsapp_client.send_text(from_phone, "\n".join(lines), preview_url=False)


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
            # Template quick-reply tap (Reclasificar / Ir a aplicación /
            # Ver componentes del gasto)
            payload = (msg.get("button") or {}).get("payload", "")
            text = (msg.get("button") or {}).get("text", "")
            parsed = _parse_button_payload(payload)
            if not parsed:
                log.info(f"  Unrecognized button payload: {payload!r}")
                return
            if parsed["action"] == "ad":
                _handle_breakdown_tap(conn, from_phone, parsed["client_id"])
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
                    if parsed["action"] == "ad":
                        _handle_breakdown_tap(conn, from_phone, parsed["client_id"])
                        return
                    nid = parsed["notification_id"]
                    _update_whatsapp_action(conn, nid, btn.get("title") or parsed["action"])
                    if parsed["action"] == "rc":
                        _send_reclassify_list(conn, from_phone, nid)
                    elif parsed["action"] == "go":
                        _send_webapp_url(conn, from_phone, nid)
        elif msg_type == "text":
            # Consultation chat: queue only — the agent runs in
            # whatsapp_agent_worker.py so the LLM never blocks this request.
            body = ((msg.get("text") or {}).get("body") or "").strip()
            if not body:
                return
            client = _fetch_client_by_phone(conn, from_phone)
            if not client:
                log.info("  WA text from unregistered/inactive number — ignoring")
                return
            if _count_recent_chat_messages(conn, str(client["id"])) >= CHAT_RATE_LIMIT_PER_HOUR:
                log.info(f"  WA chat rate limit hit for client={client['id']}")
                whatsapp_client.send_text(from_phone, CHAT_RATE_LIMIT_REPLY, preview_url=False)
                return
            if _enqueue_chat_message(conn, str(client["id"]), from_phone, msg.get("id"), body):
                log.info(f"  WA chat message queued for client={client['id']}")
            else:
                log.info("  WA chat message already queued (wamid seen) — ignoring redelivery")

        elif msg_type == "audio":
            # Nota de voz → misma cola que el texto, con media_id; el worker la
            # descarga y transcribe (el webhook nunca espera a Whisper).
            media_id = (msg.get("audio") or {}).get("id")
            if not media_id:
                return
            client = _fetch_client_by_phone(conn, from_phone)
            if not client:
                log.info("  WA audio from unregistered/inactive number — ignoring")
                return
            if _count_recent_chat_messages(conn, str(client["id"])) >= CHAT_RATE_LIMIT_PER_HOUR:
                log.info(f"  WA chat rate limit hit for client={client['id']}")
                whatsapp_client.send_text(from_phone, CHAT_RATE_LIMIT_REPLY, preview_url=False)
                return
            if _enqueue_chat_message(conn, str(client["id"]), from_phone,
                                     msg.get("id"), "[nota de voz]", media_id=media_id):
                log.info(f"  WA audio queued for client={client['id']} (media={media_id})")
            else:
                log.info("  WA audio already queued (wamid seen) — ignoring redelivery")

        else:
            # Ignore other inbound types (image/audio/etc.) — out of scope for now
            log.info(f"  Ignoring inbound WA message type={msg_type}")
    finally:
        conn.close()
