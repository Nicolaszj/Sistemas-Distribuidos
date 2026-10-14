"""TOTP (RFC 6238) para el segundo factor. Compatible con Google Authenticator,
Microsoft Authenticator, Authy, etc."""

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def new_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def code_at(secret, timestamp):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(timestamp // STEP_SECONDS)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def verify(secret, code, window=1):
    """Acepta el código del intervalo actual y de los vecinos, por desfase de reloj."""
    if not secret or not code:
        return False
    now = time.time()
    return any(
        hmac.compare_digest(code_at(secret, now + step * STEP_SECONDS), code.strip())
        for step in range(-window, window + 1)
    )


def provisioning_uri(secret, username, issuer="DFSha"):
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
