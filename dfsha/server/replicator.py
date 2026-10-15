"""Hilo de replicación del ControlNode standby.

Mientras es standby, este hilo:
  1. Pide al primary las entradas de oplog nuevas (GET /ha/oplog) y las reaplica.
  2. Si quedó por detrás del punto más antiguo disponible (poda), pide un snapshot
     completo (GET /ha/snapshot) y reinicia la secuencia aplicada.
  3. Si el primary no responde durante HA_TIMEOUT_SECONDS, se autopromueve.
  4. Si el primary anuncia una época mayor, el propio nodo se degrada (fencing).

El mismo ciclo sirve de heartbeat entre ControlNodes (no se abre una conexión
aparte). Sigue el patrón del heartbeat del DataNode (hilo daemon + Event de parada).
"""

import threading
import time

import requests

from .. import cluster_auth
from . import config, ha, metadata

_stop = threading.Event()
_thread = None


def _node_token():
    return cluster_auth.issue_node_token(config.CLUSTER_SECRET, "controlnode-standby")


def _pull_once(state):
    """Un ciclo de sincronización. Devuelve True si se tuvo noticia del primary."""
    token = _node_token()
    verify = config.HA_PEER_CA
    base = state.peer_url
    try:
        response = requests.get(
            f"{base}/ha/oplog",
            params={"after": state.applied_seq, "ack": state.applied_seq,
                    "epoch": state.epoch},
            headers={"X-Node-Token": token},
            verify=verify,
            timeout=max(2, config.HA_HEARTBEAT_SECONDS),
        )
    except requests.RequestException:
        return False

    # El primary puede habernos dicho que ya no es primary (otro ganó la época).
    if response.status_code == 409:
        return True  # hay un primary vivo, pero no es este par; no promover
    if not response.ok:
        return False

    data = response.json()
    peer_epoch = data.get("epoch", 0)
    # Fencing: si el par tiene una época mayor, adoptarla (ya es el caso normal:
    # el standby sigue la época del primary).
    if peer_epoch > state.epoch:
        with metadata.connect() as db:
            metadata.set_ha_state(db, epoch=peer_epoch)
        state.epoch = peer_epoch

    oldest = data.get("oldest_seq", 0)
    # Hueco por poda: el primary ya no tiene las entradas que nos faltan.
    if oldest and state.applied_seq + 1 < oldest:
        _snapshot(state, token, verify, base)
        return True

    entries = data.get("entries", [])
    if entries:
        last = ha.apply_entries(entries)
        if last is not None:
            state.set_applied_seq(last)
    return True


def _snapshot(state, token, verify, base):
    response = requests.get(
        f"{base}/ha/snapshot",
        headers={"X-Node-Token": token},
        verify=verify,
        timeout=60,
    )
    response.raise_for_status()
    applied_seq = int(response.headers.get("X-DFSha-Oplog-Seq", "0"))
    ha.load_snapshot(response.content, applied_seq)
    state.applied_seq = applied_seq


def _loop(state):
    last_ok = time.time()
    while not _stop.is_set():
        if not state.is_primary:
            if _pull_once(state):
                last_ok = time.time()
                state.note_peer_seen()
            elif time.time() - last_ok >= config.HA_TIMEOUT_SECONDS:
                # El primary lleva demasiado tiempo sin responder: promoverse.
                state.promote()
        _stop.wait(config.HA_HEARTBEAT_SECONDS)


def start(state=None):
    """Arranca el hilo si el nodo es standby y tiene un par configurado."""
    global _thread
    state = state or ha.state
    if state.is_primary or not state.peer_url:
        return None
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(state,), name="dfsha-ha-replicator", daemon=True
    )
    _thread.start()
    return _thread


def stop():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
