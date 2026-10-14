import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKDIR = Path(tempfile.mkdtemp(prefix="dfsha-test-"))

# La configuración se lee al importar, así que el entorno va antes de importar dfsha.
os.environ["DFSHA_HOME"] = str(WORKDIR / "data")
os.environ["DFSHA_BLOCK_SIZE"] = "16"
os.environ["DFSHA_LOGIN_MAX_FAILURES"] = "3"
os.environ["DFSHA_LOGIN_BLOCK_SECONDS"] = "2"
sys.path.insert(0, str(ROOT))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def certs():
    directory = WORKDIR / "certs"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_certs.py"), "controlnode", "--out", str(directory)],
        check=True,
        capture_output=True,
    )
    return directory


@pytest.fixture(scope="session")
def server(certs):
    """Servidor real con TLS en un hilo, para probar el sistema como lo usa el cliente."""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(
        "dfsha.server.main:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ssl_certfile=str(certs / "controlnode.crt"),
        ssl_keyfile=str(certs / "controlnode.key"),
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    while not instance.started:
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def make_client(server, certs):
    from dfsha.client.api import DfsClient

    counter = iter(range(10_000))

    def make(name=None, password="contrasena-segura"):
        name = name or f"u{next(counter)}_{time.time_ns()}"
        client = DfsClient(server, ca_file=str(certs / "ca.crt"))
        client.register(name, password)
        client.login(name, password)
        client.username = name
        return client

    return make
