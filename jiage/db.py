import sqlite3
from .config import get_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cite_key TEXT,
    title TEXT,
    author TEXT,
    filename TEXT NOT NULL,
    page_count INTEGER,
    doc_type TEXT DEFAULT 'born-digital',
    is_primary BOOLEAN DEFAULT 1,
    is_secondary BOOLEAN DEFAULT 0,
    is_reference BOOLEAN DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(filename)
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


def get_conn(collection: str) -> sqlite3.Connection:
    path = get_db_path(collection)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """给旧 DB 补列: lines 表 bbox/block_label (P1); documents 表 source_type (P2)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lines)")}
    if "bbox" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN bbox TEXT")
    if "block_label" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN block_label TEXT")

    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "is_primary" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_primary BOOLEAN DEFAULT 1")
    if "is_secondary" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_secondary BOOLEAN DEFAULT 0")
    if "is_reference" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_reference BOOLEAN DEFAULT 0")


def init_db(collection: str):
    """初始化单个 collection 的 DB（建表 + 迁移）"""
    conn = get_conn(collection)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()
