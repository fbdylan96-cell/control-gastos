import os
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

_CR_TZ = ZoneInfo("America/Costa_Rica")


def get_connection():
    if os.environ.get("IS_PROD_DB", "0").strip() == "1":
        return psycopg2.connect(os.environ["DB_PROD_URL"])
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_notifications_past_window(conn):
    """Return notifications where the 24hr window is closed and rules not yet ingested."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                n.id            AS notification_id,
                n.individual_id,
                n.business_id,
                n.final_category,
                n.final_subcategory,
                n.reclassified_by,
                cl.merchant,
                cl.classified_by,
                e.merchant_guess
            FROM core.transactions_notifications n
            JOIN core.transactions_classified cl ON cl.id  = n.classified_id
            JOIN core.transactions_enriched   e  ON e.raw_id = cl.raw_id
            WHERE n.email_notified     = TRUE
              AND n.email_notified_at  < NOW() - INTERVAL '24 hours'
              AND n.rule_processing    = FALSE
              AND n.final_category     IS NOT NULL
              AND e.transaction_status != 'Descartado'
            ORDER BY n.email_notified_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_category_rule(conn, row):
    """Upsert one row into core.category_rules. Updates category/subcategory on conflict."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.category_rules (
                id, business_id, individual_id,
                merchant_key, merchant_raw,
                category, subcategory,
                source, confidence
            ) VALUES (
                %(id)s, %(business_id)s, %(individual_id)s,
                %(merchant_key)s, %(merchant_raw)s,
                %(category)s, %(subcategory)s,
                %(source)s, %(confidence)s
            )
            ON CONFLICT ON CONSTRAINT uq_rule DO UPDATE
                SET category     = EXCLUDED.category,
                    subcategory  = EXCLUDED.subcategory,
                    merchant_raw = EXCLUDED.merchant_raw,
                    source       = EXCLUDED.source,
                    confidence   = EXCLUDED.confidence,
                    updated_at   = now()
            """,
            row,
        )
    conn.commit()


def mark_rule_processed(conn, notification_id: str):
    """Mark a notification as ingested into category_rules."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.transactions_notifications SET rule_processing = TRUE WHERE id = %s",
            (notification_id,),
        )
    conn.commit()


def update_reclassification(conn, notification_id: str, category: str, subcategory: str):
    action_value = f"{category} / {subcategory}" if subcategory else category
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.transactions_notifications
            SET final_category     = %s,
                final_subcategory  = %s,
                reclassified_by    = 'user',
                reclassified_at    = now(),
                email_action_at    = now(),
                email_action_value = %s
            WHERE id = %s
            """,
            (category, subcategory or None, action_value, notification_id),
        )
    conn.commit()


def compute_amount_local(conn, amount, currency):
    """Convert `amount` in `currency` to CRC. Returns (amount_local, fx_rate, fx_rate_date).

    CRC is identity (fx_rate=1). For USD/EUR uses the latest core.exchange_rates
    cross-section (amount_local = amount * rate_vs_usd[CRC] / rate_vs_usd[currency]).
    Returns (None, None, None) when conversion isn't possible.
    """
    if amount is None or not currency:
        return None, None, None
    cg = currency.upper()
    if cg == "CRC":
        return round(float(amount), 2), 1.0, date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (SELECT MAX(rate_date) AS d FROM core.exchange_rates)
            SELECT crc.rate_vs_usd, fx.rate_vs_usd, latest.d
            FROM latest
            LEFT JOIN core.exchange_rates crc ON crc.rate_date = latest.d AND crc.currency = 'CRC'
            LEFT JOIN core.exchange_rates fx  ON fx.rate_date  = latest.d AND fx.currency  = %s
            """,
            (cg,),
        )
        row = cur.fetchone()
    if not row or row[2] is None:
        return None, None, None
    crc_rate, fx_src, rate_date = row
    if crc_rate is None or fx_src is None or float(fx_src) == 0:
        return None, None, None
    fx_rate = float(crc_rate) / float(fx_src)
    return round(float(amount) * fx_rate, 2), fx_rate, rate_date


# ── Investment / brokerage access (core.client_investment) ────────────────────

def get_investment(conn, client_id):
    """Return the client's investment row as a dict, or None if absent."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT client_id, enabled, provider,
                   token_cipher, token_nonce, key_version,
                   connected_at, revoked_at, last_used_at
            FROM core.client_investment
            WHERE client_id = %s
            """,
            (str(client_id),),
        )
        return cur.fetchone()


def set_investment_enabled(conn, client_id, enabled):
    """Upsert the investment gate flag for a client (idempotent).

    Disabling also wipes any stored broker token so a stale credential never
    outlives the service it belongs to.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.client_investment (client_id, enabled)
            VALUES (%s, %s)
            ON CONFLICT (client_id) DO UPDATE
                SET enabled      = EXCLUDED.enabled,
                    token_cipher = CASE WHEN EXCLUDED.enabled
                                        THEN core.client_investment.token_cipher END,
                    token_nonce  = CASE WHEN EXCLUDED.enabled
                                        THEN core.client_investment.token_nonce END,
                    revoked_at   = CASE WHEN EXCLUDED.enabled
                                        THEN core.client_investment.revoked_at
                                        ELSE now() END,
                    updated_at   = now()
            """,
            (str(client_id), bool(enabled)),
        )
    conn.commit()


def store_broker_token(conn, client_id, token_cipher, token_nonce):
    """Persist an encrypted broker token; clears any prior revocation.

    Only stores when the client's investment row exists AND is enabled —
    guards against the admin disabling the service mid-OAuth-flow. Returns
    True when the token was actually stored.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.client_investment
            SET token_cipher = %s, token_nonce = %s,
                connected_at = now(), revoked_at = NULL, updated_at = now()
            WHERE client_id = %s AND enabled = TRUE
            """,
            (psycopg2.Binary(token_cipher), psycopg2.Binary(token_nonce), str(client_id)),
        )
        stored = cur.rowcount
    conn.commit()
    return bool(stored)


def revoke_broker_token(conn, client_id):
    """Drop the stored token and mark the connection revoked."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.client_investment
            SET token_cipher = NULL, token_nonce = NULL,
                revoked_at = now(), updated_at = now()
            WHERE client_id = %s
            """,
            (str(client_id),),
        )
    conn.commit()


def touch_broker_token_used(conn, client_id):
    """Record that the stored token was just used to read Alpaca data."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.client_investment SET last_used_at = now() WHERE client_id = %s",
            (str(client_id),),
        )
    conn.commit()


def insert_manual_transaction(conn, *, individual_id, business_id, merchant, amount,
                              currency, txn_type, category, subcategory):
    """Insert a user-entered transaction across all four pipeline tables so it behaves
    like any ingested one. The notification row is pre-marked notified (email + WhatsApp)
    so the notifiers never send anything for it.
    """
    now_cr = datetime.now(_CR_TZ)
    raw_id = str(uuid.uuid4())
    enriched_id = str(uuid.uuid4())
    classified_id = str(uuid.uuid4())
    notif_id = str(uuid.uuid4())
    message_id = f"manual-{uuid.uuid4()}"
    body = f"Transacción manual: {merchant or ''} {currency} {amount}".strip()

    amount_local, fx_rate, fx_rate_date = compute_amount_local(conn, amount, currency)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.transactions_raw
                (id, individual_id, business_id, message_id, subject, body_text_full,
                 ms_date, local_date, label_source, month, year, year_month)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s)
            """,
            (raw_id, individual_id, business_id, message_id, "Transacción manual", body,
             int(now_cr.timestamp() * 1000), now_cr,
             now_cr.month, now_cr.year, now_cr.strftime("%Y-%m")),
        )
        cur.execute(
            """
            INSERT INTO core.transactions_enriched
                (id, raw_id, individual_id, business_id, merchant_guess, amount_guess,
                 currency_guess, desc_guess, transaction_type_guess, transaction_approval,
                 transaction_status, ai_assistance, member_detected, assigned_individual_id,
                 amount_local, currency_local, fx_rate, fx_rate_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Aprobada',
                    'Procesado', FALSE, FALSE, %s, %s, 'CRC', %s, %s)
            """,
            (enriched_id, raw_id, individual_id, business_id, merchant, amount,
             currency, merchant, txn_type, individual_id,
             amount_local, fx_rate, fx_rate_date),
        )
        cur.execute(
            """
            INSERT INTO core.transactions_classified
                (id, raw_id, individual_id, business_id, merchant, category, subcategory, classified_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
            """,
            (classified_id, raw_id, individual_id, business_id, merchant, category, subcategory),
        )
        cur.execute(
            """
            INSERT INTO core.transactions_notifications
                (id, classified_id, individual_id, business_id, final_category, final_subcategory,
                 email_notified, email_notified_at, whatsapp_notified, whatsapp_notified_at)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, now(), TRUE, now())
            """,
            (notif_id, classified_id, individual_id, business_id, category, subcategory),
        )
    conn.commit()
