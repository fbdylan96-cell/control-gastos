"""Diagnóstico Financiero: generación del reporte Excel y envío por correo.

Los datos vienen del formulario público /diagnostico/ (sin login) y NO se
guardan en la base de datos: se arma un .xlsx en memoria y se envía por SMTP
(neto@investorcr.com) al cliente y al asesor.

Variables de entorno requeridas (.env de la raíz, ya presentes en el server):
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD
"""

import io
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ADVISOR_EMAIL = os.environ.get("DIAGNOSTICO_ADVISOR_EMAIL", "dylanmos96@gmail.com")

_CR_TZ = ZoneInfo("America/Costa_Rica")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Límites de sanitización (el endpoint es público)
_MAX_ROWS = 100
_MAX_TEXT = 300
_MAX_NOTAS = 2000

# Estilos neto
_INK = "1B1C20"
_TEAL = "3A9C8E"
_FILL_SECTION = PatternFill("solid", fgColor=_INK)
_FILL_SUBTOTAL = PatternFill("solid", fgColor="E2E6EB")
_FONT_TITLE = Font(name="Calibri", size=14, bold=True, color=_INK)
_FONT_SECTION = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
_FONT_BOLD = Font(name="Calibri", size=11, bold=True)
_CRC_FMT = '"₡"#,##0'
_THIN = Border(bottom=Side(style="thin", color="E2E6EB"))


def _clean_text(value, max_len=_MAX_TEXT):
    return str(value or "").strip()[:max_len]


def _clean_amount(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if n != n or n in (float("inf"), float("-inf")) or n < 0:
        return 0.0
    return min(n, 1e12)


def _clean_rows(raw, fields=("amount",)):
    """Normaliza una lista [{name, <montos>}] recortando filas y valores."""
    rows = []
    if not isinstance(raw, list):
        return rows
    for item in raw[:_MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        row = {"name": _clean_text(item.get("name"))}
        for f in fields:
            row[f] = _clean_amount(item.get(f))
        if row["name"] or any(row[f] for f in fields):
            rows.append(row)
    return rows


def sanitize_payload(data):
    """Valida y normaliza el JSON del formulario. Devuelve (payload, error)."""
    if not isinstance(data, dict):
        return None, "Datos inválidos."

    nombre = _clean_text(data.get("nombre"), 120)
    correo = _clean_text(data.get("correo"), 200).lower()
    celular = _clean_text(data.get("celular"), 40)

    if len(nombre) < 3:
        return None, "Ingrese su nombre completo."
    if not _EMAIL_RE.match(correo):
        return None, "Ingrese un correo electrónico válido."
    if len(re.sub(r"\D", "", celular)) < 8:
        return None, "Ingrese un número celular válido (mínimo 8 dígitos)."

    payload = {
        "nombre": nombre,
        "correo": correo,
        "celular": celular,
        "fijos": _clean_rows(data.get("fijos")),
        "variables": _clean_rows(data.get("variables")),
        "hormiga": _clean_rows(data.get("hormiga")),
        "ingresos": _clean_rows(data.get("ingresos")),
        "activos": _clean_rows(data.get("activos")),
        "deudas": _clean_rows(data.get("deudas"), fields=("saldo", "tasa", "cuota")),
        "fe_tiene": _clean_text(data.get("fe_tiene"), 10),
        "fe_monto": _clean_amount(data.get("fe_monto")),
        "seguros": [_clean_text(s, 60) for s in (data.get("seguros") or [])[:20]
                    if _clean_text(s, 60)],
        "retiro": _clean_text(data.get("retiro"), 120),
        "metas": [_clean_text(m) for m in (data.get("metas") or [])[:3]],
        "notas": _clean_text(data.get("notas"), _MAX_NOTAS),
    }
    return payload, None


def _totales(p):
    tf = sum(r["amount"] for r in p["fijos"])
    tv = sum(r["amount"] for r in p["variables"])
    th = sum(r["amount"] for r in p["hormiga"])
    ti = sum(r["amount"] for r in p["ingresos"])
    ta = sum(r["amount"] for r in p["activos"])
    td = sum(r["saldo"] for r in p["deudas"])
    tc = sum(r["cuota"] for r in p["deudas"])
    gastos = tf + tv + th + tc
    return {
        "fijos": tf, "variables": tv, "hormiga": th, "ingresos": ti,
        "activos": ta, "deudas": td, "cuotas": tc, "gastos": gastos,
        "flujo": ti - gastos, "patrimonio": ta - td,
    }


def build_excel(p):
    """Arma el .xlsx del diagnóstico en memoria y devuelve los bytes."""
    t = _totales(p)
    fecha = datetime.now(_CR_TZ)

    wb = Workbook()
    ws = wb.active
    ws.title = "Diagnóstico"
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 16
    ws.sheet_view.showGridLines = False

    row = 1

    def put(col, r, value, font=None, fmt=None, fill=None, align=None):
        cell = ws.cell(row=r, column=col, value=value)
        if font:
            cell.font = font
        if fmt:
            cell.number_format = fmt
        if fill:
            cell.fill = fill
        if align:
            cell.alignment = Alignment(horizontal=align)
        return cell

    def section(title):
        nonlocal row
        row += 1
        for c in range(1, 5):
            put(c, row, None, fill=_FILL_SECTION)
        put(1, row, title, font=_FONT_SECTION, fill=_FILL_SECTION)
        row += 1

    def item(label, value, fmt=_CRC_FMT, bold=False, fill=None):
        nonlocal row
        put(1, row, label, font=_FONT_BOLD if bold else None, fill=fill)
        put(2, row, value, font=_FONT_BOLD if bold else None, fmt=fmt, fill=fill,
            align="right")
        row += 1

    put(1, row, "DIAGNÓSTICO FINANCIERO PERSONAL — neto", font=_FONT_TITLE)
    row += 1
    put(1, row, "Fecha: " + fecha.strftime("%d/%m/%Y %H:%M") + " (hora Costa Rica)")
    row += 1

    section("DATOS DEL CLIENTE")
    item("Nombre completo", p["nombre"], fmt="General")
    item("Correo electrónico", p["correo"], fmt="General")
    item("Número celular", p["celular"], fmt="General")

    section("RESUMEN — FLUJO MENSUAL")
    item("Ingresos", t["ingresos"])
    item("Gastos fijos", t["fijos"])
    item("Gastos variables", t["variables"])
    item("Gastos hormiga", t["hormiga"])
    item("Cuotas de deudas", t["cuotas"])
    item("FLUJO DE CAJA", t["flujo"], bold=True, fill=_FILL_SUBTOTAL)
    if t["ingresos"] > 0:
        item("Carga de deuda (cuotas / ingreso)", t["cuotas"] / t["ingresos"],
             fmt="0.0%")
        item("Gastos hormiga (% del ingreso)", t["hormiga"] / t["ingresos"],
             fmt="0.0%")
        item("Tasa de ahorro (flujo / ingreso)", t["flujo"] / t["ingresos"],
             fmt="0.0%")

    section("RESUMEN — PATRIMONIO")
    item("Activos", t["activos"])
    item("Deudas (saldo)", t["deudas"])
    item("PATRIMONIO NETO", t["patrimonio"], bold=True, fill=_FILL_SUBTOTAL)

    def detalle(titulo, rows, total):
        section(titulo)
        for r in rows:
            item(r["name"] or "(sin descripción)", r["amount"])
        item("Subtotal", total, bold=True, fill=_FILL_SUBTOTAL)

    detalle("GASTOS FIJOS", p["fijos"], t["fijos"])
    detalle("GASTOS VARIABLES", p["variables"], t["variables"])
    detalle("GASTOS HORMIGA", p["hormiga"], t["hormiga"])
    detalle("INGRESOS MENSUALES", p["ingresos"], t["ingresos"])
    detalle("ACTIVOS", p["activos"], t["activos"])

    section("DEUDAS")
    put(1, row, "Deuda", font=_FONT_BOLD)
    put(2, row, "Saldo total", font=_FONT_BOLD, align="right")
    put(3, row, "Tasa anual", font=_FONT_BOLD, align="right")
    put(4, row, "Cuota mensual", font=_FONT_BOLD, align="right")
    row += 1
    for d in p["deudas"]:
        put(1, row, d["name"] or "(sin nombre)")
        put(2, row, d["saldo"], fmt=_CRC_FMT, align="right")
        put(3, row, d["tasa"] / 100.0, fmt="0.0%", align="right")
        put(4, row, d["cuota"], fmt=_CRC_FMT, align="right")
        row += 1
    put(1, row, "Total", font=_FONT_BOLD, fill=_FILL_SUBTOTAL)
    put(2, row, t["deudas"], font=_FONT_BOLD, fmt=_CRC_FMT, fill=_FILL_SUBTOTAL,
        align="right")
    put(3, row, None, fill=_FILL_SUBTOTAL)
    put(4, row, t["cuotas"], font=_FONT_BOLD, fmt=_CRC_FMT, fill=_FILL_SUBTOTAL,
        align="right")
    row += 1

    section("PROTECCIÓN Y RETIRO")
    item("¿Tiene fondo de emergencia?",
         {"si": "Sí", "no": "No"}.get(p["fe_tiene"], "—"), fmt="General")
    item("Monto disponible para emergencias", p["fe_monto"])
    gastos_basicos = t["fijos"] + t["variables"]
    if gastos_basicos > 0:
        item("Meses de gastos cubiertos (meta 3–6)",
             p["fe_monto"] / gastos_basicos, fmt="0.0")
    item("Seguros vigentes", ", ".join(p["seguros"]) or "Ninguno reportado",
         fmt="General")
    item("Retiro", p["retiro"] or "—", fmt="General")

    section("METAS FINANCIERAS")
    for i, m in enumerate(p["metas"], start=1):
        if m:
            item(f"Meta {i}", m, fmt="General")
    if p["notas"]:
        item("Notas para el asesor", p["notas"], fmt="General")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def send_report(p):
    """Envía el reporte al cliente y al asesor. Lanza excepción si falla."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD no configurados en .env")

    xlsx = build_excel(p)
    fecha = datetime.now(_CR_TZ).strftime("%Y-%m-%d")

    msg = EmailMessage()
    msg["Subject"] = f"Diagnóstico Financiero — {p['nombre']}"
    msg["From"] = f"neto <{user}>"
    msg["To"] = p["correo"]
    msg["Cc"] = ADVISOR_EMAIL
    msg.set_content(
        f"Hola {p['nombre']},\n\n"
        "Adjuntamos el reporte de su Diagnóstico Financiero Personal. "
        "Su asesor lo revisará y lo contactará para coordinar la sesión de "
        "asesoría.\n\n"
        f"Datos de contacto registrados:\n"
        f"  • Correo: {p['correo']}\n"
        f"  • Celular: {p['celular']}\n\n"
        "— neto by Empowered Investor\n"
    )
    msg.add_attachment(
        xlsx,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"diagnostico-financiero-{fecha}.xlsx",
    )

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
