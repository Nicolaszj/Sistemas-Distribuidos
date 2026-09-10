import requests


class DfsError(Exception):
    pass


class DfsClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.token = None

    def register(self, username, password):
        self.request(
            "POST", "/auth/register", json={"username": username, "password": password}
        )

    def login(self, username, password):
        response = self.request(
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        self.token = response.json()["token"]

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

    def upload(self, path, source):
        return self.request(
            "POST", "/files/upload", params={"path": path}, data=source
        ).json()

    def blocks(self, path):
        return self.request("GET", "/files/blocks", params={"path": path}).json()

    def read_block(self, block_id):
        return self.request("GET", f"/blocks/{block_id}").content

    def request(self, method, endpoint, **kwargs):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = self.session.request(
                method, self.base_url + endpoint, headers=headers, **kwargs
            )
        except requests.RequestException as error:
            raise DfsError(f"No hay conexión con el servidor: {error}")

        if not response.ok:
            raise DfsError(describe_error(response))

        return response


def describe_error(response):
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = None

    return detail if isinstance(detail, str) else f"Error {response.status_code}"
