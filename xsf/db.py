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
    lang TEXT DEFAULT 'zh',
    tri_rows INTEGER DEFAULT NULL,
    page_map TEXT DEFAULT NULL,
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
    page_h INTEGER,
    gloss TEXT,
    trans TEXT,
    translit TEXT,
    draft_flags TEXT
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
    text,
    trans,
    tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    card_id UNINDEXED,
    kind UNINDEXED,
    doc_id UNINDEXED,
    title UNINDEXED,
    text,
    tokenize='trigram'
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
    # 两制转录 (P2, 2026-10-17): 非中文语种 OCR 直接产出拉丁化文本 (text 列),
    # 词对照/流畅译文为可空平行层; documents.lang 驱动工位页单行/三行渲染
    # 设计: mem 小书房/design/完善路线图.md P2 (用户裁定: 拉丁化原文复用 text 列)
    if "gloss" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN gloss TEXT")
    if "trans" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN trans TEXT")
    if "translit" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN translit TEXT")
    # P2c AI 草稿标记 (2026-10-17): JSON {"gloss":"ai",...} — autofill 写入打标,
    # 人工修订该列 (值变) 后自动清除; 未变列的标记跨保存保留
    if "draft_flags" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN draft_flags TEXT")
    doc_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "lang" not in doc_cols2:
        conn.execute("ALTER TABLE documents ADD COLUMN lang TEXT DEFAULT 'zh'")
    if "tri_rows" not in doc_cols2:
        conn.execute("ALTER TABLE documents ADD COLUMN tri_rows INTEGER DEFAULT NULL")
    if "page_map" not in doc_cols2:
        conn.execute("ALTER TABLE documents ADD COLUMN page_map TEXT DEFAULT NULL")
    # 语料包台账 (P3, 2026-10-17): .sqpkg 导出记录 (导出中心数据源)
    # 受控词表 (P4 渐进, 2026-10-18 实体类别先行): kind='entity_category' 等
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vocabs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            term TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(kind, term)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS export_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exported_at TEXT DEFAULT (datetime('now')),
            pkg_type TEXT NOT NULL,
            doc_ids TEXT NOT NULL,
            filename TEXT NOT NULL,
            edits_count INTEGER DEFAULT 0
        )
    """)
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

    # 轻量修订史 (P1, 2026-10-17): 人工文本修订逐块留痕, 依据最终必填
    # (空 basis=待补, 导出变更包时拒绝), 入包勾选供语料包变更通道
    # 设计: mem 小书房/design/完善路线图.md P1
    conn.execute("""
        CREATE TABLE IF NOT EXISTS revision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_num INTEGER NOT NULL,
            block_num INTEGER NOT NULL,
            old_value TEXT,
            new_value TEXT,
            basis TEXT DEFAULT '',
            in_pkg INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_revlog_doc
            ON revision_log(doc_id, created_at)
    """)

    # 卡片资产层 (P4, 2026-10-17): 文献卡片 (元数据/目录/摘要) + 候选队列
    # (目录节点/实体提及), draft→reviewed 生命周期, reviewed 卡进检索路
    # 词表按用户裁定延后——category 为自由标签起步
    # 设计: mem 小书房/design/完善路线图.md P4 + 石渠 20 号稿
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '{}',
            status TEXT DEFAULT 'draft',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT,
            UNIQUE(doc_id, kind)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ctype TEXT NOT NULL,
            label TEXT NOT NULL,
            category TEXT,
            confidence REAL,
            page_num INTEGER,
            block_num INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cards_doc
            ON doc_cards(doc_id, kind)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cand_doc
            ON card_candidates(doc_id, status)
    """)

    # ── 知识地图 Wiki (P6, 2026-10-18): collection 级编译产物 ──
    # 模型: 编译产物——LLM 一键生成, 无人审核, 可再生(清旧插新), 不入修订史。
    # stale 判定: wiki_status.base_edits < 当前 revision_log 总数 → 前端提示重新生成。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            slug TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            refs TEXT,
            position INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(collection, slug)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_status (
            collection TEXT PRIMARY KEY,
            status TEXT DEFAULT 'none',
            base_edits INTEGER DEFAULT 0,
            generated_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wiki_coll
            ON wiki_pages(collection, position)
    """)

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

    # ── 实体图层 (P8, 2026-10-18): 书架级编译产物 (ontology RAG 底座) ──
    # 原则: 文献是事实源(资产层), 实体/提及/关系是编译层(可再生, 不入修订史, 不进语料包)。
    # entities=登记簿(归一后的实体), entity_mentions=签到簿(文献中的出现+锚点),
    # entity_links=关系簿(M1 空表定型/M2 填数据), entity_additions=人工 patch 层(重编不丢)。
    # ext_ids 开放字典: {"cbdb":12345,...} —— 跨书架确定性合并的钥匙 (namespace 可插拔)。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical TEXT NOT NULL,
            category TEXT,
            aliases TEXT DEFAULT '[]',
            ext_ids TEXT DEFAULT '{}',
            props TEXT DEFAULT '{}',
            origin TEXT DEFAULT 'ai',
            compiled_at TEXT,
            UNIQUE(canonical)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            page_num INTEGER,
            block_num INTEGER,
            surface TEXT,
            valid INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(entity_id, doc_id, page_num, block_num, surface)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            rel TEXT,
            evidence TEXT,
            origin TEXT DEFAULT 'ai',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(src_id, dst_id, rel)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_additions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            evidence TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_status (
            collection TEXT PRIMARY KEY,
            status TEXT DEFAULT 'none',
            base_edits INTEGER DEFAULT 0,
            generated_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mentions_entity
            ON entity_mentions(entity_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mentions_doc
            ON entity_mentions(doc_id)
    """)
    # 检索未命中轨迹 (C1): 零命中查询沉淀 → 人工提炼为 alias/constraint 补丁。
    # 实体错误反馈 (C4): 消费侧(检索/引文核对/人工)发现归一错误 → 报告 → 巡查处置。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER,
            kind TEXT DEFAULT 'other',
            note TEXT,
            doc_id INTEGER,
            page_num INTEGER,
            mention_id INTEGER,
            source TEXT DEFAULT 'web',
            resolved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_misses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL UNIQUE,
            hits INTEGER DEFAULT 1,
            source TEXT DEFAULT 'web',
            note TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── 检索效用度量 (帮忙率) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            total_docs INTEGER NOT NULL DEFAULT 0,
            entity_hits INTEGER NOT NULL DEFAULT 0,
            wiki_hits INTEGER NOT NULL DEFAULT 0,
            card_hits INTEGER NOT NULL DEFAULT 0,
            expanded INTEGER NOT NULL DEFAULT 0,
            clicked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_events_time "
                 "ON search_events(created_at)")

    # ── blocks_fts trigram 化 (2026-10-19): unicode61 整串 CJK 单 token,
    # 子串命不中 → 重建为 trigram (≥3 字子串匹配)。
    # 2026-10-19 二改: 加 trans 列 (多语种译文进索引, 中文可搜外文文献);
    # 检测条件: 无 trigram 或无 trans 列均触发重建。一次性, 幂等。──
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='blocks_fts'").fetchone()
        if row and ('trigram' not in (row["sql"] or "")
                    or 'trans' not in (row["sql"] or '')):
            conn.execute("DROP TABLE IF EXISTS blocks_fts_old")
            conn.execute("ALTER TABLE blocks_fts RENAME TO blocks_fts_old")
            conn.execute("""
                CREATE VIRTUAL TABLE blocks_fts USING fts5(
                    doc_id UNINDEXED,
                    page_num UNINDEXED,
                    block_num UNINDEXED,
                    text,
                    trans,
                    tokenize='trigram'
                )
            """)
            # 回填: 行级 trans 拼接进块级索引 (多行块 group_concat)
            conn.execute(
                "INSERT INTO blocks_fts(doc_id, page_num, block_num, text, trans) "
                "SELECT b.doc_id, b.page_num, b.block_num, b.text, "
                "  (SELECT group_concat(t, ' ') FROM ("
                "     SELECT l.trans AS t FROM lines l "
                "     WHERE l.doc_id = b.doc_id AND l.page_num = b.page_num "
                "       AND l.block_num = b.block_num "
                "       AND l.trans IS NOT NULL AND l.trans != '')) "
                "FROM blocks_fts_old b")
            conn.execute("DROP TABLE blocks_fts_old")
    except Exception:
        pass   # 重建失败保持旧表 (检索仍可用, 只是子串受限)

    # ── 卡片 FTS 回填 (2026-10-19): reviewed 的 toc/entity 卡进 cards_fts ──
    # 幂等: 仅当 cards_fts 为空且 doc_cards 有 reviewed 卡时回填一次;
    # 之后由 api 的 _card_fts_sync 在保存/状态切换/采纳时维护。
    try:
        n_fts = conn.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0]
        if n_fts == 0:
            rows = conn.execute(
                "SELECT dc.id, dc.doc_id, dc.kind, dc.content, d.title "
                "FROM doc_cards dc JOIN documents d ON d.id = dc.doc_id "
                "WHERE dc.status = 'reviewed' "
                "  AND dc.kind IN ('toc', 'entity')").fetchall()
            for r in rows:
                flat = _card_flat_text(r["kind"], r["content"])
                if flat:
                    conn.execute(
                        "INSERT INTO cards_fts(card_id, kind, doc_id, title, text) "
                        "VALUES(?,?,?,?,?)",
                        (r["id"], r["kind"], r["doc_id"],
                         r["title"] or "", flat))
    except Exception:
        pass   # 旧库无表/结构差异 → 跳过, _card_hits 有空兜底


def _card_flat_text(kind: str, content_json: str) -> str:
    """卡片内容扁平化为可检索文本 (只取人读字段, 不含 JSON 键名).

    toc: 递归 tree/nodes 取 label+category; entity: items 取 label+category。
    """
    import json as _json
    try:
        c = _json.loads(content_json or '{}') if content_json else {}
    except (ValueError, TypeError):
        return ''
    parts: list[str] = []

    if kind == 'toc':
        def _walk(nodes):
            for n in nodes if isinstance(nodes, list) else []:
                if not isinstance(n, dict):
                    continue
                lbl = str(n.get('label') or '').strip()
                if lbl:
                    parts.append(lbl)
                cat = str(n.get('category') or '').strip()
                if cat:
                    parts.append(cat)
                _walk(n.get('children') or [])
        _walk(c.get('tree') or c.get('nodes') or [])
    elif kind == 'entity':
        for it in c.get('items') or []:
            if not isinstance(it, dict):
                continue
            lbl = str(it.get('label') or '').strip()
            if lbl:
                parts.append(lbl)
            cat = str(it.get('category') or '').strip()
            if cat:
                parts.append(cat)
    return ' '.join(parts)


def init_db(collection: str):
    """初始化单个 collection 的 DB（建表 + 迁移）"""
    get_db_path(collection).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn(collection, create=True)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()
