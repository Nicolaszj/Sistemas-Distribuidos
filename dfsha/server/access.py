"""Control de acceso a rutas.

Cada usuario tiene su propio árbol. Una ruta normal (`/datos/a.txt`) se refiere al
árbol del usuario autenticado; una ruta que empieza por `/~dueño` (`/~ana/datos`)
se refiere al árbol de otro usuario y solo se puede usar si la ACL lo permite.

Permisos: `r` (listar, leer, descargar) y `rw` (además crear, sobrescribir y
borrar). Se otorgan a usuarios o a grupos, sobre un directorio (se heredan a todo
su contenido) o sobre un archivo. El dueño siempre tiene `rw` y es el único que
puede cambiar la ACL.
"""

from dataclasses import dataclass

from fastapi import HTTPException

from .. import paths
from . import metadata

READ = "r"
READ_WRITE = "rw"


@dataclass
class Target:
    owner_id: int
    path: str
    display: str


def target(db, user_id, path):
    """Separa el dueño y la ruta dentro de su árbol."""
    normalized = paths.normalize(path)
    parts = normalized.split("/")
    if len(parts) > 1 and parts[1].startswith("~"):
        owner = metadata.find_user(db, parts[1][1:])
        if owner is None:
            raise HTTPException(status_code=404, detail=f"No existe el usuario {parts[1][1:]}")
        inner = "/" + "/".join(parts[2:])
        return Target(owner["id"], paths.normalize(inner), normalized)
    return Target(user_id, normalized, normalized)


def allows(permission, need):
    return permission == READ_WRITE or (permission == READ and need == READ)


def permission_on(db, user_id, owner_id, directory_ids, file_id=None):
    if user_id == owner_id:
        return READ_WRITE
    return metadata.best_permission(db, user_id, directory_ids, file_id)


def _deny(user_id, owner_id, display):
    # Sin ningún permiso se responde 404 para no revelar qué rutas existen.
    raise HTTPException(status_code=404, detail=f"No existe la ruta {display}")


def directory(db, user_id, path, need=READ):
    """Devuelve (Target, fila del directorio) o lanza 404/403."""
    where = target(db, user_id, path)
    chain = metadata.resolve_chain(db, where.owner_id, where.path)
    if chain is None:
        raise HTTPException(status_code=404, detail=f"No existe el directorio {where.display}")

    permission = permission_on(db, user_id, where.owner_id, [row["id"] for row in chain])
    if permission is None:
        _deny(user_id, where.owner_id, where.display)
    if not allows(permission, need):
        raise HTTPException(status_code=403, detail=f"Sin permiso de escritura en {where.display}")
    return where, chain[-1]


def file(db, user_id, path, need=READ):
    """Devuelve (Target, fila del archivo) o lanza 404/403."""
    where = target(db, user_id, path)
    parent, name = paths.split(where.path)
    chain = metadata.resolve_chain(db, where.owner_id, parent) if name else None
    row = None
    if chain is not None:
        row = db.execute(
            "SELECT * FROM files WHERE directory_id = ? AND name = ?",
            (chain[-1]["id"], name),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No existe el archivo {where.display}")

    permission = permission_on(
        db, user_id, where.owner_id, [r["id"] for r in chain], row["id"]
    )
    if permission is None:
        _deny(user_id, where.owner_id, where.display)
    if not allows(permission, need):
        raise HTTPException(status_code=403, detail=f"Sin permiso de escritura en {where.display}")
    return where, row


def block(db, user_id, block_id):
    """Fila del bloque si el usuario puede leer el archivo al que pertenece."""
    row = metadata.find_block(db, block_id)
    if row is None:
        return None
    directory_ids = metadata.directory_ancestors(db, row["directory_id"])
    permission = permission_on(db, user_id, row["owner_id"], directory_ids, row["file_id"])
    return row if permission is not None else None
