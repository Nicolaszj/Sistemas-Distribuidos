import hashlib
import io
import time

import pytest
import requests

from dfsha.client.api import DfsClient, DfsError, OtpRequired
from dfsha.client.files import dfs_open
from dfsha.server import totp

DATA = bytes(range(256)) * 3  # 768 bytes -> 48 bloques de 16 bytes


def test_sin_tls_no_conecta(server):
    with pytest.raises(DfsError):
        DfsClient(server).register("x", "contrasena-segura")  # CA desconocida


def test_http_plano_rechazado(server):
    with pytest.raises(requests.RequestException):
        requests.get(server.replace("https", "http") + "/docs", timeout=3)


def test_put_get_con_checksums(make_client):
    client = make_client()
    result = client.upload("/a.bin", io.BytesIO(DATA))
    assert result["blocks"] == 48
    assert result["checksum"] == hashlib.sha256(DATA).hexdigest()

    plan = client.blocks("/a.bin")
    assert all(block["ticket"] and block["checksum"] for block in plan["blocks"])
    assert b"".join(client.read_block(block) for block in plan["blocks"]) == DATA


def test_ticket_no_sirve_para_otro_bloque(make_client, server, certs):
    client = make_client()
    client.upload("/a.bin", io.BytesIO(DATA))
    first, second = client.blocks("/a.bin")["blocks"][:2]

    response = requests.get(
        f"{server}/blocks/{second['id']}",
        headers={"X-Block-Ticket": first["ticket"]},
        verify=str(certs / "ca.crt"),
    )
    assert response.status_code == 403


def test_contrasena_corta_rechazada(server, certs):
    with pytest.raises(DfsError, match="8 caracteres"):
        DfsClient(server, ca_file=str(certs / "ca.crt")).register("corto", "123")


def test_bloqueo_por_fuerza_bruta(make_client, server, certs):
    victim = make_client()
    attacker = DfsClient(server, ca_file=str(certs / "ca.crt"))
    for _ in range(3):
        with pytest.raises(DfsError, match="incorrectos"):
            attacker.login(victim.username, "mala-contrasena")
    with pytest.raises(DfsError, match="Demasiados intentos"):
        attacker.login(victim.username, "contrasena-segura")

    time.sleep(2.5)
    attacker.login(victim.username, "contrasena-segura")


def test_2fa(make_client, server, certs):
    client = make_client()
    secret = client.setup_2fa()["secret"]
    with pytest.raises(DfsError):
        client.enable_2fa("000000" if totp.code_at(secret, time.time()) != "000000" else "111111")
    client.enable_2fa(totp.code_at(secret, time.time()))

    fresh = DfsClient(server, ca_file=str(certs / "ca.crt"))
    with pytest.raises(OtpRequired):
        fresh.login(client.username, "contrasena-segura")
    fresh.login(client.username, "contrasena-segura", totp.code_at(secret, time.time()))
    assert fresh.me()["totp_enabled"] is True


def test_api_keys(make_client, server, certs):
    client = make_client()
    created = client.create_api_key("backup")
    robot = DfsClient(server, ca_file=str(certs / "ca.crt"), api_key=created["key"])
    assert robot.me()["username"] == client.username

    assert [key["name"] for key in client.list_api_keys()] == ["backup"]
    client.delete_api_key(created["id"])
    with pytest.raises(DfsError, match="API key"):
        robot.me()


def test_acl_usuario_lectura(make_client):
    ana, beto = make_client(), make_client()
    ana.mkdir("/proyecto")
    ana.upload("/proyecto/doc.txt", io.BytesIO(b"hola beto"))
    ana.upload("/privado.txt", io.BytesIO(b"no tocar"))

    shared = f"/~{ana.username}/proyecto"
    with pytest.raises(DfsError, match="No existe"):
        beto.ls(shared)

    ana.grant("/proyecto", "r", user=beto.username)
    assert [entry["name"] for entry in beto.ls(shared)["entries"]] == ["doc.txt"]
    with dfs_open(beto, f"{shared}/doc.txt") as file:
        assert file.read() == b"hola beto"

    with pytest.raises(DfsError, match="permiso"):
        beto.upload(f"{shared}/nuevo.txt", io.BytesIO(b"x"))
    with pytest.raises(DfsError, match="permiso"):
        beto.rm(f"{shared}/doc.txt")
    with pytest.raises(DfsError, match="No existe"):
        beto.stat(f"/~{ana.username}/privado.txt")
    with pytest.raises(DfsError, match="dueño"):
        beto.grant(shared, "rw", user=beto.username)

    assert beto.shared()[0]["path"] == shared
    ana.revoke("/proyecto", user=beto.username)
    with pytest.raises(DfsError):
        beto.ls(shared)


def test_acl_grupo_escritura(make_client):
    ana, beto, carla = make_client(), make_client(), make_client()
    ana.create_group(f"equipo{ana.username}")
    group = f"equipo{ana.username}"
    ana.add_member(group, beto.username)
    ana.mkdir("/compartido")
    ana.grant("/compartido", "rw", group=group)

    path = f"/~{ana.username}/compartido"
    beto.mkdir(f"{path}/sub")
    beto.upload(f"{path}/sub/de-beto.txt", io.BytesIO(b"aporte"))
    assert ana.ls("/compartido/sub")["entries"][0]["name"] == "de-beto.txt"

    with pytest.raises(DfsError):
        carla.ls(path)
    with pytest.raises(DfsError, match="dueño"):
        beto.add_member(group, carla.username)

    ana.remove_member(group, beto.username)
    with pytest.raises(DfsError):
        beto.ls(path)


def test_bloque_ajeno_sin_permiso(make_client):
    ana, beto = make_client(), make_client()
    ana.upload("/a.bin", io.BytesIO(DATA))
    block_id = ana.blocks("/a.bin")["blocks"][0]["id"]
    with pytest.raises(DfsError, match="No existe el bloque"):
        beto.read_block(block_id)


def test_rf3_lectura_por_rangos(make_client):
    client = make_client()
    client.upload("/a.bin", io.BytesIO(DATA))
    with dfs_open(client, "/a.bin") as file:
        file.seek(100)
        assert file.read(50) == DATA[100:150]
        assert file.tell() == 150
        file.seek(-10, io.SEEK_END)
        assert file.read() == DATA[-10:]
        file.seek(0)
        assert file.read() == DATA


def test_rf3_escritura_y_append(make_client):
    client = make_client()
    with dfs_open(client, "/log.txt", "w") as file:
        file.write("linea 1\n")
    with dfs_open(client, "/log.txt", "a") as file:
        file.write("linea 2\n")
    with dfs_open(client, "/log.txt") as file:
        assert file.read() == b"linea 1\nlinea 2\n"
    with dfs_open(client, "/log.txt", "w") as file:
        file.write("nuevo")
    with dfs_open(client, "/log.txt") as file:
        assert file.read() == b"nuevo"
    assert client.stat("/log.txt")["checksum"] == hashlib.sha256(b"nuevo").hexdigest()


def test_rf3_lock_exclusivo(make_client):
    ana, beto = make_client(), make_client()
    ana.mkdir("/equipo")
    ana.grant("/equipo", "rw", user=beto.username)
    ana.upload("/equipo/plan.txt", io.BytesIO(b"v1"))
    path = f"/~{ana.username}/equipo/plan.txt"

    handle = dfs_open(beto, path, "w")
    handle.write("v2 de beto")
    assert ana.stat("/equipo/plan.txt")["locked"] is True
    with pytest.raises(DfsError, match="bloqueado"):
        ana.upload("/equipo/plan.txt", io.BytesIO(b"v2 de ana"), overwrite=True)
    with pytest.raises(DfsError, match="bloqueado"):
        ana.rm("/equipo/plan.txt")
    handle.close()

    assert ana.stat("/equipo/plan.txt")["locked"] is False
    with dfs_open(ana, "/equipo/plan.txt") as file:
        assert file.read() == b"v2 de beto"
    ana.upload("/equipo/plan.txt", io.BytesIO(b"v3"), overwrite=True)


def test_lock_expira(make_client):
    ana, beto = make_client(), make_client()
    ana.mkdir("/equipo")
    ana.grant("/equipo", "rw", user=beto.username)
    ana.upload("/equipo/x.txt", io.BytesIO(b"x"))
    beto.lock(f"/~{ana.username}/equipo/x.txt", ttl=1)
    time.sleep(1.2)
    ana.upload("/equipo/x.txt", io.BytesIO(b"y"), overwrite=True)


def test_subida_sin_overwrite_no_pisa(make_client):
    client = make_client()
    client.upload("/a.txt", io.BytesIO(b"uno"))
    with pytest.raises(DfsError, match="Ya existe"):
        client.upload("/a.txt", io.BytesIO(b"dos"))
