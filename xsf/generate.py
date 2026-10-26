"""可复用 LLM 生成核心 — Web 路由与 CLI 共用.

约定:
- provider_id 省略 (None) 时使用默认 LLM provider (config.get_default_llm_provider_id)
- progress: 可选回调 progress(text: str) 报告进度
- 未配置 provider 抛 LLMNotConfigured; 业务错误抛 ValueError; 其他异常由调用方处理
- 数据落库与 Web 路由原逻辑一致 (修订史/基线戳/draft_flags 等)
"""

from __future__ import annotations

import json

from .db import get_conn
from .llm import chat, autofill_rows, LLMNotConfigured  # noqa: F401
from .config import get_default_llm_provider_id
from .bib_utils import (BIB_TYPE_FIELDS, BIB_FIELD_LABELS,
                        normalize_bib_type, sync_doc_fields)
from . import entity as ent
from .ingest import _tokenize


def _api():
    """延迟导入 api (避免循环导入): 复用其页码/文档辅助函数与 wiki prompt."""
    from . import api as A
    return A


def _noop(_msg):
    pass


# ════════════════════════════════════════════════════════════
# 卡片候选 (目录/摘要/元数据/实体) — 单文献
# ════════════════════════════════════════════════════════════

def gen_cards_core(collection, doc_id, targets="toc,entities,summary,meta",
                   prompt=None, toc_source=None, bib_source=None,
                   provider_id=None, progress=None):
    """生成目录/摘要/元数据/实体候选 (落库: 实体->候选队列; 摘要->draft 卡;
    元数据->bib_data; 目录树经返回值 tree 交调用方确认合并)。"""
    progress = progress or _noop
    A = _api()
    want = {t.strip() for t in (targets or "toc,entities,summary,meta").split(",")
            if t.strip() in ("toc", "entities", "summary", "meta")}
    if not want:
        want = {"toc", "entities", "summary"}
    if provider_id is None:
        provider_id = get_default_llm_provider_id()  # 未配置则抛 LLMNotConfigured

    doc = A._get_card_doc(collection, doc_id)
    if doc is None:
        raise ValueError("文献不存在")
    conn = get_conn(collection)
    try:
        blocks = conn.execute(
            "SELECT page_num, block_num, text FROM blocks_fts "
            "WHERE doc_id = ? ORDER BY page_num, block_num LIMIT 40",
            (doc_id,)).fetchall()
    finally:
        conn.close()
    if not blocks:
        raise ValueError("文献无文本块，无法生成")

    sample = "\n".join(
        f"[p{b['page_num']}b{b['block_num']}] {b['text'][:120]}"
        for b in blocks)
    fields = []
    if "toc" in want:
        fields.append('"toc": [{"label": "目录/主题节点", '
                      '"category": "自由类目标签（如 职官/兵事/贡赋/舆地 或自拟）", '
                      '"page_num": 页码, '
                      '"children": [同构子节点数组, 最多嵌套3层, 叶子可省略]}]'
                      '（根节点 3-8 个, 层级按目录实际结构）')
    if "entities" in want:
        _ev = []
        try:
            _vc = get_conn(collection)
            try:
                _ev = [r["term"] for r in _vc.execute(
                    "SELECT term FROM vocabs WHERE kind = 'entity_category' ORDER BY id"
                ).fetchall()]
            finally:
                _vc.close()
        except Exception:
            _ev = []
        _cat_desc = ('实体类别（从词表选择: ' + '/'.join(_ev) + '）'
                     if _ev else '实体类别（如 人名/地名/机构/物产/职官）')
        fields.append('"entities": [{"label": "实体名（人名/地名/机构/物产等）", '
                      '"category": "' + _cat_desc + '", '
                      '"page_num": 页码, "confidence": 0到1}]（3-15 个）')
    if "summary" in want:
        fields.append('"summary": "100-200字摘要"')
    if "meta" in want:
        _bt = normalize_bib_type(doc["bib_type"])
        _fields_desc = ", ".join(
            f'"{f}": "{BIB_FIELD_LABELS.get(f, f)}"'
            for f in BIB_TYPE_FIELDS.get(_bt, BIB_TYPE_FIELDS["@book"]))
        fields.append('"meta": {' + _fields_desc + '}（从题名页/序跋/卷端/'
                      '版权页识别, 按 ' + (_bt or "@book") + ' 字段, '
                      '不确定的字段给空串）')
    system = (
        "你是文献编目助手。基于给定的文献块样本（每行前缀 [p页b块]），"
        "输出严格 JSON：{" + ", ".join(fields) + "}。"
        "只输出要求的字段, 只输出 JSON, 不要解释。"
    )
    extra_prompt = (prompt or "").strip()
    if "meta" in want and extra_prompt:
        system += f"\n补充要求（用户指示, 优先遵循）: {extra_prompt}"
    user_content = sample
    toc_src = (toc_source or "").strip()
    if "toc" in want and toc_src:
        system += ("\n用户提供了目录文本（多级缩进/编号等形式）— "
                   "优先按该目录文本解析出 toc 树（保留其层级与页码/卷次信息）, "
                   "文献块样本仅作参考。")
        user_content = ("目录文本:\n" + toc_src[:6000]
                        + "\n\n文献块样本(参考):\n" + sample)
    bib_src = (bib_source or "").strip()
    if "meta" in want and bib_src:
        system += ("\n用户提供了书目数据 (BibTeX/RIS/引文格式等) — "
                   "优先解析书目数据填 meta 字段, 文献块样本仅作参考。")
        user_content = f"书目数据:\n{bib_src[:4000]}\n\n文献块样本(参考):\n{sample}"

    progress(f"请求 LLM 生成卡片候选 (targets={sorted(want)})…")
    content, model = chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user_content}], provider_id)
    try:
        parsed = json.loads(
            content[content.index('{'):content.rindex('}') + 1])
    except (ValueError, AttributeError):
        raise ValueError(f"LLM 输出无法解析: {content[:120]}")

    toc_tree_raw = (parsed.get("toc") or []) if "toc" in want else []

    def _clean_tree(nodes, depth=0):
        out = []
        for x in nodes if isinstance(nodes, list) else []:
            if not isinstance(x, dict):
                continue
            label = str(x.get("label") or "").strip()
            if not label:
                continue
            node = {"label": label,
                    "category": str(x.get("category") or '').strip() or None,
                    "page_num": int(x.get("page_num") or 0) or None,
                    "from": "ai"}
            if depth < 2:
                kids = _clean_tree(x.get("children") or [], depth + 1)
                if kids:
                    node["children"] = kids
            out.append(node)
        return out

    def _count_tree(nodes):
        n = 0
        for x in nodes:
            n += 1 + _count_tree(x.get("children") or [])
        return n

    toc_tree = _clean_tree(toc_tree_raw)
    entity_items = (parsed.get("entities") or []) if "entities" in want else []
    summary_text = (parsed.get("summary") or "").strip() if "summary" in want else ""
    meta_items = (parsed.get("meta") or {}) if "meta" in want else {}

    n_meta = 0
    need_meta = isinstance(meta_items, dict) and bool(meta_items)
    if need_meta or summary_text:
        conn_m = get_conn(collection)
        try:
            drow = conn_m.execute(
                "SELECT bib_type, bib_data FROM documents WHERE id = ?",
                (doc_id,)).fetchone()
            try:
                bib = json.loads(drow["bib_data"] or "{}") if drow else {}
            except ValueError:
                bib = {}
            if need_meta:
                doc_bt = normalize_bib_type(
                    drow["bib_type"] if drow else doc.get("bib_type"))
                fk_set = set(BIB_TYPE_FIELDS.get(
                    doc_bt, BIB_TYPE_FIELDS["@book"])) | {"note"}
                for fk in fk_set:
                    v = str(meta_items.get(fk) or "").strip()
                    if v:
                        bib[fk] = v
                        n_meta += 1
            if summary_text:
                bib["abstract"] = summary_text   # 摘要降为元数据字段 (2026-10-19)
            synced = sync_doc_fields(bib)
            conn_m.execute(
                "UPDATE documents SET bib_data = ?, "
                "title = COALESCE(?, title), author = COALESCE(?, author) "
                "WHERE id = ?",
                (json.dumps(bib, ensure_ascii=False),
                 synced["title"], synced["author"], doc_id))
            conn_m.commit()
        finally:
            conn_m.close()

    conn = get_conn(collection)
    try:
        # 页码语义: 目录节点页码=原书页 (来自书目目录文本/LLM 读目录页), 不换算;
        # 实体页码来自内容块物理页 (pdf), 校准时换算为原书页落库
        pm_groups = A._pm_groups(A._load_page_map(conn, doc_id))
        if pm_groups:
            for it in entity_items:
                if it.get("page_num"):
                    it["page_num"] = A._pm_reverse(pm_groups, int(it["page_num"]))
        n_ent = 0
        for it in entity_items:
            label = str(it.get("label") or "").strip()
            if not label:
                continue
            conn.execute(
                """INSERT INTO card_candidates
                   (doc_id, ctype, label, category, confidence, page_num, status)
                   VALUES (?, 'entity', ?, ?, ?, ?, 'pending')""",
                (doc_id, label,
                 str(it.get("category") or '').strip() or None,
                 float(it.get("confidence") or 0.5),
                 int(it.get("page_num") or 0) or None))
            n_ent += 1
        conn.commit()
        return {"ok": True, "toc": _count_tree(toc_tree), "tree": toc_tree,
                "entities": n_ent, "meta": n_meta,
                "summary": bool(summary_text), "model": model}
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# Wiki 知识地图 — 书架级
# ════════════════════════════════════════════════════════════

def gen_wiki_core(collection, provider_id=None, progress=None):
    """编译知识地图 (清旧插新 + ready + 基线戳)。"""
    progress = progress or _noop
    A = _api()
    if provider_id is None:
        provider_id = get_default_llm_provider_id()
    conn = get_conn(collection)
    try:
        progress("组装 Wiki 输入 (文献列表+已审定卡+块采样)…")
        payload = json.dumps(A._wiki_llm_payload(conn), ensure_ascii=False)
        progress("请求 LLM 编译知识地图…")
        content, model = chat(
            [{"role": "system", "content": A.WIKI_SYSTEM_PROMPT},
             {"role": "user", "content": payload}], provider_id)
        i0, i1 = content.find('{'), content.rfind('}') + 1
        if i0 < 0 or i1 <= i0:
            raise ValueError("LLM 输出未包含 JSON 对象")
        pages = json.loads(content[i0:i1]).get("pages", [])
        if not pages:
            raise ValueError("LLM 输出 pages 为空")
        rev_now = conn.execute(
            "SELECT COUNT(*) c FROM revision_log").fetchone()["c"]
        conn.execute("DELETE FROM wiki_pages WHERE collection = ?", (collection,))
        for pos, pg in enumerate(pages):
            slug = (pg.get("slug") or pg.get("title") or f"page-{pos}")[:60]
            conn.execute(
                "INSERT INTO wiki_pages "
                "(collection, slug, title, content, refs, position) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(collection, slug) DO NOTHING",
                (collection, slug, pg.get("title") or slug,
                 pg.get("content") or "",
                 json.dumps(pg.get("refs") or []), pos))
        conn.execute(
            "UPDATE wiki_status SET status = 'ready', base_edits = ?, "
            "generated_at = datetime('now', 'localtime') WHERE collection = ?",
            (rev_now, collection))
        conn.commit()
        return {"pages": len(pages), "model": model}
    except Exception:
        conn.execute("UPDATE wiki_status SET status = 'none' WHERE collection = ?",
                     (collection,))
        conn.commit()
        raise
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════
# 实体图谱 — 书架级 / 单文献增量
# ════════════════════════════════════════════════════════════

def gen_entitygraph_core(collection, doc_ids=None, provider_id=None, progress=None):
    """编译实体图 (doc_ids 省略=全书架; 给定列表=这些文献增量重建)。"""
    progress = progress or _noop
    if provider_id is None:
        provider_id = get_default_llm_provider_id()
    conn = get_conn(collection)
    try:
        conn.execute(
            "INSERT INTO entity_status(collection, status, base_edits, generated_at) "
            "VALUES(?, 'generating', 0, NULL) "
            "ON CONFLICT(collection) DO UPDATE SET status = 'generating'",
            (collection,))
        conn.commit()
    finally:
        conn.close()
    try:
        def _p(i, n):
            progress(f"编译实体提及 {i}/{n}")
        total = ent.generate_graph(collection, doc_ids, provider_id, progress=_p)
        return {"mentions": total}
    except Exception:
        try:
            c2 = get_conn(collection)
            c2.execute("UPDATE entity_status SET status = 'none' WHERE collection = ?",
                       (collection,))
            c2.commit()
            c2.close()
        except Exception:
            pass
        raise


# ════════════════════════════════════════════════════════════
# 翻译 / 对照 (autofill) — 页级 + 文献级
# ════════════════════════════════════════════════════════════

def autofill_page(collection, doc_id, page_num, targets=None,
                  provider_id=None, progress=None,
                  batch_blocks: int = 4, batch_chars: int = 800):
    """AI 填充某页多行制 draft 层 (translit/gloss/trans 子集)。"""
    progress = progress or _noop
    A = _api()
    targets = targets or ["gloss", "trans"]
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    if provider_id is None:
        provider_id = get_default_llm_provider_id()
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            "SELECT id, lang, tri_rows FROM documents WHERE id = ?",
            (doc_id,)).fetchone()
        if doc is None:
            raise ValueError("文献不存在")
        rows_mode = A._tri_rows_for(doc)
        if rows_mode < 3:
            raise ValueError("AI 填充仅适用于非中文多行制文献 (三行/四行); 请先切换行制")
        if rows_mode == 3:
            targets = [t for t in targets if t != "translit"]
        rows = conn.execute(
            """SELECT id, page_num, block_num, text, translit, gloss, trans,
                      draft_flags
               FROM lines WHERE doc_id = ? AND page_num = ?
               ORDER BY block_num, line_num""",
            (doc_id, page_num)).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError("该页无文本块")
    blocks = [{"text": r["text"]} for r in rows]
    src_lang = doc["lang"] or "zh"
    progress(f"请求 LLM 填充 (targets={targets}, {len(blocks)} 块)…")
    results, model = autofill_rows(
        blocks, src_lang=src_lang, targets=targets, provider_id=provider_id,
        batch_blocks=batch_blocks, batch_chars=batch_chars)
    filled = 0
    touched = set()      # (page_num, block_num) 待重建 FTS 的块
    conn = get_conn(collection)
    try:
        for r, draft in zip(rows, results):
            if not draft:
                continue
            try:
                flags = json.loads(r["draft_flags"]) if r["draft_flags"] else {}
            except (json.JSONDecodeError, ValueError):
                flags = {}
            updates, params = [], []
            for key, prefix in (("translit", "[转] "),
                                ("gloss", "[对] "),
                                ("trans", "[译] ")):
                if key not in draft:
                    continue
                old_v = r[key] or ""
                new_v = draft[key]
                if new_v == old_v:
                    continue
                updates.append(f"{key} = ?")
                params.append(new_v)
                flags[key] = "ai"
                conn.execute(
                    """INSERT INTO revision_log
                       (doc_id, page_num, block_num, old_value, new_value,
                        basis, in_pkg)
                       VALUES (?, ?, ?, ?, ?, ?, 0)""",
                    (doc_id, page_num,
                     conn.execute("SELECT block_num FROM lines WHERE id = ?",
                                  (r["id"],)).fetchone()["block_num"],
                     prefix + old_v, prefix + new_v, f"AI草稿·{model}"))
                filled += 1
            if updates:
                if flags:
                    updates.append("draft_flags = ?")
                    params.append(json.dumps(flags, ensure_ascii=False))
                params.append(r["id"])
                conn.execute(f"UPDATE lines SET {', '.join(updates)} WHERE id = ?",
                             params)
                touched.add((r["page_num"], r["block_num"]))
        # 重建受影响块的 FTS (译文/转写进索引 — 中文可搜外文文献)
        for pg, bn in touched:
            lr = conn.execute(
                "SELECT text, trans FROM lines WHERE doc_id = ? "
                "AND page_num = ? AND block_num = ? ORDER BY line_num",
                (doc_id, pg, bn)).fetchall()
            conn.execute(
                "DELETE FROM blocks_fts WHERE doc_id = ? "
                "AND page_num = ? AND block_num = ?", (doc_id, pg, bn))
            conn.execute(
                "INSERT INTO blocks_fts(doc_id, page_num, block_num, "
                "text, trans) VALUES (?, ?, ?, ?, ?)",
                (doc_id, pg, bn,
                 _tokenize("\n".join(x["text"] for x in lr)),
                 " ".join(x["trans"] for x in lr
                          if (x["trans"] or "").strip()) or None))
        conn.commit()
        return {"filled": filled, "model": model,
                "empty": sum(1 for d in results if not d)}
    finally:
        conn.close()


def autofill_doc(collection, doc_id, targets=None, provider_id=None,
                 progress=None,
                 batch_blocks: int = 4, batch_chars: int = 800):
    """对整个文献的每页跑 autofill (页级循环)。"""
    progress = progress or _noop
    if provider_id is None:
        provider_id = get_default_llm_provider_id()
    conn = get_conn(collection)
    try:
        pages = [r["page_num"] for r in conn.execute(
            "SELECT DISTINCT page_num FROM lines WHERE doc_id = ? "
            "ORDER BY page_num", (doc_id,)).fetchall()]
    finally:
        conn.close()
    if not pages:
        raise ValueError("文献无文本块")
    total = {"filled": 0, "empty": 0}
    for i, pg in enumerate(pages):
        progress(f"页 {pg} ({i + 1}/{len(pages)})")
        r = autofill_page(collection, doc_id, pg, targets, provider_id,
                          batch_blocks=batch_blocks, batch_chars=batch_chars)
        total["filled"] += r["filled"]
        total["empty"] += r["empty"]
    return total
