"""Pruebas unitarias de la alta disponibilidad del ControlNode (hito 3).

No dependen de Docker: usan recarga de módulos y TestClient de FastAPI.
"""

import importlib
import os

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
