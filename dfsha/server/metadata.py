import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import paths
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    parent_id INTEGER REFERENCES directories(id),
    name TEXT NOT NULL,
    UNIQUE (user_id, parent_id, name)
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    directory_id INTEGER NOT NULL REFERENCES directories(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    checksum TEXT,
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    sequence INTEGER NOT NULL,
    size INTEGER NOT NULL,
    checksum TEXT,
    UNIQUE (file_id, sequence)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    owner_id INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS acl (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    node_type TEXT NOT NULL CHECK (node_type IN ('dir', 'file')),
    node_id INTEGER NOT NULL,
    grantee_type TEXT NOT NULL CHECK (grantee_type IN ('user', 'group')),
    grantee_id INTEGER NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('r', 'rw')),
    UNIQUE (node_type, node_id, grantee_type, grantee_id)
);

CREATE TABLE IF NOT EXISTS locks (
    owner_id INTEGER NOT NULL REFERENCES users(id),
    path TEXT NOT NULL,
    holder_id INTEGER NOT NULL REFERENCES users(id),
    lock_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (owner_id, path)
);
"""

# Columnas agregadas después del hito 1; se crean en bases ya existentes.
MIGRATIONS = [
    ("users", "totp_secret", "TEXT"),
    ("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("files", "checksum", "TEXT"),
    ("blocks", "checksum", "TEXT"),
]


@contextmanager
def connect():
    connection = sqlite3.connect(config.DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize():
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now():
    return datetime.now(timezone.utc).isoformat()


def find_user(db, username):
    return db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def find_user_by_id(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_totp(db, user_id, secret, enabled):
    db.execute(
        "UPDATE users SET totp_secret = ?, totp_enabled = ? WHERE id = ?",
        (secret, int(enabled), user_id),
    )


def create_user(db, username, password_hash, salt):
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
        (username, password_hash, salt),
    )
    user_id = cursor.lastrowid
    db.execute(
        "INSERT INTO directories (user_id, parent_id, name) VALUES (?, NULL, '')",
        (user_id,),
    )
    return user_id


def root_directory(db, user_id):
    return db.execute(
        "SELECT * FROM directories WHERE user_id = ? AND parent_id IS NULL",
        (user_id,),
    ).fetchone()


def resolve_chain(db, user_id, path):
    """Directorios desde la raíz del usuario hasta `path`, o None si no existe."""
    current = root_directory(db, user_id)
    if current is None:
        return None

    chain = [current]
    for name in paths.normalize(path).split("/"):
        if not name:
            continue
        current = db.execute(
            "SELECT * FROM directories WHERE user_id = ? AND parent_id = ? AND name = ?",
            (user_id, current["id"], name),
        ).fetchone()
        if current is None:
            return None
        chain.append(current)
    return chain


def resolve_directory(db, user_id, path):
    chain = resolve_chain(db, user_id, path)
    return chain[-1] if chain else None


def directory_ancestors(db, directory_id):
    """Ids del directorio y de todos sus ancestros hasta la raíz."""
    ids = []
    while directory_id is not None:
        ids.append(directory_id)
        row = db.execute(
            "SELECT parent_id FROM directories WHERE id = ?", (directory_id,)
        ).fetchone()
        directory_id = row["parent_id"] if row else None
    return ids


def directory_path(db, directory_id):
    names = []
    while True:
        row = db.execute(
            "SELECT parent_id, name FROM directories WHERE id = ?", (directory_id,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        names.append(row["name"])
        directory_id = row["parent_id"]
    return "/" + "/".join(reversed(names))


def resolve_file(db, user_id, path):
    parent, name = paths.split(path)
    if not name:
        return None

    directory = resolve_directory(db, user_id, parent)
    if directory is None:
        return None

    return db.execute(
        "SELECT * FROM files WHERE directory_id = ? AND name = ?",
        (directory["id"], name),
    ).fetchone()


def list_directory(db, directory_id):
    directories = db.execute(
        "SELECT name FROM directories WHERE parent_id = ? ORDER BY name",
        (directory_id,),
    ).fetchall()
    files = db.execute(
        "SELECT name, size, created_at FROM files WHERE directory_id = ? ORDER BY name",
        (directory_id,),
    ).fetchall()
    return directories, files


def create_directory(db, user_id, parent_id, name):
    cursor = db.execute(
        "INSERT INTO directories (user_id, parent_id, name) VALUES (?, ?, ?)",
        (user_id, parent_id, name),
    )
    return cursor.lastrowid


def is_directory_empty(db, directory_id):
    children = db.execute(
        "SELECT 1 FROM directories WHERE parent_id = ? LIMIT 1", (directory_id,)
    ).fetchone()
    files = db.execute(
        "SELECT 1 FROM files WHERE directory_id = ? LIMIT 1", (directory_id,)
    ).fetchone()
    return children is None and files is None


def delete_directory(db, directory_id):
    db.execute("DELETE FROM acl WHERE node_type = 'dir' AND node_id = ?", (directory_id,))
    db.execute("DELETE FROM directories WHERE id = ?", (directory_id,))


def _insert_blocks(db, file_id, blocks):
    db.executemany(
        "INSERT INTO blocks (id, file_id, sequence, size, checksum) VALUES (?, ?, ?, ?, ?)",
        [
            (block_id, file_id, sequence, block_size, checksum)
            for sequence, (block_id, block_size, checksum) in enumerate(blocks)
        ],
    )


def create_file(db, user_id, directory_id, name, size, blocks, checksum=None):
    """`blocks` es una lista de tuplas (id, tamaño, sha256) en orden."""
    cursor = db.execute(
        "INSERT INTO files (user_id, directory_id, name, size, created_at, checksum)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, directory_id, name, size, now(), checksum),
    )
    file_id = cursor.lastrowid
    _insert_blocks(db, file_id, blocks)
    return file_id


def replace_file(db, file_id, size, blocks, checksum=None):
    """Reemplaza el contenido de un archivo en una sola transacción, conservando su
    id (y por lo tanto sus ACL). Devuelve los ids de los bloques viejos."""
    old = [row["id"] for row in file_blocks(db, file_id)]
    db.execute("DELETE FROM blocks WHERE file_id = ?", (file_id,))
    db.execute(
        "UPDATE files SET size = ?, checksum = ?, created_at = ? WHERE id = ?",
        (size, checksum, now(), file_id),
    )
    _insert_blocks(db, file_id, blocks)
    return old


def file_blocks(db, file_id):
    return db.execute(
        "SELECT id, sequence, size, checksum FROM blocks WHERE file_id = ? ORDER BY sequence",
        (file_id,),
    ).fetchall()


def find_block(db, block_id):
    return db.execute(
        "SELECT blocks.*, files.user_id AS owner_id, files.directory_id,"
        " files.id AS file_id FROM blocks"
        " JOIN files ON files.id = blocks.file_id WHERE blocks.id = ?",
        (block_id,),
    ).fetchone()


def delete_file(db, file_id):
    db.execute("DELETE FROM acl WHERE node_type = 'file' AND node_id = ?", (file_id,))
    db.execute("DELETE FROM blocks WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))


# --- API keys ---------------------------------------------------------------


def create_api_key(db, user_id, name, prefix, key_hash):
    cursor = db.execute(
        "INSERT INTO api_keys (user_id, name, prefix, key_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, name, prefix, key_hash, now()),
    )
    return cursor.lastrowid


def find_api_key(db, key_hash):
    row = db.execute(
        "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    if row is not None:
        db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), row["id"]))
    return row


def list_api_keys(db, user_id):
    return db.execute(
        "SELECT id, name, prefix, created_at, last_used_at FROM api_keys"
        " WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()


def delete_api_key(db, user_id, key_id):
    cursor = db.execute(
        "DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)
    )
    return cursor.rowcount > 0


# --- Grupos -----------------------------------------------------------------


def create_group(db, name, owner_id):
    cursor = db.execute(
        "INSERT INTO groups (name, owner_id) VALUES (?, ?)", (name, owner_id)
    )
    db.execute(
        "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
        (cursor.lastrowid, owner_id),
    )
    return cursor.lastrowid


def find_group(db, name):
    return db.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()


def add_group_member(db, group_id, user_id):
    db.execute(
        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
        (group_id, user_id),
    )


def remove_group_member(db, group_id, user_id):
    db.execute(
        "DELETE FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )


def user_groups(db, user_id):
    return db.execute(
        "SELECT groups.* FROM groups JOIN group_members ON group_members.group_id = groups.id"
        " WHERE group_members.user_id = ? ORDER BY groups.name",
        (user_id,),
    ).fetchall()


def group_members(db, group_id):
    return db.execute(
        "SELECT users.username FROM users JOIN group_members ON group_members.user_id = users.id"
        " WHERE group_members.group_id = ? ORDER BY users.username",
        (group_id,),
    ).fetchall()


# --- ACL --------------------------------------------------------------------


def grant(db, owner_id, node_type, node_id, grantee_type, grantee_id, permission):
    db.execute(
        "INSERT INTO acl (owner_id, node_type, node_id, grantee_type, grantee_id, permission)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (node_type, node_id, grantee_type, grantee_id)"
        " DO UPDATE SET permission = excluded.permission",
        (owner_id, node_type, node_id, grantee_type, grantee_id, permission),
    )


def revoke(db, node_type, node_id, grantee_type, grantee_id):
    cursor = db.execute(
        "DELETE FROM acl WHERE node_type = ? AND node_id = ?"
        " AND grantee_type = ? AND grantee_id = ?",
        (node_type, node_id, grantee_type, grantee_id),
    )
    return cursor.rowcount > 0


def node_acl(db, node_type, node_id):
    return db.execute(
        "SELECT acl.grantee_type, acl.permission,"
        " CASE acl.grantee_type WHEN 'user' THEN users.username ELSE groups.name END AS grantee"
        " FROM acl"
        " LEFT JOIN users ON acl.grantee_type = 'user' AND users.id = acl.grantee_id"
        " LEFT JOIN groups ON acl.grantee_type = 'group' AND groups.id = acl.grantee_id"
        " WHERE acl.node_type = ? AND acl.node_id = ? ORDER BY grantee",
        (node_type, node_id),
    ).fetchall()


def best_permission(db, user_id, directory_ids, file_id=None):
    """Mejor permiso que tiene `user_id` (directo o por grupo) sobre un archivo o
    sobre cualquiera de los directorios dados. Los permisos se heredan hacia abajo."""
    nodes = [("dir", directory_id) for directory_id in directory_ids]
    if file_id is not None:
        nodes.append(("file", file_id))

    clauses = " OR ".join("(node_type = ? AND node_id = ?)" for _ in nodes)
    parameters = [value for node in nodes for value in node]
    rows = db.execute(
        f"SELECT permission FROM acl WHERE ({clauses}) AND ("
        " (grantee_type = 'user' AND grantee_id = ?) OR"
        " (grantee_type = 'group' AND grantee_id IN"
        "   (SELECT group_id FROM group_members WHERE user_id = ?)))",
        parameters + [user_id, user_id],
    ).fetchall()

    permissions = {row["permission"] for row in rows}
    if "rw" in permissions:
        return "rw"
    if "r" in permissions:
        return "r"
    return None


def shared_with(db, user_id):
    return db.execute(
        "SELECT DISTINCT acl.node_type, acl.node_id, acl.permission, users.username AS owner"
        " FROM acl JOIN users ON users.id = acl.owner_id"
        " WHERE (acl.grantee_type = 'user' AND acl.grantee_id = ?) OR"
        " (acl.grantee_type = 'group' AND acl.grantee_id IN"
        "   (SELECT group_id FROM group_members WHERE user_id = ?))",
        (user_id, user_id),
    ).fetchall()


def file_by_id(db, file_id):
    return db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


# --- Locks (leases) -----------------------------------------------------------


def get_lock(db, owner_id, path):
    row = db.execute(
        "SELECT * FROM locks WHERE owner_id = ? AND path = ?", (owner_id, path)
    ).fetchone()
    if row is not None and row["expires_at"] < time.time():
        db.execute("DELETE FROM locks WHERE owner_id = ? AND path = ?", (owner_id, path))
        return None
    return row


def set_lock(db, owner_id, path, holder_id, lock_id, expires_at):
    db.execute(
        "INSERT INTO locks (owner_id, path, holder_id, lock_id, expires_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (owner_id, path) DO UPDATE SET"
        " holder_id = excluded.holder_id, lock_id = excluded.lock_id,"
        " expires_at = excluded.expires_at",
        (owner_id, path, holder_id, lock_id, expires_at),
    )


def delete_lock(db, owner_id, path):
    db.execute("DELETE FROM locks WHERE owner_id = ? AND path = ?", (owner_id, path))
