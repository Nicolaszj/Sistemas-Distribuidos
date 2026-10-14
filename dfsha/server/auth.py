import hashlib
import hmac
import os
import secrets
import threading
import time

import jwt

from . import config

ALGORITHM = "HS256"
ITERATIONS = 200_000
API_KEY_PREFIX = "dfsha"


def hash_password(password, salt=None):
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), ITERATIONS
    ).hex()
    return digest, salt


def verify_password(password, salt, expected_hash):
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, expected_hash)


def create_token(user_id, username):
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": int(time.time()) + config.TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=ALGORITHM)


def read_token(token):
    payload = jwt.decode(token, config.SECRET_KEY, algorithms=[ALGORITHM])
    return int(payload["sub"]), payload["username"]


def new_api_key():
    """Devuelve (llave completa, prefijo visible). La llave solo se muestra una vez;
    en la base queda su SHA-256 (tiene 256 bits de entropía, no necesita PBKDF2)."""
    prefix = secrets.token_hex(4)
    key = f"{API_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    return key, prefix


def hash_api_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class LoginThrottle:
    """Bloquea temporalmente un usuario después de varios intentos fallidos
    seguidos, para frenar ataques de fuerza bruta contra las contraseñas."""

    def __init__(self, max_failures, block_seconds):
        self.max_failures = max_failures
        self.block_seconds = block_seconds
        self.failures = {}
        self.lock = threading.Lock()

    def blocked_for(self, username):
        with self.lock:
            count, since = self.failures.get(username, (0, 0))
            if count < self.max_failures:
                return 0
            remaining = self.block_seconds - (time.time() - since)
            if remaining <= 0:
                del self.failures[username]
                return 0
            return int(remaining) + 1

    def failed(self, username):
        with self.lock:
            count, _ = self.failures.get(username, (0, 0))
            self.failures[username] = (count + 1, time.time())

    def succeeded(self, username):
        with self.lock:
            self.failures.pop(username, None)


throttle = LoginThrottle(config.LOGIN_MAX_FAILURES, config.LOGIN_BLOCK_SECONDS)
