"""智能校对: job 存储 + 候选转录 + 分歧行定位。

设计依据: pqa design/client/19-smart-proofread.md
对齐核心在 collate.py (零依赖); 本模块负责:
- 内存 job store (单用户场景, 与 reocr job 模式一致)
- 页归一序列构建 (带逐字→line_id 映射, 供分歧点定位回行)
- 引擎通道候选获取 (缓存 ocr_candidates 表)
- 人工通道: 从确认页映射切片取候选
"""

import logging
import threading

import pymupdf

from .db import get_conn
from .collate import align, collate, normalize

logger = logging.getLogger("xsf.smartcheck")

_lock = threading.Lock()
# {(collection, doc_id, page_num): {"state", "reason", "divergences", ...}}
_jobs: dict = {}


def _get(collection, doc_id, page_num):
    with _lock:
        job = _jobs.get((collection, doc_id, page_num))
        return dict(job) if job else None


def _set(collection, doc_id, page_num, **kw):
    with _lock:
        _jobs.setdefault((collection, doc_id, page_num), {}).update(kw)


def pop(collection, doc_id, page_num):
    with _lock:
        _jobs.pop((collection, doc_id, page_num), None)


# ── 页归一序列 + 逐字行映射 ──────────────────────────────

def build_page_sequence(collection, doc_id, page_num):
    """页竖排文本 → (归一字符列表, 逐字 line_id 列表)。

    只取 vertical_text 块 (版心/表格由 block_label 自然排除)。
    返回 None 表示该页无竖排块 (表格页/图版页 → unsupported)。
    """
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            """SELECT id, text, block_label FROM lines
               WHERE doc_id = ? AND page_num = ? AND block_label = 'vertical_text'
               ORDER BY block_num, line_num""",
            (doc_id, page_num),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    chars, line_ids = [], []
    for r in rows:
        for ch in normalize(r["text"]):
            chars.append(ch)
            line_ids.append(r["id"])
    return chars, line_ids


def attach_line_ids(divergences, chars_a, line_ids_a):
    """分歧点 idx_a (归一序列下标) → line_id 集合。"""
    for d in divergences:
        ids = sorted({line_ids_a[i] for i in d.get("idx_a") or []
                      if 0 <= i < len(line_ids_a)})
        d["line_ids"] = ids
        d.pop("idx_a", None)
        d.pop("idx_b", None)
    return divergences


# ── 引擎通道候选 (带缓存) ────────────────────────────────

def render_page_png(collection, doc_id, page_num) -> bytes | None:
    """与 page_image 端点同源: 150dpi PNG 渲染。"""
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            "SELECT filename, linked_pdf FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    finally:
        conn.close()
    if doc is None:
        return None
    from .config import get_upload_path
    pdf_path = get_upload_path(collection, doc["linked_pdf"] or doc["filename"])
    if not pdf_path.exists():
        return None
    src = pymupdf.open(pdf_path)
    try:
        if page_num < 1 or page_num > len(src):
            return None
        return src[page_num - 1].get_pixmap(dpi=150).tobytes("png")
    finally:
        src.close()


def get_engine_candidate(collection, doc_id, page_num, provider) -> str:
    """第二引擎转录 (ocr_candidates 缓存优先)。"""
    conn = get_conn(collection)
    try:
        row = conn.execute(
            "SELECT text FROM ocr_candidates WHERE doc_id = ? AND page_num = ? "
            "AND provider = ?",
            (doc_id, page_num, provider.name),
        ).fetchone()
        if row:
            return row["text"]
    finally:
        conn.close()

    img = render_page_png(collection, doc_id, page_num)
    if img is None:
        raise RuntimeError("页图渲染失败 (PDF 缺失或页码越界)")
    text = provider.ocr_image_plain(img).strip()
    if not text:
        raise RuntimeError("引擎返回空转录")

    conn = get_conn(collection)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ocr_candidates "
            "(doc_id, page_num, provider, model_ver, text) VALUES (?,?,?,?,?)",
            (doc_id, page_num, provider.name, None, text),
        )
        conn.commit()
    finally:
        conn.close()
    return text


def get_manual_candidate(collection, transcript_id, page_num) -> str | None:
    """人工通道: 按确认页映射切片 (归一文本区间)。"""
    conn = get_conn(collection)
    try:
        tp = conn.execute(
            "SELECT char_start, char_end FROM transcript_pages "
            "WHERE transcript_id = ? AND page_num = ? AND confirmed = 1",
            (transcript_id, page_num),
        ).fetchone()
        tr = conn.execute(
            "SELECT text FROM manual_transcripts WHERE id = ?",
            (transcript_id,),
        ).fetchone()
    finally:
        conn.close()
    if not tp or not tr:
        return None
    from .collate import slice_page
    return slice_page(tr["text"], tp["char_start"] or 0,
                      tp["char_end"] or 0) or None


# ── 主流程 ────────────────────────────────────────────────

def run_check(collection, doc_id, page_num, channel, provider=None,
              transcript_id=None):
    """同步执行单页校对 (引擎通道由 api 层放后台线程调用)。"""
    seq = build_page_sequence(collection, doc_id, page_num)
    if seq is None:
        _set(collection, doc_id, page_num, state="unsupported",
             reason="该页无竖排文本块 (表格/图版页), 请用画框重 OCR 处理")
        return
    chars_a, line_ids_a = seq

    try:
        if channel == "engine":
            text_b = get_engine_candidate(collection, doc_id, page_num, provider)
        else:
            text_b = get_manual_candidate(collection, transcript_id, page_num)
            if text_b is None:
                _set(collection, doc_id, page_num, state="error",
                     reason="人工文本未映射到该页 (或映射未确认)")
                return
    except Exception as e:
        logger.warning("smart-check 候选获取失败 coll=%s doc=%s p%s: %s",
                       collection, doc_id, page_num, e)
        _set(collection, doc_id, page_num, state="error", reason=str(e))
        return

    result = collate(align(chars_a, normalize(text_b)))
    if not result["accepted"]:
        _set(collection, doc_id, page_num, state="rejected",
             reason=f"对齐自检未通过 (一致率 {result['agree']:.0%} < 60%), "
                    "疑似全局错位, 未写库")
        return

    divs = attach_line_ids(result["divergences"], chars_a, line_ids_a)

    # pageno (版心页码) 只展示不写 flag
    flag_divs = [d for d in divs if d["kind"] != "pageno"]

    # 幂等刷新: 先清本页既有 diff flag, 再按本次分歧重写
    touched = {lid for d in flag_divs for lid in d["line_ids"]}
    conn = get_conn(collection)
    try:
        conn.execute(
            "UPDATE lines SET suspect = REPLACE(REPLACE(REPLACE("
            "COALESCE(suspect, ''), 'diff,', ''), ',diff', ''), 'diff', '') "
            "WHERE doc_id = ? AND page_num = ? AND suspect LIKE '%diff%'",
            (doc_id, page_num),
        )
        for lid in touched:
            conn.execute(
                "UPDATE lines SET suspect = CASE "
                "WHEN suspect IS NULL OR suspect = '' THEN 'diff' "
                "WHEN suspect LIKE '%diff%' THEN suspect "
                "ELSE suspect || ',diff' END WHERE id = ?",
                (lid,),
            )
        conn.commit()
    finally:
        conn.close()

    _set(collection, doc_id, page_num, state="done", reason=None,
         divergences=divs,
         agree=round(result["agree"], 3),
         source=channel)
