import hashlib
import secrets
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

import jwt
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .. import cluster_auth, paths
from . import access, auth, config, metadata, storage, totp

app = FastAPI(title="DFSha", version="0.2.0")
bearer = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

metadata.initialize()


class Credentials(BaseModel):
    username: str
    password: str
    otp: Optional[str] = None


class PathRequest(BaseModel):
    path: str


class CodeRequest(BaseModel):
    code: str


class NameRequest(BaseModel):
    name: str


class MemberRequest(BaseModel):
    username: str


class GrantRequest(BaseModel):
    path: str
    user: Optional[str] = None
    group: Optional[str] = None
    permission: Literal["r", "rw"] = "r"


class RevokeRequest(BaseModel):
    path: str
    user: Optional[str] = None
    group: Optional[str] = None


class LockRequest(BaseModel):
    path: str
    ttl: Optional[int] = None


class NodeRegistration(BaseModel):
    node_id: str
    url: str


def _node_identity(node_id, token):
    if not token:
        raise HTTPException(status_code=401, detail="Falta X-Node-Token")
    try:
        authenticated = cluster_auth.verify_node_token(config.CLUSTER_SECRET, token)
    except cluster_auth.InvalidClusterToken as error:
        raise HTTPException(status_code=403, detail=f"Token de nodo inválido: {error}")
    if authenticated != node_id:
        raise HTTPException(status_code=403, detail="El token no corresponde al nodo")


@app.post("/nodes/register")
def register_node(
    registration: NodeRegistration,
    token: Optional[str] = Header(None, alias="X-Node-Token"),
):
    _node_identity(registration.node_id, token)
    if not registration.node_id or not registration.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Registro de nodo inválido")
    with metadata.connect() as db:
        metadata.register_node(db, registration.node_id, registration.url, time.time())
    return {"node_id": registration.node_id, "status": "online"}


@app.post("/nodes/{node_id}/heartbeat")
def heartbeat_node(
    node_id: str,
    token: Optional[str] = Header(None, alias="X-Node-Token"),
):
    _node_identity(node_id, token)
    with metadata.connect() as db:
        if not metadata.heartbeat_node(db, node_id, time.time()):
            raise HTTPException(status_code=404, detail="Nodo no registrado")
    return {"node_id": node_id, "status": "online"}


# --- Autenticación --------------------------------------------------------------


def optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    api_key: Optional[str] = Depends(api_key_header),
):
    if credentials is not None:
        try:
            user_id, _ = auth.read_token(credentials.credentials)
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
        return user_id

    if api_key:
        with metadata.connect() as db:
            row = metadata.find_api_key(db, auth.hash_api_key(api_key))
        if row is None:
            raise HTTPException(status_code=401, detail="API key inválida")
        return row["user_id"]

    return None


def current_user(user_id: Optional[int] = Depends(optional_user)):
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Falta autenticación (Bearer token o X-API-Key)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


@app.post("/auth/register", status_code=201)
def register(credentials: Credentials):
    if not paths.is_valid_name(credentials.username) or credentials.username.startswith("~"):
        raise HTTPException(status_code=400, detail="Nombre de usuario inválido")
    if len(credentials.password) < 8:
        raise HTTPException(
            status_code=400, detail="La contraseña debe tener al menos 8 caracteres"
        )

    password_hash, salt = auth.hash_password(credentials.password)
    with metadata.connect() as db:
        try:
            user_id = metadata.create_user(db, credentials.username, password_hash, salt)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="El usuario ya existe")

    return {"id": user_id, "username": credentials.username}


@app.post("/auth/login")
def login(credentials: Credentials):
    wait = auth.throttle.blocked_for(credentials.username)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos, espera {wait} s",
            headers={"Retry-After": str(wait)},
        )

    with metadata.connect() as db:
        user = metadata.find_user(db, credentials.username)

    if user is None or not auth.verify_password(
        credentials.password, user["salt"], user["password_hash"]
    ):
        auth.throttle.failed(credentials.username)
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    if user["totp_enabled"]:
        if not credentials.otp:
            raise HTTPException(
                status_code=401,
                detail="Se requiere el código 2FA",
                headers={"X-DFSha-OTP": "required"},
            )
        if not totp.verify(user["totp_secret"], credentials.otp):
            auth.throttle.failed(credentials.username)
            raise HTTPException(status_code=401, detail="Código 2FA incorrecto")

    auth.throttle.succeeded(credentials.username)
    return {
        "token": auth.create_token(user["id"], user["username"]),
        "expires_in": config.TOKEN_TTL_SECONDS,
    }


@app.get("/auth/me")
def me(user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        user = metadata.find_user_by_id(db, user_id)
        groups = [row["name"] for row in metadata.user_groups(db, user_id)]
    return {
        "id": user_id,
        "username": user["username"],
        "totp_enabled": bool(user["totp_enabled"]),
        "groups": groups,
    }


@app.post("/auth/2fa/setup")
def setup_2fa(user_id: int = Depends(current_user)):
    secret = totp.new_secret()
    with metadata.connect() as db:
        user = metadata.find_user_by_id(db, user_id)
        if user["totp_enabled"]:
            raise HTTPException(status_code=409, detail="El 2FA ya está activo")
        metadata.set_totp(db, user_id, secret, enabled=False)

    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, user["username"]),
    }


@app.post("/auth/2fa/enable")
def enable_2fa(request: CodeRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        user = metadata.find_user_by_id(db, user_id)
        if not user["totp_secret"]:
            raise HTTPException(status_code=400, detail="Primero ejecuta /auth/2fa/setup")
        if not totp.verify(user["totp_secret"], request.code):
            raise HTTPException(status_code=400, detail="Código 2FA incorrecto")
        metadata.set_totp(db, user_id, user["totp_secret"], enabled=True)
    return {"totp_enabled": True}


@app.post("/auth/2fa/disable")
def disable_2fa(request: CodeRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        user = metadata.find_user_by_id(db, user_id)
        if not user["totp_enabled"]:
            raise HTTPException(status_code=400, detail="El 2FA no está activo")
        if not totp.verify(user["totp_secret"], request.code):
            raise HTTPException(status_code=400, detail="Código 2FA incorrecto")
        metadata.set_totp(db, user_id, None, enabled=False)
    return {"totp_enabled": False}


@app.post("/auth/apikeys", status_code=201)
def create_api_key(request: NameRequest, user_id: int = Depends(current_user)):
    key, prefix = auth.new_api_key()
    with metadata.connect() as db:
        key_id = metadata.create_api_key(
            db, user_id, request.name, prefix, auth.hash_api_key(key)
        )
    return {"id": key_id, "name": request.name, "key": key, "prefix": prefix}


@app.get("/auth/apikeys")
def list_api_keys(user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        rows = metadata.list_api_keys(db, user_id)
    return {"keys": [dict(row) for row in rows]}


@app.delete("/auth/apikeys/{key_id}")
def delete_api_key(key_id: int, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        if not metadata.delete_api_key(db, user_id, key_id):
            raise HTTPException(status_code=404, detail="No existe la API key")
    return {"id": key_id}


# --- Grupos ---------------------------------------------------------------------


def owned_group(db, name, user_id):
    group = metadata.find_group(db, name)
    if group is None:
        raise HTTPException(status_code=404, detail=f"No existe el grupo {name}")
    if group["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="Solo el dueño administra el grupo")
    return group


@app.post("/groups", status_code=201)
def create_group(request: NameRequest, user_id: int = Depends(current_user)):
    if not paths.is_valid_name(request.name):
        raise HTTPException(status_code=400, detail="Nombre de grupo inválido")
    with metadata.connect() as db:
        try:
            metadata.create_group(db, request.name, user_id)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="El grupo ya existe")
    return {"name": request.name}


@app.get("/groups")
def list_groups(user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        groups = [
            {
                "name": group["name"],
                "owner": group["owner_id"] == user_id,
                "members": [row["username"] for row in metadata.group_members(db, group["id"])],
            }
            for group in metadata.user_groups(db, user_id)
        ]
    return {"groups": groups}


@app.post("/groups/{name}/members", status_code=201)
def add_member(name: str, request: MemberRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        group = owned_group(db, name, user_id)
        member = metadata.find_user(db, request.username)
        if member is None:
            raise HTTPException(status_code=404, detail=f"No existe el usuario {request.username}")
        metadata.add_group_member(db, group["id"], member["id"])
    return {"group": name, "username": request.username}


@app.delete("/groups/{name}/members/{username}")
def remove_member(name: str, username: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        group = owned_group(db, name, user_id)
        member = metadata.find_user(db, username)
        if member is None:
            raise HTTPException(status_code=404, detail=f"No existe el usuario {username}")
        metadata.remove_group_member(db, group["id"], member["id"])
    return {"group": name, "username": username}


# --- ACL ------------------------------------------------------------------------


def owned_node(db, user_id, path):
    """(tipo, id) de una ruta del propio árbol del usuario."""
    where = access.target(db, user_id, path)
    if where.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Solo el dueño puede cambiar la ACL")

    directory = metadata.resolve_directory(db, user_id, where.path)
    if directory is not None:
        return "dir", directory["id"]

    file = metadata.resolve_file(db, user_id, where.path)
    if file is not None:
        return "file", file["id"]

    raise HTTPException(status_code=404, detail=f"No existe la ruta {path}")


def grantee(db, user, group):
    if bool(user) == bool(group):
        raise HTTPException(status_code=400, detail="Indica exactamente un user o un group")
    if user:
        row = metadata.find_user(db, user)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No existe el usuario {user}")
        return "user", row["id"]
    row = metadata.find_group(db, group)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No existe el grupo {group}")
    return "group", row["id"]


@app.post("/acl/grant")
def grant(request: GrantRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        node_type, node_id = owned_node(db, user_id, request.path)
        grantee_type, grantee_id = grantee(db, request.user, request.group)
        if grantee_type == "user" and grantee_id == user_id:
            raise HTTPException(status_code=400, detail="El dueño ya tiene todos los permisos")
        metadata.grant(db, user_id, node_type, node_id, grantee_type, grantee_id, request.permission)
    return {"path": paths.normalize(request.path), "permission": request.permission}


@app.post("/acl/revoke")
def revoke(request: RevokeRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        node_type, node_id = owned_node(db, user_id, request.path)
        grantee_type, grantee_id = grantee(db, request.user, request.group)
        if not metadata.revoke(db, node_type, node_id, grantee_type, grantee_id):
            raise HTTPException(status_code=404, detail="No había un permiso que revocar")
    return {"path": paths.normalize(request.path)}


@app.get("/acl")
def get_acl(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        node_type, node_id = owned_node(db, user_id, path)
        rows = metadata.node_acl(db, node_type, node_id)
    return {
        "path": paths.normalize(path),
        "type": node_type,
        "entries": [dict(row) for row in rows],
    }


@app.get("/acl/shared")
def shared_with_me(user_id: int = Depends(current_user)):
    entries = []
    with metadata.connect() as db:
        for row in metadata.shared_with(db, user_id):
            if row["node_type"] == "dir":
                inner = metadata.directory_path(db, row["node_id"])
            else:
                file = metadata.file_by_id(db, row["node_id"])
                parent = metadata.directory_path(db, file["directory_id"])
                inner = paths.normalize(f"{parent}/{file['name']}")
            entries.append(
                {
                    "path": paths.normalize(f"/~{row['owner']}{inner}"),
                    "type": row["node_type"],
                    "permission": row["permission"],
                }
            )
    return {"entries": entries}


# --- Gestión del sistema de archivos (RF1) ----------------------------------------


@app.get("/fs/ls")
def list_path(path: str = "/", user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        where, directory = access.directory(db, user_id, path)
        directories, files = metadata.list_directory(db, directory["id"])

    entries = [{"name": row["name"], "type": "dir"} for row in directories]
    entries += [
        {
            "name": row["name"],
            "type": "file",
            "size": row["size"],
            "created_at": row["created_at"],
        }
        for row in files
    ]
    return {"path": where.display, "entries": entries}


@app.get("/fs/stat")
def stat_path(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        try:
            where, _ = access.directory(db, user_id, path)
            return {"path": where.display, "type": "dir"}
        except HTTPException as error:
            if error.status_code != 404:
                raise
        where, file = access.file(db, user_id, path)
        lock = metadata.get_lock(db, where.owner_id, where.path)

    return {
        "path": where.display,
        "type": "file",
        "size": file["size"],
        "created_at": file["created_at"],
        "checksum": file["checksum"],
        "locked": lock is not None,
    }


@app.post("/fs/mkdir", status_code=201)
def make_directory(request: PathRequest, user_id: int = Depends(current_user)):
    parent_path, name = paths.split(request.path)
    if not paths.is_valid_name(name) or name.startswith("~"):
        raise HTTPException(status_code=400, detail="Nombre de directorio inválido")

    with metadata.connect() as db:
        where, parent = access.directory(db, user_id, parent_path, access.READ_WRITE)
        try:
            metadata.create_directory(db, where.owner_id, parent["id"], name)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"Ya existe {request.path}")

    return {"path": paths.normalize(request.path)}


@app.post("/fs/rmdir")
def remove_directory(request: PathRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        where, directory = access.directory(db, user_id, request.path, access.READ_WRITE)
        if where.path == paths.ROOT:
            raise HTTPException(status_code=400, detail="No se puede borrar la raíz")
        if not metadata.is_directory_empty(db, directory["id"]):
            raise HTTPException(status_code=409, detail="El directorio no está vacío")

        metadata.delete_directory(db, directory["id"])

    return {"path": where.display}


@app.delete("/fs/rm")
def remove_file(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        where, file = access.file(db, user_id, path, access.READ_WRITE)
        check_lock(db, where, user_id)

        block_ids = [row["id"] for row in metadata.file_blocks(db, file["id"])]
        metadata.delete_file(db, file["id"])
        metadata.delete_lock(db, where.owner_id, where.path)

    for block_id in block_ids:
        storage.remove(block_id)

    return {"path": where.display, "removed_blocks": len(block_ids)}


# --- Locks (RF3) ----------------------------------------------------------------


def check_lock(db, where, user_id):
    lock = metadata.get_lock(db, where.owner_id, where.path)
    if lock is not None and lock["holder_id"] != user_id:
        raise HTTPException(
            status_code=423, detail=f"{where.display} está bloqueado por otro usuario"
        )


@app.post("/fs/lock")
def lock_file(request: LockRequest, user_id: int = Depends(current_user)):
    """Lock exclusivo de escritura con lease: expira solo si el cliente se cae y no
    lo renueva. Volver a pedirlo siendo el dueño del lock lo renueva."""
    ttl = max(1, min(request.ttl or config.LOCK_TTL_SECONDS, 3600))
    with metadata.connect() as db:
        where, _ = access.file(db, user_id, request.path, access.READ_WRITE)
        check_lock(db, where, user_id)
        current = metadata.get_lock(db, where.owner_id, where.path)
        lock_id = current["lock_id"] if current else secrets.token_hex(8)
        expires_at = time.time() + ttl
        metadata.set_lock(db, where.owner_id, where.path, user_id, lock_id, expires_at)
    return {"path": where.display, "lock_id": lock_id, "expires_at": expires_at, "ttl": ttl}


@app.post("/fs/unlock")
def unlock_file(request: PathRequest, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        where, _ = access.file(db, user_id, request.path)
        lock = metadata.get_lock(db, where.owner_id, where.path)
        if lock is None:
            return {"path": where.display, "locked": False}
        if lock["holder_id"] != user_id and where.owner_id != user_id:
            raise HTTPException(status_code=423, detail="El lock pertenece a otro usuario")
        metadata.delete_lock(db, where.owner_id, where.path)
    return {"path": where.display, "locked": False}


# --- Transferencia de archivos (RF2) -----------------------------------------------


def _available_data_nodes():
    with metadata.connect() as db:
        nodes = metadata.available_nodes(
            db, time.time(), config.NODE_TIMEOUT_SECONDS
        )
        if not nodes and config.DATANODES:
            for index, url in enumerate(config.DATANODES):
                metadata.register_node(
                    db, f"static-{index}", url, time.time()
                )
            nodes = metadata.available_nodes(
                db, time.time(), config.NODE_TIMEOUT_SECONDS
            )
        return nodes


def _write_block_to_node(data, block_id):
    nodes = _available_data_nodes()
    if not nodes:
        storage.write(data, block_id)
        return None

    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET,
        block_id,
        cluster_auth.WRITE,
        config.BLOCK_TICKET_TTL_SECONDS,
    )
    verify = config.NODE_CA
    for node in nodes:
        try:
            response = requests.put(
                f"{node['url']}/blocks/{block_id}",
                data=data,
                headers={"X-Block-Ticket": ticket},
                verify=verify,
                timeout=30,
            )
            response.raise_for_status()
            return node["id"]
        except requests.RequestException:
            continue
    raise HTTPException(status_code=503, detail="Ningún DataNode pudo guardar el bloque")


def _remove_block_from_node(block_id, node_id):
    if not node_id:
        storage.remove(block_id)
        return
    with metadata.connect() as db:
        node = db.execute("SELECT url FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if node is None:
        return
    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET, block_id, cluster_auth.WRITE,
        config.BLOCK_TICKET_TTL_SECONDS,
    )
    try:
        response = requests.delete(
            f"{node['url']}/blocks/{block_id}",
            headers={"X-Block-Ticket": ticket},
            verify=config.NODE_CA,
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        print(f"ADVERTENCIA: no se pudo limpiar el bloque {block_id} en {node_id}")


@app.post("/files/upload", status_code=201)
async def upload_file(
    path: str,
    request: Request,
    overwrite: bool = False,
    user_id: int = Depends(current_user),
):
    parent_path, name = paths.split(path)
    if not paths.is_valid_name(name):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")

    with metadata.connect() as db:
        where, directory = access.directory(db, user_id, parent_path, access.READ_WRITE)
        file_path = paths.normalize(f"{where.path}/{name}")
        existing = metadata.resolve_file(db, where.owner_id, file_path)
        if existing is not None:
            if not overwrite:
                raise HTTPException(status_code=409, detail=f"Ya existe el archivo {path}")
            access.file(db, user_id, path, access.READ_WRITE)
        check_lock(db, access.Target(where.owner_id, file_path, path), user_id)
        directory_id = directory["id"]

    written = []
    file_hash = hashlib.sha256()

    locations = {}
    pending = []

    def stage(block):
        file_hash.update(block)
        checksum = hashlib.sha256(block).hexdigest()
        block_id = uuid.uuid4().hex
        pending.append((block, block_id, len(block), checksum))

    try:
        buffer = bytearray()
        async for chunk in request.stream():
            buffer.extend(chunk)
            while len(buffer) >= config.BLOCK_SIZE:
                stage(bytes(buffer[: config.BLOCK_SIZE]))
                del buffer[: config.BLOCK_SIZE]
        if buffer:
            stage(bytes(buffer))

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(pending) or 1))) as executor:
            node_ids = list(
                executor.map(
                    lambda item: _write_block_to_node(item[0], item[1]), pending
                )
            )
        written = [
            (item[1], item[2], item[3]) for item in pending
        ]
        locations.update(
            {item[1]: node_id for item, node_id in zip(pending, node_ids) if node_id}
        )

        size = sum(block_size for _, block_size, _ in written)
        checksum = file_hash.hexdigest()
        old_blocks = []
        old_locations = {}
        with metadata.connect() as db:
            check_lock(db, access.Target(where.owner_id, file_path, path), user_id)
            current = metadata.resolve_file(db, where.owner_id, file_path)
            if current is None:
                metadata.create_file(
                    db, where.owner_id, directory_id, name, size, written, checksum
                )
            elif overwrite:
                old_locations = {
                    row["id"]: row["id"]
                    for row in metadata.file_blocks(db, current["id"])
                }
                for block_id in old_locations:
                    location = metadata.block_location(db, block_id)
                    old_locations[block_id] = location["id"] if location else None
                old_blocks = metadata.replace_file(db, current["id"], size, written, checksum)
            else:
                raise HTTPException(status_code=409, detail=f"Ya existe el archivo {path}")
            for block_id, node_id in locations.items():
                metadata.set_block_location(db, block_id, node_id)
    except BaseException:
        # Una subida fallida no deja bloques huérfanos ni un archivo a medias.
        for block_id, _, _ in written:
            _remove_block_from_node(block_id, locations.get(block_id))
        raise

    for block_id in old_blocks:
        _remove_block_from_node(block_id, old_locations.get(block_id))

    return {
        "path": paths.normalize(path),
        "size": size,
        "blocks": len(written),
        "checksum": checksum,
        "replaced": existing is not None,
    }


@app.get("/files/download")
def download_file(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        _, file = access.file(db, user_id, path)
        block_rows = metadata.file_blocks(db, file["id"])
        locations = {
            row["id"]: metadata.block_location(db, row["id"]) for row in block_rows
        }

    def blocks():
        for row in block_rows:
            location = locations[row["id"]]
            if location:
                ticket = cluster_auth.issue_block_ticket(
                    config.CLUSTER_SECRET,
                    row["id"],
                    cluster_auth.READ,
                    config.BLOCK_TICKET_TTL_SECONDS,
                    user_id,
                )
                try:
                    response = requests.get(
                        f"{location['url']}/blocks/{row['id']}",
                        headers={"X-Block-Ticket": ticket},
                        verify=config.NODE_CA,
                        timeout=30,
                    )
                    response.raise_for_status()
                    yield response.content
                    continue
                except requests.RequestException as error:
                    raise HTTPException(
                        status_code=503,
                        detail=f"No se pudo descargar el bloque {row['id']}: {error}",
                    )
            yield storage.read(row["id"])

    return StreamingResponse(
        blocks(),
        media_type="application/octet-stream",
        headers={"Content-Length": str(file["size"])},
    )


@app.get("/files/blocks")
def list_file_blocks(path: str, user_id: int = Depends(current_user)):
    """Plan de lectura: bloques en orden, su checksum y un ticket firmado por bloque
    con el que el cliente lo pide al nodo que lo guarda."""
    with metadata.connect() as db:
        where, file = access.file(db, user_id, path)
        rows = metadata.file_blocks(db, file["id"])
        locations = {
            row["id"]: metadata.block_location(db, row["id"]) for row in rows
        }

    return {
        "path": where.display,
        "size": file["size"],
        "checksum": file["checksum"],
        "block_size": config.BLOCK_SIZE,
        "blocks": [
            {
                "id": row["id"],
                "sequence": row["sequence"],
                "size": row["size"],
                "checksum": row["checksum"],
                "url": (
                    f"{location['url']}/blocks/{row['id']}"
                    if (location := locations[row["id"]]) is not None
                    else f"/blocks/{row['id']}"
                ),
                "ticket": cluster_auth.issue_block_ticket(
                    config.CLUSTER_SECRET,
                    row["id"],
                    cluster_auth.READ,
                    config.BLOCK_TICKET_TTL_SECONDS,
                    user_id,
                ),
            }
            for row in rows
        ],
    }


@app.get("/blocks/{block_id}")
def read_block(
    block_id: str,
    offset: int = 0,
    length: Optional[int] = None,
    ticket: Optional[str] = Header(None, alias="X-Block-Ticket"),
    user_id: Optional[int] = Depends(optional_user),
):
    """Devuelve un bloque (o un rango de él, para read() con desplazamiento).
    Acepta un ticket de bloque (X-Block-Ticket) o las credenciales del usuario."""
    if ticket:
        try:
            cluster_auth.verify_block_ticket(config.CLUSTER_SECRET, ticket, block_id)
        except cluster_auth.InvalidClusterToken as error:
            raise HTTPException(status_code=403, detail=f"Ticket inválido: {error}")
        with metadata.connect() as db:
            found = metadata.find_block(db, block_id)
    elif user_id is not None:
        with metadata.connect() as db:
            found = access.block(db, user_id, block_id)
    else:
        raise HTTPException(status_code=401, detail="Falta autenticación o ticket")

    if found is None:
        raise HTTPException(status_code=404, detail="No existe el bloque")

    try:
        data = storage.read(block_id)
    except storage.CorruptBlock as error:
        raise HTTPException(status_code=500, detail=str(error))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="El bloque no está en este nodo")

    if offset < 0 or (length is not None and length < 0):
        raise HTTPException(status_code=400, detail="Rango inválido")
    end = None if length is None else offset + length
    return Response(data[offset:end], media_type="application/octet-stream")
