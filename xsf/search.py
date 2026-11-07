import json
import sqlite3
from itertools import product
from pathlib import Path

import jieba
from opencc import OpenCC

from .db import get_conn

_s2t = OpenCC('s2t')
_t2s = OpenCC('t2s')


# ── B1: OCR 形近混淆变体表 (2026-11-07) ─────────────────
# 数据文件 xsf/data/ocr_confusions.tsv (带 provenance, 人工审核增删);
# 双向 map: 查正字命中错字语料, 反之亦然。扩展策略 = 对繁简变体做
# 「单位置替换」(线性量, 不做笛卡尔积防爆表)。
def _load_confusions() -> dict[str, set[str]]:
    path = Path(__file__).parent / 'data' / 'ocr_confusions.tsv'
    m: dict[str, set[str]] = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            a, b = parts[0].strip(), parts[1].strip()
            if len(a) != 1 or len(b) != 1 or a == b:
                continue
            m.setdefault(a, set()).add(b)
            m.setdefault(b, set()).add(a)
    except OSError:
        pass
    return m


_CONFUSIONS = _load_confusions()


def _char_variants(ch: str) -> list[str]:
    """单字的所有繁简变体。"""
    return list({ch, _s2t.convert(ch), _t2s.convert(ch)})


def _all_variants(token: str) -> set[str]:
    """字符级笛卡尔积：生成所有可能的繁简混合形式。"""
    char_options = [_char_variants(ch) for ch in token]
    return {''.join(combo) for combo in product(*char_options) if ''.join(combo)}


def _confusion_variants(token: str, base: list[str]) -> list[str]:
    """B1: 对 base (繁简变体集) 做单位置混淆替换, 去重保序, 不含 base 已有项。"""
    if not _CONFUSIONS:
        return []
    out: list[str] = []
    seen = set(base)
    for v in base:
        for i, ch in enumerate(v):
            for alt in _CONFUSIONS.get(ch, ()):
                cand = v[:i] + alt + v[i + 1:]
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
    return out


def _expand_token(token: str) -> str:
    """繁简互转扩展：返回覆盖所有繁简混合形式的 FTS5 OR 表达式。"""
    variants = _all_variants(token)
    if len(variants) <= 1:
        return f'"{token}"'
    return '(' + ' OR '.join(f'"{v}"' for v in sorted(variants)) + ')'


def _fts_query(query: str) -> str:
    """将搜索词转为 FTS5 MATCH 表达式（繁简互转）— 仅 ≥3 字词段 (trigram)."""
    match_expr, _ = _fts_plan(query)
    return match_expr or ''


def _norm_name(s: str) -> str:
    return ''.join((s or '').split())


def _parse_aliases(raw) -> list[str]:
    """aliases 列 (JSON 数组字符串) → 名称列表。"""
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _entity_expansions(conn, query: str, limit: int = 8) -> dict:
    """整条查询命中实体 (规范名/别名) 时 → {查询词: [该实体其它名称]}。

    用于检索前归一扩展 (异体字/别称/音译变体), 救 miss。
    B2 (2026-11-07): 改查 entity_names 拆表 + 变体组 (繁体查询不再漏)。
    表缺失时返回 {}。
    """
    qn = _norm_name(query)
    if len(qn) < 2:
        return {}
    try:
        where, params = like_variant_groups(qn, ['n.name'])
        if not where:
            return {}
        rows = conn.execute(
            f"SELECT entity_id FROM entity_names n WHERE {where} LIMIT ?",
            params + [limit]).fetchall()
    except sqlite3.OperationalError:
        return {}
    names = []
    for r in rows:
        ent = conn.execute(
            "SELECT canonical, aliases FROM entities WHERE id = ?",
            (r["entity_id"],)).fetchone()
        if not ent:
            continue
        for n in [ent["canonical"]] + _parse_aliases(ent["aliases"]):
            nn = (n or '').strip()
            if nn and _norm_name(nn) != qn and nn not in names:
                names.append(nn)
    return {query: names} if names else {}


# FTS5 MATCH 语法字符: 用户查询里出现会破坏短语引号结构 (如 a"b →
# unterminated string), 一律剔除 — 检索语义不受影响 (这些字符不参与词形)
_FTS_STRIP = set('"()*:^{}[]')


def _query_tokens(query: str) -> list[str]:
    """查询分词：CJK 连续段 jieba 切词，非 CJK 连续段整段保留。

    jieba 会把西里尔查询切成单字（全部 <3 字被丢弃, 只剩 LIKE 兜底且
    匹配不上）——非 CJK 段必须整体作为一个 token (2026-10-26 修)。
    查询先剔除 FTS 语法字符 (批 A1, 2026-11-05): 非词形字符不参与检索。
    """
    from .ingest import _CJK_SEG
    query = ''.join(ch for ch in query if ch not in _FTS_STRIP)
    tokens = []
    for i, p in enumerate(_CJK_SEG.split(query)):
        if not p.strip():
            continue
        if i % 2 == 1:
            tokens.extend(t for t in jieba.cut_for_search(p) if t.strip())
        else:
            tokens.append(p)
    return tokens


def _fts_plan_plain(query: str) -> tuple[str, list[str]]:
    """trigram 检索计划 (无实体扩展): (MATCH 表达式 or '', 短词 LIKE 兜底变体表).

    blocks_fts 为 trigram tokenizer: ≥3 字段走 FTS 短语 (bm25);
    1-2 字段 FTS 命不中, 收集繁简变体交由 LIKE 过滤。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return '', []
    parts, like_groups = [], []
    for t in tokens:
        variants = sorted(_all_variants(t))
        if len(variants) > 32:
            variants = [t]
        # B1: 词内含混淆表字符才追加单位置替换变体 (无混淆字符的行为零变化)
        if _CONFUSIONS:
            conf = _confusion_variants(t, variants)
            if conf:
                variants = variants + conf[:max(0, 48 - len(variants))]
        if len(t) >= 3:
            parts.append('(' + ' OR '.join(f'"{v}"' for v in variants) + ')')
        else:
            like_groups.append(variants)
    match_expr = ' AND '.join(parts)
    return match_expr, like_groups


def _fts_plan(query: str, expansions: dict = None) -> tuple[str, list[str]]:
    """检索计划。expansions ({词: [实体其它名称]}) 非空时按「名称原子」扩展:

    以整条查询命中实体为前提, 把「查询词 + 该实体全部名称」各作为一个短语,
    整体 OR —— 避免把别名拆成子词后被 AND/LIKE 组合误杀。
    仅取 ≥3 字的名称 (trigram 可匹配)。
    """
    names = [query]
    for vs in (expansions or {}).values():
        names.extend(vs)
    names = [n.strip() for n in names if n and n.strip()]
    if expansions and len(names) > 1:
        parts = []
        for n in names:
            vs = [v for v in sorted(_all_variants(n)) if len(v) >= 3]
            if vs:
                parts.append('(' + ' OR '.join(f'"{v}"' for v in vs) + ')')
        if parts:
            return ' OR '.join(parts), []
    return _fts_plan_plain(query)


def _fts_where(match_expr: str, like_groups: list[list[str]],
               alias: str = 'f') -> tuple[str, list]:
    """构造 WHERE 片段 + 参数 (MATCH 与 LIKE 组合; 两者可只有其一).

    like_groups: 每个 token 一组繁简变体——组内 OR (命中任一变体即可),
    组间 AND (多词须都命中)。此前变体展平后彼此 AND, 繁体变体查不到
    会杀掉整条查询 (2026-10-26 修, 暴露于俄文译文检索)。
    """
    conds, params = [], []
    if match_expr:
        conds.append(f'blocks_fts MATCH ?')
        params.append(match_expr)
    if like_groups:
        groups = []
        for variants in like_groups:
            esc = [t.replace('\\', '\\\\').replace('%', r'\%')
                   .replace('_', r'\_') for t in variants]
            groups.append('(' + ' OR '.join(
                f"({alias}.text LIKE ? ESCAPE '\\' OR "
                f"{alias}.trans LIKE ? ESCAPE '\\')" for _ in esc) + ')')
            params.extend(v for e in esc for v in (f'%{e}%', f'%{e}%'))
        conds.append(' AND '.join(groups))
    return ' AND '.join(conds), params


def like_variant_groups(query: str, col_exprs: list[str]) -> tuple[str, list]:
    """A3/B2b 共用: 分词 + 繁简/混淆变体的 LIKE 条件 (组内 OR 组间 AND).

    col_exprs: 参与 LIKE 的列表达式 (如 ['a.note', 'a.clip_text'])。
    返回 (where, params); 空查询返回 ('', [])。语义与 _fts_where 的
    LIKE 路完全同款 (2026-10-26 修复的繁简误杀在此继承)。
    """
    q = (query or '').strip()
    tokens = _query_tokens(q) or ([q] if q else [])
    conds, params = [], []
    ncol = max(1, len(col_exprs))
    for t in tokens:
        variants = sorted(_all_variants(t))
        if len(variants) > 32:
            variants = [t]
        if _CONFUSIONS:
            conf = _confusion_variants(t, variants)
            if conf:
                variants = variants + conf[:max(0, 48 - len(variants))]
        esc = [v.replace('\\', '\\\\').replace('%', r'\%')
               .replace('_', r'\_') for v in variants]
        grp = ' OR '.join(
            '(' + ' OR '.join(f"{c} LIKE ? ESCAPE '\\'" for c in col_exprs) + ')'
            for _ in esc)
        conds.append(f'({grp})')
        params.extend(f'%{e}%' for e in esc for _ in range(ncol))
    return ' AND '.join(conds), params


def get_highlight_terms(query: str) -> list[str]:
    """生成用于前端高亮的所有繁简/混淆变体（按长度降序）。

    A3+ 起与检索主路同源: 高亮词表 = 检索词表, 错字形态命中的块也能标出。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return []
    all_terms: set[str] = set()
    for token in tokens:
        variants = sorted(_all_variants(token))
        all_terms |= set(variants)
        if _CONFUSIONS:
            all_terms |= set(_confusion_variants(token, variants))
    return sorted(all_terms, key=len, reverse=True)


def _search_filters(source_type: str, doc_id: int, label: str = None,
                    exclude: str = None, year_from=None, year_to=None
                    ) -> tuple[str, list]:
    """构造 source_type / doc_id / 块标签 / 年份过滤 (WHERE 追加段 + 参数).

    label:   逗号分隔白名单 — 只保留这些 block_label 的块 (如 text,vertical_text)
    exclude: 逗号分隔黑名单 — 剔除这些标签 (默认 UI 传 header,page_number,footer,number)
             块标签可能为 NULL (无结构化标签的入库路): NULL 视为非噪声, 不被剔除
    标签过滤走 EXISTS 探 lines (blocks_fts 不存 label), 每命中块一次索引探查
    """
    extra_where = ''
    params = []
    valid_types = {'primary', 'secondary', 'reference'}
    if source_type and source_type in valid_types:
        extra_where += f' AND d.is_{source_type} = 1'
    if doc_id is not None:
        extra_where += ' AND f.doc_id = ?'
        params.append(doc_id)
    labels = None
    if label:
        labels = [s.strip() for s in label.split(',') if s.strip()]
    elif exclude:
        labels = [s.strip() for s in exclude.split(',') if s.strip()]
    if labels:
        ph = ','.join('?' * len(labels))
        if label:
            extra_where += (f' AND EXISTS (SELECT 1 FROM lines l '
                            f'WHERE l.doc_id = f.doc_id '
                            f'AND l.page_num = f.page_num '
                            f'AND l.block_num = f.block_num '
                            f'AND l.block_label IN ({ph}))')
        else:
            extra_where += (f' AND EXISTS (SELECT 1 FROM lines l '
                            f'WHERE l.doc_id = f.doc_id '
                            f'AND l.page_num = f.page_num '
                            f'AND l.block_num = f.block_num '
                            f'AND (l.block_label IS NULL '
                            f'OR l.block_label NOT IN ({ph})))')
        params.extend(labels)
    # 年份: bib_data.$.date 前 4 位; 未填日期的文献不命中 (显式选用, 不做默认)
    if year_from is not None or year_to is not None:
        yexpr = "CAST(substr(json_extract(d.bib_data,'$.date'),1,4) AS INTEGER)"
        if year_from is not None:
            extra_where += f' AND {yexpr} >= ?'
            params.append(int(year_from))
        if year_to is not None:
            extra_where += f' AND {yexpr} <= ?'
            params.append(int(year_to))
    return extra_where, params


def search(query: str, collection: str,
           limit: int = 20, offset: int = 0,
           source_type: str = None, doc_id: int = None,
           label: str = None, exclude: str = None,
           year_from: int = None, year_to: int = None) -> list[dict]:
    """全文搜索，返回匹配块列表 (rank 序, 分页).

    source_type: 'primary'/'secondary'/'reference' 之一，用于按来源类型过滤。
    doc_id: 给定时只搜该文档 (用于分组视图展开单篇)。
    label/exclude: 块标签白/黑名单 (逗号分隔), 批 A2 (2026-11-05)。
    year_from/year_to: bib_data 年份范围 (未填日期文献不命中)。
    """
    conn = get_conn(collection)
    try:
        match_expr, like_terms = _fts_plan(query, _entity_expansions(conn, query))
        if not match_expr and not like_terms:
            return []

        extra_where, filter_params = _search_filters(
            source_type, doc_id, label, exclude, year_from, year_to)
        fts_where, fts_params = _fts_where(match_expr, like_terms)

        rows = conn.execute(
            f'''SELECT f.doc_id, f.page_num, f.block_num,
                      d.filename, d.title, d.cite_key
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE {fts_where}{extra_where}
                {'ORDER BY rank' if match_expr else 'ORDER BY f.rowid'}
                LIMIT ? OFFSET ?''',
            fts_params + filter_params + [limit, max(0, offset)]
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def count_hits(query: str, collection: str,
               source_type: str = None, doc_id: int = None,
               label: str = None, exclude: str = None,
               year_from: int = None, year_to: int = None) -> dict:
    """统计命中规模: 匹配块总数 + 涉及文献数."""
    conn = get_conn(collection)
    try:
        match_expr, like_terms = _fts_plan(query, _entity_expansions(conn, query))
        if not match_expr and not like_terms:
            return {"blocks": 0, "docs": 0}

        extra_where, filter_params = _search_filters(
            source_type, doc_id, label, exclude, year_from, year_to)
        fts_where, fts_params = _fts_where(match_expr, like_terms)

        row = conn.execute(
            f'''SELECT COUNT(*) AS blocks, COUNT(DISTINCT f.doc_id) AS docs
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE {fts_where}{extra_where}''',
            fts_params + filter_params
        ).fetchone()
    finally:
        conn.close()
    return {"blocks": row["blocks"], "docs": row["docs"]}


def search_grouped(query: str, collection: str,
                   source_type: str = None, limit: int = 200,
                   label: str = None, exclude: str = None,
                   year_from: int = None, year_to: int = None) -> list[dict]:
    """按文档聚合命中: 每篇的命中块数 + 最佳 rank.

    排序: 命中块数降序, 同数按最佳 rank. 供搜索页分组视图.
    """
    extra_where, filter_params = _search_filters(
        source_type, None, label, exclude, year_from, year_to)

    conn = get_conn(collection)
    try:
        match_expr, like_terms = _fts_plan(query, _entity_expansions(conn, query))
        if not match_expr and not like_terms:
            return []
        fts_where, fts_params = _fts_where(match_expr, like_terms)
        rank_sel = ('MIN(f.rank) AS best_rank' if match_expr
                    else 'MIN(f.rowid) AS best_rank')
        rank_ord = 'best_rank ASC'
        rows = conn.execute(
            f'''SELECT f.doc_id, d.filename, d.title, d.cite_key,
                       COUNT(*) AS hit_count, {rank_sel}
                FROM blocks_fts f
                JOIN documents d ON d.id = f.doc_id
                WHERE {fts_where}{extra_where}
                GROUP BY f.doc_id
                ORDER BY hit_count DESC, {rank_ord}
                LIMIT ?''',
            fts_params + filter_params + [limit]
        ).fetchall()
    finally:
        conn.close()

    return [dict(r) for r in rows]


def expansions_for(query: str, collection: str) -> dict:
    """对外: 查询词的实体别名扩展 {词: [变体, ...]} (供检索页提示)."""
    conn = get_conn(collection)
    try:
        return _entity_expansions(conn, query)
    finally:
        conn.close()


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
