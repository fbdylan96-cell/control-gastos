"""
Step 5: Classification engine.

For each unclassified approved transaction in transactions_enriched:
  1. Normalize merchant_guess → merchant_key (lookup key) + merchant (display name)
  2. Look up merchant_key in core.category_rules
     → hit:  apply stored category/subcategory (classified_by = 'rules')
     → miss: call OpenAI to pick from core.categories (classified_by = 'openai')
  3. Write to core.transactions_classified
  4. Seed core.transactions_notifications with final_category / final_subcategory
"""

import json
import logging
import os
import uuid

from openai import OpenAI

from banks.utils import clean_merchant_key, format_merchant_display
from db import (
    find_category_rule,
    get_categories,
    get_unclassified_enriched,
    insert_classified_transaction,
    insert_notification_row,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global merchant→category hint catalog (used to guide AI classification)
# ---------------------------------------------------------------------------

_GLOBAL_CATALOG = """
Uber Eats -> Comida
Uber -> Transporte (si no es Uber Eats)
Apple -> Membresias
Spotify -> Membresias
Mxm -> Supermercado
Auto Mercado -> Supermercado
El Circo -> Restaurante
Ashos -> Supermercado
Fresh Market -> Supermercado
Quiznos -> Restaurante
Walmart -> Supermercado
Fischel -> Farmacia
Starbucks -> Restaurante
Picnic Patio -> Restaurante
Openai -> Membresias
Universal -> Utilizables
Rosti -> Restaurante
La Casona de Laly -> Restaurante
Rafael -> Restaurante
Simple Fresh Galery -> Restaurante
Flavorcup -> Restaurante
Spoon -> Restaurante
Hermanos M -> Servicios para Automovil
Vindi -> Supermercado
Red Fish -> Restaurante
Ato -> Servicios para Automovil
La Bomba Poz -> Farmacia
Decathlon -> Utilizables
Van Heusen -> Utilizables
Club la Guaria -> Restaurante
Fiorentin -> Restaurante
Helados Moyo -> Restaurante
Quilmes -> Restaurante
GNC -> Suplementos Alimenticios
Subway -> Restaurante
The Book Y Toy Company -> Utilizables
Tierra Bendita -> Restaurante
Servicentro Real -> Servicios para Automovil
KFC -> Restaurante
Krispy Kreme -> Restaurante
Auto Parking -> Servicios para Automovil
Shawaddi -> Restaurante
Mocapan -> Restaurante
Office Depot -> Utilizables
AM PM -> Restaurante
Dunkin -> Restaurante
Estacion de Quilmes -> Restaurante
Cafe -> Restaurante
Grass Fed -> Restaurante
Empanadas -> Restaurante
EGS LAS Palomas -> Restaurante
Libreria MTA -> Utilizables
(Utilizables = compras de no comida)
"""

# ---------------------------------------------------------------------------
# OpenAI classification
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """
You are a transaction classification assistant for a personal expense tracker in Costa Rica.
Given a merchant name (already normalized to lowercase), you must return the best matching
category and subcategory from the provided list.

Rules:
- The list shows VALID PAIRS in the format "Category / Subcategory" or just "Category" when no subcategory exists.
- You MUST return a pair exactly as it appears. NEVER combine a category with a subcategory that is not paired with it in the list.
- If an entry shows "Category / Subcategory", you must return both fields populated.
- If an entry shows only "Category" (no slash), return that category with null subcategory.
- If no pair fits, return "Otros" with null subcategory.
- NEVER invent categories or subcategories not in the list.
- Use the global examples catalog only as a hint — the final answer must be a valid pair from the list.
- Return valid JSON only, no markdown.
""".strip()


def _build_taxonomy_text(categories: list[dict]) -> str:
    """
    Format categories as explicit valid pairs so the AI cannot combine
    a category with a subcategory that doesn't belong to it.
    e.g. 'Casa / Alquiler', 'Casa / Colaboradores', 'Otros'
    """
    lines = set()
    for row in categories:
        cat = row["category"] or "Otros"
        sub = row["subcategory"]
        lines.add(f"- {cat} / {sub}" if sub else f"- {cat}")
    return "\n".join(sorted(lines))


def _classify_with_ai(merchant_key: str, categories: list[dict]) -> dict:
    """
    Call OpenAI to pick the best category/subcategory for merchant_key.
    Returns dict with keys: category, subcategory.
    Falls back to 'Otros' on any error.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set — skipping AI classification")
        return {"category": "Otros", "subcategory": None}

    taxonomy_text = _build_taxonomy_text(categories)
    user_content = (
        f"Merchant: {merchant_key}\n\n"
        f"Available categories:\n{taxonomy_text}\n\n"
        f"Global catalog hints:\n{_GLOBAL_CATALOG}"
    )

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _AI_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(response.choices[0].message.content)
        category = (data.get("category") or "Otros").strip()
        subcategory = (data.get("subcategory") or None)
        if subcategory:
            subcategory = subcategory.strip() or None

        # Validate the returned pair exists in the taxonomy
        valid_pairs = {
            (r["category"], r["subcategory"]) for r in categories
        }
        if (category, subcategory) not in valid_pairs:
            log.warning(
                f"  AI returned invalid pair ({category!r}, {subcategory!r}) — falling back to Otros"
            )
            return {"category": "Otros", "subcategory": None}

        return {"category": category, "subcategory": subcategory}
    except Exception as e:
        log.error(f"  AI classification error: {e}")
        return {"category": "Otros", "subcategory": None}


# ---------------------------------------------------------------------------
# Main classification function
# ---------------------------------------------------------------------------

def classify_transaction(conn, enriched_row: dict) -> bool:
    """
    Classify one enriched transaction and write to transactions_classified
    and transactions_notifications.
    Returns True on success, False if skipped or errored.
    """
    raw_id = str(enriched_row["raw_id"])
    individual_id = str(enriched_row["individual_id"])
    # Use assigned_individual_id so classified/notification rows belong to the actual spender
    assigned_individual_id = str(enriched_row.get("assigned_individual_id") or individual_id)
    business_id = str(enriched_row["business_id"])
    merchant_guess = enriched_row.get("merchant_guess") or ""

    # Step 1: normalize merchant
    merchant_key = clean_merchant_key(merchant_guess) if merchant_guess else ""
    merchant = format_merchant_display(merchant_guess) if merchant_guess else ""

    if not merchant_key:
        log.info(f"  raw_id={raw_id} — no merchant_guess, classifying as Otros")

    log.info(f"  raw_id={raw_id} merchant_key={merchant_key!r} assigned_to={assigned_individual_id}")

    # Step 2A: look up rule using the assigned individual's context
    rule = find_category_rule(conn, business_id, assigned_individual_id, merchant_key) if merchant_key else None

    if rule:
        category = rule["category"]
        subcategory = rule["subcategory"]
        classified_by = "rules"
        log.info(f"  Rule hit → {category} / {subcategory}")
    else:
        # Step 2B: AI classification
        log.info(f"  No rule found — calling AI")
        categories = get_categories(conn, business_id, assigned_individual_id)
        result = _classify_with_ai(merchant_key or merchant_guess or "desconocido", categories)
        category = result["category"]
        subcategory = result["subcategory"]
        classified_by = "openai"
        log.info(f"  AI result → {category} / {subcategory}")

    # Step 3: insert into transactions_classified under the assigned individual
    classified_id = str(uuid.uuid4())
    classified_row = {
        "id": classified_id,
        "raw_id": raw_id,
        "individual_id": assigned_individual_id,
        "business_id": business_id,
        "merchant": merchant or None,
        "category": category,
        "subcategory": subcategory,
        "classified_by": classified_by,
    }
    insert_classified_transaction(conn, classified_row)

    # Step 4: seed transactions_notifications under the assigned individual
    notification_row = {
        "id": str(uuid.uuid4()),
        "classified_id": classified_id,
        "individual_id": assigned_individual_id,
        "business_id": business_id,
        "final_category": category,
        "final_subcategory": subcategory,
    }
    insert_notification_row(conn, notification_row)

    return True


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_classification(conn) -> int:
    """
    Classify all pending enriched transactions.
    Returns the number of transactions processed.
    """
    rows = get_unclassified_enriched(conn)
    log.info(f"Classification: {len(rows)} transaction(s) to process")
    processed = 0
    for row in rows:
        try:
            if classify_transaction(conn, row):
                processed += 1
        except Exception as e:
            log.error(f"  Failed raw_id={row.get('raw_id')}: {e}")
    log.info(f"Classification complete: {processed}/{len(rows)} processed")
    return processed
