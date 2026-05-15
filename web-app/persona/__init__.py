import io
import uuid
from datetime import datetime, timedelta
from functools import wraps

import openpyxl
import psycopg2
import psycopg2.extras
from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, session, url_for)
from openpyxl.utils import get_column_letter
from werkzeug.security import check_password_hash

from db import get_connection

persona_bp = Blueprint('persona', __name__)

INDIVIDUAL_BIZ_ID = '00000000-0000-0000-0000-000000009999'


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session or session.get("app") != "persona":
            return redirect(url_for("persona.login"))
        return f(*args, **kwargs)
    return decorated


# ── Login / Logout ────────────────────────────────────────────────────────────

@persona_bp.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session and session.get("app") == "persona":
        return redirect(url_for("persona.transacciones"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, password_hash, business_id, client_name
                    FROM core.clients
                    WHERE username = %s
                    """,
                    (username,),
                )
                user = cur.fetchone()
        finally:
            conn.close()

        if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
            error = "Usuario o contraseña incorrectos."
        elif str(user["business_id"]) != INDIVIDUAL_BIZ_ID:
            error = "Esta cuenta pertenece a una empresa. Use el portal de Empresa."
        else:
            session.clear()
            session["user_id"] = str(user["id"])
            session["business_id"] = INDIVIDUAL_BIZ_ID
            session["client_name"] = user["client_name"]
            session["app"] = "persona"
            return redirect(url_for("persona.transacciones"))

    return render_template("persona/login.html", error=error)


@persona_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("persona.login"))


# ── Transacciones recientes ───────────────────────────────────────────────────

@persona_bp.route("/transacciones")
@login_required
def transacciones():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT te.merchant_guess,
                       te.amount_guess,
                       te.currency_guess,
                       te.transaction_type_guess,
                       tr.local_date,
                       tn.id          AS notification_id,
                       tn.final_category,
                       tn.final_subcategory
                FROM core.transactions_enriched te
                JOIN core.transactions_raw tr ON te.raw_id = tr.id
                LEFT JOIN core.transactions_classified tc ON tc.raw_id = tr.id
                LEFT JOIN core.transactions_notifications tn ON tn.classified_id = tc.id
                WHERE tr.individual_id = %s
                  AND tr.local_date >= NOW() - INTERVAL '24 hours'
                ORDER BY tr.local_date DESC
                """,
                (session["user_id"],),
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT category, subcategory
                FROM core.categories
                WHERE business_id = %s
                  AND (individual_id = %s OR individual_id IS NULL)
                ORDER BY category, subcategory NULLS FIRST
                """,
                (INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
            categories = cur.fetchall()
    finally:
        conn.close()

    return render_template("persona/transacciones.html", rows=rows, categories=categories)


@persona_bp.route("/transacciones/reclassify", methods=["POST"])
@login_required
def transacciones_reclassify():
    notification_id = request.form.get("notification_id")
    raw_value = request.form.get("category_value", "")
    parts = raw_value.split("|", 1)
    final_category = parts[0].strip() or None
    final_subcategory = parts[1].strip() or None if len(parts) > 1 else None

    if not notification_id or not final_category:
        flash("Datos inválidos.", "danger")
        return redirect(url_for("persona.transacciones"))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE core.transactions_notifications
                SET final_category    = %s,
                    final_subcategory = %s,
                    reclassified_by   = 'user',
                    reclassified_at   = NOW()
                WHERE id = %s AND individual_id = %s
                """,
                (final_category, final_subcategory, notification_id, session["user_id"]),
            )
        conn.commit()
        flash("Reclasificación guardada.", "success")
    except Exception as e:
        flash(f"Error al guardar: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("persona.transacciones"))


# ── Reportes ──────────────────────────────────────────────────────────────────

def _resolve_date_range(filter_type, date_from_str, date_to_str):
    now = datetime.now()
    if filter_type == "mes_anterior":
        last_day = now.replace(day=1) - timedelta(days=1)
        first_day = last_day.replace(day=1)
        return first_day.date(), last_day.date()
    if filter_type == "anio_actual":
        return now.replace(month=1, day=1).date(), now.date()
    if filter_type == "especifica" and date_from_str and date_to_str:
        try:
            return (
                datetime.strptime(date_from_str, "%Y-%m-%d").date(),
                datetime.strptime(date_to_str, "%Y-%m-%d").date(),
            )
        except ValueError:
            pass
    return now.replace(day=1).date(), now.date()


def _fetch_reportes(individual_id, date_from, date_to):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT tr.message_id,
                       tr.local_date,
                       te.merchant_guess,
                       te.amount_guess,
                       te.currency_guess,
                       te.transaction_type_guess,
                       tn.final_category,
                       tn.final_subcategory
                FROM core.transactions_enriched te
                JOIN core.transactions_raw tr ON te.raw_id = tr.id
                LEFT JOIN core.transactions_classified tc ON tc.raw_id = tr.id
                LEFT JOIN core.transactions_notifications tn ON tn.classified_id = tc.id
                WHERE tr.individual_id = %s
                  AND te.transaction_approval = 'Aprobada'
                  AND te.transaction_status != 'unknown'
                  AND tr.local_date::date BETWEEN %s AND %s
                ORDER BY tr.local_date DESC
                """,
                (individual_id, date_from, date_to),
            )
            return cur.fetchall()
    finally:
        conn.close()


@persona_bp.route("/reportes")
@login_required
def reportes():
    filter_type = request.args.get("filter", "mes_actual")
    date_from_str = request.args.get("date_from", "")
    date_to_str = request.args.get("date_to", "")

    date_from, date_to = _resolve_date_range(filter_type, date_from_str, date_to_str)
    rows = _fetch_reportes(session["user_id"], date_from, date_to)

    return render_template(
        "persona/reportes.html",
        rows=rows,
        filter_type=filter_type,
        date_from=str(date_from),
        date_to=str(date_to),
    )


@persona_bp.route("/reportes/download")
@login_required
def reportes_download():
    filter_type = request.args.get("filter", "mes_actual")
    date_from_str = request.args.get("date_from", "")
    date_to_str = request.args.get("date_to", "")

    date_from, date_to = _resolve_date_range(filter_type, date_from_str, date_to_str)
    rows = _fetch_reportes(session["user_id"], date_from, date_to)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transacciones"

    headers = ["Message ID", "Fecha", "Comercio", "Monto", "Moneda", "Tipo", "Categoría", "Subcategoría"]
    ws.append(headers)

    for row in rows:
        ws.append([
            row["message_id"],
            row["local_date"].strftime("%Y-%m-%d %H:%M") if row["local_date"] else "",
            row["merchant_guess"] or "",
            float(row["amount_guess"]) if row["amount_guess"] is not None else "",
            row["currency_guess"] or "",
            row["transaction_type_guess"] or "",
            row["final_category"] or "",
            row["final_subcategory"] or "",
        ])

    for i, col in enumerate(ws.columns, 1):
        max_len = max((len(str(c.value)) for c in col if c.value), default=10)
        ws.column_dimensions[get_column_letter(i)].width = min(max_len + 2, 50)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"transacciones_{date_from}_{date_to}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Categorias ────────────────────────────────────────────────────────────────

def _parse_budget(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        n = float(s)
        return n if n >= 0 else None
    except (ValueError, TypeError):
        return None


def _upsert_categories(triples, individual_id):
    """triples: iterable of (category, subcategory, monthly_budget).
    Inserts are scoped to a specific individual under the sentinel business."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for category, subcategory, budget in triples:
                budget_value = None if category == "Otros" and subcategory is None else budget
                cur.execute(
                    """
                    INSERT INTO core.categories
                        (id, business_id, individual_id, category, subcategory, monthly_budget)
                    SELECT %s, %s, %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM core.categories
                        WHERE business_id = %s
                          AND individual_id = %s
                          AND category = %s
                          AND (
                              (subcategory = %s)
                              OR (subcategory IS NULL AND %s IS NULL)
                          )
                    )
                    """,
                    (
                        str(uuid.uuid4()), INDIVIDUAL_BIZ_ID, individual_id, category, subcategory, budget_value,
                        INDIVIDUAL_BIZ_ID, individual_id, category, subcategory, subcategory,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


@persona_bp.route("/categorias", methods=["GET", "POST"])
@login_required
def categorias():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "manual":
            categories = request.form.getlist("category")
            subcategories = request.form.getlist("subcategory")
            budgets = request.form.getlist("monthly_budget")
            triples = [
                (cat.strip(), sub.strip() or None, _parse_budget(bud))
                for cat, sub, bud in zip(
                    categories,
                    subcategories,
                    budgets + [None] * (len(categories) - len(budgets)),
                )
                if cat.strip()
            ]
            if triples:
                _upsert_categories(triples, session["user_id"])
                flash("Categorías guardadas correctamente.", "success")
            else:
                flash("No se ingresaron categorías válidas.", "warning")

        elif action == "upload":
            file = request.files.get("excel_file")
            if not file or not file.filename.lower().endswith((".xlsx", ".xls")):
                flash("Por favor suba un archivo Excel válido (.xlsx).", "danger")
            else:
                try:
                    wb = openpyxl.load_workbook(file)
                    ws = wb.active
                    triples = []
                    for row in ws.iter_rows(min_row=2, values_only=True):
                        cat = str(row[0]).strip() if row[0] is not None else ""
                        sub = str(row[1]).strip() if len(row) > 1 and row[1] is not None else None
                        bud = _parse_budget(row[2]) if len(row) > 2 else None
                        if cat:
                            triples.append((cat, sub or None, bud))
                    if triples:
                        _upsert_categories(triples, session["user_id"])
                        flash(f"{len(triples)} categoría(s) importada(s) correctamente.", "success")
                    else:
                        flash("El archivo no contiene categorías válidas.", "warning")
                except Exception as e:
                    flash(f"Error al procesar el archivo: {e}", "danger")

        return redirect(url_for("persona.categorias"))

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, category, subcategory, individual_id, monthly_budget
                FROM core.categories
                WHERE business_id = %s
                  AND (individual_id = %s OR individual_id IS NULL)
                ORDER BY category, subcategory NULLS FIRST
                """,
                (INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
            existing = cur.fetchall()
    finally:
        conn.close()

    return render_template("persona/categorias.html", existing=existing)


@persona_bp.route("/categorias/delete/<categoria_id>", methods=["POST"])
@login_required
def categorias_delete(categoria_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category, subcategory FROM core.categories
                WHERE id = %s AND business_id = %s AND individual_id = %s
                """,
                (categoria_id, INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
            row = cur.fetchone()
            if not row:
                flash("Categoría no encontrada.", "danger")
                return redirect(url_for("persona.categorias"))
            if row[0] == "Otros" and row[1] is None:
                flash("La categoría 'Otros' no puede ser eliminada.", "danger")
                return redirect(url_for("persona.categorias"))
            cur.execute(
                """
                DELETE FROM core.categories
                WHERE id = %s AND business_id = %s AND individual_id = %s
                """,
                (categoria_id, INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    flash("Categoría eliminada.", "success")
    return redirect(url_for("persona.categorias"))


@persona_bp.route("/categorias/<categoria_id>/budget", methods=["POST"])
@login_required
def categorias_update_budget(categoria_id):
    budget = _parse_budget(request.form.get("monthly_budget"))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT category, subcategory FROM core.categories
                WHERE id = %s AND business_id = %s AND individual_id = %s
                """,
                (categoria_id, INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
            row = cur.fetchone()
            if not row:
                flash("Categoría no encontrada.", "danger")
                return redirect(url_for("persona.categorias"))
            if row[0] == "Otros" and row[1] is None:
                flash("La categoría 'Otros' no admite presupuesto.", "danger")
                return redirect(url_for("persona.categorias"))
            cur.execute(
                """
                UPDATE core.categories SET monthly_budget = %s
                WHERE id = %s AND business_id = %s AND individual_id = %s
                """,
                (budget, categoria_id, INDIVIDUAL_BIZ_ID, session["user_id"]),
            )
        conn.commit()
        flash("Presupuesto actualizado.", "success")
    finally:
        conn.close()
    return redirect(url_for("persona.categorias"))


@persona_bp.route("/categorias/template")
@login_required
def categorias_template():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Categorías"
    ws.append(["categoria", "subcategoria", "presupuesto"])
    ws.append(["Alimentación", "Supermercado", 150000])
    ws.append(["Alimentación", "Restaurantes", 80000])
    ws.append(["Transporte", "Combustible", 50000])
    ws.append(["Transporte", "", ""])
    ws.append(["Entretenimiento", "Cine", ""])

    for i in [1, 2, 3]:
        ws.column_dimensions[get_column_letter(i)].width = 25

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return send_file(
        buf,
        as_attachment=True,
        download_name="plantilla_categorias.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
