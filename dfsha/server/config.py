import base64
import os
import secrets
from pathlib import Path

HOME = Path(os.getenv("DFSHA_HOME", Path(__file__).resolve().parents[2] / "data"))
DATABASE_PATH = HOME / "metadata.db"
BLOCKS_DIR = HOME / "blocks"

BLOCK_SIZE = int(os.getenv("DFSHA_BLOCK_SIZE", 4 * 1024 * 1024))
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
