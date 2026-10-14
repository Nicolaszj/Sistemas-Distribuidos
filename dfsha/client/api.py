import hashlib

import requests


class DfsError(Exception):
    pass


class OtpRequired(DfsError):
    pass


class IntegrityError(DfsError):
    pass


class DfsClient:
    def __init__(self, base_url, ca_file=None, api_key=None):
        """`ca_file`: certificado de la CA del clúster para validar TLS (https).
        `api_key`: alternativa a usuario/contraseña para scripts y automatización."""
        self.base_url = base_url.rstrip("/")
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
        params = {"path": path}
        if overwrite:
            params["overwrite"] = "true"
        return self.request("POST", "/files/upload", params=params, data=source).json()

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

    def request(self, method, endpoint, extra_headers=None, **kwargs):
        headers = dict(extra_headers or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key

        url = endpoint if endpoint.startswith("http") else self.base_url + endpoint
        try:
            response = self.session.request(
                method, url, headers=headers, verify=self.verify, **kwargs
            )
        except requests.exceptions.SSLError as error:
            raise DfsError(f"Error TLS (¿falta --ca con el certificado de la CA?): {error}")
        except requests.RequestException as error:
            raise DfsError(f"No hay conexión con el servidor: {error}")

        if response.status_code == 401 and response.headers.get("X-DFSha-OTP") == "required":
            raise OtpRequired(describe_error(response))
        if not response.ok:
            raise DfsError(describe_error(response))

        return response


def describe_error(response):
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = None

    return detail if isinstance(detail, str) else f"Error {response.status_code}"
