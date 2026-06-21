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
]


def gen_password(nombre: str, apellidos: str) -> str:
    """Derive an initial password from the client's name.

    Takes the first letter of each word in the full name, lowercase.
    E.g. "Ana García López" -> "agl"
    Mirrors mkPasswordHash() in administracion/admin.html.
    """
    full = (nombre.strip() + ' ' + apellidos.strip())
    return ''.join(w[0].lower() for w in full.split() if w)
