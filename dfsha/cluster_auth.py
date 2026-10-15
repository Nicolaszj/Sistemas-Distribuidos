"""Autenticación entre nodos del clúster.

Se usa un secreto compartido (DFSHA_CLUSTER_SECRET) que solo conocen el ControlNode
y los DataNodes; los clientes nunca lo ven.

- Tickets de bloque: el ControlNode valida usuario y ACL, y entrega al cliente un
  ticket firmado y de corta vida por cada bloque. El DataNode solo verifica la
  firma, el bloque y la operación, sin consultar la base de metadatos (mismo
  esquema que los "block access tokens" de HDFS).
- Tokens de nodo: los DataNodes se identifican ante el ControlNode (registro,
  heartbeat) y entre ellos (replicación) con un token firmado con el mismo secreto.
"""

import time

import jwt

ALGORITHM = "HS256"
READ = "r"
WRITE = "w"


class InvalidClusterToken(Exception):
    pass


def issue_block_ticket(secret, block_id, operation=READ, ttl=300, user_id=None):
    payload = {
        "typ": "block",
        "blk": block_id,
        "op": operation,
        "exp": int(time.time()) + ttl,
    }
    if user_id is not None:
        payload["sub"] = str(user_id)
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify_block_ticket(secret, ticket, block_id, operation=READ):
    payload = _decode(secret, ticket)
    if payload.get("typ") != "block" or payload.get("blk") != block_id:
        raise InvalidClusterToken("El ticket no corresponde a este bloque")
    if operation == READ and payload.get("op") not in (READ, WRITE):
        raise InvalidClusterToken("El ticket no permite leer")
    if operation == WRITE and payload.get("op") != WRITE:
        raise InvalidClusterToken("El ticket no permite escribir")
    return payload


def issue_node_token(secret, node_id, ttl=60, admin=False):
    """Token de nodo. Con `admin=True` lleva el claim `adm` que autoriza las
    operaciones de operador del clúster (p. ej. promover manualmente un standby)."""
    payload = {"typ": "node", "sub": node_id, "exp": int(time.time()) + ttl}
    if admin:
        payload["adm"] = True
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify_node_token(secret, token):
    payload = _decode(secret, token)
    if payload.get("typ") != "node":
        raise InvalidClusterToken("No es un token de nodo")
    return payload["sub"]


def issue_admin_token(secret, node_id="operator", ttl=60):
    """Token de operador del clúster para endpoints privilegiados (/ha/promote)."""
    return issue_node_token(secret, node_id, ttl=ttl, admin=True)


def verify_admin_token(secret, token):
    """Verifica que el token sea de nodo y tenga el claim de operador. Devuelve el
    sujeto (quién promueve) para registro/depuración."""
    payload = _decode(secret, token)
    if payload.get("typ") != "node":
        raise InvalidClusterToken("No es un token de nodo")
    if not payload.get("adm"):
        raise InvalidClusterToken("El token no es de operador (falta claim adm)")
    return payload["sub"]


def _decode(secret, token):
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as error:
        raise InvalidClusterToken(str(error))
