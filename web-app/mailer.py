"""Envío de correos transaccionales de la web app vía SMTP (neto@investorcr.com).

Variables de entorno (.env de la raíz, ya presentes en el server):
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD

El pipeline de notificaciones de transacciones NO usa esto (va por Gmail API,
ver notifier.py); este módulo es para correos de la web app: cambio de
contraseña, restablecimiento, y futuros correos de billing.
"""

import html as html_mod
import os
import smtplib
from email.message import EmailMessage

_LOGO_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static", "rebranding", "logos", "neto-logo-transparent-600w.png",
)

# Barra de proporciones 50-30-20 con la paleta del favicon (donut):
# teal (50%) → azul (30%) → amarillo (20%)
_BAR_50_30_20 = """
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td width="50%" height="8" bgcolor="#3A9C8E" style="font-size:0;line-height:0;">&nbsp;</td>
    <td width="30%" height="8" bgcolor="#1F4D8E" style="font-size:0;line-height:0;">&nbsp;</td>
    <td width="20%" height="8" bgcolor="#EFA91A" style="font-size:0;line-height:0;">&nbsp;</td>
  </tr>
</table>"""


def _envolver_html(titulo, cuerpo_html):
    """Envuelve un cuerpo HTML en la plantilla de marca neto (logo cid:netologo)."""
    return f"""\
<!DOCTYPE html>
<html lang="es">
<body style="margin:0;padding:0;background:#F4F6F9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F4F6F9">
<tr><td align="center" style="padding:32px 16px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
         style="max-width:600px;width:100%;background:#FFFFFF;border-radius:12px;
                border:1px solid #E2E6EB;">

    <tr><td bgcolor="#1B1C20" style="padding:26px 32px;border-radius:12px 12px 0 0;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td bgcolor="#FFFFFF" style="border-radius:8px;padding:7px 12px;">
            <img src="cid:netologo" width="96" alt="neto"
                 style="display:block;width:96px;height:auto;border:0;">
          </td>
          <td style="padding-left:18px;font-family:Helvetica,Arial,sans-serif;color:#FFFFFF;">
            <div style="font-size:16px;font-weight:bold;letter-spacing:-.01em;">
              {html_mod.escape(titulo)}</div>
            <div style="font-size:12px;color:#9CA3AF;padding-top:2px;">
              by Empowered Investor</div>
          </td>
        </tr>
      </table>
    </td></tr>

    <tr><td>{_BAR_50_30_20}</td></tr>

    <tr><td style="padding:32px;font-family:Helvetica,Arial,sans-serif;color:#1B1C20;">
      {cuerpo_html}
    </td></tr>

    <tr><td>{_BAR_50_30_20}</td></tr>
    <tr><td bgcolor="#1B1C20" style="padding:20px 32px;border-radius:0 0 12px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="font-family:Helvetica,Arial,sans-serif;font-size:12px;color:#9CA3AF;">
            <b style="color:#FFFFFF;">neto</b> by Empowered Investor<br>
            <span style="font-size:11px;">50 necesidades · 30 estilo de vida · 20 ahorro</span>
          </td>
        </tr>
      </table>
    </td></tr>

  </table>

</td></tr>
</table>
</body>
</html>"""


def send_email(to, subject, text, html_body=None, html_titulo=None):
    """Envía un correo desde neto@investorcr.com. Lanza excepción si falla.

    text: alternativa de texto plano (siempre requerida).
    html_body: cuerpo HTML interno (opcional); se envuelve en la plantilla de
    marca con el logo embebido por CID.
    """
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD no configurados en .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"neto <{user}>"
    msg["To"] = to
    msg.set_content(text)

    if html_body:
        msg.add_alternative(
            _envolver_html(html_titulo or subject, html_body), subtype="html"
        )
        with open(_LOGO_PATH, "rb") as f:
            msg.get_payload()[1].add_related(
                f.read(), maintype="image", subtype="png", cid="<netologo>")

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def _primer_nombre(nombre):
    return html_mod.escape((nombre or "").split()[0] if (nombre or "").split() else "")


def enviar_aviso_cambio_contrasena(email, nombre):
    """Aviso de que la contraseña de la cuenta fue cambiada."""
    saludo = _primer_nombre(nombre)
    send_email(
        to=email,
        subject="Tu contraseña fue actualizada — neto",
        text=(
            f"Hola {nombre},\n\n"
            "Te confirmamos que la contraseña de tu cuenta de neto fue "
            "actualizada correctamente.\n\n"
            "Si no realizaste este cambio, respondé este correo de inmediato "
            "para que aseguremos tu cuenta.\n\n"
            "— neto by Empowered Investor\n"
        ),
        html_titulo="Contraseña actualizada",
        html_body=f"""
      <div style="font-size:20px;font-weight:bold;letter-spacing:-.02em;">
        Hola {saludo}, tu contraseña fue actualizada</div>
      <p style="font-size:14px;line-height:1.6;color:#4B5563;margin:14px 0 0;">
        Te confirmamos que la contraseña de tu cuenta de <b>neto</b> fue
        actualizada correctamente. Ya podés usarla en tu próximo inicio de
        sesión.</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="margin-top:24px;">
        <tr>
          <td width="4" bgcolor="#EFA91A" style="border-radius:4px 0 0 4px;font-size:0;">&nbsp;</td>
          <td bgcolor="#F4F6F9" style="padding:14px 16px;border-radius:0 4px 4px 0;
              font-family:Helvetica,Arial,sans-serif;">
            <div style="font-size:13px;font-weight:bold;color:#1B1C20;">
              ¿No fuiste vos?</div>
            <div style="font-size:12.5px;color:#6B7280;padding-top:2px;">
              Respondé este correo de inmediato para que aseguremos tu cuenta.</div>
          </td>
        </tr>
      </table>""",
    )


def enviar_link_reset_contrasena(email, nombre, link):
    """Correo con el enlace para restablecer la contraseña (expira en 1 hora)."""
    saludo = _primer_nombre(nombre)
    link_esc = html_mod.escape(link, quote=True)
    send_email(
        to=email,
        subject="Restablecé tu contraseña — neto",
        text=(
            f"Hola {nombre},\n\n"
            "Recibimos una solicitud para restablecer la contraseña de tu "
            "cuenta de neto. Abrí este enlace para elegir una nueva "
            "(válido por 1 hora):\n\n"
            f"{link}\n\n"
            "Si no lo solicitaste, podés ignorar este correo — tu contraseña "
            "actual sigue siendo válida.\n\n"
            "— neto by Empowered Investor\n"
        ),
        html_titulo="Restablecer contraseña",
        html_body=f"""
      <div style="font-size:20px;font-weight:bold;letter-spacing:-.02em;">
        Hola {saludo}, restablezcamos tu contraseña</div>
      <p style="font-size:14px;line-height:1.6;color:#4B5563;margin:14px 0 24px;">
        Recibimos una solicitud para restablecer la contraseña de tu cuenta de
        <b>neto</b>. Presioná el botón para elegir una nueva. El enlace es
        válido por <b>1 hora</b>.</p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center">
        <tr><td bgcolor="#1B1C20" style="border-radius:8px;">
          <a href="{link_esc}" target="_blank"
             style="display:inline-block;padding:13px 28px;font-family:Helvetica,Arial,sans-serif;
                    font-size:14px;font-weight:bold;color:#FFFFFF;text-decoration:none;">
            Elegir nueva contraseña</a>
        </td></tr>
      </table>
      <p style="font-size:12.5px;line-height:1.6;color:#6B7280;margin:24px 0 0;">
        Si el botón no funciona, copiá y pegá este enlace en tu navegador:<br>
        <span style="word-break:break-all;color:#1F4D8E;">{link_esc}</span></p>
      <p style="font-size:12.5px;line-height:1.6;color:#6B7280;margin:16px 0 0;">
        Si no solicitaste este cambio, ignorá este correo — tu contraseña
        actual sigue siendo válida.</p>""",
    )
