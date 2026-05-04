import uuid

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, render_template, request
from werkzeug.security import generate_password_hash

from db import get_connection
from utils import gen_email_forward

admin_bp = Blueprint('administracion', __name__)

INDIVIDUAL_BIZ_ID = '00000000-0000-0000-0000-000000009999'


# ── Static ───────────────────────────────────────────────────────────────────

@admin_bp.route('/')
def index():
    return render_template('administracion/admin.html')


# ── GET /api/clients ──────────────────────────────────────────────────────────

@admin_bp.route('/api/clients', methods=['GET'])
def get_clients():
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id, c.client_name, c.email_address, c.phone_number,
                       c.active, c.email_notification, c.whatsapp_notification,
                       c.created_at, b.name AS business_name,
                       c.email_forward, c.username, c.password_hash
                FROM   core.clients c
                JOIN   core.businesses b ON b.id = c.business_id
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
def post_client():
    body = request.get_json()
    try:
        conn = get_connection()
        cid = str(uuid.uuid4())
        name = body['client_name']
        email_fwd = gen_email_forward(name)
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
                body.get('email_address', ''),
                body.get('username', ''),
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
        conn.commit()
        conn.close()
        return jsonify({'id': cid}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── POST /api/businesses ──────────────────────────────────────────────────────

@admin_bp.route('/api/businesses', methods=['POST'])
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
def patch_client(client_id):
    body = request.get_json()
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE core.clients SET active = %s WHERE id = %s",
                (body['active'], client_id)
            )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
