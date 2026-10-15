"""Replicación de bloques: escritura con factor N, re-replicación y lectura con
failover entre réplicas."""

import hashlib
import time

import pytest
import requests
from fastapi import HTTPException

NODES = [
    {"id": "dn1", "url": "https://dn1:8000"},
    {"id": "dn2", "url": "https://dn2:8000"},
    {"id": "dn3", "url": "https://dn3:8000"},
]


class _Ok:
    ok = True
    status_code = 200

    def raise_for_status(self):
        return None


def _put_recorder(monkeypatch, failing=()):
    from dfsha.server import replication

    calls = []

    def fake_put(url, **kwargs):
        calls.append(url)
        if any(name in url for name in failing):
            raise requests.ConnectionError("nodo caído")
        return _Ok()

    monkeypatch.setattr(replication.requests, "put", fake_put)
    monkeypatch.setattr(replication.requests, "delete", lambda *a, **k: _Ok())
    return calls


def test_escribe_en_tres_nodos(monkeypatch):
    from dfsha.server import config, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 3)
    _put_recorder(monkeypatch)
    assert replication.write_replicas(b"x", "a" * 32, NODES, sequence=0) == ["dn1", "dn2", "dn3"]
    # el orden rota con el número de bloque para repartir la réplica principal
    assert replication.write_replicas(b"x", "b" * 32, NODES, sequence=1) == ["dn2", "dn3", "dn1"]


def test_factor_mayor_que_nodos(monkeypatch):
    from dfsha.server import config, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 3)
    _put_recorder(monkeypatch)
    assert replication.write_replicas(b"x", "a" * 32, NODES[:2]) == ["dn1", "dn2"]


def test_factor_dos_con_nodo_caido_usa_otro(monkeypatch):
    from dfsha.server import config, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 2)
    _put_recorder(monkeypatch, failing=("dn1",))
    assert replication.write_replicas(b"x", "a" * 32, NODES) == ["dn2", "dn3"]


def test_sin_replicas_suficientes_falla_y_limpia(monkeypatch):
    from dfsha.server import config, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 3)
    _put_recorder(monkeypatch, failing=("dn2",))
    removed = []
    monkeypatch.setattr(
        replication, "remove_replicas", lambda block_id, urls: removed.append(sorted(urls))
    )
    with pytest.raises(HTTPException) as error:
        replication.write_replicas(b"x", "a" * 32, NODES)
    assert error.value.status_code == 503
    assert removed == [["dn1", "dn3"]]  # se borran las réplicas que sí quedaron


def _archivo_con_bloque(tmp_path, monkeypatch, holders):
    from dfsha.server import config, metadata

    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "m.db")
    metadata.initialize()
    with metadata.connect() as db:
        user_id = metadata.create_user(db, "ana", "h", "s")
        root = metadata.root_directory(db, user_id)["id"]
        metadata.create_file(db, user_id, root, "f.bin", 1, [("c" * 32, 1, "x")], "x")
        for node in NODES:
            metadata.register_node(db, node["id"], node["url"], time.time())
        for node_id in holders:
            metadata.set_block_location(db, "c" * 32, node_id)


def test_rereplica_desde_nodo_vivo(tmp_path, monkeypatch):
    from dfsha.server import config, metadata, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 3)
    _archivo_con_bloque(tmp_path, monkeypatch, holders=["dn1"])
    posts = []

    def fake_post(url, json, **kwargs):
        posts.append((url, json["target_url"]))
        return _Ok()

    monkeypatch.setattr(replication.requests, "post", fake_post)
    assert replication.rereplicate_once(NODES) == 2
    assert posts == [
        ("https://dn1:8000/blocks/" + "c" * 32 + "/copy-to", "https://dn2:8000"),
        ("https://dn1:8000/blocks/" + "c" * 32 + "/copy-to", "https://dn3:8000"),
    ]
    with metadata.connect() as db:
        assert {row["id"] for row in metadata.block_locations(db, "c" * 32)} == {"dn1", "dn2", "dn3"}
    # con las 3 réplicas no vuelve a copiar
    assert replication.rereplicate_once(NODES) == 0


def test_rereplica_ignora_nodos_caidos(tmp_path, monkeypatch):
    """dn2 tenía réplica pero está caído: cuentan solo las réplicas vivas (dn1) y
    con 2 nodos vivos el objetivo es 2 réplicas, así que se copia a dn3."""
    from dfsha.server import config, replication

    monkeypatch.setattr(config, "REPLICATION_FACTOR", 3)
    _archivo_con_bloque(tmp_path, monkeypatch, holders=["dn1", "dn2"])
    posts = []
    monkeypatch.setattr(
        replication.requests, "post", lambda url, json, **k: posts.append(json["target_url"]) or _Ok()
    )
    vivos = [NODES[0], NODES[2]]
    assert replication.rereplicate_once(vivos) == 1
    assert posts == ["https://dn3:8000"]


def test_replicas_vivas_primero(tmp_path, monkeypatch):
    from dfsha.server import metadata, replication

    _archivo_con_bloque(tmp_path, monkeypatch, holders=["dn1", "dn2", "dn3"])
    with metadata.connect() as db:
        urls = replication.replica_urls(db, "c" * 32, alive_ids={"dn3"})
    assert urls[0] == "https://dn3:8000"
    assert set(urls) == {node["url"] for node in NODES}


def test_cliente_lee_de_otra_replica(monkeypatch):
    from dfsha.client.api import DfsClient, DfsError

    data = b"contenido del bloque"
    block = {
        "id": "d" * 32,
        "checksum": hashlib.sha256(data).hexdigest(),
        "ticket": "t",
        "replicas": ["https://dn1:8000/blocks/x", "https://dn2:8000/blocks/x", "https://dn3:8000/blocks/x"],
    }

    class _Resp:
        def __init__(self, content):
            self.content = content

    tried = []

    def fake_request(method, url, **kwargs):
        tried.append(url)
        if "dn1" in url:
            raise DfsError("No hay conexión con el servidor")
        if "dn2" in url:
            return _Resp(b"bloque alterado")  # checksum distinto
        return _Resp(data)

    client = DfsClient("https://cn1:8000")
    monkeypatch.setattr(client, "request", fake_request)
    assert client.read_block(block) == data
    assert [url.split("/")[2] for url in tried] == ["dn1:8000", "dn2:8000", "dn3:8000"]


def test_cliente_sin_replicas_sanas_falla(monkeypatch):
    from dfsha.client.api import DfsClient, DfsError

    block = {"id": "d" * 32, "checksum": "0" * 64, "replicas": ["https://dn1:8000/blocks/x"]}
    client = DfsClient("https://cn1:8000")

    def fake_request(*a, **k):
        raise DfsError("No hay conexión con el servidor")

    monkeypatch.setattr(client, "request", fake_request)
    with pytest.raises(DfsError):
        client.read_block(block)
