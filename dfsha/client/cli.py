import argparse
import getpass
import shlex
import sys
from pathlib import Path

from .. import paths
from .api import DfsClient, DfsError

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

            arguments = shlex.split(line)
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

    def resolve(self, path):
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

        with destination.open("wb") as output:
            for block in plan["blocks"]:
                output.write(self.client.read_block(block["id"]))

        print(f"{destination}: {plan['size']} bytes desde {len(plan['blocks'])} bloques")

    def do_blocks(self, arguments):
        if not arguments:
            print("uso: blocks <ruta>")
            return

        plan = self.client.blocks(self.resolve(arguments[0]))
        print(f"{plan['path']}  {plan['size']} bytes  bloque={plan['block_size']} bytes")
        for block in plan["blocks"]:
            print(f"  [{block['sequence']}] {block['id']}  {block['size']} bytes")

    def do_help(self, arguments):
        print(USAGE)


def main():
    parser = argparse.ArgumentParser(description="Cliente CLI de DFSha")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--register", action="store_true")
    arguments = parser.parse_args()

    password = getpass.getpass("Contraseña: ")
    client = DfsClient(arguments.server)

    try:
        if arguments.register:
            client.register(arguments.user, password)
        client.login(arguments.user, password)
    except DfsError as error:
        print(error)
        return 1

    print(f"Conectado a {arguments.server} como {arguments.user}. Escribe help.")
    Shell(client).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
