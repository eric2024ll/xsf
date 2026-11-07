"""细读层: 页面批注 + AI 对话 (2026-11-03).

阅读层独立 —— 批注/聊天不进 lines、不参与实体编译, 与校对层零污染
(mem 小书房 细读方案裁定)。features.json 按书架启用; 表在首次访问时
惰性创建 (不启用的书架不迁移)。

坐标约定: 批注 coords 一律「归一化 0-1」(相对页面宽高的比例), 与缩放/
DPI/窗口无关。服务端裁文/裁图时再乘回对应坐标空间:
  - lines.bbox 是 OCR 厂商像素空间 (page_w × page_h)
  - page_image / overlay / reocr 区域是 150dpi 像素空间
  - pymupdf clip 是 PDF 点空间 (page.rect)
"""
import base64
import json
import sqlite3

import pymupdf

from .config import get_ocr_providers, resolve_original_path

# 批注类型: rect=框选(可问AI/定位) pen=画笔折线 highlight=半透明高亮
READ_KINDS = ('rect', 'pen', 'highlight')
READ_COLORS = ('#e11d48', '#2563eb', '#16a34a', '#d97706', '#7c3aed')

_READ_DDL = (
    """CREATE TABLE IF NOT EXISTS read_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL,
        page_num INTEGER NOT NULL,
        kind TEXT NOT NULL,
        coords TEXT NOT NULL,
        color TEXT,
        note TEXT,
        clip_text TEXT,
        origin TEXT DEFAULT 'human',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_read_ann_doc ON read_annotations(doc_id, page_num)",
    """CREATE TABLE IF NOT EXISTS read_chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ctx TEXT,
        model TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_read_chat_doc ON read_chat(doc_id, id)",
)


def ensure_read_tables(conn) -> None:
    """惰性建表 (仅细读启用的书架会走到这里)。幂等。"""
    for ddl in _READ_DDL:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 已存在


# ── 选区裁文 / 裁图 ──────────────────────────────────────

def _norm_rect(rect) -> dict | None:
    """{x,y,w,h} 归一化矩形校验 (0-1, w/h>0)。"""
    if not isinstance(rect, dict):
        return None
    try:
        x, y = float(rect['x']), float(rect['y'])
        w, h = float(rect['w']), float(rect['h'])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        return None
    return {'x': min(max(x, 0.0), 1.0), 'y': min(max(y, 0.0), 1.0),
            'w': min(w, 1.0), 'h': min(h, 1.0)}


def _open_pdf(collection: str, linked_pdf):
    p = resolve_original_path(collection, linked_pdf)
    if not p or not p.exists():
        return None
    try:
        return pymupdf.open(p)
    except Exception:
        return None


def region_text(conn, collection, doc, page_num: int, rect_norm: dict,
                max_chars: int = 4000) -> str:
    """选区文本: 优先取校对后的 lines (bbox 交选区), 空则 pymupdf clip 兜底。

    lines.bbox 在 OCR 厂商像素空间 (page_w×page_h); 无 bbox 的文献
    (原生 PDF 段落块) 直接走 clip。
    """
    r = _norm_rect(rect_norm)
    if r is None:
        return ''
    # ① 校对层: 与选区相交的块 (取块文本, 不截断到选区内字——上下文更有用)
    try:
        rows = conn.execute(
            "SELECT text, bbox, page_w, page_h FROM lines "
            "WHERE doc_id = ? AND page_num = ? ORDER BY block_num, line_num",
            (doc['id'], page_num)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    hits = []
    pw = ph = None
    for row in rows:
        if not row['bbox']:
            continue
        try:
            bx0, by0, bx1, by1 = json.loads(row['bbox'])
        except (ValueError, TypeError):
            continue
        pw = row['page_w'] or (bx1 - bx0)
        ph = row['page_h'] or (by1 - by0)
        if not pw or not ph:
            continue
        # bbox(厂商空间) → 归一化 → 与选区求交
        nx0, ny0 = bx0 / pw, by0 / ph
        nx1, ny1 = bx1 / pw, by1 / ph
        ix = max(0.0, min(nx1, r['x'] + r['w']) - max(nx0, r['x']))
        iy = max(0.0, min(ny1, r['y'] + r['h']) - max(ny0, r['y']))
        barea = max((nx1 - nx0) * (ny1 - ny0), 1e-9)
        # 覆盖判定: 交面积占块面积 ≥30%, 或块中心点落在选区内
        # (2026-11-03: 原只认 30%, 框选边缘的整行/整段会被丢掉 → 用户反馈「识别不全」)
        cx = (nx0 + nx1) / 2
        cy = (ny0 + ny1) / 2
        inside = (r['x'] <= cx <= r['x'] + r['w'] and
                  r['y'] <= cy <= r['y'] + r['h'])
        # 垂直覆盖行高 ≥50% 也算命中: 框只横向切一段 (选中几行中段) 时
        # 面积占比会很低, 但整行确实在选区内 → 应取回 (2026-11-03)
        vy = iy / max(ny1 - ny0, 1e-9)
        if ((ix * iy) / barea >= 0.30 or (inside and ix > 0 and iy > 0)
                or (vy >= 0.50 and ix > 0)):
            t = (row['text'] or '').strip()
            if t:
                hits.append(t)
    if hits:
        return '\n'.join(hits)[:max_chars]
    # ② 兜底: pymupdf clip 直裁 (原生 PDF 无 bbox / 选区在块缝隙)
    pdf = _open_pdf(collection, doc['linked_pdf'])
    if pdf is None:
        return ''
    try:
        if page_num < 1 or page_num > len(pdf):
            return ''
        page = pdf[page_num - 1]
        pr = page.rect
        clip = pymupdf.Rect(pr.x0 + r['x'] * pr.width,
                            pr.y0 + r['y'] * pr.height,
                            pr.x0 + (r['x'] + r['w']) * pr.width,
                            pr.y0 + (r['y'] + r['h']) * pr.height)
        t = page.get_text(clip=clip).strip()
        return t[:max_chars]
    finally:
        pdf.close()


def region_png_b64(collection, doc, page_num: int, rect_norm: dict,
                   dpi: int = 200) -> str | None:
    """选区裁剪图 → base64 PNG data URI (视觉问图用, 不落盘)。"""
    r = _norm_rect(rect_norm)
    if r is None:
        return None
    pdf = _open_pdf(collection, doc['linked_pdf'])
    if pdf is None:
        return None
    try:
        if page_num < 1 or page_num > len(pdf):
            return None
        page = pdf[page_num - 1]
        pr = page.rect
        clip = pymupdf.Rect(pr.x0 + r['x'] * pr.width,
                            pr.y0 + r['y'] * pr.height,
                            pr.x0 + (r['x'] + r['w']) * pr.width,
                            pr.y0 + (r['y'] + r['h']) * pr.height)
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        return 'data:image/png;base64,' + base64.b64encode(
            pix.tobytes('png')).decode('ascii')
    finally:
        pdf.close()


# ── 批注 ────────────────────────────────────────────────

def annotation_row(r) -> dict:
    d = dict(r)
    for k in ('coords',):
        try:
            d[k] = json.loads(d[k] or 'null')
        except (ValueError, TypeError):
            pass
    return d


def save_annotation(conn, collection, doc, page_num: int, kind: str,
                    coords, color: str = None, note: str = None,
                    origin: str = 'human', clip_text: str = None) -> dict:
    """落一条批注; rect 类自动补裁文快照 (检索用)。"""
    if kind not in READ_KINDS:
        raise ValueError(f'kind 须为 {READ_KINDS}')
    if kind == 'pen' and not (isinstance(coords, list) and len(coords) >= 2):
        raise ValueError('pen 需 ≥2 个点')
    if kind != 'pen':
        coords = _norm_rect(coords)
        if coords is None:
            raise ValueError('coords 须为 {x,y,w,h} 归一化矩形')
        if clip_text is None:
            clip_text = region_text(conn, collection, doc, page_num, coords)
    ensure_read_tables(conn)
    cur = conn.execute(
        """INSERT INTO read_annotations
           (doc_id, page_num, kind, coords, color, note, clip_text, origin)
           VALUES (?,?,?,?,?,?,?,?)""",
        (doc['id'], page_num, kind, json.dumps(coords, ensure_ascii=False),
         color or READ_COLORS[0], (note or '').strip() or None,
         (clip_text or '').strip() or None,
         origin if origin in ('human', 'ai') else 'human'))
    conn.commit()
    row = conn.execute("SELECT * FROM read_annotations WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return annotation_row(row)


def search_annotations(conn, query: str, limit: int = 50) -> list[dict]:
    """书架内批注检索 (LIKE note/clip_text)。

    A3 (2026-11-05): 与主路同款——jieba 分词 + 繁简变体, 组内 OR (任一
    变体命中), 组间 AND; 繁体查询不再漏简体批注。
    """
    ensure_read_tables(conn)
    from .search import _all_variants, _query_tokens
    q = (query or '').strip()
    tokens = _query_tokens(q) or ([q] if q else [])
    conds, params = [], []
    for t in tokens:
        variants = list(_all_variants(t)) or [t]
        esc = [v.replace('\\', '\\\\').replace('%', '\\%')
               .replace('_', '\\_') for v in variants]
        grp = ' OR '.join(
            "(a.note LIKE ? ESCAPE '\\' "
            "OR a.clip_text LIKE ? ESCAPE '\\')" for _ in esc)
        conds.append(f'({grp})')
        params.extend(v for _e in esc for v in (f'%{_e}%', f'%{_e}%'))
    if not conds:
        return []
    rows = conn.execute(
        f"""SELECT a.*, d.title, d.cite_key FROM read_annotations a
           JOIN documents d ON d.id = a.doc_id
           WHERE {' AND '.join(conds)}
           ORDER BY a.id DESC LIMIT ?""",
        params + [max(1, min(int(limit or 50), 200))]).fetchall()
    return [annotation_row(r) for r in rows]


# ── AI 对话 ─────────────────────────────────────────────

def vision_providers() -> list[dict]:
    """可问图的 provider: vl_api 且 OpenAI 兼容对话端点。

    paddle_http/aistudio_job 的 VL 服务是 OCR 专用管线, 不进多轮对话。
    """
    out = []
    for p in get_ocr_providers():
        if p.get('type') == 'vl_api' and p.get('endpoint') == 'openai_chat' \
                and p.get('model'):
            out.append({'id': p['id'], 'name': p.get('name') or p['model'],
                        'model': p['model']})
    return out


def _page_notes(conn, doc_id: int, page_num: int, limit: int = 20) -> list[dict]:
    """当前页批注 (问 AI 上下文用)。"""
    try:
        rows = conn.execute(
            """SELECT kind, color, note, clip_text FROM read_annotations
               WHERE doc_id = ? AND page_num = ?
                 AND (note IS NOT NULL AND note != ''
                      OR clip_text IS NOT NULL AND clip_text != '')
               ORDER BY id LIMIT ?""",
            (doc_id, page_num, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


CHAT_TURNS = 12          # 随问携带的历史轮数 (user+assistant 行数)
CHAT_HISTORY_CHARS = 8000

_SYSTEM_PROMPT = (
    "你是史料细读助手, 服务于个人文献库的图文对照细读界面。"
    "用户在原书页面上框选区域并向你提问; 提供的材料可能含 OCR 错字, "
    "引用时按原文、解释时可用现代汉语。回答直接、克制、有依据; "
    "材料不足以回答时明说, 不要臆测。若用户提供了视觉裁剪图, "
    "以图为准并结合所附文本作答。")


def ask(conn, collection, doc, question: str, mode: str = 'text',
        page_num: int = None, rect: dict = None, with_notes: bool = True,
        provider_id: str = None):
    """一次问答: 组上下文 → 调 LLM → user/assistant 两行入库。

    返回 (user_row, assistant_row)。mode='vision' 需有可用视觉 provider。
    """
    from .llm import chat, LLMNotConfigured
    ensure_read_tables(conn)
    question = (question or '').strip()
    if not question:
        raise ValueError('问题为空')

    clip_text = ''
    img_uri = None
    vps = vision_providers()
    use_vision = mode == 'vision' and vps
    if rect is not None:
        rn = _norm_rect(rect)
        if rn is None:
            raise ValueError('rect 须为 {x,y,w,h} 归一化矩形')
        clip_text = region_text(conn, collection, doc, page_num, rn)
        if use_vision:
            img_uri = region_png_b64(collection, doc, page_num, rn)

    # 上下文块
    ctx_parts = []
    if clip_text:
        ctx_parts.append(f'【选区文本 第{page_num}页】\n{clip_text}')
    if with_notes and page_num:
        notes = _page_notes(conn, doc['id'], page_num)
        if notes:
            lines = []
            for n in notes:
                seg = ''
                if n.get('clip_text'):
                    seg += f'「{n["clip_text"][:80]}」'
                if n.get('note'):
                    seg += f' → {n["note"][:200]}'
                if seg:
                    lines.append(seg)
            if lines:
                ctx_parts.append('【用户在本页的批注】\n' + '\n'.join(lines))
    ctx_block = ('\n\n'.join(ctx_parts) + '\n\n') if ctx_parts else ''
    user_content = f'{ctx_block}【提问】{question}' if ctx_block else question

    # 历史 (不含本次)
    hist_rows = conn.execute(
        "SELECT role, content FROM read_chat WHERE doc_id = ? ORDER BY id",
        (doc['id'],)).fetchall()
    messages = [{'role': 'system', 'content': _SYSTEM_PROMPT}]
    tail = hist_rows[-CHAT_TURNS:] if len(hist_rows) > CHAT_TURNS else hist_rows
    used = 0
    for r in reversed(tail):
        if used + len(r['content']) > CHAT_HISTORY_CHARS:
            break
        messages.insert(1, {'role': r['role'], 'content': r['content']})
        used += len(r['content'])

    model_name = None
    if use_vision:
        vp = next((p for p in vps if p['id'] == provider_id), vps[0])
        content = [{'type': 'text', 'text': user_content}]
        if img_uri:
            content.append({'type': 'image_url', 'image_url': {'url': img_uri}})
        messages.append({'role': 'user', 'content': content})
        # 视觉 provider 走 vl_api 配置, 不在 llm_chat 列表里 → 传显式 cfg
        raw = next((p for p in get_ocr_providers()
                    if p.get('id') == vp['id']), {})
        cfg = {'base_url': raw.get('base_url') or '',
               'api_key': raw.get('api_key') or '',
               'model': raw.get('model')}
        answer, model_name = chat([m for m in messages], cfg=cfg)
    else:
        messages.append({'role': 'user', 'content': user_content})
        answer, model_name = chat(messages, provider_id=provider_id)

    ctx_json = json.dumps(
        {'page': page_num, 'rect': rect, 'clip_text': clip_text,
         'mode': 'vision' if use_vision else 'text',
         'with_notes': bool(with_notes)},
        ensure_ascii=False)
    ucur = conn.execute(
        "INSERT INTO read_chat(doc_id, role, content, ctx, model) "
        "VALUES (?,?,?,?,?)",
        (doc['id'], 'user', question, ctx_json, None))
    acur = conn.execute(
        "INSERT INTO read_chat(doc_id, role, content, ctx, model) "
        "VALUES (?,?,?,?,?)",
        (doc['id'], 'assistant', answer, None, model_name))
    conn.commit()
    urow = conn.execute("SELECT * FROM read_chat WHERE id = ?",
                        (ucur.lastrowid,)).fetchone()
    arow = conn.execute("SELECT * FROM read_chat WHERE id = ?",
                        (acur.lastrowid,)).fetchone()
    return dict(urow), dict(arow)
