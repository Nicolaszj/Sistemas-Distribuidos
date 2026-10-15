"""Alta disponibilidad del ControlNode (hito 3).

Este módulo concentra el estado y las operaciones de HA para no dispersarlas por
`main.py`:

- Estado en runtime (rol, época, URL del par, instante de promoción) respaldado en
  la tabla `ha_state` de la base de metadatos.
- Snapshot consistente de la base con la API de backup online de SQLite.
- Reaplicación de entradas del oplog en el standby.
- Promoción (standby -> primary) con incremento de época para el fencing, y
  degradación (primary -> standby) al descubrir una época mayor.

El bucle de red del standby (pedir oplog, detectar caída, promover) vive en
`replicator.py`; aquí solo están las piezas sin red para poder probarlas aisladas.
"""

import os
import sqlite3
import tempfile
import threading
import time

from . import config, metadata

PRIMARY = "primary"
STANDBY = "standby"


class HaState:
    """Estado de HA de un nodo. Mutable: el rol y la época cambian con el failover."""

    def __init__(self):
        self._lock = threading.RLock()
        self.role = config.HA_ROLE
        self.epoch = 0
        self.applied_seq = 0
        self.peer_url = config.HA_PEER_URL
        self.promoted_at = 0.0  # instante de la última promoción (periodo de gracia)
        self.last_peer_ok = 0.0  # última vez que se supo del par (para /ha/status)

    # --- persistencia -----------------------------------------------------------

    def load(self):
        """Carga rol/época/applied_seq desde ha_state."""
        with metadata.connect() as db:
            row = metadata.get_ha_state(db)
        if row is not None:
            with self._lock:
                self.role = row["role"]
                self.epoch = row["epoch"]
                self.applied_seq = row["applied_seq"]
        return self

    def _persist(self, db=None, **columns):
        if db is not None:
            metadata.set_ha_state(db, **columns)
        else:
            with metadata.connect() as connection:
                metadata.set_ha_state(connection, **columns)

    # --- consultas --------------------------------------------------------------

    @property
    def is_primary(self):
        with self._lock:
            return self.role == PRIMARY

    def in_grace(self, now=None):
        """True si el nodo se promovió hace menos de HA_GRACE_SECONDS."""
        now = time.time() if now is None else now
        with self._lock:
            if self.role != PRIMARY or self.promoted_at <= 0:
                return False
            return now < self.promoted_at + config.HA_GRACE_SECONDS

    def grace_until(self):
        with self._lock:
            if self.promoted_at <= 0:
                return 0.0
            return self.promoted_at + config.HA_GRACE_SECONDS

    def status(self):
        """Diccionario para GET /ha/status (sin secretos)."""
        now = time.time()
        with self._lock:
            return {
                "role": self.role,
                "epoch": self.epoch,
                "applied_seq": self.applied_seq,
                "peer_url": self.peer_url or None,
                "peer_alive": bool(self.last_peer_ok)
                and (now - self.last_peer_ok) < config.HA_TIMEOUT_SECONDS,
                "grace_until": self.grace_until(),
                "ts": now,
            }

    # --- transiciones de rol ----------------------------------------------------

    def promote(self):
        """standby -> primary: sube la época (fencing), instala los triggers del
        oplog y marca el inicio del periodo de gracia de los DataNodes."""
        with self._lock:
            self.epoch += 1
            self.role = PRIMARY
            self.promoted_at = time.time()
            with metadata.connect() as db:
                metadata.install_oplog_triggers(db)
                metadata.set_ha_state(db, role=PRIMARY, epoch=self.epoch)
            return self.epoch

    def demote(self, new_epoch):
        """primary -> standby al descubrir una época mayor en el par (fencing).
        Deja de generar oplog propio y adopta la época nueva."""
        with self._lock:
            self.role = STANDBY
            self.epoch = max(self.epoch, new_epoch)
            with metadata.connect() as db:
                metadata.drop_oplog_triggers(db)
                metadata.set_ha_state(db, role=STANDBY, epoch=self.epoch)

    def note_peer_seen(self):
        with self._lock:
            self.last_peer_ok = time.time()

    def set_applied_seq(self, seq):
        with self._lock:
            self.applied_seq = seq
            self._persist(applied_seq=seq)


# --- snapshot y reaplicación (sin red) ---------------------------------------------


def snapshot_bytes():
    """Copia binaria y consistente de metadata.db usando el backup online de
    SQLite (seguro aunque haya escrituras concurrentes). Devuelve los bytes de un
    archivo SQLite completo que el standby puede escribir tal cual."""
    source = sqlite3.connect(config.DATABASE_PATH, timeout=30)
    handle, temp_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        destination = sqlite3.connect(temp_path, timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
        with open(temp_path, "rb") as snapshot:
            return snapshot.read()
    finally:
        source.close()
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def load_snapshot(raw, applied_seq):
    """Reemplaza el archivo de base local por el snapshot binario recibido y fija la
    última secuencia aplicada. Se usa en el standby al arrancar o al quedar muy
    atrás. Tras cargar, se asegura de que NO queden triggers de oplog (el standby no
    debe generar oplog propio)."""
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.DATABASE_PATH.with_suffix(".snapshot.tmp")
    tmp.write_bytes(raw)
    tmp.replace(config.DATABASE_PATH)
    # Limpiar posibles archivos WAL/SHM de la base anterior para no mezclar estado.
    for suffix in ("-wal", "-shm"):
        side = config.DATABASE_PATH.with_name(config.DATABASE_PATH.name + suffix)
        if side.exists():
            side.unlink()
    with metadata.connect() as db:
        metadata.drop_oplog_triggers(db)
        metadata.set_ha_state(db, applied_seq=applied_seq, role=STANDBY)


def apply_entries(entries):
    """Reaplica en orden una lista de entradas de oplog (filas o dicts) en una sola
    transacción. Devuelve la última secuencia aplicada, o None si no había nada."""
    last_seq = None
    with metadata.connect() as db:
        for entry in entries:
            metadata.apply_oplog_entry(db, entry)
            last_seq = entry["seq"]
    return last_seq


# Instancia global del nodo. main.py la inicializa en el arranque con load().
state = HaState()
