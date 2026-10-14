"""RF3 — acceso a archivos: open(), read(), seek(), write(), close(), lock().

A diferencia de put/get (transferencia de archivo completo), aquí el cliente
trabaja con un descriptor:

- Lectura ("r"): se pide el plan de bloques al ControlNode y `read(n)` descarga
  solo los rangos de los bloques que cubren [posición, posición + n). Leer 10 bytes
  del medio de un archivo de 1 GB transfiere 10 bytes, no 1 GB.
- Escritura ("w" crea/reemplaza, "a" agrega al final): DFSha sigue el modelo de
  object storage (WORM): no se actualiza un bloque en sitio. Los cambios se
  acumulan en un archivo temporal local y en close() se sube la versión nueva,
  que reemplaza a la anterior de forma atómica en el ControlNode.
- Mientras un archivo está abierto para escritura se mantiene un lock exclusivo
  (lease) en el ControlNode; otros clientes no pueden escribirlo ni borrarlo.

Uso:

    with dfs_open(client, "/datos/log.txt", "a") as f:
        f.write(b"nueva linea\\n")

    with dfs_open(client, "/datos/video.mp4") as f:
        f.seek(1_000_000)
        cabecera = f.read(64)
"""

import io
import tempfile
import threading

from .api import DfsError


class DfsFile(io.RawIOBase):
    def __init__(self, client, path, mode="r", lock_ttl=60):
        if mode not in ("r", "w", "a"):
            raise ValueError("modo debe ser 'r', 'w' o 'a'")
        super().__init__()
        self.client = client
        self.path = path
        self.mode = mode
        self.lock_ttl = lock_ttl
        self.position = 0
        self._renewer = None
        self._stop = threading.Event()

        if mode == "r":
            self.plan = client.blocks(path)
            self.size = self.plan["size"]
            return

        exists = self._exists()
        if exists:
            client.lock(path, lock_ttl)
            self._start_renewing()

        self.buffer = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        if mode == "a" and exists:
            plan = client.blocks(path)
            for block in plan["blocks"]:
                self.buffer.write(client.read_block(block))
        self.position = self.buffer.tell()

    # --- API tipo archivo --------------------------------------------------------

    def readable(self):
        return self.mode == "r"

    def writable(self):
        return self.mode in ("w", "a")

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self.position + offset
        elif whence == io.SEEK_END:
            target = self._size() + offset
        else:
            raise ValueError("whence inválido")
        if target < 0:
            raise ValueError("posición negativa")
        self.position = target
        if self.writable():
            self.buffer.seek(target)
        return self.position

    def read(self, size=-1):
        if not self.readable():
            raise io.UnsupportedOperation("archivo abierto para escritura")
        end = self.size if size is None or size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b""

        chunks = []
        start = 0
        for block in self.plan["blocks"]:
            block_end = start + block["size"]
            if block_end > self.position and start < end:
                offset = max(self.position, start) - start
                length = min(end, block_end) - start - offset
                if offset == 0 and length == block["size"]:
                    chunks.append(self.client.read_block(block))
                else:
                    chunks.append(self.client.read_block(block, offset, length))
            if block_end >= end:
                break
            start = block_end

        data = b"".join(chunks)
        self.position += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def write(self, data):
        if not self.writable():
            raise io.UnsupportedOperation("archivo abierto para lectura")
        if isinstance(data, str):
            data = data.encode()
        written = self.buffer.write(data)
        self.position = self.buffer.tell()
        return written

    def close(self):
        if self.closed:
            return
        try:
            if self.writable():
                self.buffer.seek(0)
                self.client.upload(self.path, self.buffer, overwrite=True)
                self.buffer.close()
        finally:
            self._stop.set()
            if self.writable():
                try:
                    self.client.unlock(self.path)
                except DfsError:
                    pass
            super().close()

    # --- internos ------------------------------------------------------------------

    def _size(self):
        if self.readable():
            return self.size
        current = self.buffer.tell()
        self.buffer.seek(0, io.SEEK_END)
        size = self.buffer.tell()
        self.buffer.seek(current)
        return size

    def _exists(self):
        try:
            return self.client.stat(self.path)["type"] == "file"
        except DfsError:
            return False

    def _start_renewing(self):
        """Renueva el lease a la mitad de su vida mientras el archivo esté abierto."""

        def renew():
            while not self._stop.wait(self.lock_ttl / 2):
                try:
                    self.client.lock(self.path, self.lock_ttl)
                except DfsError:
                    return

        self._renewer = threading.Thread(target=renew, daemon=True)
        self._renewer.start()


def dfs_open(client, path, mode="r", lock_ttl=60):
    return DfsFile(client, path, mode, lock_ttl)
