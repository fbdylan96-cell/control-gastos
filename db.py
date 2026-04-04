import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_active_clients(conn):
    """Return all active clients as a list of dicts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, business_id, email_forward
            FROM core.clients
            WHERE active = TRUE
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def message_exists(conn, individual_id, message_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM core.transactions_raw
            WHERE individual_id = %s AND message_id = %s
            LIMIT 1
            """,
            (str(individual_id), message_id),
        )
        return cur.fetchone() is not None


def get_unenriched_raws(conn):
    """Return transactions_raw rows that have no corresponding enriched row yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.individual_id, r.business_id, r.subject,
                   r.from_email, r.body_text_full, r.body_condensed
            FROM core.transactions_raw r
            WHERE NOT EXISTS (
                SELECT 1 FROM core.transactions_enriched e WHERE e.raw_id = r.id
            )
            ORDER BY r.created_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_enriched_transaction(conn, row):
    """Insert one row into core.transactions_enriched. Skips if raw_id already exists."""
    sql = """
        INSERT INTO core.transactions_enriched (
            id,
            raw_id,
            individual_id,
            business_id,
            bank,
            merchant_guess,
            amount_guess,
            currency_guess,
            desc_guess,
            transaction_type_guess,
            transaction_status,
            errors
        ) VALUES (
            %(id)s,
            %(raw_id)s,
            %(individual_id)s,
            %(business_id)s,
            %(bank)s,
            %(merchant_guess)s,
            %(amount_guess)s,
            %(currency_guess)s,
            %(desc_guess)s,
            %(transaction_type_guess)s,
            %(transaction_status)s,
            %(errors)s
        )
        ON CONFLICT DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def insert_raw_transaction(conn, row):
    """
    Insert one row into core.transactions_raw.
    ON CONFLICT (individual_id, message_id) DO NOTHING handles duplicates.
    """
    sql = """
        INSERT INTO core.transactions_raw (
            id,
            individual_id,
            business_id,
            message_id,
            thread_id,
            from_email,
            to_email,
            subject,
            body_text_full,
            body_condensed,
            ms_date,
            local_date,
            label_source,
            month,
            year,
            year_month
        ) VALUES (
            %(id)s,
            %(individual_id)s,
            %(business_id)s,
            %(message_id)s,
            %(thread_id)s,
            %(from_email)s,
            %(to_email)s,
            %(subject)s,
            %(body_text_full)s,
            %(body_condensed)s,
            %(ms_date)s,
            %(local_date)s,
            %(label_source)s,
            %(month)s,
            %(year)s,
            %(year_month)s
        )
        ON CONFLICT (individual_id, message_id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()
