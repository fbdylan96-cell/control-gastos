import os
import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

_CR_TZ = ZoneInfo("America/Costa_Rica")


def today_cr():
    """Fecha de hoy en Costa Rica (el server corre en UTC: de 6pm a medianoche
    hora CR, date.today() ya es 'mañana' — usar siempre esta para validar)."""
    return datetime.now(_CR_TZ).date()


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


# ── Base de datos de la asesoría (asesoria_db) ────────────────────────────────
# Separada de la de Neto app por diseño: los diagnósticos son datos de
# prospectos (no de clientes) y alimentan los reportes del servicio de
# asesoría. Esquema en asesoria_schema.sql (raíz del repo).

def get_asesoria_connection():
    if os.environ.get("IS_PROD_DB", "0").strip() == "1":
        return psycopg2.connect(os.environ["ASESORIA_DB_URL"])
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ.get("ASESORIA_DB_NAME", "asesoria_db"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def save_diagnostico(payload, ip=None, payload_raw=None, corregido_de=None):
    """Persist a sanitized /diagnostico submission in asesoria_db.

    Stores the full post-conversion payload as JSONB plus the searchable
    columns. Returns the new row id (str). Raises on failure — the caller
    (run.py) treats persistence as best-effort so a DB hiccup never blocks
    the prospect's report.

    payload_raw: the payload as the client typed it, BEFORE the USD→CRC
    conversion (which rewrites amounts and annotates names). It is what the
    advisor's editor reloads; `payload` stays the analysis-ready version.

    corregido_de: id of the diagnostico this row corrects. A correction is
    always a new row, never an UPDATE — the original submission is evidence.
    """
    conn = get_asesoria_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO diagnosticos
                    (nombre, correo, celular, tc_usd, tc_fecha, ip, payload,
                     payload_raw, corregido_de)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (payload["nombre"], payload["correo"], payload["celular"],
                 payload.get("tc_usd"), payload.get("tc_fecha"), ip,
                 psycopg2.extras.Json(payload),
                 psycopg2.extras.Json(payload_raw) if payload_raw else None,
                 corregido_de),
            )
            diag_id = str(cur.fetchone()[0])
        conn.commit()
        return diag_id
    finally:
        conn.close()


def mark_diagnostico_sent(diag_id):
    """Flag a diagnostico row after the Excel report email went out."""
    conn = get_asesoria_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE diagnosticos SET report_sent = TRUE WHERE id = %s",
                (str(diag_id),),
            )
        conn.commit()
    finally:
        conn.close()


def get_diagnostico_by_correo(correo):
    """Latest diagnostico submission for an email (dashboard de ruta).

    Returns {'id', 'created_at', 'payload', 'total'} — total = how many
    submissions exist for that email — or None when there is none.
    """
    conn = get_asesoria_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, payload,
                       count(*) OVER () AS total
                FROM diagnosticos
                WHERE correo = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(correo or "").strip().lower(),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"id": str(row[0]), "created_at": row[1], "payload": row[2],
                "total": int(row[3])}
    finally:
        conn.close()


def get_diagnostico_para_editar(correo):
    """Latest diagnostico for an email, prepared for the advisor's editor.

    Prefers `payload_raw` — what the client actually typed, before the USD→CRC
    conversion. Falls back to `payload` for rows saved before that column
    existed; `convertido` tells the caller it must strip the " [US$ …]"
    annotations the conversion left behind. None when there is no submission.
    """
    conn = get_asesoria_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, payload, payload_raw, corregido_de,
                       count(*) OVER () AS total
                FROM diagnosticos
                WHERE correo = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(correo or "").strip().lower(),),
            )
            row = cur.fetchone()
        if not row:
            return None
        raw = row[3]
        return {"id": str(row[0]), "created_at": row[1],
                "payload": raw if raw else row[2],
                "convertido": not raw,
                "corregido_de": str(row[4]) if row[4] else None,
                "total": int(row[5])}
    finally:
        conn.close()


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

    CRC is identity (fx_rate=1). For other currencies uses the most recent
    rate_date where BOTH the CRC and the target rates are non-NULL — the same
    robustness as the pipeline's get_fx_conversion: a rate set can arrive
    incomplete (BCCR outages 2026-07-09 and 2026-07-20+ dejaron cross-sections
    con solo USD, y mirar únicamente MAX(rate_date) rompía la conversión).
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
            SELECT crc.rate_vs_usd, fx.rate_vs_usd, crc.rate_date
            FROM core.exchange_rates crc
            JOIN core.exchange_rates fx
              ON fx.rate_date = crc.rate_date
             AND fx.currency = %s
             AND fx.rate_vs_usd IS NOT NULL
            WHERE crc.currency = 'CRC'
              AND crc.rate_vs_usd IS NOT NULL
            ORDER BY crc.rate_date DESC
            LIMIT 1
            """,
            (cg,),
        )
        row = cur.fetchone()
    if not row:
        return None, None, None
    crc_rate, fx_src, rate_date = row
    if float(fx_src) == 0:
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


# ── Billing Fase 2: PayPal vivo ───────────────────────────────────────────────

def get_subscription_plan(conn, plan_id):
    """Devuelve un plan por id, o None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, tier, modality, name, amount_crc, amount_usd, paypal_plan_id, active
            FROM core.subscription_plans
            WHERE id = %s
            """,
            (str(plan_id),),
        )
        return cur.fetchone()


def get_plan_by_paypal_plan_id(conn, paypal_plan_id):
    """Devuelve el plan interno que corresponde a un Billing Plan de PayPal."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, tier, modality, name, amount_crc, amount_usd, paypal_plan_id, active
            FROM core.subscription_plans
            WHERE paypal_plan_id = %s
            """,
            (paypal_plan_id,),
        )
        return cur.fetchone()


def set_plan_paypal_id(conn, plan_id, paypal_plan_id):
    """(Bootstrap) Guarda el id del Billing Plan de PayPal en el plan interno."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.subscription_plans SET paypal_plan_id = %s WHERE id = %s",
            (paypal_plan_id, str(plan_id)),
        )
    conn.commit()


def get_business_subscription(conn, business_id):
    """Suscripción 'de la familia/empresa': la fila más relevante entre los
    miembros del negocio, o None.

    client_subscriptions es por-cliente; para familias la suscripción vive en
    la fila del admin que paga (custom_id de PayPal = ese cliente). Se busca
    por negocio para que cualquier admin vea la misma membresía y no se pueda
    duplicar: prioridad activa/cortesía > trial > cancelada.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.id, s.client_id, s.status, s.trial_start, s.trial_end,
                   s.current_period_end, s.provider, s.paypal_subscription_id,
                   s.comp, s.discount_code_id, s.cancel_at_period_end,
                   p.tier, p.modality, p.name AS plan_name,
                   p.amount_crc, p.amount_usd,
                   c.client_name AS holder_name
            FROM core.client_subscriptions s
            JOIN core.clients c ON c.id = s.client_id
            LEFT JOIN core.subscription_plans p ON p.id = s.plan_id
            WHERE c.business_id = %s
            ORDER BY (s.status IN ('active', 'past_due', 'comp')) DESC,
                     (s.status = 'trial') DESC,
                     s.updated_at DESC
            LIMIT 1
            """,
            (str(business_id),),
        )
        return cur.fetchone()


def get_client_discount_pct(conn, client_id):
    """Pct del código de descuento aplicado al cliente (1-99), o None.

    Los códigos del 100% nunca llegan aquí: apply_discount_code los convierte
    en cortesía ('comp') y la UI no ofrece suscribirse. Un código inactivo
    deja de descontar aunque siga referenciado en la suscripción.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dc.discount_pct
            FROM core.client_subscriptions s
            JOIN core.discount_codes dc
              ON dc.id = s.discount_code_id AND dc.active
            WHERE s.client_id = %s AND dc.discount_pct < 100
            """,
            (str(client_id),),
        )
        row = cur.fetchone()
        return row[0] if row else None


def activate_subscription_from_paypal(conn, client_id, plan_id,
                                      paypal_subscription_id, next_billing=None):
    """Marca la suscripción del cliente como activa vía PayPal (upsert).

    Llamada desde el retorno de aprobación y desde el webhook ACTIVATED —
    ambas rutas son idempotentes entre sí. Una cuenta de cortesía nunca se
    degrada: se registra el id de PayPal pero comp/status se conservan.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.client_subscriptions
                (id, client_id, plan_id, status, paypal_subscription_id, current_period_end)
            VALUES (%s, %s, %s, 'active', %s, %s)
            ON CONFLICT (client_id) DO UPDATE
                SET plan_id = EXCLUDED.plan_id,
                    status = CASE WHEN core.client_subscriptions.comp
                                  THEN core.client_subscriptions.status
                                  ELSE 'active' END,
                    paypal_subscription_id = EXCLUDED.paypal_subscription_id,
                    current_period_end = COALESCE(EXCLUDED.current_period_end,
                                                  core.client_subscriptions.current_period_end),
                    cancel_at_period_end = FALSE,
                    updated_at = now()
            """,
            (str(uuid.uuid4()), str(client_id), str(plan_id) if plan_id else None,
             paypal_subscription_id, next_billing),
        )
    conn.commit()


def update_subscription_from_paypal(conn, paypal_subscription_id, status,
                                    next_billing=None):
    """(Webhook) Actualiza estado/próximo cobro por paypal_subscription_id.

    next_billing=None conserva el current_period_end existente (los eventos de
    cancelación/suspensión no traen próximo cobro pero el período pagado sigue
    corriendo). Devuelve cuántas filas tocó (0 → suscripción desconocida).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE core.client_subscriptions
            SET status = %s,
                current_period_end = COALESCE(%s, current_period_end),
                cancel_at_period_end = CASE WHEN %s = 'cancelled' THEN TRUE
                                            ELSE cancel_at_period_end END,
                updated_at = now()
            WHERE paypal_subscription_id = %s
              AND comp = FALSE
            """,
            (status, next_billing, status, paypal_subscription_id),
        )
        touched = cur.rowcount
    conn.commit()
    return touched


def insert_manual_transaction(conn, *, individual_id, business_id, merchant, amount,
                              currency, txn_type, category, subcategory, txn_date=None,
                              client_notes=None):
    """Insert a user-entered transaction across all four pipeline tables so it behaves
    like any ingested one. The notification row is pre-marked notified (email + WhatsApp)
    so the notifiers never send anything for it.

    txn_date: fecha (date) elegida por el cliente. None o la fecha de hoy usan el
    timestamp actual (comportamiento original). Fechas pasadas se registran a las
    12:00 hora CR de ese día. Fechas futuras se recortan a "ahora" — la validación
    amigable vive en las rutas; esto es el respaldo.
    """
    now_cr = datetime.now(_CR_TZ)
    if txn_date is not None and txn_date < now_cr.date():
        now_cr = datetime.combine(txn_date, time(12, 0), tzinfo=_CR_TZ)
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
                 client_notes, email_notified, email_notified_at, whatsapp_notified,
                 whatsapp_notified_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, now(), TRUE, now())
            """,
            (notif_id, classified_id, individual_id, business_id, category, subcategory,
             client_notes),
        )
    conn.commit()
