import base64
import hashlib
import hmac
import os
import random
import re
import string
import unicodedata


def verify_reclassification(notification_id: str, category: str, subcategory: str, sig: str) -> bool:
    secret = os.environ.get("NOTIFICATION_SECRET", "")
    if not secret:
        return False
    sub = subcategory or ""
    payload = f"{notification_id}|{category}|{sub}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode()
    return hmac.compare_digest(expected_b64, sig)


def clean_merchant_key(name: str) -> str:
    """Normalize a merchant name to a stable lookup key (lowercase, ASCII, no symbols)."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", ascii_str.lower())
    return " ".join(cleaned.split())


# Nota libre del cliente por transacción (core.transactions_notifications.client_notes).
# El tope coincide con VARCHAR(280) en la base: si llegara algo más largo (form
# manipulado, cliente sin JS) se recorta acá y nunca revienta el INSERT.
CLIENT_NOTES_MAX_LEN = 280


def clean_client_notes(value) -> str | None:
    """Normaliza la nota de una transacción. Devuelve None si queda vacía."""
    return (str(value or "").strip()[:CLIENT_NOTES_MAX_LEN]) or None


def gen_email_forward(client_name: str) -> str:
    """Generate a unique email forwarding address for a client.

    Format: gastos+{name}.{suffix}@investorcr.com
    where suffix is 4 random lowercase alphanumeric characters.

    The name slug is reduced to ASCII a-z0-9 (accents decomposed and dropped,
    punctuation removed) so the alias is always a valid email local part —
    client_name itself keeps its accents everywhere else in the system.
    """
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    nfkd = unicodedata.normalize('NFKD', client_name)
    ascii_name = nfkd.encode('ascii', 'ignore').decode('ascii')
    name_slug = re.sub(r'[^a-z0-9]', '', ascii_name.lower()) or 'cliente'
    return f'gastos+{name_slug}.{suffix}@investorcr.com'


# ── Configuración: reenvío de correos ────────────────────────────────────────
#
# Remitentes confirmados desde los que cada banco envía sus notificaciones de
# transacción. Se ofrecen al cliente para que los use como condición "De / From"
# al crear la regla de reenvío hacia su email_forward. (BAC también está en
# banks/bac.py / BAC_DOMAINS.) Si se agrega un banco nuevo, verificar la dirección
# contra un correo real antes de fijar el filtro.
BANK_NOTIFICATION_SENDERS = [
    {
        "bank": "BAC Credomatic",
        "emails": [
            # BAC migra remitentes por olas: 2026-08-24 apareció NotificacionBAC@
            # (reemplaza a notificacion@baccredomatic.cr, que a su vez reemplazó
            # a notificacionesbaccr.com el 2026-08-03). Se listan todos: el
            # filtro de reenvío del cliente debe cubrir viejos y nuevos.
            "NotificacionBAC@baccredomatic.cr",
            "notificacion@baccredomatic.cr",
            "notificacion@notificacionesbaccr.com",
            "alerta@baccredomatic.com",
        ],
    },
    {
        "bank": "Banco Promerica",
        "emails": ["info@promerica.fi.cr"],
    },
    {
        "bank": "DAVIbank (Davivienda)",
        "emails": ["costarica_clientes@davivienda.cr", "Alertas@davibank.cr"],
    },
    {
        "bank": "Grupo Mutual",
        "emails": [
            "MutualMovil@grupomutual.fi.cr",
            "MutualEnLinea@grupomutual.fi.cr",
        ],
    },
    {
        "bank": "MUCAP",
        "emails": ["info@mucap.fi.cr"],
    },
]


def gen_password(nombre: str, apellidos: str) -> str:
    """Derive an initial password from the client's name.

    Takes the first letter of each word in the full name, lowercase.
    E.g. "Ana García López" -> "agl"
    Mirrors mkPasswordHash() in administracion/admin.html.
    """
    full = (nombre.strip() + ' ' + apellidos.strip())
    return ''.join(w[0].lower() for w in full.split() if w)


# ── Cambio / restablecimiento de contraseña ──────────────────────────────────

def validar_nueva_contrasena(nueva: str, confirmacion: str) -> str | None:
    """Política de contraseñas nuevas. Devuelve el mensaje de error o None si es válida."""
    if len(nueva) < 8:
        return "La nueva contraseña debe tener al menos 8 caracteres."
    if not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", nueva) or not re.search(r"\d", nueva):
        return "La nueva contraseña debe incluir al menos una letra y un número."
    if nueva != confirmacion:
        return "La confirmación no coincide con la nueva contraseña."
    return None


# Rate limit en memoria por IP (aproximado con varios workers de gunicorn,
# suficiente para frenar spam de correos de restablecimiento).
_RATE_BUCKETS: dict = {}


def rate_limit_ok(bucket: str, ip: str, max_hits: int = 5, window_s: int = 3600) -> bool:
    import time
    now = time.time()
    key = (bucket, ip)
    hits = [ts for ts in _RATE_BUCKETS.get(key, []) if now - ts < window_s]
    if len(hits) >= max_hits:
        _RATE_BUCKETS[key] = hits
        return False
    hits.append(now)
    _RATE_BUCKETS[key] = hits
    return True


def make_reset_token(user_id: str, password_hash: str, salt: str) -> str:
    """Token firmado para restablecer contraseña. Incluye un fragmento del hash
    vigente: al cambiar la contraseña (por este u otro medio) el token muere solo."""
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(current_app.secret_key, salt=salt)
    return s.dumps({"uid": str(user_id), "ph": (password_hash or "")[-16:]})


def load_reset_token(token: str, salt: str, max_age_s: int = 3600) -> dict | None:
    """Devuelve el payload del token o None si es inválido/expirado."""
    from flask import current_app
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    s = URLSafeTimedSerializer(current_app.secret_key, salt=salt)
    try:
        return s.loads(token, max_age=max_age_s)
    except (BadSignature, SignatureExpired):
        return None
