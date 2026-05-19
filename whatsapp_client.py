"""
Meta WhatsApp Cloud API wrapper.

Handles outbound message sending (templates, interactive lists, plain text)
and webhook signature verification.
"""

import hashlib
import hmac
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

API_VERSION = "v20.0"


def _api_url() -> str:
    phone_id = os.environ.get("META_WA_PHONE_ID", "").strip()
    if not phone_id:
        raise RuntimeError("META_WA_PHONE_ID env var is not set")
    return f"https://graph.facebook.com/{API_VERSION}/{phone_id}/messages"


def _headers() -> dict:
    token = os.environ.get("META_WA_TOKEN", "").strip()
    if not token:
        raise RuntimeError("META_WA_TOKEN env var is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def normalize_phone(raw: str) -> str | None:
    """Validate and normalize a phone number for the Meta Cloud API.

    Requirements (per PROMPT_requests.md):
      - Must start with '+'
      - Spaces are stripped
    Meta requires digits-only (no '+'), so we drop it before returning.
    Returns None when the input is empty or invalid.
    """
    if not raw:
        return None
    no_ws = re.sub(r"\s+", "", raw)
    if not no_ws.startswith("+"):
        return None
    digits = no_ws[1:]
    if not digits.isdigit() or len(digits) < 8:
        return None
    return digits


def _post(payload: dict) -> dict:
    resp = requests.post(_api_url(), headers=_headers(), json=payload, timeout=20)
    if not resp.ok:
        log.error(f"WhatsApp send failed [{resp.status_code}]: {resp.text}")
        resp.raise_for_status()
    return resp.json()


def send_template(
    to: str,
    template_name: str,
    language_code: str,
    body_params: list[str],
    button_payloads: list[str] | None = None,
) -> dict:
    """Send an approved template message.

    body_params: ordered list of variable values for the template body ({{1}}, {{2}}, ...).
    button_payloads: ordered payloads for QUICK_REPLY buttons (index 0, 1, ...).
                     Pass None or empty list if the template has no quick-reply buttons.
    """
    components: list[dict] = [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        }
    ]
    for idx, payload in enumerate(button_payloads or []):
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(idx),
            "parameters": [{"type": "payload", "payload": payload}],
        })

    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    })


def send_list_message(
    to: str,
    header_text: str,
    body_text: str,
    button_label: str,
    sections: list[dict],
    footer_text: str | None = None,
) -> dict:
    """Send an interactive List Message.

    sections: list of {"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}, ...]}
    Meta hard limits: 10 sections, 10 rows per section, 100 rows total.
    Field caps: header 60, body 1024, footer 60, button 20, section title 24,
                row title 24, row description 72, row id 200.
    """
    interactive: dict = {
        "type": "list",
        "header": {"type": "text", "text": header_text[:60]},
        "body": {"text": body_text[:1024]},
        "action": {
            "button": button_label[:20],
            "sections": sections,
        },
    }
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}

    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    })


def send_text(to: str, body: str, preview_url: bool = True) -> dict:
    """Send a plain text message (only valid inside the 24h customer service window)."""
    return _post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": preview_url, "body": body},
    })


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify the X-Hub-Signature-256 header sent by Meta on every webhook callback."""
    secret = os.environ.get("META_WA_APP_SECRET", "").strip()
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])
