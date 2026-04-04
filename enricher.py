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

from banks.bac import BacParser
from banks.promerica import PromericaParser

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bank registry — add new parsers here as more banks are supported
# ---------------------------------------------------------------------------

_BANK_PARSERS = [
    BacParser(),
    PromericaParser(),
]

# Keywords used to detect which bank sent the email.
# Searched in: subject + from_email + body_condensed (case-insensitive).
_BANK_KEYWORDS = {
    "bac": [r"\bbac\b", r"baccredomatic", r"notificacionesbaccr"],
    "promerica": [r"prom[eé]rica"],
    "davibank": [r"davibank", r"davivienda"],
}

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_bank(subject: str, from_email: str, body_condensed: str) -> str:
    text = " ".join([subject or "", from_email or "", body_condensed or ""])
    for bank, patterns in _BANK_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return bank
    return "unknown"


def detect_transaction_type(subject: str, body_condensed: str) -> str:
    text = f"{subject or ''} {body_condensed or ''}".lower()

    debit_patterns = [r"\bcompra\b", r"\bgasto\b", r"\bd[eé]bito\b"]
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
- For merchant_guess: the store or payee name, in title case.
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
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_raw(raw_row: dict) -> dict:
    """
    Takes a transactions_raw row dict and returns a transactions_enriched row dict
    ready for DB insert (without id, inserted_at, created_at — those are set here or by DB).
    """
    subject = raw_row.get("subject") or ""
    from_email = raw_row.get("from_email") or ""
    body_text = raw_row.get("body_text_full") or ""
    body_condensed = raw_row.get("body_condensed") or ""

    bank = detect_bank(subject, from_email, body_condensed)
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
    needs_ai = bank == "unknown" or not all([merchant, amount, currency, desc])
    ai_enabled = os.environ.get("AI_ASSISTANCE", "0") == "1"

    if needs_ai and ai_enabled:
        log.info("  Falling back to OpenAI")
        ai = _openai_fallback(subject, from_email, body_condensed)
        if ai:
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
            "individual_id": str(raw_row["individual_id"]),
            "business_id": str(raw_row["business_id"]),
            "bank": bank if bank != "unknown" else None,
                "merchant_guess": merchant,
            "amount_guess": amount,
            "currency_guess": currency,
            "desc_guess": desc,
            "transaction_type_guess": txn_type,
            "transaction_status": status,
            "errors": None,
        }

    status = determine_status(merchant, amount, currency, desc)
    log.info(f"  status={status} merchant={merchant!r} amount={amount} currency={currency}")

    return {
        "id": str(uuid.uuid4()),
        "raw_id": str(raw_row["id"]),
        "individual_id": str(raw_row["individual_id"]),
        "business_id": str(raw_row["business_id"]),
        "bank": bank if bank != "unknown" else None,
        "merchant_guess": merchant,
        "amount_guess": amount,
        "currency_guess": currency,
        "desc_guess": desc,
        "transaction_type_guess": txn_type,
        "transaction_status": status,
        "errors": None,
    }
