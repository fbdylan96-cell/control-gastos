import base64
import re
import uuid
from datetime import datetime, timezone

import pytz

BODY_FULL_MAX_CHARS = 20000
BODY_CONDENSED_MAX = 1200
CR_TZ = pytz.timezone("America/Costa_Rica")

# ---------------------------------------------------------------------------
# HTML entities
# ---------------------------------------------------------------------------

_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'",
    "&aacute;": "á", "&eacute;": "é", "&iacute;": "í",
    "&oacute;": "ó", "&uacute;": "ú", "&ntilde;": "ñ", "&uuml;": "ü",
    "&Aacute;": "Á", "&Eacute;": "É", "&Iacute;": "Í",
    "&Oacute;": "Ó", "&Uacute;": "Ú", "&Ntilde;": "Ñ", "&Uuml;": "Ü",
}

_HTML_ENTITY_RE = re.compile(
    r"&(?:nbsp|amp|lt|gt|quot|#39|aacute|eacute|iacute|oacute|uacute|ntilde|uuml"
    r"|Aacute|Eacute|Iacute|Oacute|Uacute|Ntilde|Uuml);|&#(\d+);|&#x([0-9a-fA-F]+);",
    re.IGNORECASE,
)


def _decode_html_entities(s):
    def _repl(m):
        full = m.group(0)
        if full in _HTML_ENTITIES:
            return _HTML_ENTITIES[full]
        dec = m.group(1)
        hex_ = m.group(2)
        if dec:
            try:
                return chr(int(dec))
            except Exception:
                return " "
        if hex_:
            try:
                return chr(int(hex_, 16))
            except Exception:
                return " "
        return full
    return _HTML_ENTITY_RE.sub(_repl, s)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _normalize_whitespace(s):
    s = re.sub(r"[\u2000-\u200D\uFEFF]", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _clean_and_clamp(s, max_chars):
    t = _normalize_whitespace(str(s or "")).strip()
    return t[:max_chars] if max_chars and len(t) > max_chars else t


# ---------------------------------------------------------------------------
# HTML -> plain text
# ---------------------------------------------------------------------------

def html_to_text(html):
    s = str(html or "")
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*p\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*div\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*td\s*>", " \n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*tr\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*li\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _decode_html_entities(s)
    s = _normalize_whitespace(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------------------
# MIME body extraction
# ---------------------------------------------------------------------------

def _decode_b64(data):
    if not isinstance(data, str):
        return ""
    b64 = data.replace("-", "+").replace("_", "/").replace(" ", "").replace("\n", "")
    b64 += "=" * ((4 - len(b64) % 4) % 4)
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _traverse_parts(payload, callback):
    if not payload:
        return
    callback(payload)
    for part in payload.get("parts") or []:
        _traverse_parts(part, callback)


def _has_money_pattern(text):
    if not text:
        return False
    return bool(
        re.search(r"\b(CRC|USD|EUR)\b[\s:]*[\d.,]+", text, re.IGNORECASE)
        or re.search(r"[\d.,]+[\s:]*\b(CRC|USD|EUR)\b", text, re.IGNORECASE)
        or re.search(r"₡\s*[\d.,]+", text)
        or re.search(r"\$\s*[\d.,]+", text)
    )


def extract_body_full_prefer_html(payload):
    """
    Walk the MIME tree. Prefer text/html converted to plain text,
    fall back to text/plain. Returns plain text string.
    """
    html_parts = []
    text_parts = []

    def _collect(part):
        mt = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            return
        content = _decode_b64(data)
        if not content:
            return
        if mt == "text/html":
            html_parts.append(content)
        elif mt == "text/plain":
            text_parts.append(content)

    _traverse_parts(payload, _collect)

    if html_parts:
        text = html_to_text("\n\n".join(html_parts))
        if _has_money_pattern(text):
            return _clean_and_clamp(text, BODY_FULL_MAX_CHARS)

    if text_parts:
        text = _normalize_whitespace("\n\n".join(text_parts))
        if _has_money_pattern(text):
            return _clean_and_clamp(text, BODY_FULL_MAX_CHARS)

    # Best-effort even without money pattern
    if html_parts:
        return _clean_and_clamp(html_to_text("\n\n".join(html_parts)), BODY_FULL_MAX_CHARS)
    if text_parts:
        return _clean_and_clamp(_normalize_whitespace("\n\n".join(text_parts)), BODY_FULL_MAX_CHARS)

    return ""


def condense_bank(body_text):
    """Keep only sentences/lines that contain financial keywords."""
    txt = _clean_and_clamp(body_text, BODY_FULL_MAX_CHARS)
    kw = re.compile(
        r"(monto|importe|comercio|merchant|autori|refer|tarjeta|tipo|compra|purchase|transacc"
        r"|fecha|date|usd|crc|eur|\bCRC\b|\bUSD\b|\bEUR\b|\d{1,3}([.,]\d{3})*[.,]\d{2})",
        re.IGNORECASE,
    )
    chunks = re.split(r"(?<=\.)\s+|[\r\n]", txt)
    return _clean_and_clamp(" ".join(c for c in chunks if kw.search(c)), BODY_CONDENSED_MAX)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

def read_headers(full_msg):
    headers_list = ((full_msg.get("payload") or {}).get("headers")) or []
    hmap = {h["name"].lower(): h["value"] for h in headers_list}

    def _h(name):
        return hmap.get(name.lower(), "")

    return {
        "from": _h("From"),
        "to": _h("To"),
        "delivered_to": _h("Delivered-To"),
        "subject": _h("Subject"),
        "internal_date": int(full_msg.get("internalDate") or 0),
        "thread_id": full_msg.get("threadId") or "",
        "message_id": full_msg.get("id") or "",
    }


def detect_client(headers, clients):
    """
    Match a (possibly forwarded) bank email to a client by checking To: and
    Delivered-To: against BOTH the client's username and their unique
    email_forward alias. Covers the two forwarding styles we see:
      - Gmail auto-forward: original recipient kept in To: (matches username);
        the gastos+alias destination appears in Delivered-To: (matches email_forward).
      - Outlook 'FW': To: becomes the gastos+alias address (matches email_forward),
        while the username only appears in From:/body.
    Both keys are unique full strings, so substring matching is safe.
    Returns the matching client dict or None.
    clients: list of dicts with at least 'username', 'email_forward', 'id', 'business_id'.
    """
    candidates = [
        (headers.get("to") or "").lower(),
        (headers.get("delivered_to") or "").lower(),
    ]
    for client in clients:
        keys = [
            (client.get("username") or "").lower(),
            (client.get("email_forward") or "").lower(),
        ]
        for key in keys:
            if key and any(key in c for c in candidates):
                return client
    return None


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------

def build_raw_row(full_msg, client, label_source):
    """
    Assemble the full dict that maps to core.transactions_raw column names.
    other_transaction_id and created_at are intentionally omitted
    (reserved for future use / DB default).
    """
    payload = full_msg.get("payload") or {}
    headers = read_headers(full_msg)

    body_full = extract_body_full_prefer_html(payload)
    body_condensed = condense_bank(body_full)

    ms_date = headers["internal_date"] or None
    if ms_date:
        utc_dt = datetime.fromtimestamp(ms_date / 1000, tz=timezone.utc)
        local_dt = utc_dt.astimezone(CR_TZ)
        month = local_dt.month
        year = local_dt.year
        year_month = f"{year}-{month:02d}"
    else:
        local_dt = None
        month = None
        year = None
        year_month = None

    return {
        "id": str(uuid.uuid4()),
        "individual_id": str(client["id"]),
        "business_id": str(client["business_id"]),
        "message_id": headers["message_id"],
        "thread_id": headers["thread_id"] or None,
        "from_email": headers["from"] or None,
        "to_email": headers["to"] or None,
        "subject": headers["subject"] or None,
        "body_text_full": body_full or "",
        "body_condensed": body_condensed or None,
        "ms_date": ms_date,
        "local_date": local_dt,
        "label_source": label_source,
        "month": month,
        "year": year,
        "year_month": year_month,
    }
