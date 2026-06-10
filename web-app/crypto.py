"""Reversible encryption for sensitive secrets stored at rest.

Used to protect clients' Alpaca OAuth tokens in core.client_investment.

Unlike passwords (which are *hashed* one-way with werkzeug), a broker token
must be recovered in plaintext to call Alpaca's API, so it is *encrypted* with
AES-256-GCM (authenticated encryption). The 32-byte master key comes from the
ENCRYPTION_KEY environment variable and never touches the database; a DB dump
alone is therefore useless without it.

Generate a key once with:
    python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
and put it in .env as ENCRYPTION_KEY=...

Migrating to a cloud KMS later only means changing _master_key() — the on-disk
format (ciphertext + nonce + key_version) stays the same.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_BYTES = 32   # AES-256
_NONCE_BYTES = 12  # GCM standard nonce length


class EncryptionKeyError(RuntimeError):
    """ENCRYPTION_KEY is missing or malformed."""


def _master_key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not raw:
        raise EncryptionKeyError(
            "ENCRYPTION_KEY no está configurada. Generá una con: "
            "python -c \"import base64,os;"
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # noqa: BLE001 - surface a clear config error
        raise EncryptionKeyError("ENCRYPTION_KEY no es base64 válido.") from exc
    if len(key) != _KEY_BYTES:
        raise EncryptionKeyError(
            f"ENCRYPTION_KEY debe decodificar a {_KEY_BYTES} bytes (AES-256); "
            f"decodificó {len(key)}."
        )
    return key


def encrypt_token(plaintext: str, *, aad: str = "") -> tuple[bytes, bytes]:
    """Encrypt a secret. Returns (ciphertext, nonce).

    `aad` (additional authenticated data, e.g. the client id) is not encrypted
    but is authenticated, binding the ciphertext to that record.
    """
    aesgcm = AESGCM(_master_key())
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return ciphertext, nonce


def decrypt_token(ciphertext: bytes, nonce: bytes, *, aad: str = "") -> str:
    """Reverse of `encrypt_token`. Raises on tampering, wrong key, or wrong aad."""
    aesgcm = AESGCM(_master_key())
    plaintext = aesgcm.decrypt(bytes(nonce), bytes(ciphertext), aad.encode("utf-8"))
    return plaintext.decode("utf-8")
