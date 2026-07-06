"""Diagnóstico Financiero: generación del reporte Excel y envío por correo.

Los datos vienen del formulario público /diagnostico/ (sin login) y NO se
guardan en la base de datos: se arma un .xlsx en memoria y se envía por SMTP
(neto@investorcr.com) al cliente y al asesor.

Variables de entorno requeridas (.env de la raíz, ya presentes en el server):
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD
"""

import html as html_mod
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


_LOGO_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static", "rebranding", "logos", "neto-logo-transparent-600w.png",
)

# Barra de proporciones 50-30-20 con la paleta del favicon (donut):
# teal (50%) → azul (30%) → amarillo (20%)
_BAR_5030_20 = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td width="50%" height="8" bgcolor="#3A9C8E" style="font-size:0;line-height:0;">&nbsp;</td>
    <td width="30%" height="8" bgcolor="#1F4D8E" style="font-size:0;line-height:0;">&nbsp;</td>
    <td width="20%" height="8" bgcolor="#EFA91A" style="font-size:0;line-height:0;">&nbsp;</td>
  </tr>
</table>"""


def _fmt_crc(n):
    """₡1.234.567 (separador de miles al estilo es-CR)."""
    return "₡" + f"{round(n):,}".replace(",", ".")


def _build_html(p, fecha_larga, archivo):
    """Cuerpo HTML del correo con la marca neto (logo inline cid:netologo)."""
    esc = html_mod.escape
    t = _totales(p)
    flujo_color = "#3A9C8E" if t["flujo"] >= 0 else "#B3402E"
    patri_color = "#3A9C8E" if t["patrimonio"] >= 0 else "#B3402E"

    def kpi(label, value, color="#1B1C20"):
        return f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:#F4F6F9;border-radius:10px;">
          <tr><td style="padding:14px 16px;font-family:Helvetica,Arial,sans-serif;">
            <div style="font-size:10px;font-weight:bold;letter-spacing:.06em;
                        text-transform:uppercase;color:#6B7280;">{label}</div>
            <div style="font-size:19px;font-weight:bold;color:{color};
                        padding-top:4px;">{value}</div>
          </td></tr>
        </table>"""

    return f"""\
<!DOCTYPE html>
<html lang="es">
<body style="margin:0;padding:0;background:#F4F6F9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F4F6F9">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
         style="max-width:600px;width:100%;background:#FFFFFF;border-radius:12px;
                border:1px solid #E2E6EB;">

    <!-- Header -->
    <tr><td bgcolor="#1B1C20" style="padding:26px 32px;border-radius:12px 12px 0 0;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td bgcolor="#FFFFFF" style="border-radius:8px;padding:7px 12px;">
            <img src="cid:netologo" width="96" alt="neto"
                 style="display:block;width:96px;height:auto;border:0;">
          </td>
          <td style="padding-left:18px;font-family:Helvetica,Arial,sans-serif;color:#FFFFFF;">
            <div style="font-size:16px;font-weight:bold;letter-spacing:-.01em;">
              Diagnóstico Financiero Personal</div>
            <div style="font-size:12px;color:#9CA3AF;padding-top:2px;">
              by Empowered Investor</div>
          </td>
        </tr>
      </table>
    </td></tr>

    <!-- Barra 50-30-20 -->
    <tr><td>{_BAR_5030_20}</td></tr>

    <!-- Cuerpo -->
    <tr><td style="padding:32px;font-family:Helvetica,Arial,sans-serif;color:#1B1C20;">

      <div style="font-size:20px;font-weight:bold;letter-spacing:-.02em;">
        Hola {esc(p["nombre"].split()[0])}, su diagnóstico está listo</div>

      <p style="font-size:14px;line-height:1.6;color:#4B5563;margin:14px 0 24px;">
        Gracias por completar su Diagnóstico Financiero Personal. Adjuntamos el
        reporte completo en Excel con el detalle de sus gastos, ingresos,
        patrimonio y metas. Su asesor recibió una copia y lo contactará para
        coordinar la sesión de asesoría.</p>

      <!-- KPIs -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="49%" valign="top">{kpi("Ingresos / mes", _fmt_crc(t["ingresos"]))}</td>
          <td width="2%">&nbsp;</td>
          <td width="49%" valign="top">{kpi("Gastos / mes", _fmt_crc(t["gastos"]))}</td>
        </tr>
        <tr><td colspan="3" height="10" style="font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr>
          <td width="49%" valign="top">{kpi("Flujo de caja", _fmt_crc(t["flujo"]), flujo_color)}</td>
          <td width="2%">&nbsp;</td>
          <td width="49%" valign="top">{kpi("Patrimonio neto", _fmt_crc(t["patrimonio"]), patri_color)}</td>
        </tr>
      </table>

      <!-- Adjunto -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="margin-top:24px;">
        <tr>
          <td width="4" bgcolor="#3A9C8E" style="border-radius:4px 0 0 4px;font-size:0;">&nbsp;</td>
          <td bgcolor="#F4F6F9" style="padding:14px 16px;border-radius:0 4px 4px 0;
              font-family:Helvetica,Arial,sans-serif;">
            <div style="font-size:13px;font-weight:bold;color:#1B1C20;">
              Reporte adjunto</div>
            <div style="font-size:12.5px;color:#6B7280;padding-top:2px;">
              {esc(archivo)} · generado el {esc(fecha_larga)}</div>
          </td>
        </tr>
      </table>

      <p style="font-size:13px;line-height:1.6;color:#4B5563;margin:24px 0 0;">
        <b>Datos de contacto registrados:</b><br>
        Correo: {esc(p["correo"])}<br>
        Celular: {esc(p["celular"])}</p>

    </td></tr>

    <!-- Footer -->
    <tr><td>{_BAR_5030_20}</td></tr>
    <tr><td bgcolor="#1B1C20" style="padding:20px 32px;border-radius:0 0 12px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="font-family:Helvetica,Arial,sans-serif;font-size:12px;color:#9CA3AF;">
            <b style="color:#FFFFFF;">neto</b> by Empowered Investor<br>
            <span style="font-size:11px;">50 necesidades · 30 estilo de vida · 20 ahorro</span>
          </td>
          <td align="right" style="font-family:Helvetica,Arial,sans-serif;
              font-size:11px;color:#6B7280;">
            Sus datos no se almacenan<br>en ninguna base de datos.
          </td>
        </tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>"""


def send_report(p):
    """Envía el reporte al cliente y al asesor. Lanza excepción si falla."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD no configurados en .env")

    xlsx = build_excel(p)
    ahora = datetime.now(_CR_TZ)
    fecha = ahora.strftime("%Y-%m-%d")
    archivo = f"diagnostico-financiero-{fecha}.xlsx"

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

    # Alternativa HTML con la marca neto; el logo va embebido (cid) para que
    # se muestre sin depender de imágenes remotas bloqueadas por el cliente.
    msg.add_alternative(_build_html(p, ahora.strftime("%d/%m/%Y"), archivo),
                        subtype="html")
    with open(_LOGO_PATH, "rb") as f:
        msg.get_payload()[1].add_related(
            f.read(), maintype="image", subtype="png", cid="<netologo>")

    msg.add_attachment(
        xlsx,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=archivo,
    )

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
