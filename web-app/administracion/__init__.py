import os
import uuid
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import generate_password_hash

from db import get_connection
from utils import gen_email_forward

admin_bp = Blueprint('administracion', __name__)

INDIVIDUAL_BIZ_ID = '00000000-0000-0000-0000-000000009999'


# ── Auth ─────────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("administracion.login"))
        return f(*args, **kwargs)
    return decorated


def admin_required_api(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get("admin_authenticated"):
        return redirect(url_for("administracion.index"))

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        expected = os.environ.get("ADMIN_PASSWORD", "")
        if not expected:
            error = "ADMIN_PASSWORD no está configurado en el servidor."
        elif password == expected:
            session["admin_authenticated"] = True
            return redirect(url_for("administracion.index"))
        else:
            error = "Contraseña incorrecta."

    return render_template("administracion/login.html", error=error)


@admin_bp.route('/logout')
def logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("administracion.login"))


# ── Static ───────────────────────────────────────────────────────────────────

@admin_bp.route('/')
@admin_required
def index():
    return render_template('administracion/admin.html')


@admin_bp.route('/api/verify-password', methods=['POST'])
@admin_required_api
def verify_password():
    expected = os.environ.get('ADMIN_PASSWORD', '')
    if not expected:
        return jsonify({'error': 'ADMIN_PASSWORD not configured'}), 500
    body = request.get_json(silent=True) or {}
    if body.get('password') == expected:
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 403


# ── GET /api/clients ──────────────────────────────────────────────────────────

@admin_bp.route('/api/clients', methods=['GET'])
@admin_required_api
def get_clients():
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id, c.client_name, c.email_address, c.phone_number,
                       c.active, c.email_notification, c.whatsapp_notification,
                       c.created_at, b.name AS business_name,
                       c.email_forward, c.username, c.password_hash,
                       COALESCE(ci.enabled, FALSE) AS investment_enabled
                FROM   core.clients c
                JOIN   core.businesses b ON b.id = c.business_id
                LEFT   JOIN core.client_investment ci ON ci.client_id = c.id
                ORDER  BY c.created_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        for r in rows:
            r['id'] = str(r['id'])
            r['created_at'] = r['created_at'].isoformat() if r['created_at'] else None
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── POST /api/clients ─────────────────────────────────────────────────────────

@admin_bp.route('/api/clients', methods=['POST'])
@admin_required_api
def post_client():
    body = request.get_json()
    try:
        conn = get_connection()
        cid = str(uuid.uuid4())
        name = body['client_name']
        email_fwd = gen_email_forward(name)
        email_address = body.get('email_address', '').lower()
        username = body.get('username', '').lower()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO core.clients
                    (id, business_id, business_admin, client_name, email_address, username,
                     password_hash, phone_number, email_forward, active, email_notification, whatsapp_notification)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s)
            """, (
                cid,
                body['business_id'],
                body.get('business_admin', False),
                name,
                email_address,
                username,
                generate_password_hash(body.get('password_hash', '')),
                body.get('phone_number'),
                email_fwd,
                body.get('email_notification', False),
                body.get('whatsapp_notification', False),
            ))
            cur.execute(
                """
                INSERT INTO core.categories (id, business_id, individual_id, category, subcategory)
                SELECT %s, %s, NULL, 'Otros', NULL
                WHERE NOT EXISTS (
                    SELECT 1 FROM core.categories
                    WHERE business_id = %s
                      AND individual_id IS NULL
                      AND category = 'Otros'
                      AND subcategory IS NULL
                )
                """,
                (str(uuid.uuid4()), body['business_id'], body['business_id']),
            )
            if body.get('investment_client'):
                cur.execute(
                    """
                    INSERT INTO core.client_investment (client_id, enabled)
                    VALUES (%s, TRUE)
                    ON CONFLICT (client_id) DO UPDATE
                        SET enabled = TRUE, updated_at = now()
                    """,
                    (cid,),
                )
        conn.commit()
        conn.close()
        return jsonify({'id': cid}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── POST /api/businesses ──────────────────────────────────────────────────────

@admin_bp.route('/api/businesses', methods=['POST'])
@admin_required_api
def post_business():
    body = request.get_json()
    try:
        conn = get_connection()
        bid = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.businesses (id, name) VALUES (%s, %s)",
                (bid, body['name'])
            )
        conn.commit()
        conn.close()
        return jsonify({'id': bid}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── PATCH /api/clients/<id> ───────────────────────────────────────────────────

@admin_bp.route('/api/clients/<client_id>', methods=['PATCH'])
@admin_required_api
def patch_client(client_id):
    body = request.get_json() or {}
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            if 'active' in body:
                cur.execute(
                    "UPDATE core.clients SET active = %s WHERE id = %s",
                    (body['active'], client_id)
                )
            if 'investment_client' in body:
                cur.execute(
                    """
                    INSERT INTO core.client_investment (client_id, enabled)
                    VALUES (%s, %s)
                    ON CONFLICT (client_id) DO UPDATE
                        SET enabled = EXCLUDED.enabled, updated_at = now()
                    """,
                    (client_id, bool(body['investment_client'])),
                )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── DELETE /api/clients/<id> ──────────────────────────────────────────────────
#
# Three scenarios:
#   * individual (sentinel business)  → wipe ALL of the client's data; the
#       shared sentinel business record is preserved.
#   * empresa member, others remain   → reassign this member's transactions to a
#       surviving member (a remaining admin, else the oldest member), delete the
#       member's own categories/rules and the member row. Transactions stay.
#   * empresa member, last one        → wipe the whole business: every member's
#       transactions, categories, rules, the members, and the business record.

def _reassign_target(cur, business_id, exclude_id):
    """Pick the surviving member that inherits a deleted member's transactions:
    the oldest remaining business_admin, else the oldest remaining member."""
    cur.execute(
        """
        SELECT id, client_name FROM core.clients
        WHERE business_id = %s AND id <> %s AND business_admin = TRUE
        ORDER BY created_at ASC LIMIT 1
        """,
        (business_id, exclude_id),
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        """
        SELECT id, client_name FROM core.clients
        WHERE business_id = %s AND id <> %s
        ORDER BY created_at ASC LIMIT 1
        """,
        (business_id, exclude_id),
    )
    return cur.fetchone()


def _delete_impact(cur, client_id):
    """Return a dict describing what deleting `client_id` would do, or None."""
    cur.execute(
        """
        SELECT c.id, c.client_name, c.business_id, b.name AS business_name
        FROM core.clients c JOIN core.businesses b ON b.id = c.business_id
        WHERE c.id = %s
        """,
        (client_id,),
    )
    client = cur.fetchone()
    if not client:
        return None

    business_id = str(client['business_id'])
    if business_id == INDIVIDUAL_BIZ_ID:
        cur.execute("SELECT count(*) AS n FROM core.transactions_raw WHERE individual_id = %s", (client_id,))
        return {
            'scenario': 'individual',
            'client_name': client['client_name'],
            'business_name': None,
            'transaction_count': cur.fetchone()['n'],
            'will_delete_business': False,
            'reassign_to': None,
        }

    cur.execute("SELECT count(*) AS n FROM core.clients WHERE business_id = %s AND id <> %s", (business_id, client_id))
    remaining = cur.fetchone()['n']
    if remaining == 0:
        cur.execute("SELECT count(*) AS n FROM core.transactions_raw WHERE business_id = %s", (business_id,))
        return {
            'scenario': 'business_last',
            'client_name': client['client_name'],
            'business_name': client['business_name'],
            'transaction_count': cur.fetchone()['n'],
            'will_delete_business': True,
            'reassign_to': None,
        }

    target = _reassign_target(cur, business_id, client_id)
    cur.execute("SELECT count(*) AS n FROM core.transactions_raw WHERE individual_id = %s", (client_id,))
    return {
        'scenario': 'business_member',
        'client_name': client['client_name'],
        'business_name': client['business_name'],
        'transaction_count': cur.fetchone()['n'],
        'will_delete_business': False,
        'reassign_to': target['client_name'] if target else None,
    }


def _wipe_individual(cur, client_id):
    # Child-to-parent order so NOT NULL FKs are never violated mid-delete.
    cur.execute("DELETE FROM core.transactions_notifications WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.transactions_classified WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.transactions_enriched WHERE individual_id = %s OR assigned_individual_id = %s", (client_id, client_id))
    cur.execute("DELETE FROM core.transactions_raw WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.category_rules WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.categories WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.client_investment WHERE client_id = %s", (client_id,))
    cur.execute("DELETE FROM core.clients WHERE id = %s", (client_id,))


def _wipe_business(cur, business_id):
    cur.execute("DELETE FROM core.transactions_notifications WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.transactions_classified WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.transactions_enriched WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.transactions_raw WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.category_rules WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.categories WHERE business_id = %s", (business_id,))
    cur.execute(
        """
        DELETE FROM core.client_investment
        WHERE client_id IN (SELECT id FROM core.clients WHERE business_id = %s)
        """,
        (business_id,),
    )
    cur.execute("DELETE FROM core.clients WHERE business_id = %s", (business_id,))
    cur.execute("DELETE FROM core.businesses WHERE id = %s", (business_id,))


def _reassign_and_delete_member(cur, client_id, target_id):
    cur.execute("UPDATE core.transactions_raw SET individual_id = %s WHERE individual_id = %s", (target_id, client_id))
    cur.execute("UPDATE core.transactions_enriched SET individual_id = %s WHERE individual_id = %s", (target_id, client_id))
    cur.execute("UPDATE core.transactions_enriched SET assigned_individual_id = %s WHERE assigned_individual_id = %s", (target_id, client_id))
    cur.execute("UPDATE core.transactions_classified SET individual_id = %s WHERE individual_id = %s", (target_id, client_id))
    cur.execute("UPDATE core.transactions_notifications SET individual_id = %s WHERE individual_id = %s", (target_id, client_id))
    # The departing member's own (individual-scoped) categories/rules go away;
    # business-level ones (individual_id IS NULL) are untouched.
    cur.execute("DELETE FROM core.category_rules WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.categories WHERE individual_id = %s", (client_id,))
    cur.execute("DELETE FROM core.client_investment WHERE client_id = %s", (client_id,))
    cur.execute("DELETE FROM core.clients WHERE id = %s", (client_id,))


@admin_bp.route('/api/clients/<client_id>/delete-impact', methods=['GET'])
@admin_required_api
def delete_impact(client_id):
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                impact = _delete_impact(cur, client_id)
        finally:
            conn.close()
        if impact is None:
            return jsonify({'error': 'client not found'}), 404
        return jsonify(impact)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/clients/<client_id>', methods=['DELETE'])
@admin_required_api
def delete_client(client_id):
    body = request.get_json(silent=True) or {}
    expected = os.environ.get('ADMIN_PASSWORD', '')
    if not expected:
        return jsonify({'error': 'ADMIN_PASSWORD not configured'}), 500
    if body.get('password') != expected:
        return jsonify({'error': 'forbidden'}), 403

    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, business_id FROM core.clients WHERE id = %s", (client_id,))
                client = cur.fetchone()
                if not client:
                    return jsonify({'error': 'client not found'}), 404

                business_id = str(client['business_id'])
                if business_id == INDIVIDUAL_BIZ_ID:
                    _wipe_individual(cur, client_id)
                    result = 'individual_deleted'
                else:
                    cur.execute(
                        "SELECT count(*) AS n FROM core.clients WHERE business_id = %s AND id <> %s",
                        (business_id, client_id),
                    )
                    if cur.fetchone()['n'] == 0:
                        _wipe_business(cur, business_id)
                        result = 'business_deleted'
                    else:
                        target = _reassign_target(cur, business_id, client_id)
                        _reassign_and_delete_member(cur, client_id, str(target['id']))
                        result = 'member_deleted'
            conn.commit()
        finally:
            conn.close()
        return jsonify({'ok': True, 'result': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
