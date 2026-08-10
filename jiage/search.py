import jieba
from .db import get_conn


def _fts_query(query: str) -> str:
    """将搜索词转为 FTS5 MATCH 表达式"""
    tokens = [t for t in jieba.cut_for_search(query) if t.strip()]
    if not tokens:
        return ''
    return ' AND '.join(f'"{t}"' for t in tokens)


def search(query: str, collection: str = None,
           limit: int = 20) -> list[dict]:
    """全文搜索，返回匹配块列表"""
    fts_q = _fts_query(query)
    if not fts_q:
        return []

    conn = get_conn()
    try:
        if collection:
            rows = conn.execute(
                '''SELECT f.doc_id, f.page_num, f.block_num,
                          d.collection, d.filename, d.title, d.cite_key
                   FROM blocks_fts f
                   JOIN documents d ON d.id = f.doc_id
                   WHERE blocks_fts MATCH ? AND d.collection = ?
                   ORDER BY rank
                   LIMIT ?''',
                (fts_q, collection, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT f.doc_id, f.page_num, f.block_num,
                          d.collection, d.filename, d.title, d.cite_key
                   FROM blocks_fts f
                   JOIN documents d ON d.id = f.doc_id
                   WHERE blocks_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?''',
                (fts_q, limit)
            ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def get_block_lines(doc_id: int, page_num: int, block_num: int) -> list[str]:
    """取某块的原始文本行"""
    conn = get_conn()
    try:
        rows = conn.execute(
            '''SELECT text FROM lines
               WHERE doc_id = ? AND page_num = ? AND block_num = ?
               ORDER BY line_num''',
            (doc_id, page_num, block_num)
        ).fetchall()
    finally:
        conn.close()
    return [r['text'] for r in rows]


def get_context(doc_id: int, page_num: int, block_num: int,
                radius: int = 1) -> list[dict]:
    """取命中块周围 ±radius 个块的全部行"""
    conn = get_conn()
    try:
        rows = conn.execute(
            '''SELECT block_num, line_num, text FROM lines
               WHERE doc_id = ? AND page_num = ?
                 AND block_num BETWEEN ? AND ?
               ORDER BY block_num, line_num''',
            (doc_id, page_num,
             block_num - radius, block_num + radius)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
