import sqlite3

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .. import paths
from . import auth, config, metadata, storage

app = FastAPI(title="DFSha", version="0.1.0")
security = HTTPBearer()

metadata.initialize()


class Credentials(BaseModel):
    username: str
    password: str


class PathRequest(BaseModel):
    path: str


def current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        user_id, _ = auth.read_token(credentials.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return user_id


@app.post("/auth/register", status_code=201)
def register(credentials: Credentials):
    password_hash, salt = auth.hash_password(credentials.password)
    with metadata.connect() as db:
        try:
            user_id = metadata.create_user(db, credentials.username, password_hash, salt)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="El usuario ya existe")

    return {"id": user_id, "username": credentials.username}


@app.post("/auth/login")
def login(credentials: Credentials):
    with metadata.connect() as db:
        user = metadata.find_user(db, credentials.username)

    if user is None or not auth.verify_password(
        credentials.password, user["salt"], user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    return {
        "token": auth.create_token(user["id"], user["username"]),
        "expires_in": config.TOKEN_TTL_SECONDS,
    }


@app.get("/fs/ls")
def list_path(path: str = "/", user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        directory = metadata.resolve_directory(db, user_id, path)
        if directory is None:
            raise HTTPException(status_code=404, detail=f"No existe el directorio {path}")

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
    return {"path": paths.normalize(path), "entries": entries}


@app.get("/fs/stat")
def stat_path(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        directory = metadata.resolve_directory(db, user_id, path)
        if directory is not None:
            return {"path": paths.normalize(path), "type": "dir"}

        file = metadata.resolve_file(db, user_id, path)

    if file is None:
        raise HTTPException(status_code=404, detail=f"No existe la ruta {path}")

    return {
        "path": paths.normalize(path),
        "type": "file",
        "size": file["size"],
        "created_at": file["created_at"],
    }


@app.post("/fs/mkdir", status_code=201)
def make_directory(request: PathRequest, user_id: int = Depends(current_user)):
    parent_path, name = paths.split(request.path)
    if not paths.is_valid_name(name):
        raise HTTPException(status_code=400, detail="Nombre de directorio inválido")

    with metadata.connect() as db:
        parent = metadata.resolve_directory(db, user_id, parent_path)
        if parent is None:
            raise HTTPException(
                status_code=404, detail=f"No existe el directorio {parent_path}"
            )
        try:
            metadata.create_directory(db, user_id, parent["id"], name)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"Ya existe {request.path}")

    return {"path": paths.normalize(request.path)}


@app.post("/fs/rmdir")
def remove_directory(request: PathRequest, user_id: int = Depends(current_user)):
    if paths.normalize(request.path) == paths.ROOT:
        raise HTTPException(status_code=400, detail="No se puede borrar la raíz")

    with metadata.connect() as db:
        directory = metadata.resolve_directory(db, user_id, request.path)
        if directory is None:
            raise HTTPException(
                status_code=404, detail=f"No existe el directorio {request.path}"
            )
        if not metadata.is_directory_empty(db, directory["id"]):
            raise HTTPException(status_code=409, detail="El directorio no está vacío")

        metadata.delete_directory(db, directory["id"])

    return {"path": paths.normalize(request.path)}


@app.delete("/fs/rm")
def remove_file(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        file = metadata.resolve_file(db, user_id, path)
        if file is None:
            raise HTTPException(status_code=404, detail=f"No existe el archivo {path}")

        block_ids = [row["id"] for row in metadata.file_blocks(db, file["id"])]
        metadata.delete_file(db, file["id"])

    for block_id in block_ids:
        storage.remove(block_id)

    return {"path": paths.normalize(path), "removed_blocks": len(block_ids)}


@app.post("/files/upload", status_code=201)
async def upload_file(path: str, request: Request, user_id: int = Depends(current_user)):
    parent_path, name = paths.split(path)
    if not paths.is_valid_name(name):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")

    with metadata.connect() as db:
        directory = metadata.resolve_directory(db, user_id, parent_path)
        if directory is None:
            raise HTTPException(
                status_code=404, detail=f"No existe el directorio {parent_path}"
            )
        if metadata.resolve_file(db, user_id, path) is not None:
            raise HTTPException(status_code=409, detail=f"Ya existe el archivo {path}")
        directory_id = directory["id"]

    written = []
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        while len(buffer) >= config.BLOCK_SIZE:
            block = bytes(buffer[: config.BLOCK_SIZE])
            written.append((storage.write(block), len(block)))
            del buffer[: config.BLOCK_SIZE]

    if buffer:
        block = bytes(buffer)
        written.append((storage.write(block), len(block)))

    size = sum(block_size for _, block_size in written)
    with metadata.connect() as db:
        metadata.create_file(db, user_id, directory_id, name, size, written)

    return {"path": paths.normalize(path), "size": size, "blocks": len(written)}


@app.get("/files/download")
def download_file(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        file = metadata.resolve_file(db, user_id, path)
        if file is None:
            raise HTTPException(status_code=404, detail=f"No existe el archivo {path}")

        block_ids = [row["id"] for row in metadata.file_blocks(db, file["id"])]

    def blocks():
        for block_id in block_ids:
            yield storage.read(block_id)

    return StreamingResponse(
        blocks(),
        media_type="application/octet-stream",
        headers={"Content-Length": str(file["size"])},
    )


@app.get("/files/blocks")
def list_file_blocks(path: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        file = metadata.resolve_file(db, user_id, path)
        if file is None:
            raise HTTPException(status_code=404, detail=f"No existe el archivo {path}")

        rows = metadata.file_blocks(db, file["id"])

    return {
        "path": paths.normalize(path),
        "size": file["size"],
        "block_size": config.BLOCK_SIZE,
        "blocks": [
            {
                "id": row["id"],
                "sequence": row["sequence"],
                "size": row["size"],
                "url": f"/blocks/{row['id']}",
            }
            for row in rows
        ],
    }


@app.get("/blocks/{block_id}")
def read_block(block_id: str, user_id: int = Depends(current_user)):
    with metadata.connect() as db:
        block = metadata.find_block(db, user_id, block_id)

    if block is None:
        raise HTTPException(status_code=404, detail="No existe el bloque")

    return Response(storage.read(block_id), media_type="application/octet-stream")
