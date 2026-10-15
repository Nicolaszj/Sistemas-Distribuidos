"""Aplicación HTTP de un DataNode.

El DataNode no conoce usuarios ni metadatos: solo valida tickets de bloque
firmados por el ControlNode y opera sobre su almacenamiento local.
"""

import os
import threading
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .. import cluster_auth
from . import config, storage

app = FastAPI(title="DFSha DataNode", version="0.3.0")
_stop = threading.Event()
_heartbeat_thread = None


def _ticket(ticket, block_id, operation):
    if not ticket:
        raise HTTPException(status_code=401, detail="Falta X-Block-Ticket")
    try:
        return cluster_auth.verify_block_ticket(
            config.CLUSTER_SECRET, ticket, block_id, operation
        )
    except cluster_auth.InvalidClusterToken as error:
        raise HTTPException(status_code=403, detail=f"Ticket inválido: {error}")


def _valid_block_id(block_id):
    return len(block_id) == 32 and all(character in "0123456789abcdef" for character in block_id)


@app.get("/health")
def health():
    return {"status": "ok", "node_id": config.NODE_ID}


@app.put("/blocks/{block_id}")
async def write_block(
    block_id: str,
    request: Request,
    ticket: Optional[str] = Header(None, alias="X-Block-Ticket"),
):
    if not _valid_block_id(block_id):
        raise HTTPException(status_code=400, detail="Identificador de bloque inválido")
    _ticket(ticket, block_id, cluster_auth.WRITE)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="El bloque no puede estar vacío")
    try:
        storage.write(data, block_id)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el bloque: {error}")
    return {"id": block_id, "size": len(data)}


@app.get("/blocks/{block_id}")
def read_block(
    block_id: str,
    offset: int = 0,
    length: Optional[int] = None,
    ticket: Optional[str] = Header(None, alias="X-Block-Ticket"),
):
    if not _valid_block_id(block_id):
        raise HTTPException(status_code=400, detail="Identificador de bloque inválido")
    _ticket(ticket, block_id, cluster_auth.READ)
    if offset < 0 or (length is not None and length < 0):
        raise HTTPException(status_code=400, detail="Rango inválido")
    try:
        data = storage.read(block_id)
    except storage.CorruptBlock as error:
        raise HTTPException(status_code=500, detail=str(error))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="El bloque no está en este nodo")
    end = None if length is None else offset + length
    return Response(data[offset:end], media_type="application/octet-stream")


@app.delete("/blocks/{block_id}")
def delete_block(
    block_id: str,
    ticket: Optional[str] = Header(None, alias="X-Block-Ticket"),
):
    if not _valid_block_id(block_id):
        raise HTTPException(status_code=400, detail="Identificador de bloque inválido")
    _ticket(ticket, block_id, cluster_auth.WRITE)
    storage.remove(block_id)
    return {"id": block_id, "removed": True}


class CopyRequest(BaseModel):
    target_url: str


@app.post("/blocks/{block_id}/copy-to")
def copy_block(
    block_id: str,
    request: CopyRequest,
    token: Optional[str] = Header(None, alias="X-Node-Token"),
):
    """Re-replicación DataNode → DataNode: el ControlNode le pide a este nodo que
    envíe un bloque que tiene a otro DataNode. Solo lo puede pedir un nodo del
    clúster (token firmado con el secreto del clúster). Este nodo descifra el bloque
    con su llave, se firma un ticket de escritura y lo sube al destino, que lo vuelve
    a cifrar con la suya."""
    if not _valid_block_id(block_id):
        raise HTTPException(status_code=400, detail="Identificador de bloque inválido")
    if not token:
        raise HTTPException(status_code=401, detail="Falta X-Node-Token")
    try:
        cluster_auth.verify_node_token(config.CLUSTER_SECRET, token)
    except cluster_auth.InvalidClusterToken as error:
        raise HTTPException(status_code=403, detail=f"Token de nodo inválido: {error}")
    if not request.target_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL de destino inválida")

    try:
        data = storage.read(block_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="El bloque no está en este nodo")
    except storage.CorruptBlock as error:
        raise HTTPException(status_code=500, detail=str(error))

    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET, block_id, cluster_auth.WRITE, config.BLOCK_TICKET_TTL_SECONDS
    )
    try:
        response = requests.put(
            f"{request.target_url.rstrip('/')}/blocks/{block_id}",
            data=data,
            headers={"X-Block-Ticket": ticket},
            verify=config.NODE_CA,
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail=f"No se pudo copiar al destino: {error}")
    return {"id": block_id, "copied_to": request.target_url, "size": len(data)}


def _register_once(base_url, payload, verify):
    """Registro + heartbeat contra un ControlNode. Devuelve True si tuvo éxito.
    Un 503 not_primary (ese ControlNode es standby) cuenta como fallo para que el
    hilo pruebe con el siguiente de la lista."""
    token = cluster_auth.issue_node_token(config.CLUSTER_SECRET, config.NODE_ID)
    response = requests.post(
        f"{base_url}/nodes/register",
        json=payload,
        headers={"X-Node-Token": token},
        verify=verify,
        timeout=5,
    )
    response.raise_for_status()
    token = cluster_auth.issue_node_token(config.CLUSTER_SECRET, config.NODE_ID)
    response = requests.post(
        f"{base_url}/nodes/{config.NODE_ID}/heartbeat",
        headers={"X-Node-Token": token},
        verify=verify,
        timeout=5,
    )
    response.raise_for_status()
    return True


def _register_and_heartbeat():
    if not config.CONTROL_URLS or not config.NODE_ID or not config.DATANODE_URL:
        return
    verify = os.getenv("DFSHA_CONTROL_CA") or True
    payload = {"node_id": config.NODE_ID, "url": config.DATANODE_URL}
    # Preferir el último ControlNode que respondió como primary.
    preferred = config.CONTROL_URLS[0]
    while not _stop.is_set():
        # Ordenar la lista empezando por el preferido, luego el resto.
        urls = [preferred] + [u for u in config.CONTROL_URLS if u != preferred]
        for base_url in urls:
            try:
                if _register_once(base_url, payload, verify):
                    preferred = base_url  # recordar el primary actual
                    break
            except requests.RequestException as error:
                # Conexión caída o 503 not_primary (ese nodo es standby): probar
                # el siguiente ControlNode de la lista.
                print(f"ADVERTENCIA: ControlNode {base_url} no disponible: {error}")
                continue
        _stop.wait(config.NODE_HEARTBEAT_SECONDS)


@app.on_event("startup")
def start_heartbeat():
    global _heartbeat_thread
    _stop.clear()
    _heartbeat_thread = threading.Thread(
        target=_register_and_heartbeat, name="dfsha-datanode-heartbeat", daemon=True
    )
    _heartbeat_thread.start()


@app.on_event("shutdown")
def stop_heartbeat():
    _stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=5)
