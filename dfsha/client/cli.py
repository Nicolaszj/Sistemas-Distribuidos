import argparse
import getpass
import hashlib
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlparse

from .. import paths
from .api import DfsClient, DfsError, OtpRequired
from .files import dfs_open

USAGE = """Comandos disponibles:
  ls [ruta]                 lista un directorio
  cd <ruta>                 cambia de directorio
  pwd                       muestra el directorio actual
  mkdir <ruta>              crea un directorio
  rmdir <ruta>              borra un directorio vacío
  rm <ruta>                 borra un archivo
  put <local> [remoto]      sube un archivo al DFS
  get <remoto> [local]      descarga un archivo del DFS
  blocks <ruta>             muestra los bloques de un archivo
  stat <ruta>               tipo, tamaño, checksum y lock de una ruta

Acceso a archivos (RF3):
  cat <ruta>                imprime el contenido de un archivo
  read <ruta> <offset> <n>  lee n bytes desde offset (solo transfiere ese rango)
  write <ruta> <texto>      crea o reemplaza un archivo con el texto
  append <ruta> <texto>     agrega el texto al final del archivo
  lock <ruta> [segundos]    toma (o renueva) el lock exclusivo de escritura
  unlock <ruta>             libera el lock

Compartir (ACL):
  share <ruta> <usuario|@grupo> <r|rw>   da permiso sobre un archivo o directorio
  unshare <ruta> <usuario|@grupo>        quita el permiso
  acl <ruta>                             muestra quién tiene acceso
  shared                                 lo que otros usuarios compartieron contigo
  group new <nombre>                     crea un grupo
  group add|del <nombre> <usuario>       agrega o quita un miembro
  groups                                 lista tus grupos
  (las rutas de otro usuario se escriben /~usuario/ruta)

Cuenta:
  whoami                    usuario, grupos y estado del 2FA
  2fa setup|enable|disable  configura el segundo factor (TOTP)
  apikey new <nombre>       crea una API key
  apikey ls                 lista tus API keys
  apikey rm <id>            revoca una API key

  help                      muestra esta ayuda
  exit                      termina la sesión"""


class Shell:
    def __init__(self, client):
        self.client = client
        self.cwd = paths.ROOT
        self.commands = {
            "ls": self.do_ls,
            "cd": self.do_cd,
            "pwd": self.do_pwd,
            "mkdir": self.do_mkdir,
            "rmdir": self.do_rmdir,
            "rm": self.do_rm,
            "put": self.do_put,
            "get": self.do_get,
            "blocks": self.do_blocks,
            "stat": self.do_stat,
            "cat": self.do_cat,
            "read": self.do_read,
            "write": self.do_write,
            "append": self.do_append,
            "lock": self.do_lock,
            "unlock": self.do_unlock,
            "share": self.do_share,
            "unshare": self.do_unshare,
            "acl": self.do_acl,
            "shared": self.do_shared,
            "group": self.do_group,
            "groups": self.do_groups,
            "whoami": self.do_whoami,
            "2fa": self.do_2fa,
            "apikey": self.do_apikey,
            "help": self.do_help,
        }

    def run(self):
        while True:
            try:
                line = input(f"dfsha:{self.cwd}$ ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if not line:
                continue

            try:
                arguments = shlex.split(line)
            except ValueError as error:
                print(f"Error de sintaxis: {error}")
                continue
            command = arguments.pop(0)
            if command in ("exit", "quit"):
                return

            handler = self.commands.get(command)
            if handler is None:
                print(f"Comando desconocido: {command}")
                continue

            try:
                handler(arguments)
            except DfsError as error:
                print(error)
            except OSError as error:
                print(f"Error local: {error}")
            except (ValueError, IndexError) as error:
                print(f"Argumentos inválidos: {error}")

    def resolve(self, path):
        if path.startswith("~"):
            path = "/" + path
        return paths.normalize(path, self.cwd)

    def do_ls(self, arguments):
        target = self.resolve(arguments[0] if arguments else self.cwd)
        for entry in self.client.ls(target)["entries"]:
            if entry["type"] == "dir":
                print(f"{'<dir>':>12}  {entry['name']}/")
            else:
                print(f"{entry['size']:>12}  {entry['name']}")

    def do_cd(self, arguments):
        if not arguments:
            self.cwd = paths.ROOT
            return

        target = self.resolve(arguments[0])
        if self.client.stat(target)["type"] != "dir":
            print(f"{target} no es un directorio")
            return

        self.cwd = target

    def do_pwd(self, arguments):
        print(self.cwd)

    def do_mkdir(self, arguments):
        if not arguments:
            print("uso: mkdir <ruta>")
            return

        self.client.mkdir(self.resolve(arguments[0]))

    def do_rmdir(self, arguments):
        if not arguments:
            print("uso: rmdir <ruta>")
            return

        self.client.rmdir(self.resolve(arguments[0]))

    def do_rm(self, arguments):
        if not arguments:
            print("uso: rm <ruta>")
            return

        self.client.rm(self.resolve(arguments[0]))

    def do_put(self, arguments):
        if not arguments:
            print("uso: put <local> [remoto]")
            return

        source = Path(arguments[0])
        target = self.resolve(arguments[1] if len(arguments) > 1 else source.name)

        with source.open("rb") as data:
            result = self.client.upload(target, data)

        print(f"{target}: {result['size']} bytes en {result['blocks']} bloques")

    def do_get(self, arguments):
        if not arguments:
            print("uso: get <remoto> [local]")
            return

        target = self.resolve(arguments[0])
        plan = self.client.blocks(target)
        destination = Path(arguments[1] if len(arguments) > 1 else target.split("/")[-1])

        digest = hashlib.sha256()
        with destination.open("wb") as output:
            for block in plan["blocks"]:
                data = self.client.read_block(block)
                digest.update(data)
                output.write(data)

        if plan.get("checksum") and digest.hexdigest() != plan["checksum"]:
            print(f"ADVERTENCIA: el checksum de {destination} no coincide")
            return

        verified = " (SHA-256 verificado)" if plan.get("checksum") else ""
        print(
            f"{destination}: {plan['size']} bytes desde {len(plan['blocks'])} bloques"
            f"{verified}"
        )

    def do_blocks(self, arguments):
        if not arguments:
            print("uso: blocks <ruta>")
            return

        plan = self.client.blocks(self.resolve(arguments[0]))
        print(f"{plan['path']}  {plan['size']} bytes  bloque={plan['block_size']} bytes")
        for block in plan["blocks"]:
            checksum = (block.get("checksum") or "")[:12]
            node = urlparse(block.get("url", "")).netloc or "controlnode"
            print(
                f"  [{block['sequence']}] {block['id']}  {block['size']} bytes"
                f"  {checksum}  en {node}"
            )

    def do_stat(self, arguments):
        info = self.client.stat(self.resolve(arguments[0] if arguments else self.cwd))
        for key, value in info.items():
            print(f"  {key}: {value}")

    # --- RF3 -----------------------------------------------------------------------

    def do_cat(self, arguments):
        if not arguments:
            print("uso: cat <ruta>")
            return
        with dfs_open(self.client, self.resolve(arguments[0])) as file:
            show(file.read())

    def do_read(self, arguments):
        if len(arguments) < 3:
            print("uso: read <ruta> <offset> <n>")
            return
        with dfs_open(self.client, self.resolve(arguments[0])) as file:
            file.seek(int(arguments[1]))
            show(file.read(int(arguments[2])))

    def do_write(self, arguments, mode="w"):
        if len(arguments) < 2:
            print(f"uso: {'write' if mode == 'w' else 'append'} <ruta> <texto>")
            return
        target = self.resolve(arguments[0])
        with dfs_open(self.client, target, mode) as file:
            file.write(" ".join(arguments[1:]) + "\n")
            size = file.tell()
        print(f"{target}: {size} bytes")

    def do_append(self, arguments):
        self.do_write(arguments, mode="a")

    def do_lock(self, arguments):
        if not arguments:
            print("uso: lock <ruta> [segundos]")
            return
        ttl = int(arguments[1]) if len(arguments) > 1 else None
        result = self.client.lock(self.resolve(arguments[0]), ttl)
        print(f"{result['path']} bloqueado por {result['ttl']} s (lock {result['lock_id']})")

    def do_unlock(self, arguments):
        if not arguments:
            print("uso: unlock <ruta>")
            return
        result = self.client.unlock(self.resolve(arguments[0]))
        print(f"{result['path']} desbloqueado")

    # --- ACL y grupos ------------------------------------------------------------------

    def do_share(self, arguments):
        if len(arguments) < 3 or arguments[2] not in ("r", "rw"):
            print("uso: share <ruta> <usuario|@grupo> <r|rw>")
            return
        user, group = grantee(arguments[1])
        result = self.client.grant(self.resolve(arguments[0]), arguments[2], user, group)
        print(f"{result['path']}: {arguments[1]} ahora tiene permiso {arguments[2]}")

    def do_unshare(self, arguments):
        if len(arguments) < 2:
            print("uso: unshare <ruta> <usuario|@grupo>")
            return
        user, group = grantee(arguments[1])
        result = self.client.revoke(self.resolve(arguments[0]), user, group)
        print(f"{result['path']}: permiso de {arguments[1]} revocado")

    def do_acl(self, arguments):
        result = self.client.acl(self.resolve(arguments[0] if arguments else self.cwd))
        print(f"{result['path']} ({result['type']})")
        if not result["entries"]:
            print("  solo el dueño tiene acceso")
        for entry in result["entries"]:
            prefix = "@" if entry["grantee_type"] == "group" else ""
            print(f"  {entry['permission']:<3} {prefix}{entry['grantee']}")

    def do_shared(self, arguments):
        entries = self.client.shared()
        if not entries:
            print("Nadie ha compartido nada contigo todavía")
        for entry in entries:
            print(f"  {entry['permission']:<3} {entry['type']:<4} {entry['path']}")

    def do_group(self, arguments):
        if len(arguments) >= 2 and arguments[0] == "new":
            self.client.create_group(arguments[1])
            print(f"Grupo {arguments[1]} creado")
        elif len(arguments) >= 3 and arguments[0] == "add":
            self.client.add_member(arguments[1], arguments[2])
            print(f"{arguments[2]} agregado a {arguments[1]}")
        elif len(arguments) >= 3 and arguments[0] == "del":
            self.client.remove_member(arguments[1], arguments[2])
            print(f"{arguments[2]} retirado de {arguments[1]}")
        else:
            print("uso: group new <nombre> | group add|del <nombre> <usuario>")

    def do_groups(self, arguments):
        for group in self.client.groups():
            role = "dueño" if group["owner"] else "miembro"
            print(f"  {group['name']} ({role}): {', '.join(group['members'])}")

    # --- cuenta ----------------------------------------------------------------------------

    def do_whoami(self, arguments):
        info = self.client.me()
        print(f"  usuario: {info['username']}")
        print(f"  grupos: {', '.join(info['groups']) or '-'}")
        print(f"  2FA: {'activo' if info['totp_enabled'] else 'inactivo'}")

    def do_2fa(self, arguments):
        action = arguments[0] if arguments else ""
        if action == "setup":
            result = self.client.setup_2fa()
            print("Agrega esta cuenta en tu app autenticadora (Google Authenticator, etc.):")
            print_qr(result["otpauth_uri"])
            print(f"  clave manual: {result['secret']}")
            print("Luego confirma con: 2fa enable <código de 6 dígitos>")
        elif action == "enable" and len(arguments) > 1:
            self.client.enable_2fa(arguments[1])
            print("2FA activado: desde ahora el login pide el código")
        elif action == "disable" and len(arguments) > 1:
            self.client.disable_2fa(arguments[1])
            print("2FA desactivado")
        else:
            print("uso: 2fa setup | 2fa enable <código> | 2fa disable <código>")

    def do_apikey(self, arguments):
        action = arguments[0] if arguments else ""
        if action == "new" and len(arguments) > 1:
            result = self.client.create_api_key(arguments[1])
            print(f"API key {result['id']} ({result['name']}). Guárdala, no se vuelve a mostrar:")
            print(f"  {result['key']}")
        elif action == "ls":
            for key in self.client.list_api_keys():
                used = key["last_used_at"] or "nunca"
                print(f"  {key['id']:>3}  {key['name']:<16} dfsha_{key['prefix']}_…  uso: {used}")
        elif action == "rm" and len(arguments) > 1:
            self.client.delete_api_key(int(arguments[1]))
            print(f"API key {arguments[1]} revocada")
        else:
            print("uso: apikey new <nombre> | apikey ls | apikey rm <id>")

    def do_help(self, arguments):
        print(USAGE)


def show(data):
    try:
        print(data.decode(), end="" if data.endswith(b"\n") else "\n")
    except UnicodeDecodeError:
        print(data)


def grantee(name):
    return (None, name[1:]) if name.startswith("@") else (name, None)


def print_qr(uri):
    try:
        import qrcode

        code = qrcode.QRCode(border=1)
        code.add_data(uri)
        code.print_ascii(invert=True)
    except ImportError:
        pass
    print(f"  {uri}")


def main():
    parser = argparse.ArgumentParser(description="Cliente CLI de DFSha")
    parser.add_argument("--server", default=os.getenv("DFSHA_SERVER", "http://localhost:8000"))
    parser.add_argument("--user", help="usuario (no hace falta con --api-key)")
    parser.add_argument("--register", action="store_true", help="crea el usuario antes de entrar")
    parser.add_argument("--ca", default=os.getenv("DFSHA_CA"), help="certificado de la CA para https")
    parser.add_argument("--api-key", default=os.getenv("DFSHA_API_KEY"), help="entra con una API key")
    parser.add_argument("--otp", help="código 2FA (si no se da y hace falta, se pide)")
    arguments = parser.parse_args()

    client = DfsClient(arguments.server, ca_file=arguments.ca, api_key=arguments.api_key)

    try:
        if arguments.api_key:
            name = client.me()["username"]
        else:
            if not arguments.user:
                parser.error("--user es obligatorio si no se usa --api-key")
            password = getpass.getpass("Contraseña: ")
            if arguments.register:
                client.register(arguments.user, password)
            try:
                client.login(arguments.user, password, arguments.otp)
            except OtpRequired:
                client.login(arguments.user, password, input("Código 2FA: ").strip())
            name = arguments.user
    except DfsError as error:
        print(error)
        return 1

    print(f"Conectado a {arguments.server} como {name}. Escribe help.")
    Shell(client).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
