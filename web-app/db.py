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
                   api_key_cipher, api_secret_cipher, key_version,
                   connected_at, revoked_at, last_used_at
            FROM core.client_investment
            WHERE client_id = %s
            """,
            (str(client_id),),
        )
        return cur.fetchone()


def set_investment_enabled(conn, client_id, enabled):
    """Upsert the investment gate flag for a client (idempotent).

    Disabling also wipes any stored broker credentials so a stale credential
    never outlives the service it belongs to.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.client_investment (client_id, enabled)
            VALUES (%s, %s)
            ON CONFLICT (client_id) DO UPDATE
                SET enabled           = EXCLUDED.enabled,
                    api_key_cipher    = CASE WHEN EXCLUDED.enabled
                                             THEN core.client_investment.api_key_cipher END,
                    api_secret_cipher = CASE WHEN EXCLUDED.enabled
                                             THEN core.client_investment.api_secret_cipher END,
                    revoked_at        = CASE WHEN EXCLUDED.enabled
                                             THEN core.client_investment.revoked_at
                                             ELSE now() END,
                    updated_at        = now()
            """,
            (str(client_id), bool(enabled)),
        )
    conn.commit()


def store_broker_credentials(conn, client_id, api_key_cipher, api_secret_cipher):
    """Persist the client's encrypted Alpaca API key pair (admin-loaded).

    Only stores when the client's investment row exists AND is enabled —
    guards against loading credentials for a disabled service. Returns
    True when the credentials were actually stored.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.client_investment
            SET api_key_cipher = %s, api_secret_cipher = %s,
                connected_at = now(), revoked_at = NULL, updated_at = now()
            WHERE client_id = %s AND enabled = TRUE
            """,
            (psycopg2.Binary(api_key_cipher), psycopg2.Binary(api_secret_cipher),
             str(client_id)),
        )
        stored = cur.rowcount
    conn.commit()
    return bool(stored)


def clear_broker_credentials(conn, client_id):
    """Drop the stored API key pair and mark the connection revoked."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.client_investment
            SET api_key_cipher = NULL, api_secret_cipher = NULL,
                revoked_at = now(), updated_at = now()
            WHERE client_id = %s
            """,
            (str(client_id),),
        )
    conn.commit()


def touch_broker_credentials_used(conn, client_id):
    """Record that the stored credentials were just used to read Alpaca data."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.client_investment SET last_used_at = now() WHERE client_id = %s",
            (str(client_id),),
        )
    conn.commit()


# ── Billing / suscripciones (core.client_subscriptions, planes, descuentos) ───
#
# Fase 1: PayPal NO está cableado. El estado de billing se maneja en la BD:
#   * trial → prueba gratuita de 30 días (independiente de core.clients.active,
#     que sólo controla el pipeline de correo).
#   * comp  → cortesía (código 100% o marcada por el admin); sin cargos.
# Las columnas paypal_* se llenan en Fase 2.

_TRIAL_DAYS = 30


def ensure_trial_subscription(conn, client_id, trial_days=_TRIAL_DAYS):
    """Crea una suscripción de prueba (30 días) para el cliente si no tiene.

    Idempotente (client_id UNIQUE + ON CONFLICT DO NOTHING)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.client_subscriptions
                (id, client_id, status, trial_start, trial_end, current_period_end)
            VALUES (%s, %s, 'trial', now(), now() + make_interval(days => %s), NULL)
            ON CONFLICT (client_id) DO NOTHING
            """,
            (str(uuid.uuid4()), str(client_id), int(trial_days)),
        )
    conn.commit()


def get_client_subscription(conn, client_id):
    """Devuelve la suscripción del cliente unida con su plan, o None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.id, s.client_id, s.plan_id, s.status, s.trial_start, s.trial_end,
                   s.current_period_end, s.provider, s.paypal_subscription_id,
                   s.comp, s.discount_code_id, s.cancel_at_period_end,
                   p.tier, p.modality, p.name AS plan_name,
                   p.amount_crc, p.amount_usd
            FROM core.client_subscriptions s
            LEFT JOIN core.subscription_plans p ON p.id = s.plan_id
            WHERE s.client_id = %s
            """,
            (str(client_id),),
        )
        return cur.fetchone()


def list_subscription_plans(conn, tier=None, active_only=True):
    """Devuelve los planes (opcionalmente filtrados por tier), ordenados
    mensual → anual."""
    clauses, params = [], []
    if tier:
        clauses.append("tier = %s")
        params.append(tier)
    if active_only:
        clauses.append("active = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, tier, modality, name, amount_crc, amount_usd, paypal_plan_id, active
            FROM core.subscription_plans
            {where}
            ORDER BY tier, (modality = 'anual')
            """,
            params,
        )
        return cur.fetchall()


def apply_discount_code(conn, client_id, code):
    """Valida y aplica un código de descuento a la suscripción del cliente.

    Un código del 100% deja la cuenta como cortesía ('comp') sin cargos.
    Devuelve (ok: bool, mensaje: str). Todo o nada en una sola transacción.
    """
    norm = (code or "").strip().lower()
    if not norm:
        return False, "Ingrese un código."
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # FOR UPDATE serializes concurrent redemptions of the same code so the
            # max_redemptions check-then-increment can't be raced past the cap.
            # Plain FOR UPDATE (not SKIP LOCKED): the 2nd redeemer blocks until the
            # 1st commits, then re-reads the updated times_redeemed.
            cur.execute(
                """
                SELECT id, discount_pct, active, max_redemptions, times_redeemed
                FROM core.discount_codes
                WHERE lower(code) = %s
                FOR UPDATE
                """,
                (norm,),
            )
            dc = cur.fetchone()
            if not dc or not dc["active"]:
                return False, "Código inválido o inactivo."
            if (dc["max_redemptions"] is not None
                    and dc["times_redeemed"] >= dc["max_redemptions"]):
                return False, "Este código ya alcanzó su límite de usos."

            # Asegura que exista una suscripción (trial) antes de aplicar.
            cur.execute(
                """
                INSERT INTO core.client_subscriptions
                    (id, client_id, status, trial_start, trial_end)
                VALUES (%s, %s, 'trial', now(), now() + interval '30 days')
                ON CONFLICT (client_id) DO NOTHING
                """,
                (str(uuid.uuid4()), str(client_id)),
            )

            # Registra la redención (idempotente por cliente + código).
            cur.execute(
                """
                INSERT INTO core.discount_redemptions (id, code_id, client_id)
                VALUES (%s, %s, %s)
                ON CONFLICT ON CONSTRAINT uq_redemption DO NOTHING
                """,
                (str(uuid.uuid4()), dc["id"], str(client_id)),
            )
            first_time = cur.rowcount == 1
            if first_time:
                cur.execute(
                    "UPDATE core.discount_codes SET times_redeemed = times_redeemed + 1 WHERE id = %s",
                    (dc["id"],),
                )

            if dc["discount_pct"] == 100:
                cur.execute(
                    """
                    UPDATE core.client_subscriptions
                    SET status = 'comp', comp = TRUE, discount_code_id = %s,
                        current_period_end = NULL, updated_at = now()
                    WHERE client_id = %s
                    """,
                    (dc["id"], str(client_id)),
                )
                msg = "¡Código aplicado! Tu cuenta queda como cortesía, sin cargos."
            else:
                cur.execute(
                    """
                    UPDATE core.client_subscriptions
                    SET discount_code_id = %s, updated_at = now()
                    WHERE client_id = %s
                    """,
                    (dc["id"], str(client_id)),
                )
                msg = f"¡Código aplicado! Descuento del {dc['discount_pct']}% guardado."
        conn.commit()
        if not first_time:
            return True, "Este código ya estaba aplicado en tu cuenta."
        return True, msg
    except Exception:
        conn.rollback()
        raise


def list_discount_codes(conn):
    """(Admin) Todos los códigos de descuento, más nuevos primero."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, code, description, discount_pct, active,
                   max_redemptions, times_redeemed, created_at
            FROM core.discount_codes
            ORDER BY created_at DESC
            """
        )
        return cur.fetchall()


def create_discount_code(conn, code, description, discount_pct, max_redemptions=None):
    """(Admin) Crea un código de descuento. Lanza ValueError si es inválido o
    ya existe."""
    norm = (code or "").strip().lower()
    if not norm:
        raise ValueError("El código no puede estar vacío.")
    try:
        pct = int(discount_pct)
    except (TypeError, ValueError):
        raise ValueError("El porcentaje debe ser un número entre 1 y 100.")
    if not (1 <= pct <= 100):
        raise ValueError("El porcentaje debe estar entre 1 y 100.")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM core.discount_codes WHERE lower(code) = %s", (norm,))
        if cur.fetchone():
            raise ValueError("Ya existe un código con ese nombre.")
        cur.execute(
            """
            INSERT INTO core.discount_codes (id, code, description, discount_pct, max_redemptions)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), norm, (description or "").strip() or None, pct, max_redemptions),
        )
    conn.commit()


def set_discount_code_active(conn, code_id, active):
    """(Admin) Activa o desactiva un código de descuento."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.discount_codes SET active = %s WHERE id = %s",
            (bool(active), str(code_id)),
        )
    conn.commit()


def set_subscription_comp(conn, client_id, comp):
    """(Admin) Marca/quita la cortesía de un cliente. Crea la fila si no existe.

    Al marcar cortesía: status='comp', comp=TRUE, sin próxima fecha de cobro.
    Al quitarla: si estaba en cortesía vuelve a 'trial'; otros estados se
    conservan.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.client_subscriptions
                (id, client_id, status, trial_start, trial_end, comp)
            VALUES (%s, %s, %s, now(), now() + interval '30 days', %s)
            ON CONFLICT (client_id) DO UPDATE
                SET comp = EXCLUDED.comp,
                    status = CASE WHEN EXCLUDED.comp THEN 'comp'
                                  WHEN core.client_subscriptions.status = 'comp' THEN 'trial'
                                  ELSE core.client_subscriptions.status END,
                    current_period_end = CASE WHEN EXCLUDED.comp THEN NULL
                                              ELSE core.client_subscriptions.current_period_end END,
                    updated_at = now()
            """,
            (str(uuid.uuid4()), str(client_id), 'comp' if comp else 'trial', bool(comp)),
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
