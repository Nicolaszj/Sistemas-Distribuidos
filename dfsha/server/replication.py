"""Replicación de bloques entre DataNodes (factor configurable, 3 por defecto).

Modelo de consistencia
----------------------
- Los bloques son inmutables (WORM): un bloque nunca se modifica, un archivo
  reescrito recibe bloques nuevos. Por eso dos réplicas de un mismo bloque no
  pueden divergir; solo pueden faltar.
- Escritura síncrona: una subida termina bien solo cuando cada bloque quedó
  guardado en min(factor, DataNodes vivos) nodos. Hasta entonces el archivo no se
  registra en los metadatos, así que ningún lector ve un archivo a medias.
- Cada réplica se valida de extremo a extremo con el SHA-256 del bloque, que el
  cliente comprueba al leer; si una réplica está dañada o su nodo no responde, el
  cliente pasa a la siguiente.
- Re-replicación: un hilo del ControlNode primario busca bloques con menos réplicas
  vivas de las debidas (p. ej. porque se cayó un DataNode) y ordena a un DataNode
  que sí lo tiene copiarlo directamente a otro (comunicación DataNode ↔ DataNode).
"""

import threading

import requests
from fastapi import HTTPException

from .. import cluster_auth
from . import config, ha, metadata

_stop = threading.Event()
_thread = None


def target_replicas(alive_count):
    """Réplicas que debe tener un bloque con `alive_count` DataNodes vivos."""
    return max(1, min(config.REPLICATION_FACTOR, alive_count))


def write_replicas(data, block_id, nodes, sequence=0):
    """Guarda el bloque en min(factor, len(nodes)) DataNodes y devuelve sus ids.

    El orden de prueba rota según el número de bloque, así que las primeras réplicas
    de bloques consecutivos caen en nodos distintos y el reparto queda equilibrado.
    Si un nodo falla se prueba el siguiente; si no se alcanzan las réplicas
    necesarias se borran las que sí se escribieron y se responde 503."""
    needed = target_replicas(len(nodes))
    start = sequence % len(nodes)
    ordered = nodes[start:] + nodes[:start]
    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET, block_id, cluster_auth.WRITE, config.BLOCK_TICKET_TTL_SECONDS
    )

    written = []
    for node in ordered:
        if len(written) == needed:
            break
        try:
            response = requests.put(
                f"{node['url']}/blocks/{block_id}",
                data=data,
                headers={"X-Block-Ticket": ticket},
                verify=config.NODE_CA,
                timeout=30,
            )
            response.raise_for_status()
            written.append(node["id"])
        except requests.RequestException:
            continue

    if len(written) < needed:
        remove_replicas(block_id, {node["id"]: node["url"] for node in nodes if node["id"] in written})
        raise HTTPException(
            status_code=503,
            detail=f"Solo {len(written)} de {needed} DataNodes guardaron el bloque {block_id}",
        )
    return written


def remove_replicas(block_id, urls_by_node):
    """Borra el bloque de cada DataNode indicado ({node_id: url}). Los fallos se
    registran pero no detienen la operación: un bloque huérfano solo ocupa espacio."""
    if not urls_by_node:
        return
    ticket = cluster_auth.issue_block_ticket(
        config.CLUSTER_SECRET, block_id, cluster_auth.WRITE, config.BLOCK_TICKET_TTL_SECONDS
    )
    for node_id, url in urls_by_node.items():
        try:
            requests.delete(
                f"{url}/blocks/{block_id}",
                headers={"X-Block-Ticket": ticket},
                verify=config.NODE_CA,
                timeout=10,
            ).raise_for_status()
        except requests.RequestException:
            print(f"ADVERTENCIA: no se pudo borrar el bloque {block_id} en {node_id}")


def replica_urls(db, block_id, alive_ids):
    """URLs de las réplicas de un bloque, primero las de nodos vivos."""
    rows = metadata.block_locations(db, block_id)
    alive = [row["url"] for row in rows if row["id"] in alive_ids]
    dead = [row["url"] for row in rows if row["id"] not in alive_ids]
    return alive + dead


def rereplicate_once(alive_nodes, limit=100):
    """Un ciclo de re-replicación. Devuelve cuántas copias nuevas se hicieron."""
    if not alive_nodes:
        return 0
    alive = {node["id"]: node["url"] for node in alive_nodes}
    needed = target_replicas(len(alive))

    with metadata.connect() as db:
        rows = db.execute(
            "SELECT blocks.id AS block_id, block_locations.node_id AS node_id"
            " FROM blocks JOIN block_locations ON block_locations.block_id = blocks.id"
        ).fetchall()

    holders = {}
    for row in rows:
        holders.setdefault(row["block_id"], set()).add(row["node_id"])

    copies = 0
    token = cluster_auth.issue_node_token(config.CLUSTER_SECRET, "controlnode")
    for block_id, nodes in holders.items():
        if copies >= limit:
            break
        alive_holders = [node_id for node_id in nodes if node_id in alive]
        missing = needed - len(alive_holders)
        if missing <= 0 or not alive_holders:
            continue  # sin réplica viva no hay de dónde copiar
        source = alive_holders[0]
        targets = [node_id for node_id in alive if node_id not in nodes][:missing]
        for target in targets:
            try:
                response = requests.post(
                    f"{alive[source]}/blocks/{block_id}/copy-to",
                    json={"target_url": alive[target]},
                    headers={"X-Node-Token": token},
                    verify=config.NODE_CA,
                    timeout=60,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                print(f"ADVERTENCIA: no se pudo copiar {block_id} de {source} a {target}: {error}")
                continue
            with metadata.connect() as db:
                metadata.set_block_location(db, block_id, target)
            copies += 1
            print(f"Re-replicación: bloque {block_id} copiado de {source} a {target}")
    return copies


def _loop(available_nodes):
    while not _stop.wait(config.REREPLICATION_SECONDS):
        if not ha.state.is_primary:
            continue
        try:
            rereplicate_once(available_nodes())
        except Exception as error:  # el hilo no debe morir por un fallo puntual
            print(f"ADVERTENCIA: ciclo de re-replicación falló: {error}")


def start(available_nodes):
    """Arranca el hilo de re-replicación. `available_nodes` devuelve los DataNodes
    vivos (la misma función que usa la subida)."""
    global _thread
    if config.REREPLICATION_SECONDS <= 0:
        return None
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(available_nodes,), name="dfsha-rereplication", daemon=True
    )
    _thread.start()
    return _thread


def stop():
    _stop.set()
