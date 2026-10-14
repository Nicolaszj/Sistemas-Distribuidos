"""Almacenamiento de bloques en disco (rol DataNode).

Cada bloque se guarda cifrado con AES-256-GCM:

    MAGIC (4 bytes) | nonce (12 bytes) | texto cifrado + tag GCM (16 bytes)

El identificador del bloque se usa como dato asociado (AAD), así que un bloque no
se puede renombrar ni intercambiar por otro sin que el descifrado falle. Los
bloques escritos antes de activar el cifrado (sin MAGIC) se leen tal cual.
"""

import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

MAGIC = b"DFS1"
NONCE_SIZE = 12

_cipher = AESGCM(config.STORAGE_KEY)


class CorruptBlock(Exception):
    pass


def path_for(block_id):
    return config.BLOCKS_DIR / block_id[:2] / block_id


def encrypt(block_id, data):
    nonce = os.urandom(NONCE_SIZE)
    return MAGIC + nonce + _cipher.encrypt(nonce, data, block_id.encode())


def decrypt(block_id, raw):
    if not raw.startswith(MAGIC):
        return raw

    nonce = raw[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    try:
        return _cipher.decrypt(nonce, raw[len(MAGIC) + NONCE_SIZE :], block_id.encode())
    except InvalidTag:
        raise CorruptBlock(f"El bloque {block_id} fue alterado o la llave no coincide")


def write(data, block_id=None):
    block_id = block_id or uuid.uuid4().hex
    destination = path_for(block_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(encrypt(block_id, data))
    temporary.replace(destination)
    return block_id


def read(block_id):
    return decrypt(block_id, path_for(block_id).read_bytes())


def remove(block_id):
    path_for(block_id).unlink(missing_ok=True)
