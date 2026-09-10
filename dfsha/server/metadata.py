import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import paths
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL
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
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    sequence INTEGER NOT NULL,
    size INTEGER NOT NULL,
    UNIQUE (file_id, sequence)
);
"""


@contextmanager
def connect():
    connection = sqlite3.connect(config.DATABASE_PATH)
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


def find_user(db, username):
    return db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


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


def resolve_directory(db, user_id, path):
    current = root_directory(db, user_id)
    for name in paths.normalize(path).split("/"):
        if not name:
            continue
        current = db.execute(
            "SELECT * FROM directories WHERE user_id = ? AND parent_id = ? AND name = ?",
            (user_id, current["id"], name),
        ).fetchone()
        if current is None:
            return None
    return current


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
    db.execute("DELETE FROM directories WHERE id = ?", (directory_id,))


def create_file(db, user_id, directory_id, name, size, block_sizes):
    cursor = db.execute(
        "INSERT INTO files (user_id, directory_id, name, size, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, directory_id, name, size, datetime.now(timezone.utc).isoformat()),
    )
    file_id = cursor.lastrowid
    db.executemany(
        "INSERT INTO blocks (id, file_id, sequence, size) VALUES (?, ?, ?, ?)",
        [
            (block_id, file_id, sequence, block_size)
            for sequence, (block_id, block_size) in enumerate(block_sizes)
        ],
    )
    return file_id


def file_blocks(db, file_id):
    return db.execute(
        "SELECT id, sequence, size FROM blocks WHERE file_id = ? ORDER BY sequence",
        (file_id,),
    ).fetchall()


def find_block(db, user_id, block_id):
    return db.execute(
        "SELECT blocks.* FROM blocks"
        " JOIN files ON files.id = blocks.file_id"
        " WHERE blocks.id = ? AND files.user_id = ?",
        (block_id, user_id),
    ).fetchone()


def delete_file(db, file_id):
    db.execute("DELETE FROM blocks WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
