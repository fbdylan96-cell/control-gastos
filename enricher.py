"""
Step 4: Enrichment pipeline.
Reads a transactions_raw row and produces a transactions_enriched row.
"""

import json
import logging
import os
import re
import uuid

from openai import OpenAI

import db as _db
from banks.bac import BacParser
from banks.bcr import BcrParser
from banks.davibank import DavibankParser
from banks.promerica import PromericaParser
from banks.utils import clean_merchant_key

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bank registry — add new parsers here as more banks are supported
# ---------------------------------------------------------------------------

_BANK_PARSERS = [
    BacParser(),
    BcrParser(),
    DavibankParser(),
    PromericaParser(),
]

# Keywords used to detect which bank sent the email.
# Searched in: subject + from_email + body_condensed (case-insensitive).
_BANK_KEYWORDS = {
    "bac": [r"\bbac\b", r"baccredomatic", r"notificacionesbaccr"],
    "bcr": [r"bancobcr", r"banco\s+bcr", r"\bbcr\b"],
    "promerica": [r"prom[eé]rica"],
    "davibank": [r"davibank", r"davivienda"],
}

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_bank(subject: str, from_email: str, body_text_full: str, body_condensed: str) -> str:
    text = " ".join([subject or "", from_email or "", body_condensed or "", body_text_full or ""])
    for bank, patterns in _BANK_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return bank
    return "unknown"


def detect_approval(subject: str, body_text_full: str) -> str:
    text = f"{subject or ''} {body_text_full or ''}"
    if re.search(r"deneg", text, re.IGNORECASE):
        return "Denegada"
    return "Aprobada"


def detect_transaction_type(subject: str, body_condensed: str) -> str:
    text = f"{subject or ''} {body_condensed or ''}".lower()

    debit_patterns = [r"\bcompra\b", r"\bgasto\b", r"\bd[eé]bito\b", r"\bdebitado\b", r"\bdebit[oó]\b", r"\btarjeta BCR\b"]
    credit_patterns = [r"\bcr[eé]dito\b", r"\brecibido\b"]

    for pat in debit_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return "debito"
    for pat in credit_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return "credito"
    return "unknown"


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------

def determine_status(merchant, amount, currency, desc) -> str:
    all_fields = [merchant, amount, currency, desc]
    populated = [f for f in all_fields if f is not None and str(f).strip() not in ("", "unknown")]

    if len(populated) == 4:
        return "Procesado"
    if amount is not None:
        return "Procesado parcialmente"
    return "Descartado"


# ---------------------------------------------------------------------------
# OpenAI fallback
# ---------------------------------------------------------------------------

_OPENAI_SYSTEM_PROMPT = """
You are a financial data extraction assistant for a personal expense tracking tool in Costa Rica.
You will receive bank notification emails (subject, sender, and condensed body text) and must extract transaction data.

Rules:
- Only extract data that is explicitly stated in the email. Do NOT invent or guess data.
- If the email is not a financial transaction notification (e.g. marketing, welcome messages, account statements without a specific transaction), set "is_transaction" to false.
- For merchant_guess: the store or payee name, in title case. NEVER use a bank name as the merchant (e.g. BAC, Baccredomatic, BCR, Banco de Costa Rica, Promerica, DAVIbank, Davivienda). If the payee is a bank, set merchant_guess to null.
- For amount_guess: numeric amount only as a float (e.g. 15000.00). No currency symbols.
- For currency_guess: use exactly "CRC", "USD", or "EUR". Null if unknown.
- For desc_guess: a short human-readable description (e.g. "Compra en Auto Mercado").
- If a field cannot be determined, use null.

Respond with valid JSON only, no markdown:
{
  "is_transaction": true,
  "merchant_guess": "...",
  "amount_guess": 1234.56,
  "currency_guess": "CRC",
  "desc_guess": "..."
}
""".strip()


def _openai_fallback(subject: str, from_email: str, body_condensed: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set — skipping AI fallback")
        return {}

    client = OpenAI(api_key=api_key)
    user_content = (
        f"Subject: {subject or ''}\n"
        f"From: {from_email or ''}\n"
        f"Body:\n{body_condensed or ''}"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _OPENAI_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(response.choices[0].message.content)

        if not data.get("is_transaction", True):
            log.info("  OpenAI: not a transaction — will be Descartado")
            return {}

        amount = data.get("amount_guess")
        if amount is not None:
            try:
                amount = round(float(amount), 2)
            except (TypeError, ValueError):
                amount = None

        return {
            "merchant_guess": data.get("merchant_guess") or None,
            "amount_guess": amount,
            "currency_guess": data.get("currency_guess") or None,
            "desc_guess": data.get("desc_guess") or None,
        }
    except Exception as e:
        log.error(f"  OpenAI fallback error: {e}")
        return {}


# ---------------------------------------------------------------------------
# Business member detection
# ---------------------------------------------------------------------------

_NAME_STOPWORDS = {"de", "la", "el", "los", "las", "del", "al", "y", "e", "o", "en"}


def _detect_member(body_text_full: str, members: list) -> dict | None:
    """
    Search body_text_full for a business member's name.
    Returns the best-matching member dict or None.

    Threshold: matches >= 2 AND matches > len(meaningful_words) / 2.
    Meaningful words = name tokens after removing stopwords and single-char tokens.
    On a tie between two members, returns None (ambiguous).
    """
    body_norm = clean_merchant_key(body_text_full or "")
    best_member = None
    best_count = 0
    tied = False

    for member in members:
        name_norm = clean_merchant_key(member.get("client_name") or "")
        words = [
            w for w in name_norm.split()
            if w not in _NAME_STOPWORDS and len(w) > 1
        ]
        if not words:
            continue

        matches = sum(
            1 for w in words
            if re.search(rf"\b{re.escape(w)}\b", body_norm)
        )

        if matches >= 2 and matches * 2 > len(words):
            if matches > best_count:
                best_count = matches
                best_member = member
                tied = False
            elif matches == best_count:
                tied = True

    return None if tied else best_member


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_raw(raw_row: dict, conn=None, client: dict | None = None) -> dict:
    """
    Takes a transactions_raw row dict and returns a transactions_enriched row dict
    ready for DB insert (without id, inserted_at, created_at — those are set here or by DB).
    """
    subject = raw_row.get("subject") or ""
    from_email = raw_row.get("from_email") or ""
    body_text = raw_row.get("body_text_full") or ""
    body_condensed = raw_row.get("body_condensed") or ""
    individual_id = str(raw_row["individual_id"])
    business_id = str(raw_row["business_id"])

    approval = detect_approval(subject, body_text)

    # Member detection — only for business admins when conn and client are provided
    member_detected = False
    assigned_individual_id = individual_id
    if conn and client and client.get("business_admin"):
        members = _db.get_business_members(conn, business_id, individual_id)
        match = _detect_member(body_text, members)
        if match:
            assigned_individual_id = str(match["id"])
            member_detected = True
            log.info(f"  Member detected: {match['client_name']} → assigned_individual_id={assigned_individual_id}")

    # Denied transactions: mark as Procesado and skip all extraction
    if approval == "Denegada":
        log.info("  Transaction denied — skipping extraction")
        return {
            "id": str(uuid.uuid4()),
            "raw_id": str(raw_row["id"]),
            "individual_id": individual_id,
            "business_id": business_id,
            "bank": None,
            "merchant_guess": None,
            "amount_guess": None,
            "currency_guess": None,
            "desc_guess": None,
            "transaction_type_guess": "unknown",
            "transaction_approval": "Denegada",
            "transaction_status": "Procesado",
            "ai_assistance": False,
            "member_detected": member_detected,
            "assigned_individual_id": assigned_individual_id,
            "errors": None,
        }

    bank = detect_bank(subject, from_email, body_text, body_condensed)
    txn_type = detect_transaction_type(subject, body_condensed)

    log.info(f"  Enriching: bank={bank} type={txn_type}")

    # Try bank-specific parser
    fields = {}
    parser = next((p for p in _BANK_PARSERS if p.can_handle(bank)), None)
    if parser:
        try:
            fields = parser.parse(subject, body_text, body_condensed)
        except Exception as e:
            log.error(f"  Parser error for bank={bank}: {e}")

    merchant = fields.get("merchant_guess")
    amount = fields.get("amount_guess")
    currency = fields.get("currency_guess")
    desc = fields.get("desc_guess")

    # Use OpenAI if bank unknown OR any field still missing
    needs_ai = bank == "unknown" or not all([merchant, amount is not None, currency, desc])
    ai_enabled = os.environ.get("AI_ASSISTANCE", "0") == "1"
    ai_used = False

    if needs_ai and ai_enabled:
        log.info("  Falling back to OpenAI")
        ai = _openai_fallback(subject, from_email, body_condensed)
        if ai:
            ai_used = True
            merchant = merchant or ai.get("merchant_guess")
            amount = amount or ai.get("amount_guess")
            currency = currency or ai.get("currency_guess")
            desc = desc or ai.get("desc_guess")
    elif needs_ai and not ai_enabled:
        log.info("  AI assistance is OFF — transaction_status will remain 'unknown'")
        status = "unknown"
        log.info(f"  status={status} merchant={merchant!r} amount={amount} currency={currency}")
        return {
            "id": str(uuid.uuid4()),
            "raw_id": str(raw_row["id"]),
            "individual_id": individual_id,
            "business_id": business_id,
            "bank": bank if bank != "unknown" else None,
            "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
            "transaction_type_guess": txn_type,
            "transaction_approval": approval,
            "transaction_status": status,
            "ai_assistance": False,
            "member_detected": member_detected,
            "assigned_individual_id": assigned_individual_id,
            "errors": None,
        }

    status = determine_status(merchant, amount, currency, desc)
    log.info(f"  status={status} merchant={merchant!r} amount={amount} currency={currency}")

    return {
        "id": str(uuid.uuid4()),
        "raw_id": str(raw_row["id"]),
        "individual_id": individual_id,
        "business_id": business_id,
        "bank": bank if bank != "unknown" else None,
        "merchant_guess": merchant,
        "amount_guess": amount,
        "currency_guess": currency,
        "desc_guess": desc,
        "transaction_type_guess": txn_type,
        "transaction_approval": approval,
        "transaction_status": status,
        "ai_assistance": ai_used,
        "member_detected": member_detected,
        "assigned_individual_id": assigned_individual_id,
        "errors": None,
    }
