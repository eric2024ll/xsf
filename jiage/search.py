import jieba
from opencc import OpenCC

from .db import get_conn

_s2t = OpenCC('s2t')
_t2s = OpenCC('t2s')


def _expand_token(token: str) -> str:
    """繁简互转扩展：返回覆盖繁/简的 FTS5 OR 表达式。"""
    variants = {token, _s2t.convert(token), _t2s.convert(token)}
    variants = {v for v in variants if v}
    if len(variants) == 1:
        return f'"{token}"'
    return '(' + ' OR '.join(f'"{v}"' for v in sorted(variants)) + ')'


def _fts_query(query: str) -> str:
    """将搜索词转为 FTS5 MATCH 表达式（繁简互转）"""
    tokens = [t for t in jieba.cut_for_search(query) if t.strip()]
    if not tokens:
        return ''
    return ' AND '.join(_expand_token(t) for t in tokens)


def search(query: str, collection: str,
           limit: int = 20,
           source_type: str = None) -> list[dict]:
    """全文搜索，返回匹配块列表。

    source_type: 'primary'/'secondary'/'reference' 之一，用于按来源类型过滤。
    """
    fts_q = _fts_query(query)
    if not fts_q:
        return []

    valid_types = {'primary', 'secondary', 'reference'}
    extra_where = ''
    params = [fts_q]

    if source_type and source_type in valid_types:
        extra_where = f' AND d.is_{source_type} = 1'

    conn = get_conn(collection)
    try:
        rows = conn.execute(
            f'''SELECT f.doc_id, f.page_num, f.block_num,
                      d.filename, d.title, d.cite_key
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE blocks_fts MATCH ?{extra_where}
                ORDER BY rank
                LIMIT ?''',
            params + [limit]
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def get_block_lines(doc_id: int, page_num: int, block_num: int,
                    collection: str) -> list[str]:
    """取某块的原始文本行"""
    conn = get_conn(collection)
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
                radius: int = 1, collection: str = None) -> list[dict]:
    """取命中块周围 ±radius 个块的全部行"""
    conn = get_conn(collection)
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
