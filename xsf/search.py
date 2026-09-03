from itertools import product

import jieba
from opencc import OpenCC

from .db import get_conn

_s2t = OpenCC('s2t')
_t2s = OpenCC('t2s')


def _char_variants(ch: str) -> list[str]:
    """单字的所有繁简变体。"""
    return list({ch, _s2t.convert(ch), _t2s.convert(ch)})


def _all_variants(token: str) -> set[str]:
    """字符级笛卡尔积：生成所有可能的繁简混合形式。"""
    char_options = [_char_variants(ch) for ch in token]
    return {''.join(combo) for combo in product(*char_options) if ''.join(combo)}


def _expand_token(token: str) -> str:
    """繁简互转扩展：返回覆盖所有繁简混合形式的 FTS5 OR 表达式。"""
    variants = _all_variants(token)
    if len(variants) <= 1:
        return f'"{token}"'
    return '(' + ' OR '.join(f'"{v}"' for v in sorted(variants)) + ')'


def _fts_query(query: str) -> str:
    """将搜索词转为 FTS5 MATCH 表达式（繁简互转）"""
    tokens = [t for t in jieba.cut_for_search(query) if t.strip()]
    if not tokens:
        return ''
    return ' AND '.join(_expand_token(t) for t in tokens)


def get_highlight_terms(query: str) -> list[str]:
    """生成用于前端高亮的所有繁简变体（按长度降序）。"""
    tokens = [t for t in jieba.cut_for_search(query) if t.strip()]
    if not tokens:
        return []
    all_terms: set[str] = set()
    for token in tokens:
        all_terms |= _all_variants(token)
    return sorted(all_terms, key=len, reverse=True)


def _search_filters(source_type: str, doc_id: int) -> tuple[str, list]:
    """构造 source_type / doc_id 过滤 (WHERE 追加段 + 参数)."""
    extra_where = ''
    params = []
    valid_types = {'primary', 'secondary', 'reference'}
    if source_type and source_type in valid_types:
        extra_where += f' AND d.is_{source_type} = 1'
    if doc_id is not None:
        extra_where += ' AND f.doc_id = ?'
        params.append(doc_id)
    return extra_where, params


def search(query: str, collection: str,
           limit: int = 20, offset: int = 0,
           source_type: str = None, doc_id: int = None) -> list[dict]:
    """全文搜索，返回匹配块列表 (rank 序, 分页).

    source_type: 'primary'/'secondary'/'reference' 之一，用于按来源类型过滤。
    doc_id: 给定时只搜该文档 (用于分组视图展开单篇)。
    """
    fts_q = _fts_query(query)
    if not fts_q:
        return []

    extra_where, filter_params = _search_filters(source_type, doc_id)

    conn = get_conn(collection)
    try:
        rows = conn.execute(
            f'''SELECT f.doc_id, f.page_num, f.block_num,
                      d.filename, d.title, d.cite_key
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE blocks_fts MATCH ?{extra_where}
                ORDER BY rank
                LIMIT ? OFFSET ?''',
            [fts_q] + filter_params + [limit, max(0, offset)]
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def count_hits(query: str, collection: str,
               source_type: str = None, doc_id: int = None) -> dict:
    """统计命中规模: 匹配块总数 + 涉及文献数."""
    fts_q = _fts_query(query)
    if not fts_q:
        return {"blocks": 0, "docs": 0}

    extra_where, filter_params = _search_filters(source_type, doc_id)

    conn = get_conn(collection)
    try:
        row = conn.execute(
            f'''SELECT COUNT(*) AS blocks, COUNT(DISTINCT f.doc_id) AS docs
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE blocks_fts MATCH ?{extra_where}''',
            [fts_q] + filter_params
        ).fetchone()
    finally:
        conn.close()
    return {"blocks": row["blocks"], "docs": row["docs"]}


def search_grouped(query: str, collection: str,
                   source_type: str = None, limit: int = 200) -> list[dict]:
    """按文档聚合命中: 每篇的命中块数 + 最佳 rank.

    排序: 命中块数降序, 同数按最佳 rank. 供搜索页分组视图.
    """
    fts_q = _fts_query(query)
    if not fts_q:
        return []

    extra_where, filter_params = _search_filters(source_type, None)

    conn = get_conn(collection)
    try:
        rows = conn.execute(
            f'''SELECT f.doc_id, d.filename, d.title, d.cite_key,
                       COUNT(*) AS hit_count, MIN(f.rank) AS best_rank
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE blocks_fts MATCH ?{extra_where}
                GROUP BY f.doc_id
                ORDER BY hit_count DESC, best_rank ASC
                LIMIT ?''',
            [fts_q] + filter_params + [limit]
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
