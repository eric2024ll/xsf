import json
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
    source_tags TEXT DEFAULT '["primary"]',
    bib_type TEXT DEFAULT NULL,
    bib_data TEXT DEFAULT NULL,
    linked_pdf TEXT DEFAULT NULL,
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
    block_label TEXT,
    page_w INTEGER,
    page_h INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lines_doc_page
    ON lines(doc_id, page_num);
CREATE INDEX IF NOT EXISTS idx_lines_doc_block
    ON lines(doc_id, page_num, block_num);

CREATE TABLE IF NOT EXISTS ocr_page_state (
    doc_id INTEGER NOT NULL,
    page_num INTEGER NOT NULL,
    status TEXT NOT NULL,          -- done | error
    error TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (doc_id, page_num)
);

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    doc_id UNINDEXED,
    page_num UNINDEXED,
    block_num UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS page_regions (
    doc_id INTEGER NOT NULL,
    page_num INTEGER NOT NULL,
    region_idx INTEGER NOT NULL,          -- 阅读顺序 (1 起)
    bbox TEXT NOT NULL,                   -- [x0,y0,x1,y1] 150dpi 坐标空间
    direction TEXT NOT NULL DEFAULT 'h',  -- h 横排 | v_rtl 竖右起 | v_ltr 竖左起
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (doc_id, page_num, region_idx)
);
"""


def get_conn(collection: str, create: bool = False) -> sqlite3.Connection:
    """打开书架 DB. 默认 mode=rw 禁止 sqlite 静默创建新文件:
    查询已删除书架时应报错, 而不是悄悄造出一个空壳 xsf.db 让书架「复活」.
    create=True 仅用于 init_db 建库."""
    path = get_db_path(collection)
    mode = 'rwc' if create else 'rw'
    if mode == 'rw':
        if not path.exists():
            raise sqlite3.OperationalError(f'书架 DB 不存在: {path}')
        if path.stat().st_size == 0:
            raise sqlite3.OperationalError(f'书架 DB 为空壳 (0 字节): {path}')
    conn = sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True, timeout=30)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """给旧 DB 补列: lines 表 bbox/block_label (P1); documents 表 source_type (P2);
    lines.suspect + ocr_page_state (P3, 2026-09-06)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lines)")}
    if "bbox" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN bbox TEXT")
    if "block_label" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN block_label TEXT")
    if "page_w" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN page_w INTEGER")
    if "page_h" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN page_h INTEGER")
    if "suspect" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN suspect TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_page_state (
            doc_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (doc_id, page_num)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_regions (
            doc_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            region_idx INTEGER NOT NULL,
            bbox TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'h',
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (doc_id, page_num, region_idx)
        )
    """)

    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "is_primary" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_primary BOOLEAN DEFAULT 1")
    if "is_secondary" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_secondary BOOLEAN DEFAULT 0")
    if "is_reference" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN is_reference BOOLEAN DEFAULT 0")

    if "source_tags" not in doc_cols:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN source_tags TEXT DEFAULT '[\"primary\"]'"
        )
        rows = conn.execute(
            "SELECT id, is_primary, is_secondary, is_reference FROM documents"
        ).fetchall()
        for r in rows:
            tags = []
            if r["is_primary"]:
                tags.append("primary")
            if r["is_secondary"]:
                tags.append("secondary")
            if r["is_reference"]:
                tags.append("reference")
            if not tags:
                tags = ["primary"]
            conn.execute(
                "UPDATE documents SET source_tags = ? WHERE id = ?",
                (json.dumps(tags), r["id"]),
            )

    if "bib_type" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN bib_type TEXT DEFAULT NULL")
    if "bib_data" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN bib_data TEXT DEFAULT NULL")
    if "linked_pdf" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN linked_pdf TEXT DEFAULT NULL")

    # 智能校对 (P4, 2026-09-19): 候选缓存 + 人工录入 + 页映射
    # 设计: pqa design/client/19-smart-proofread.md
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ocr_candidates (
            id INTEGER PRIMARY KEY,
            doc_id INTEGER NOT NULL,
            page_num INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model_ver TEXT,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(doc_id, page_num, provider)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_transcripts (
            id INTEGER PRIMARY KEY,
            doc_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcript_pages (
            id INTEGER PRIMARY KEY,
            transcript_id INTEGER NOT NULL REFERENCES manual_transcripts(id),
            page_num INTEGER NOT NULL,
            char_start INTEGER,
            char_end INTEGER,
            confidence REAL,
            confirmed INTEGER DEFAULT 0,
            UNIQUE(transcript_id, page_num)
        )
    """)


def init_db(collection: str):
    """初始化单个 collection 的 DB（建表 + 迁移）"""
    get_db_path(collection).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn(collection, create=True)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()
