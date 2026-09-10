import hashlib
import hmac
import os
import time

import jwt

from . import config

ALGORITHM = "HS256"
ITERATIONS = 200_000


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
