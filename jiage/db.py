import sqlite3
from .config import get_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    cite_key TEXT,
    title TEXT,
    author TEXT,
    filename TEXT NOT NULL,
    page_count INTEGER,
    doc_type TEXT DEFAULT 'born-digital',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(collection, filename)
);

CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_num INTEGER NOT NULL,
    block_num INTEGER NOT NULL,
    line_num INTEGER NOT NULL,
    text TEXT NOT NULL,
    bbox TEXT,
    block_label TEXT
);

CREATE INDEX IF NOT EXISTS idx_lines_doc_page
    ON lines(doc_id, page_num);
CREATE INDEX IF NOT EXISTS idx_lines_doc_block
    ON lines(doc_id, page_num, block_num);

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    doc_id UNINDEXED,
    page_num UNINDEXED,
    block_num UNINDEXED,
    text
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """给旧 DB 的 lines 表补 bbox/block_label 列 (P1 OCR)。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lines)")}
    if "bbox" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN bbox TEXT")
    if "block_label" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN block_label TEXT")


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()
