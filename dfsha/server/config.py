import base64
import os
import secrets
from pathlib import Path

HOME = Path(os.getenv("DFSHA_HOME", Path(__file__).resolve().parents[2] / "data"))
DATABASE_PATH = HOME / "metadata.db"
BLOCKS_DIR = HOME / "blocks"

BLOCK_SIZE = int(os.getenv("DFSHA_BLOCK_SIZE", 4 * 1024 * 1024))
NODE_ID = os.getenv("DFSHA_NODE_ID", "")
CONTROL_URL = os.getenv("DFSHA_CONTROL_URL", "").rstrip("/")
DATANODE_URL = os.getenv("DFSHA_DATANODE_URL", "").rstrip("/")


def _control_urls():
    """Lista de ControlNodes a los que un DataNode puede reportarse.

    Compatibilidad hacia atrás: si solo está `DFSHA_CONTROL_URL` (una sola URL),
    la lista tiene ese único elemento. `DFSHA_CONTROL_URLS` (separada por comas)
    permite declarar primary + standby para el failover del hito 3. Si están las
    dos, se combinan sin duplicar y conservando el orden (primero la lista nueva).
    """
    urls = []
    for raw in os.getenv("DFSHA_CONTROL_URLS", "").split(","):
        cleaned = raw.strip().rstrip("/")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    if CONTROL_URL and CONTROL_URL not in urls:
        urls.append(CONTROL_URL)
    return urls


CONTROL_URLS = _control_urls()
NODE_CA = os.getenv("DFSHA_NODE_CA") or os.getenv("DFSHA_CONTROL_CA") or True
DATANODES = [
    value.strip().rstrip("/")
    for value in os.getenv("DFSHA_DATANODES", "").split(",")
    if value.strip()
]
NODE_HEARTBEAT_SECONDS = int(os.getenv("DFSHA_NODE_HEARTBEAT_SECONDS", 10))
NODE_TIMEOUT_SECONDS = int(os.getenv("DFSHA_NODE_TIMEOUT_SECONDS", 30))

# --- Alta disponibilidad del ControlNode (hito 3) ------------------------------
# Rol inicial del ControlNode. Puede cambiar en runtime por promoción/degradación
# (ver dfsha/server/ha.py). Por defecto `primary` para no alterar el despliegue
# monolítico/distribuido actual de un solo ControlNode.
HA_ROLE = os.getenv("DFSHA_HA_ROLE", "primary").strip().lower()
if HA_ROLE not in ("primary", "standby"):
    raise ValueError(
        f"DFSHA_HA_ROLE inválido: {HA_ROLE!r} (usa 'primary' o 'standby')"
    )
# URL del otro ControlNode (el par). Vacío = HA desactivado (nodo solo).
HA_PEER_URL = os.getenv("DFSHA_HA_PEER_URL", "").rstrip("/")
# CA para validar el TLS del par; por defecto hereda la de los nodos.
HA_PEER_CA = os.getenv("DFSHA_HA_PEER_CA") or NODE_CA
# Intervalo del ciclo de sincronización/heartbeat del standby.
HA_HEARTBEAT_SECONDS = int(os.getenv("DFSHA_HA_HEARTBEAT_SECONDS", 3))
# Tiempo sin respuesta del primary antes de considerarlo caído (≥ 3× heartbeat).
HA_TIMEOUT_SECONDS = int(os.getenv("DFSHA_HA_TIMEOUT_SECONDS", 10))
# Tras promoverse, periodo durante el cual no se marca a un DataNode offline por
# tener un last_heartbeat viejo (el replicado quedó atrasado tras el failover).
HA_GRACE_SECONDS = int(os.getenv("DFSHA_HA_GRACE_SECONDS", 30))
# Máximo de entradas de oplog antes de podar las ya confirmadas por el standby.
HA_OPLOG_RETENTION = int(os.getenv("DFSHA_HA_OPLOG_RETENTION", 10000))
TOKEN_TTL_SECONDS = int(os.getenv("DFSHA_TOKEN_TTL", 3600))
BLOCK_TICKET_TTL_SECONDS = int(os.getenv("DFSHA_BLOCK_TICKET_TTL", 300))
LOCK_TTL_SECONDS = int(os.getenv("DFSHA_LOCK_TTL", 60))

LOGIN_MAX_FAILURES = int(os.getenv("DFSHA_LOGIN_MAX_FAILURES", 5))
LOGIN_BLOCK_SECONDS = int(os.getenv("DFSHA_LOGIN_BLOCK_SECONDS", 60))


def _persistent_secret(variable, filename, generate):
    """Lee un secreto de una variable de entorno o, si no existe, de un archivo
    en DFSHA_HOME que se crea la primera vez. En un clúster todos los nodos deben
    recibir el mismo valor por variable de entorno."""
    value = os.getenv(variable)
    if value:
        return value

    path = HOME / filename
    if path.exists():
        return path.read_text().strip()

    HOME.mkdir(parents=True, exist_ok=True)
    value = generate()
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


SECRET_KEY = _persistent_secret(
    "DFSHA_SECRET", "jwt.secret", lambda: secrets.token_urlsafe(48)
)
STORAGE_KEY = base64.urlsafe_b64decode(
    _persistent_secret(
        "DFSHA_STORAGE_KEY",
        "storage.key",
        lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
)
if len(STORAGE_KEY) != 32:
    raise ValueError("DFSHA_STORAGE_KEY debe ser una llave AES-256 en base64 (32 bytes)")
# Secreto compartido entre ControlNode y DataNodes: firma los tickets de acceso a
# bloques y los tokens de nodo (ver dfsha/cluster_auth.py).
CLUSTER_SECRET = _persistent_secret(
    "DFSHA_CLUSTER_SECRET", "cluster.secret", lambda: secrets.token_urlsafe(48)
)
