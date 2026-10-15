import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import requests


class DfsError(Exception):
    pass


class OtpRequired(DfsError):
    pass


class IntegrityError(DfsError):
    pass


class _Retryable(Exception):
    """Fallo de conexión/timeout que permite reintentar en otro ControlNode."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _normalize_urls(base_url):
    """Admite una str (con una o varias URLs separadas por comas) o una lista."""
    if isinstance(base_url, (list, tuple)):
        raw = base_url
    else:
        raw = str(base_url).split(",")
    urls = []
    for item in raw:
        cleaned = item.strip().rstrip("/")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    if not urls:
        raise DfsError("Se requiere al menos una URL de ControlNode")
    return urls


def _is_not_primary(response):
    """True si la respuesta es el 503 {"error":"not_primary"} de un standby."""
    if response.status_code != 503:
        return False
    try:
        return response.json().get("error") == "not_primary"
    except ValueError:
        return False


class DfsClient:
    def __init__(self, base_url, ca_file=None, api_key=None):
        """`base_url`: URL del ControlNode, o una lista de URLs (primary + standby)
        para alta disponibilidad. También admite una sola cadena con URLs separadas
        por comas. Con una sola URL se comporta igual que antes.
        `ca_file`: certificado de la CA del clúster para validar TLS (https).
        `api_key`: alternativa a usuario/contraseña para scripts y automatización."""
        self.urls = _normalize_urls(base_url)
        # URL del ControlNode que se usa ahora; se mueve al que responda como primary.
        self.base_url = self.urls[0]
        self.session = requests.Session()
        # Se pasa en cada petición: requests ignora session.verify si existe la
        # variable de entorno REQUESTS_CA_BUNDLE.
        self.verify = ca_file or True
        self.token = None
        self.api_key = api_key

    # --- autenticación ---------------------------------------------------------

    def register(self, username, password):
        self.request(
            "POST", "/auth/register", json={"username": username, "password": password}
        )

    def login(self, username, password, otp=None):
        body = {"username": username, "password": password}
        if otp:
            body["otp"] = otp
        response = self.request("POST", "/auth/login", json=body)
        self.token = response.json()["token"]

    def me(self):
        return self.request("GET", "/auth/me").json()

    def setup_2fa(self):
        return self.request("POST", "/auth/2fa/setup").json()

    def enable_2fa(self, code):
        return self.request("POST", "/auth/2fa/enable", json={"code": code}).json()

    def disable_2fa(self, code):
        return self.request("POST", "/auth/2fa/disable", json={"code": code}).json()

    def create_api_key(self, name):
        return self.request("POST", "/auth/apikeys", json={"name": name}).json()

    def list_api_keys(self):
        return self.request("GET", "/auth/apikeys").json()["keys"]

    def delete_api_key(self, key_id):
        return self.request("DELETE", f"/auth/apikeys/{key_id}").json()

    # --- grupos y ACL ------------------------------------------------------------

    def create_group(self, name):
        return self.request("POST", "/groups", json={"name": name}).json()

    def groups(self):
        return self.request("GET", "/groups").json()["groups"]

    def add_member(self, group, username):
        return self.request(
            "POST", f"/groups/{group}/members", json={"username": username}
        ).json()

    def remove_member(self, group, username):
        return self.request("DELETE", f"/groups/{group}/members/{username}").json()

    def grant(self, path, permission, user=None, group=None):
        return self.request(
            "POST",
            "/acl/grant",
            json={"path": path, "permission": permission, "user": user, "group": group},
        ).json()

    def revoke(self, path, user=None, group=None):
        return self.request(
            "POST", "/acl/revoke", json={"path": path, "user": user, "group": group}
        ).json()

    def acl(self, path):
        return self.request("GET", "/acl", params={"path": path}).json()

    def shared(self):
        return self.request("GET", "/acl/shared").json()["entries"]

    # --- sistema de archivos -------------------------------------------------------

    def ls(self, path):
        return self.request("GET", "/fs/ls", params={"path": path}).json()

    def stat(self, path):
        return self.request("GET", "/fs/stat", params={"path": path}).json()

    def mkdir(self, path):
        return self.request("POST", "/fs/mkdir", json={"path": path}).json()

    def rmdir(self, path):
        return self.request("POST", "/fs/rmdir", json={"path": path}).json()

    def rm(self, path):
        return self.request("DELETE", "/fs/rm", params={"path": path}).json()

    def lock(self, path, ttl=None):
        return self.request("POST", "/fs/lock", json={"path": path, "ttl": ttl}).json()

    def unlock(self, path):
        return self.request("POST", "/fs/unlock", json={"path": path}).json()

    # --- transferencia ---------------------------------------------------------------

    def upload(self, path, source, overwrite=False):
        """Sube un archivo con failover seguro entre ControlNodes.

        Solo se reintenta ante error de conexión o 503 not_primary (el standby no
        procesó nada). Para poder reintentar sin corromper el archivo, el origen se
        rebobina al inicio antes de cada intento; si el origen no es rebobinable
        (p. ej. una tubería), se lee entero en memoria una vez. El servidor, por su
        parte, no deja un archivo a medias ni bloques huérfanos si una subida se
        interrumpe (subida atómica), así que un reintento no acumula basura."""
        params = {"path": path}
        if overwrite:
            params["overwrite"] = "true"

        rewind = getattr(source, "seek", None)
        start = source.tell() if callable(getattr(source, "tell", None)) else None
        if rewind is None or start is None:
            # Origen no rebobinable: materializarlo para poder reintentar.
            body = source.read() if hasattr(source, "read") else source
            source = None
        else:
            body = None

        candidates = [self.base_url] + [u for u in self.urls if u != self.base_url]
        last_error = None
        for base in candidates:
            if source is not None:
                source.seek(start)  # rebobinar para re-enviar el archivo completo
            data = source if source is not None else body
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            elif self.api_key:
                headers["X-API-Key"] = self.api_key
            try:
                response = self._send(
                    "POST", f"{base}/files/upload", headers,
                    {"params": params, "data": data},
                )
            except _Retryable as error:
                last_error = error.cause
                continue
            if _is_not_primary(response):
                last_error = DfsError("El ControlNode respondió 'no soy el primary'")
                continue
            self.base_url = base
            if not response.ok:
                raise DfsError(describe_error(response))
            return response.json()

        raise DfsError(
            f"Ningún ControlNode acepto la subida ({', '.join(self.urls)}): {last_error}"
        )

    def blocks(self, path):
        return self.request("GET", "/files/blocks", params={"path": path}).json()

    def read_block(self, block, offset=0, length=None):
        """Lee un bloque del plan usando su ticket. Si se pide el bloque completo,
        verifica su SHA-256 contra el registrado en el ControlNode."""
        params = {}
        if offset:
            params["offset"] = offset
        if length is not None:
            params["length"] = length

        if isinstance(block, str):
            block = {"id": block, "url": f"/blocks/{block}"}

        headers = {"X-Block-Ticket": block["ticket"]} if block.get("ticket") else {}
        url = block.get("url", f"/blocks/{block['id']}")
        data = self.request("GET", url, params=params, extra_headers=headers).content

        whole = offset == 0 and length is None
        if whole and block.get("checksum"):
            if hashlib.sha256(data).hexdigest() != block["checksum"]:
                raise IntegrityError(f"El bloque {block['id']} no coincide con su checksum")
        return data

    def read_blocks_parallel(self, blocks, max_workers=None):
        """Descarga bloques independientes en paralelo y conserva su orden."""
        workers = max_workers or int(os.getenv("DFSHA_CLIENT_WORKERS", "8"))
        workers = max(1, min(workers, len(blocks) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.read_block, blocks))

    def request(self, method, endpoint, extra_headers=None, safe=True, **kwargs):
        """Realiza una petición con failover entre ControlNodes.

        `safe`: True para operaciones idempotentes/de solo lectura (ls, stat, get,
        blocks). Ante un ControlNode que no responde o que contesta 503 not_primary,
        el cliente reintenta automáticamente contra las otras URLs.

        Las operaciones de escritura (upload, mkdir, rm, lock, ...) pasan safe=True
        también porque el único reintento permitido es ante error de conexión o 503
        not_primary (el ControlNode ni siquiera llegó a procesar la escritura, o es
        un standby). No se reintenta ante otros errores (p. ej. 409, 423) para no
        repetir una operación que sí se evaluó. La protección contra subidas a
        medias vive en `upload` (reposiciona el origen y el servidor limpia bloques
        huérfanos si una subida falla)."""
        headers = dict(extra_headers or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key

        # Si el endpoint es una URL absoluta (p. ej. la URL de un bloque en un
        # DataNode), no se aplica failover de ControlNode: va directa.
        if endpoint.startswith("http"):
            try:
                response = self._send(method, endpoint, headers, kwargs)
            except _Retryable as error:
                raise DfsError(f"No hay conexión con el servidor: {error.cause}")
            if response.status_code == 401 and response.headers.get("X-DFSha-OTP") == "required":
                raise OtpRequired(describe_error(response))
            if not response.ok:
                raise DfsError(describe_error(response))
            return response

        # Orden de intento: el ControlNode actual primero, luego el resto.
        candidates = [self.base_url] + [u for u in self.urls if u != self.base_url]
        last_error = None
        for base in candidates:
            try:
                response = self._send(method, base + endpoint, headers, kwargs)
            except _Retryable as error:
                last_error = error.cause
                continue  # ese ControlNode no respondió: probar el siguiente

            # 503 not_primary: ese nodo es standby, reintentar en otro.
            if _is_not_primary(response):
                last_error = DfsError("El ControlNode respondió 'no soy el primary'")
                continue

            # Respuesta válida de un primary: fijarlo como actual y devolver.
            self.base_url = base
            if response.status_code == 401 and response.headers.get("X-DFSha-OTP") == "required":
                raise OtpRequired(describe_error(response))
            if not response.ok:
                raise DfsError(describe_error(response))
            return response

        raise DfsError(
            f"Ningún ControlNode respondió ({', '.join(self.urls)}): {last_error}"
        )

    def _send(self, method, url, headers, kwargs):
        """Una sola petición HTTP. Traduce fallos de conexión en _Retryable para que
        el failover pueda probar con otro ControlNode."""
        try:
            return self.session.request(
                method, url, headers=headers, verify=self.verify, **kwargs
            )
        except requests.exceptions.SSLError as error:
            # Un error TLS no es de disponibilidad: no tiene sentido reintentar.
            raise DfsError(f"Error TLS (¿falta --ca con el certificado de la CA?): {error}")
        except requests.RequestException as error:
            raise _Retryable(error)


def describe_error(response):
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = None

    return detail if isinstance(detail, str) else f"Error {response.status_code}"
