import time

import pytest

from dfsha import cluster_auth
from dfsha.server import config, storage, totp


def test_bloques_cifrados_en_disco():
    data = b"secreto muy importante"
    block_id = storage.write(data)
    raw = storage.path_for(block_id).read_bytes()

    assert data not in raw
    assert raw.startswith(storage.MAGIC)
    assert storage.read(block_id) == data


def test_bloque_alterado_se_detecta():
    block_id = storage.write(b"contenido original")
    path = storage.path_for(block_id)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))

    with pytest.raises(storage.CorruptBlock):
        storage.read(block_id)


def test_bloque_no_se_puede_intercambiar():
    a = storage.write(b"bloque A")
    b = storage.write(b"bloque B")
    storage.path_for(b).write_bytes(storage.path_for(a).read_bytes())

    with pytest.raises(storage.CorruptBlock):
        storage.read(b)


def test_totp_vector_rfc6238():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert totp.code_at(secret, 59) == "287082"
    assert totp.code_at(secret, 1111111109) == "081804"


def test_totp_verifica_codigo_actual():
    secret = totp.new_secret()
    assert totp.verify(secret, totp.code_at(secret, time.time()))
    assert not totp.verify(secret, "000000") or totp.code_at(secret, time.time()) == "000000"


def test_ticket_de_bloque():
    ticket = cluster_auth.issue_block_ticket(config.CLUSTER_SECRET, "abc", cluster_auth.READ)
    cluster_auth.verify_block_ticket(config.CLUSTER_SECRET, ticket, "abc")

    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_block_ticket(config.CLUSTER_SECRET, ticket, "otro")
    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_block_ticket(config.CLUSTER_SECRET, ticket, "abc", cluster_auth.WRITE)
    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_block_ticket("otro-secreto", ticket, "abc")


def test_ticket_expirado():
    ticket = cluster_auth.issue_block_ticket(config.CLUSTER_SECRET, "abc", ttl=-1)
    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_block_ticket(config.CLUSTER_SECRET, ticket, "abc")


def test_token_de_nodo():
    token = cluster_auth.issue_node_token(config.CLUSTER_SECRET, "datanode1")
    assert cluster_auth.verify_node_token(config.CLUSTER_SECRET, token) == "datanode1"
