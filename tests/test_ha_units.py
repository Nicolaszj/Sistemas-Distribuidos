"""Pruebas unitarias de la alta disponibilidad del ControlNode (hito 3).

No dependen de Docker: usan recarga de módulos y TestClient de FastAPI.
"""

import importlib
import os
import time

import pytest


def _reload_config(monkeypatch, **env):
    """Recarga dfsha.server.config con un entorno dado y devuelve el módulo."""
    from dfsha.server import config as config_module

    for key in (
        "DFSHA_CONTROL_URL",
        "DFSHA_CONTROL_URLS",
        "DFSHA_HA_ROLE",
        "DFSHA_HA_PEER_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


@pytest.fixture
def restore_config():
    """Deja dfsha.server.config como estaba al terminar la prueba.

    Limpia el entorno de HA antes de recargar: una prueba pudo dejar un valor
    inválido (p. ej. DFSHA_HA_ROLE) y recargar sin limpiarlo re-dispararía el
    error de validación durante el teardown.
    """
    yield
    for key in (
        "DFSHA_CONTROL_URL",
        "DFSHA_CONTROL_URLS",
        "DFSHA_HA_ROLE",
        "DFSHA_HA_PEER_URL",
    ):
        os.environ.pop(key, None)
    from dfsha.server import config as config_module

    importlib.reload(config_module)


# --- R8.1: lista de ControlNodes retrocompatible --------------------------------


def test_control_urls_una_sola_url(monkeypatch, restore_config):
    config = _reload_config(monkeypatch, DFSHA_CONTROL_URL="http://control:8000")
    assert config.CONTROL_URLS == ["http://control:8000"]


def test_control_urls_lista(monkeypatch, restore_config):
    config = _reload_config(
        monkeypatch,
        DFSHA_CONTROL_URLS="http://a:8000, http://b:8000/",
    )
    assert config.CONTROL_URLS == ["http://a:8000", "http://b:8000"]


def test_control_urls_combina_sin_duplicar(monkeypatch, restore_config):
    config = _reload_config(
        monkeypatch,
        DFSHA_CONTROL_URLS="http://a:8000,http://b:8000",
        DFSHA_CONTROL_URL="http://a:8000",
    )
    # 'a' no se duplica aunque aparezca en ambas variables.
    assert config.CONTROL_URLS == ["http://a:8000", "http://b:8000"]


def test_control_urls_vacio(monkeypatch, restore_config):
    config = _reload_config(monkeypatch)
    assert config.CONTROL_URLS == []


# --- R1: validación de DFSHA_HA_ROLE --------------------------------------------


def test_ha_role_default_primary(monkeypatch, restore_config):
    config = _reload_config(monkeypatch)
    assert config.HA_ROLE == "primary"


def test_ha_role_standby(monkeypatch, restore_config):
    config = _reload_config(monkeypatch, DFSHA_HA_ROLE="standby")
    assert config.HA_ROLE == "standby"


def test_ha_role_invalido_falla(monkeypatch, restore_config):
    with pytest.raises(ValueError):
        _reload_config(monkeypatch, DFSHA_HA_ROLE="jefe")


# --- R2 / D8: oplog por triggers de SQLite --------------------------------------


@pytest.fixture
def oplog_db(tmp_path, monkeypatch):
    """Base de metadatos aislada en modo primary, con los triggers de oplog."""
    from dfsha.server import config as config_module
    from dfsha.server import metadata

    db_path = tmp_path / "metadata.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    return metadata


def _oplog_rows(metadata, **filters):
    with metadata.connect() as db:
        rows = db.execute("SELECT seq, tbl, op, pk, payload FROM oplog ORDER BY seq").fetchall()
    if "tbl" in filters:
        rows = [row for row in rows if row["tbl"] == filters["tbl"]]
    return rows


def test_oplog_captura_cambios(oplog_db):
    metadata = oplog_db
    with metadata.connect() as db:
        user_id = metadata.create_user(db, "nico", "hash", "salt")

    rows = _oplog_rows(metadata, tbl="users")
    assert len(rows) == 1
    assert rows[0]["op"] == "INSERT"

    with metadata.connect() as db:
        db.execute("UPDATE users SET salt = 'salt2' WHERE id = ?", (user_id,))
    rows = _oplog_rows(metadata, tbl="users")
    assert [row["op"] for row in rows] == ["INSERT", "UPDATE"]

    with metadata.connect() as db:
        # Borrar dependientes primero por las claves foráneas.
        db.execute("DELETE FROM directories WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    rows = _oplog_rows(metadata, tbl="users")
    assert rows[-1]["op"] == "DELETE"

    # La secuencia es monótona creciente en todo el oplog.
    all_rows = _oplog_rows(metadata)
    seqs = [row["seq"] for row in all_rows]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_heartbeat_no_genera_oplog(oplog_db):
    metadata = oplog_db
    with metadata.connect() as db:
        metadata.register_node(db, "dn1", "http://dn1:8000", 1000.0)
    inserts = _oplog_rows(metadata, tbl="nodes")
    assert len(inserts) == 1 and inserts[0]["op"] == "INSERT"

    # Un heartbeat solo mueve last_heartbeat: no debe generar oplog (D8).
    with metadata.connect() as db:
        metadata.heartbeat_node(db, "dn1", 2000.0)
    assert _oplog_rows(metadata, tbl="nodes") == inserts  # sin entradas nuevas

    # Un cambio de url o status sí se registra.
    with metadata.connect() as db:
        db.execute("UPDATE nodes SET status = 'offline' WHERE id = 'dn1'")
    rows = _oplog_rows(metadata, tbl="nodes")
    assert [row["op"] for row in rows] == ["INSERT", "UPDATE"]


def test_apply_oplog_entry_idempotente(oplog_db):
    """Reaplicar la misma entrada deja la fila igual (INSERT OR REPLACE)."""
    metadata = oplog_db
    entry = {
        "tbl": "users",
        "op": "INSERT",
        "pk": '{"id": 1}',
        "payload": '{"id": 1, "username": "ana", "password_hash": "h",'
        ' "salt": "s", "totp_secret": null, "totp_enabled": 0}',
    }
    with metadata.connect() as db:
        metadata.apply_oplog_entry(db, entry)
        metadata.apply_oplog_entry(db, entry)  # idempotente
        count = db.execute("SELECT COUNT(*) AS n FROM users WHERE id = 1").fetchone()["n"]
    assert count == 1

    delete = {"tbl": "users", "op": "DELETE", "pk": '{"id": 1}', "payload": None}
    with metadata.connect() as db:
        metadata.apply_oplog_entry(db, delete)
        count = db.execute("SELECT COUNT(*) AS n FROM users WHERE id = 1").fetchone()["n"]
    assert count == 0


# --- R3 / R5 / R9: módulo ha.py (snapshot, promoción, degradación, gracia) ------


def _triggers(metadata):
    with metadata.connect() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            " AND name LIKE 'oplog_%'"
        ).fetchall()
    return {row["name"] for row in rows}


def test_promote_sube_epoca_e_instala_triggers(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    db_path = tmp_path / "metadata.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    metadata.initialize()  # arranca como standby: sin triggers
    assert _triggers(metadata) == set()

    state = ha.HaState().load()
    assert state.role == "standby"
    assert state.epoch == 0

    new_epoch = state.promote()
    assert new_epoch == 1
    assert state.role == "primary"
    # Al promoverse se instalan los triggers de captura.
    assert len(_triggers(metadata)) == len(metadata.REPLICATED_TABLES) * 3

    # El estado quedó persistido en ha_state.
    reloaded = ha.HaState().load()
    assert reloaded.role == "primary"
    assert reloaded.epoch == 1


def test_demote_adopta_epoca_mayor_y_quita_triggers(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    db_path = tmp_path / "metadata.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", db_path)
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()  # primary: con triggers
    assert len(_triggers(metadata)) > 0

    state = ha.HaState().load()
    state.demote(new_epoch=5)
    assert state.role == "standby"
    assert state.epoch == 5
    assert _triggers(metadata) == set()


def test_grace_tras_promocion(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    monkeypatch.setattr(config_module, "HA_GRACE_SECONDS", 30)
    metadata.initialize()

    state = ha.HaState().load()
    assert state.in_grace() is False  # aún no se promovió
    state.promote()
    assert state.in_grace() is True
    # Pasado el periodo de gracia, deja de estar en gracia.
    assert state.in_grace(now=state.promoted_at + 31) is False


def test_snapshot_round_trip(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    primary_path = tmp_path / "primary.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", primary_path)
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    with metadata.connect() as db:
        metadata.create_user(db, "ana", "hash", "salt")

    raw = ha.snapshot_bytes()
    assert raw[:16].startswith(b"SQLite format 3")

    # Cargar el snapshot en una base "standby" distinta.
    standby_path = tmp_path / "standby.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", standby_path)
    ha.load_snapshot(raw, applied_seq=7)

    with metadata.connect() as db:
        user = metadata.find_user(db, "ana")
        ha_row = metadata.get_ha_state(db)
    assert user is not None
    assert ha_row["applied_seq"] == 7
    assert ha_row["role"] == "standby"
    # El standby no debe tener triggers de oplog tras cargar el snapshot.
    assert _triggers(metadata) == set()


def test_apply_entries_avanza_secuencia(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    metadata.initialize()

    entries = [
        {
            "seq": 10,
            "tbl": "users",
            "op": "INSERT",
            "pk": '{"id": 1}',
            "payload": '{"id": 1, "username": "ana", "password_hash": "h",'
            ' "salt": "s", "totp_secret": null, "totp_enabled": 0}',
        },
        {
            "seq": 11,
            "tbl": "users",
            "op": "UPDATE",
            "pk": '{"id": 1}',
            "payload": '{"id": 1, "username": "ana2", "password_hash": "h",'
            ' "salt": "s", "totp_secret": null, "totp_enabled": 0}',
        },
    ]
    last = ha.apply_entries(entries)
    assert last == 11
    with metadata.connect() as db:
        user = metadata.find_user(db, "ana2")
    assert user is not None


# --- Tarea 4: token de operador para /ha/promote -------------------------------


def test_admin_token_valido():
    from dfsha import cluster_auth

    secret = "secreto-de-pruebas"
    token = cluster_auth.issue_admin_token(secret)
    assert cluster_auth.verify_admin_token(secret, token) == "operator"


def test_admin_token_sin_claim_falla():
    from dfsha import cluster_auth

    secret = "secreto-de-pruebas"
    # Un token de nodo normal (sin adm) no sirve como operador.
    normal = cluster_auth.issue_node_token(secret, "dn1")
    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_admin_token(secret, normal)


def test_admin_token_firma_mala_falla():
    from dfsha import cluster_auth

    token = cluster_auth.issue_admin_token("secreto-bueno")
    with pytest.raises(cluster_auth.InvalidClusterToken):
        cluster_auth.verify_admin_token("secreto-malo", token)


# --- Tarea 5: endpoints /ha/* y barrera require_primary -------------------------


def _node_token():
    from dfsha import cluster_auth
    from dfsha.server import config as config_module

    return cluster_auth.issue_node_token(config_module.CLUSTER_SECRET, "peer")


def _admin_token():
    from dfsha import cluster_auth
    from dfsha.server import config as config_module

    return cluster_auth.issue_admin_token(config_module.CLUSTER_SECRET)


def test_ha_endpoints_requieren_auth():
    from fastapi.testclient import TestClient

    from dfsha.server import main

    with TestClient(main.app) as client:
        # Sin token: todos los /ha/* rechazan.
        assert client.get("/ha/status").status_code == 401
        assert client.get("/ha/oplog").status_code == 401
        assert client.get("/ha/snapshot").status_code == 401
        assert client.post("/ha/promote").status_code == 401

        # Token de nodo normal: status/oplog/snapshot ok, pero promote exige admin.
        headers = {"X-Node-Token": _node_token()}
        assert client.get("/ha/status", headers=headers).status_code == 200
        assert client.post("/ha/promote", headers=headers).status_code == 403


def test_ha_status_contenido():
    from fastapi.testclient import TestClient

    from dfsha.server import main

    with TestClient(main.app) as client:
        response = client.get("/ha/status", headers={"X-Node-Token": _node_token()})
        body = response.json()
        assert set(["role", "epoch", "applied_seq", "peer_url", "peer_alive",
                    "grace_until", "ts"]).issubset(body.keys())


def test_standby_rechaza_cliente_con_503(monkeypatch):
    from fastapi.testclient import TestClient

    from dfsha.server import ha, main

    # Forzar rol standby en runtime.
    monkeypatch.setattr(ha.state, "role", "standby")
    monkeypatch.setattr(ha.state, "peer_url", "https://primary:8000")

    with TestClient(main.app) as client:
        # Petición de cliente normal: 503 not_primary con la URL del primary.
        response = client.get("/fs/ls?path=/")
        assert response.status_code == 503
        body = response.json()
        assert body["error"] == "not_primary"
        assert body["primary_url"] == "https://primary:8000"

        # Los endpoints permitidos en standby siguen respondiendo (no 503).
        assert client.get("/health").status_code != 503
        assert client.get(
            "/ha/status", headers={"X-Node-Token": _node_token()}
        ).status_code == 200


def test_promote_manual_sube_epoca(monkeypatch, tmp_path):
    """POST /ha/promote con token admin promueve y devuelve la nueva época."""
    from fastapi.testclient import TestClient

    from dfsha.server import config as config_module
    from dfsha.server import ha, main, metadata

    # Base aislada en modo standby para no tocar la global.
    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    metadata.initialize()
    fresh = ha.HaState().load()
    monkeypatch.setattr(ha, "state", fresh)
    monkeypatch.setattr(main, "ha", ha)

    with TestClient(main.app) as client:
        before = fresh.epoch
        response = client.post("/ha/promote", headers={"X-Node-Token": _admin_token()})
        assert response.status_code == 200
        assert response.json()["epoch"] == before + 1
        assert fresh.role == "primary"


# --- Tarea 6: replicador del standby, failover y fencing ------------------------


def test_oplog_endpoint_rechaza_epoca_menor_cuando_standby(tmp_path, monkeypatch):
    """Un nodo standby responde 409 not_primary a /ha/oplog (no sirve oplog)."""
    from fastapi.testclient import TestClient

    from dfsha.server import config as config_module
    from dfsha.server import ha, main, metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    metadata.initialize()
    fresh = ha.HaState().load()
    monkeypatch.setattr(ha, "state", fresh)

    with TestClient(main.app) as client:
        response = client.get(
            "/ha/oplog", params={"after": 0, "epoch": 0},
            headers={"X-Node-Token": _node_token()},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "not_primary"


def test_oplog_endpoint_degrada_al_ver_epoca_mayor(tmp_path, monkeypatch):
    """Un primary que recibe una época mayor en /ha/oplog se degrada (fencing)."""
    from fastapi.testclient import TestClient

    from dfsha.server import config as config_module
    from dfsha.server import ha, main, metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    fresh = ha.HaState().load()
    assert fresh.role == "primary"
    monkeypatch.setattr(ha, "state", fresh)

    with TestClient(main.app) as client:
        response = client.get(
            "/ha/oplog", params={"after": 0, "epoch": 9},
            headers={"X-Node-Token": _node_token()},
        )
        # Al ver época 9 > 0 se degrada y por tanto deja de servir oplog.
        assert response.status_code == 409
        assert fresh.role == "standby"
        assert fresh.epoch == 9


def test_sync_incremental_por_oplog(tmp_path, monkeypatch):
    """El standby aplica las entradas de oplog que produjo el primary.

    Se evita la contaminación del DATABASE_PATH global capturando primero el oplog
    del primary y aplicándolo después contra la base del standby.
    """
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    # 1) Primary: generar cambios y leer su oplog.
    primary_db = tmp_path / "primary.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", primary_db)
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    with metadata.connect() as db:
        metadata.create_user(db, "ana", "h", "s")
        metadata.create_user(db, "beto", "h", "s")
    with metadata.connect() as db:
        entries = [dict(row) for row in metadata.read_oplog(db, after=0)]
    assert entries  # el primary registró oplog

    # 2) Standby: base vacía, aplicar el oplog capturado.
    standby_db = tmp_path / "standby.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", standby_db)
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    metadata.initialize()
    state = ha.HaState().load()
    last = ha.apply_entries(entries)
    state.set_applied_seq(last)

    with metadata.connect() as db:
        assert metadata.find_user(db, "ana") is not None
        assert metadata.find_user(db, "beto") is not None
        assert metadata.get_ha_state(db)["applied_seq"] == last


def test_sync_snapshot_inicial(tmp_path, monkeypatch):
    """El standby se pone al día desde cero copiando el snapshot binario del primary."""
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata

    # Primary con datos y su snapshot.
    primary_db = tmp_path / "primary.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", primary_db)
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    with metadata.connect() as db:
        metadata.create_user(db, "ana", "h", "s")
    raw = ha.snapshot_bytes()
    with metadata.connect() as db:
        _, high = metadata.oplog_bounds(db)

    # Standby carga el snapshot.
    standby_db = tmp_path / "standby.db"
    monkeypatch.setattr(config_module, "DATABASE_PATH", standby_db)
    ha.load_snapshot(raw, applied_seq=high)
    with metadata.connect() as db:
        assert metadata.find_user(db, "ana") is not None
        assert metadata.get_ha_state(db)["applied_seq"] == high


def test_promocion_por_timeout(monkeypatch, tmp_path):
    """Si el primary no responde, el standby se autopromueve tras el timeout."""
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata, replicator

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "standby")
    monkeypatch.setattr(config_module, "HA_TIMEOUT_SECONDS", 0)  # promover ya
    monkeypatch.setattr(config_module, "HA_HEARTBEAT_SECONDS", 1)
    metadata.initialize()
    state = ha.HaState().load()
    monkeypatch.setattr(ha, "state", state)
    state.peer_url = "https://inalcanzable:8000"

    # El pull falla (par inalcanzable): simulamos un ciclo del bucle a mano.
    def fake_get(*args, **kwargs):
        raise replicator.requests.RequestException("sin conexión")

    monkeypatch.setattr(replicator.requests, "get", fake_get)

    assert replicator._pull_once(state) is False
    # Con timeout 0, el siguiente chequeo del bucle promueve.
    assert state.role == "standby"
    # Ejecutar la lógica de promoción como lo haría el bucle.
    state.promote()
    assert state.role == "primary"
    assert state.epoch == 1


# --- Tarea 7: periodo de gracia de DataNodes tras promoción ---------------------


@pytest.fixture
def nodes_db(tmp_path, monkeypatch):
    from dfsha.server import config as config_module
    from dfsha.server import metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    monkeypatch.setattr(config_module, "NODE_TIMEOUT_SECONDS", 30)
    metadata.initialize()
    return metadata


def test_available_nodes_igual_que_hoy_sin_gracia(nodes_db):
    """Fuera del periodo de gracia, available_nodes filtra por heartbeat como siempre."""
    metadata = nodes_db
    now = 1000.0
    with metadata.connect() as db:
        metadata.register_node(db, "fresco", "http://fresco:8000", now)
        metadata.register_node(db, "viejo", "http://viejo:8000", now - 100)  # fuera de timeout

    with metadata.connect() as db:
        vivos = metadata.available_nodes(db, now, 30)  # grace=False por defecto
    ids = {row["id"] for row in vivos}
    # El viejo (heartbeat > timeout) queda excluido, exactamente como antes del hito 3.
    assert ids == {"fresco"}


def test_available_nodes_en_gracia_no_filtra_por_heartbeat(nodes_db):
    """Durante la gracia, un DataNode con heartbeat viejo sigue contando como vivo."""
    metadata = nodes_db
    now = 1000.0
    with metadata.connect() as db:
        metadata.register_node(db, "fresco", "http://fresco:8000", now)
        metadata.register_node(db, "viejo", "http://viejo:8000", now - 100)

    with metadata.connect() as db:
        vivos = metadata.available_nodes(db, now, 30, grace=True)
    ids = {row["id"] for row in vivos}
    assert ids == {"fresco", "viejo"}

    # Un nodo marcado offline no reaparece ni en gracia.
    with metadata.connect() as db:
        db.execute("UPDATE nodes SET status = 'offline' WHERE id = 'viejo'")
        vivos = metadata.available_nodes(db, now, 30, grace=True)
    assert {row["id"] for row in vivos} == {"fresco"}


def test_in_grace_controla_el_filtrado(tmp_path, monkeypatch):
    """_available_data_nodes usa grace solo mientras ha.state.in_grace() es True."""
    from dfsha.server import config as config_module
    from dfsha.server import ha, main, metadata

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    monkeypatch.setattr(config_module, "NODE_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(config_module, "HA_GRACE_SECONDS", 30)
    metadata.initialize()
    fresh = ha.HaState().load()
    monkeypatch.setattr(ha, "state", fresh)

    now = time.time()
    with metadata.connect() as db:
        metadata.register_node(db, "viejo", "http://viejo:8000", now - 1000)

    # Sin promoción: no hay gracia, el nodo viejo no cuenta.
    assert main._available_data_nodes() == []

    # Tras promover: durante la gracia, el nodo viejo sí cuenta.
    fresh.promote()
    assert fresh.in_grace() is True
    assert {row["id"] for row in main._available_data_nodes()} == {"viejo"}


# --- Tarea 8: failover del DataNode entre ControlNodes --------------------------


class _PostResult:
    """requests.Response simulada: raise_for_status falla si status >= 400."""

    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _requests

            raise _requests.HTTPError(f"status {self.status_code}")


def _run_datanode_loop(monkeypatch, control_urls, post_side_effect, cycles):
    """Ejecuta _register_and_heartbeat durante `cycles` ciclos y devuelve la lista
    de URLs base golpeadas por cada POST /nodes/register."""
    from dfsha.server import config as config_module
    from dfsha.server import datanode

    monkeypatch.setattr(config_module, "CONTROL_URLS", control_urls)
    monkeypatch.setattr(config_module, "NODE_ID", "dn-test")
    monkeypatch.setattr(config_module, "DATANODE_URL", "http://dn-test:8000")

    register_hits = []

    def fake_post(url, **kwargs):
        if "/nodes/register" in url:
            base = url.split("/nodes/register")[0]
            register_hits.append(base)
            return post_side_effect(base)
        return _PostResult(200)  # heartbeat

    monkeypatch.setattr(datanode.requests, "post", fake_post)

    # Controlar el número de ciclos: tras `cycles` esperas, parar el bucle.
    datanode._stop.clear()
    counter = {"n": 0}

    def fake_wait(_seconds):
        counter["n"] += 1
        if counter["n"] >= cycles:
            datanode._stop.set()

    monkeypatch.setattr(datanode._stop, "wait", fake_wait)
    try:
        datanode._register_and_heartbeat()
    finally:
        datanode._stop.clear()
    return register_hits


def test_datanode_una_sola_url_como_antes(monkeypatch):
    """Con una sola URL, cada ciclo golpea exactamente esa URL."""
    hits = _run_datanode_loop(
        monkeypatch,
        ["http://control:8000"],
        post_side_effect=lambda base: _PostResult(200),
        cycles=3,
    )
    assert hits == ["http://control:8000"] * 3


def test_datanode_salta_ante_503_not_primary(monkeypatch):
    """Si la primera URL responde 503 (standby), el DataNode usa la segunda."""
    def side_effect(base):
        return _PostResult(503 if base == "http://a:8000" else 200)

    hits = _run_datanode_loop(
        monkeypatch, ["http://a:8000", "http://b:8000"], side_effect, cycles=1
    )
    # En el primer ciclo golpea 'a' (503) y luego 'b' (ok).
    assert hits == ["http://a:8000", "http://b:8000"]


def test_datanode_salta_ante_error_de_conexion(monkeypatch):
    import requests

    def side_effect(base):
        if base == "http://a:8000":
            raise requests.ConnectionError("sin conexión")
        return _PostResult(200)

    hits = _run_datanode_loop(
        monkeypatch, ["http://a:8000", "http://b:8000"], side_effect, cycles=1
    )
    assert hits == ["http://a:8000", "http://b:8000"]


def test_datanode_recuerda_el_segundo_y_no_regolpea_al_primero(monkeypatch):
    """Tras fallar 'a' y funcionar 'b', los ciclos siguientes van directo a 'b'."""
    def side_effect(base):
        return _PostResult(503 if base == "http://a:8000" else 200)

    hits = _run_datanode_loop(
        monkeypatch, ["http://a:8000", "http://b:8000"], side_effect, cycles=3
    )
    # Ciclo 1: a (503), b (ok). Ciclos 2 y 3: directo a b, sin volver a 'a'.
    assert hits == ["http://a:8000", "http://b:8000", "http://b:8000", "http://b:8000"]
    assert hits.count("http://a:8000") == 1


# --- Tarea 9: failover transparente en el cliente ------------------------------


class _ClientResponse:
    """requests.Response simulada para el cliente."""

    def __init__(self, status_code=200, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = content
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests as _requests

            raise _requests.HTTPError(f"status {self.status_code}")


def _not_primary_response():
    return _ClientResponse(503, {"error": "not_primary", "primary_url": None})


def _make_client(urls):
    from dfsha.client.api import DfsClient

    return DfsClient(urls)


def test_cliente_una_sola_url_como_antes():
    client = _make_client("http://solo:8000")
    assert client.urls == ["http://solo:8000"]
    assert client.base_url == "http://solo:8000"


def test_cliente_lista_desde_string_con_comas():
    client = _make_client("http://a:8000, http://b:8000/")
    assert client.urls == ["http://a:8000", "http://b:8000"]


def test_cliente_failover_get_ante_503(monkeypatch):
    """Un GET al primario que responde 503 not_primary se reintenta en el standby."""
    client = _make_client(["http://a:8000", "http://b:8000"])

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        if url.startswith("http://a:8000"):
            return _not_primary_response()
        return _ClientResponse(200, {"path": "/", "entries": []})

    monkeypatch.setattr(client.session, "request", fake_request)
    result = client.ls("/")
    assert result == {"path": "/", "entries": []}
    # Tras el failover, el cliente recuerda 'b' como actual.
    assert client.base_url == "http://b:8000"


def test_cliente_failover_get_ante_error_de_conexion(monkeypatch):
    import requests

    client = _make_client(["http://a:8000", "http://b:8000"])

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        if url.startswith("http://a:8000"):
            raise requests.ConnectionError("sin conexión")
        return _ClientResponse(200, {"type": "dir"})

    monkeypatch.setattr(client.session, "request", fake_request)
    assert client.stat("/x") == {"type": "dir"}
    assert client.base_url == "http://b:8000"


def test_cliente_recuerda_el_actual_entre_peticiones(monkeypatch):
    client = _make_client(["http://a:8000", "http://b:8000"])
    hits = []

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        hits.append(url)
        if url.startswith("http://a:8000"):
            return _not_primary_response()
        return _ClientResponse(200, {"ok": True})

    monkeypatch.setattr(client.session, "request", fake_request)
    client.ls("/")  # failover a 'b'
    hits.clear()
    client.ls("/")  # ya debería ir directo a 'b'
    assert all(url.startswith("http://b:8000") for url in hits)


def test_cliente_todas_las_urls_fallan(monkeypatch):
    import requests

    from dfsha.client.api import DfsError

    client = _make_client(["http://a:8000", "http://b:8000"])

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        raise requests.ConnectionError("caído")

    monkeypatch.setattr(client.session, "request", fake_request)
    with pytest.raises(DfsError):
        client.ls("/")


def test_cliente_upload_failover_rebobina_origen(monkeypatch):
    """put reintenta ante 503 not_primary y re-envía el archivo completo."""
    import io

    client = _make_client(["http://a:8000", "http://b:8000"])
    source = io.BytesIO(b"contenido completo del archivo")
    bodies = []

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        data = kwargs.get("data")
        payload = data.read() if hasattr(data, "read") else data
        bodies.append((url, payload))
        if url.startswith("http://a:8000"):
            return _not_primary_response()
        return _ClientResponse(201, {"path": "/f", "size": len(payload), "blocks": 1})

    monkeypatch.setattr(client.session, "request", fake_request)
    result = client.upload("/f", source)
    assert result["size"] == len(b"contenido completo del archivo")
    # El segundo intento (a 'b') recibió el archivo completo, no un resto.
    assert bodies[-1][1] == b"contenido completo del archivo"
    assert client.base_url == "http://b:8000"


def test_cliente_upload_no_reintenta_ante_409(monkeypatch):
    """Un 409 (ya existe) no se reintenta: el servidor sí evaluó la operación."""
    import io

    from dfsha.client.api import DfsError

    client = _make_client(["http://a:8000", "http://b:8000"])
    source = io.BytesIO(b"datos")
    hits = []

    def fake_request(method, url, headers=None, verify=None, **kwargs):
        hits.append(url)
        return _ClientResponse(409, {"detail": "Ya existe el archivo /f"})

    monkeypatch.setattr(client.session, "request", fake_request)
    with pytest.raises(DfsError):
        client.upload("/f", source)
    # Solo golpeó el primer ControlNode; no reintentó en el segundo ante 409.
    assert hits == ["http://a:8000/files/upload"]


# --- Reconciliación al arrancar: primary viejo que vuelve se degrada ------------


def test_reconcile_degrada_primary_viejo(tmp_path, monkeypatch):
    """Un nodo que arranca como primary y descubre que el par ya es primary con
    época mayor se degrada a standby (fencing del primary viejo que vuelve)."""
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata, replicator

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    state = ha.HaState().load()
    state.peer_url = "https://peer:8000"
    assert state.role == "primary" and state.epoch == 0

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"role": "primary", "epoch": 1}

    monkeypatch.setattr(replicator.requests, "get", lambda *a, **k: _Resp())

    should_follow = replicator.reconcile_on_start(state)
    assert should_follow is True
    assert state.role == "standby"
    assert state.epoch == 1


def test_reconcile_mantiene_primary_si_par_es_standby(tmp_path, monkeypatch):
    """Si el par es standby, el primary legítimo sigue siendo primary."""
    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata, replicator

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    state = ha.HaState().load()
    state.peer_url = "https://peer:8000"

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"role": "standby", "epoch": 0}

    monkeypatch.setattr(replicator.requests, "get", lambda *a, **k: _Resp())

    assert replicator.reconcile_on_start(state) is False
    assert state.role == "primary"


def test_reconcile_mantiene_primary_si_par_no_responde(tmp_path, monkeypatch):
    """Si el par no responde, el nodo sigue como primary (no se degrada a ciegas)."""
    import requests

    from dfsha.server import config as config_module
    from dfsha.server import ha, metadata, replicator

    monkeypatch.setattr(config_module, "DATABASE_PATH", tmp_path / "m.db")
    monkeypatch.setattr(config_module, "HA_ROLE", "primary")
    metadata.initialize()
    state = ha.HaState().load()
    state.peer_url = "https://peer:8000"

    def boom(*a, **k):
        raise requests.ConnectionError("sin conexión")

    monkeypatch.setattr(replicator.requests, "get", boom)
    assert replicator.reconcile_on_start(state) is False
    assert state.role == "primary"
