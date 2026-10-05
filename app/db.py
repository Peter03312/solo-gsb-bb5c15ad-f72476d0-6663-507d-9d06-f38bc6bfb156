"""SQLite 数据访问层：连接管理与建表初始化。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "network.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    node       TEXT NOT NULL,
    max_supply REAL NOT NULL CHECK (max_supply >= 0)
);

CREATE TABLE IF NOT EXISTS incompatible_pairs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_a INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_b INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    CHECK (source_a <> source_b),
    UNIQUE (source_a, source_b)
);

CREATE TABLE IF NOT EXISTS pipes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    capacity  REAL NOT NULL CHECK (capacity >= 0)
);

CREATE TABLE IF NOT EXISTS valves (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pipe_id INTEGER NOT NULL UNIQUE REFERENCES pipes(id) ON DELETE CASCADE,
    open    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS demands (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL DEFAULT '',
    node     TEXT NOT NULL,
    demand   REAL NOT NULL CHECK (demand >= 0),
    priority INTEGER NOT NULL DEFAULT 0
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", str(_DEFAULT_DB))


def init_db() -> None:
    """建库建表（幂等），应用启动时自动调用。"""
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    """每次调用开启一个新连接；正常退出自动提交，异常自动回滚。"""
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
