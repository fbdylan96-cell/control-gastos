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

    Format: crgastostesting+{name}{suffix}@gmail.com
    where suffix is 4 random lowercase alphanumeric characters.
    """
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    name_slug = client_name.lower().replace(' ', '')
    return f'crgastostesting+{name_slug}.{suffix}@gmail.com'


def gen_password(nombre: str, apellidos: str) -> str:
    """Derive an initial password from the client's name.

    Takes the first letter of each word in the full name, lowercase.
    E.g. "Ana García López" -> "agl"
    Mirrors mkPasswordHash() in administracion/admin.html.
    """
    full = (nombre.strip() + ' ' + apellidos.strip())
    return ''.join(w[0].lower() for w in full.split() if w)
