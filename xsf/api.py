"""xsf FastAPI Web 界面"""

import asyncio
import html
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import pymupdf
from fastapi import FastAPI, UploadFile, File, Form, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    get_collections_dir, get_upload_dir,
    list_collections, get_db_path, validate_collection_name,
)
from .db import init_db, get_conn
from .search import (search, search_grouped, count_hits, get_block_lines,
                     get_context, get_highlight_terms)
from .bib_utils import (
    generate_cite_key, sync_doc_fields, parse_bib_data, to_bibtex,
    parse_bib_entries, match_docs_to_entries, normalize_bib_type,
    BIB_TYPE_FIELDS, BIB_TYPE_LABELS, BIB_FIELD_LABELS,
)
from .ingest import (
    ingest_pdf, ingest_scanned_pdf, ingest_markdown, ingest_image,
    ingest_office, ingest_native_pdf, ingest_epub, probe_pdf_text_layer,
    _OFFICE_EXTS, _epub_spine_files, remove_doc, _tokenize,
)
from .reocr import (
    _reocr_lock, _reocr_jobs, _reocr_get, _reocr_set,
    _run_reocr_doc, register_job, pop_job,
)
from .folder_scan import get_scan_status, start_scan_threads, shutdown_scan

# ── 通用后台任务 (AI 生成类: autofill/卡片候选/wiki; 2026-10-18 用户裁定
#    「ai生成或填充内容在后台，不要影响前台页面操作」) ──────────────
_bg_jobs: dict = {}
_bg_jobs_lock = threading.Lock()


def _bg_job_start(kind: str, target) -> str:
    """起后台线程跑 target() (无参), 返回 job id; 结果/异常记入 _bg_jobs."""
    jid = f"{kind}-{int(time.time() * 1000) % 10_000_000}"
    with _bg_jobs_lock:
        _bg_jobs[jid] = {"kind": kind, "status": "running",
                         "started": time.strftime("%m-%d %H:%M:%S")}

    def _run():
        try:
            result = target()
            with _bg_jobs_lock:
                _bg_jobs[jid].update(status="done", result=result)
        except Exception as e:  # noqa: BLE001 — 后台任务兜底
            with _bg_jobs_lock:
                _bg_jobs[jid].update(status="error", error=str(e))
        finally:
            with _bg_jobs_lock:
                while len(_bg_jobs) > 60:
                    _bg_jobs.pop(next(iter(_bg_jobs)))

    threading.Thread(target=_run, daemon=True).start()
    return jid

from .suspect import detect_suspect

_VERSION = "0.3.0"

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

# 静态资源版本号: 取 static 目录最新 mtime, 引用处带 ?v= 破浏览器启发式强缓存
_static_v = format(
    int(max((p.stat().st_mtime for p in (_BASE_DIR / "static").rglob("*") if p.is_file()), default=0)),
    "x",
)
templates.env.globals["static_v"] = _static_v


def _first_line_id_for_block(doc_id: int, page_num: int, block_num: int,
                             collection: str) -> int | None:
    """取某 block 的第一行 id，供搜索结果跳转到校对页。"""
    conn = get_conn(collection)
    try:
        row = conn.execute(
            """SELECT id FROM lines
               WHERE doc_id = ? AND page_num = ? AND block_num = ?
               ORDER BY line_num LIMIT 1""",
            (doc_id, page_num, block_num),
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化所有已有 collection 的 DB; 启动文件夹扫描线程"""
    for coll in list_collections():
        init_db(coll)
        try:
            conn = get_conn(coll)
            conn.execute("UPDATE entity_status SET status = 'none' "
                         "WHERE status = 'generating'")
            conn.commit()
            conn.close()
        except Exception:
            pass
    start_scan_threads()
    yield
    shutdown_scan()


app = FastAPI(title="小書房：放書架的地方，也能看書", version=_VERSION, lifespan=lifespan)

# 实体图编译的进程内锁 (DB 的 entity_status 仅供 UI 显示, 不作锁: 重启/崩溃后不残留)
_ENTITY_RUNNING: set = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="4" fill="#3b5998"/>'
    '<text x="16" y="23" font-size="18" text-anchor="middle" '
    'fill="#fff" font-family="serif">架</text></svg>'
).encode('utf-8')



@app.get("/jobs/{job_id}")
async def bg_job_status(job_id: str):
    """后台任务状态 (前端 2s 轮询): running/done/error."""
    with _bg_jobs_lock:
        j = _bg_jobs.get(job_id)
    if j is None:
        return JSONResponse(status_code=404,
                            content={"error": "任务不存在（服务重启或已过期）"})
    return {"job": job_id, **j}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ── Last Collection 持久化 ────────────────────────────────

import re as _re
from urllib.parse import quote as _urlquote, unquote as _urlunquote
_COLL_PATH_RE = _re.compile(r'^/collections/([^/]+)')


class LastCollectionMiddleware(BaseHTTPMiddleware):
    """凡访问 /collections/{c}/... 就把 c 记为最近书架 (cookie, 7 天)。

    cookie 值做 URL 编码 (书架名可能含中文/特殊字符，避免 header 编码问题)。
    只记录不校验存在性 (省目录扫描)，校验放到读取时 (GET /)。
    """

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        m = _COLL_PATH_RE.match(request.url.path)
        if m and resp.status_code < 400:
            resp.set_cookie(
                'last_collection', _urlquote(m.group(1), safe=''),
                httponly=True, samesite='lax',
                max_age=7 * 24 * 3600,
            )
        return resp


app.add_middleware(LastCollectionMiddleware)

# ── 页面 ──────────────────────────────────────────────

def _nav_ctx(active: str = "", collection: str = "", **kw):
    """构建导航栏通用 context."""
    ctx = {
        "active_nav": active,
        "collection": collection,
        "collections": list_collections(),
    }
    ctx.update(kw)
    return ctx


@app.get("/")
async def bookshelf(request: Request):
    last = _urlunquote(request.cookies.get('last_collection', ''))
    return templates.TemplateResponse(
        request,
        "bookshelf.html",
        _nav_ctx("bookshelf", last, version=_VERSION,
                 last_collection=last),
    )


@app.get("/search")
async def search_page(request: Request, c: str = "", q: str = ""):
    coll = c if c and c in list_collections() else ""
    hl = get_highlight_terms(q) if q else []
    return templates.TemplateResponse(
        request,
        "search.html",
        _nav_ctx("search", coll, q=q, highlight_terms=hl),
    )


# ── Collection 列表 ────────────────────────────────────

@app.get("/api/collections")
async def api_collections():
    return {"collections": list_collections()}


@app.post("/api/collections/{collection}")
async def api_create_collection(collection: str):
    """创建新书架: 初始化 DB."""
    err = validate_collection_name(collection)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if collection in list_collections():
        return JSONResponse({"error": "书架已存在"}, status_code=409)
    init_db(collection)
    return {"ok": True, "name": collection}


@app.delete("/api/collections/{collection}")
async def api_delete_collection(request: Request, collection: str):
    """删除书架: DB 目录 + 源文件目录.

    若当前 last_collection cookie 就是被删书架, 一并清除, 避免残留 cookie
    触发对已删书架的请求 → get_conn 静默重建空 DB → 书架「复活」。
    """
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    # DB 目录
    db_dir = get_db_path(collection).parent
    if db_dir.exists():
        shutil.rmtree(db_dir)
    # 源文件目录
    coll_dir = get_collections_dir() / collection
    if coll_dir.exists():
        shutil.rmtree(coll_dir)
    last = _urlunquote(request.cookies.get("last_collection", ""))
    if last == collection:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("last_collection")
        return resp
    return {"ok": True}


@app.patch("/api/collections/{collection}")
async def api_rename_collection(
    request: Request,
    collection: str,
    new_name: str = Body("", embed=True),
):
    """重命名书架: 移动 DB 目录 + 源文件目录.

    若当前 last_collection cookie 就是旧名, 一并更新为新名, 避免导航栏出现
    已不存在的书架。
    """
    new_name = (new_name or "").strip()
    err = validate_collection_name(new_name)
    if err:
        return JSONResponse({"error": f"新{err}"}, status_code=400)
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    if new_name in list_collections():
        return JSONResponse({"error": "名称已被占用"}, status_code=409)
    # DB 目录
    old_db = get_db_path(collection).parent
    new_db = get_db_path(new_name).parent
    if old_db.exists():
        old_db.rename(new_db)
    # 源文件目录
    old_coll = get_collections_dir() / collection
    new_coll = get_collections_dir() / new_name
    if old_coll.exists():
        old_coll.rename(new_coll)

    resp = JSONResponse({"ok": True, "new_name": new_name})
    last = _urlunquote(request.cookies.get("last_collection", ""))
    if last == collection:
        resp.set_cookie(
            "last_collection",
            _urlquote(new_name, safe=""),
            httponly=True,
            samesite="lax",
            max_age=7 * 24 * 3600,
        )
    return resp


# ── OCR 配置 (generic_http provider 管理) ───────────────

@app.get("/api/ocr-config")
async def api_get_ocr_config():
    """provider 列表 (api_key 打码) + default + default_llm (P2c)。"""
    from .config import (get_ocr_providers, get_default_ocr_provider_id,
                         get_default_llm_provider_id)
    providers = []
    for p in get_ocr_providers():
        providers.append({
            'id': p['id'],
            'name': p.get('name', p['id']),
            'type': p.get('type', 'vl_api'),
            'endpoint': p.get('endpoint', 'paddle_http'),
            'url': p.get('base_url', p.get('url', '')),
            'model': p.get('model') or '',
            'structured': p.get('endpoint', 'paddle_http') != 'openai_chat',
            'prompt': p.get('prompt') or '',
            'has_prompt': bool((p.get('prompt') or '').strip()),
            'has_key': bool(p.get('api_key')),
            'updated_at': p.get('updated_at') or p.get('created_at'),
        })
    return {
        'providers': providers,
        'default': get_default_ocr_provider_id(),
        'default_llm': get_default_llm_provider_id(),
    }


@app.post("/api/ocr-config/provider")
async def api_save_ocr_provider(name: str = Form(...), url: str = Form(''),
                                id: str = Form(None),
                                api_key: str = Form(None),
                                model: str = Form(None),
                                endpoint: str = Form('paddle_http'),
                                prompt: str = Form(''),
                                ptype: str = Form('vl_api')):
    """新增/编辑 provider (v3)。ptype: vl_api (视觉) / llm_chat (翻译 LLM, P2c)。
    api_key 留空且为编辑 → 保留旧值。"""
    from .config import save_ocr_provider
    try:
        p = save_ocr_provider(name=name, url=url, pid=id,
                              api_key=(api_key or '').strip() or None,
                              model=model, endpoint=endpoint,
                              prompt=prompt or '', ptype=ptype)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if p.get('endpoint') == 'aistudio_job' and not p.get('api_key'):
        return JSONResponse(
            {"error": "aistudio_job 类型必须填写 token (API Key)"},
            status_code=400,
        )
    return {"ok": True, "id": p['id']}


@app.post("/api/ocr-config/test")
async def api_test_ocr_provider(id: str = Form(...)):
    """连接测试: 按端点类型轻量探活, 不跑大推理。
    llm_chat 型 (P2c) 走翻译 LLM 探活 (chat ping)。"""
    from .config import get_ocr_provider_cfg
    from .ocr.vl_api import VLApiAdapter
    try:
        cfg = get_ocr_provider_cfg(id)
    except RuntimeError as e:
        # llm_chat 型不在 OCR 解析链内, 直接查配置
        from .config import get_llm_providers
        llm = next((p for p in get_llm_providers() if p['id'] == id), None)
        if llm is None:
            return JSONResponse({"error": str(e)}, status_code=404)
        try:
            from .llm import chat
            _, model = chat([{"role": "user", "content": "ping"}],
                            provider_id=id, max_tokens=8)
            return {"ok": True, "detail": f"LLM 连通 ({model})"}
        except Exception as ex:
            return {"ok": False, "detail": f"LLM 探活失败: {ex}"}
    result = VLApiAdapter(cfg).test_connection()
    return result


@app.delete("/api/ocr-config/provider/{pid}")
async def api_delete_ocr_provider(pid: str):
    from .config import delete_ocr_provider
    if not delete_ocr_provider(pid):
        return JSONResponse({"error": f"provider 不存在: {pid}"},
                            status_code=404)
    return {"ok": True}


@app.post("/api/ocr-config/default")
async def api_set_ocr_default(id: str = Form(...),
                              scope: str = Form('ocr')):
    """设默认 provider。scope: 'ocr' (视觉) / 'llm' (翻译, P2c)。"""
    from .config import set_default_ocr_provider
    try:
        set_default_ocr_provider(id, scope=scope)
    except KeyError:
        return JSONResponse({"error": f"provider 不存在: {id}"},
                            status_code=404)
    return {"ok": True, "default": id}



# ── 搜索 ──────────────────────────────────────────────

def _pm_map(anchors, x, reverse=False):
    """page_map 分段线性换算. anchors: [[printed, pdf], ..] 升序.
    正向 printed->pdf (跳转); reverse=True pdf->printed (落库/反算).
    段内插值取整, 段外按相邻段斜率外推, 单锚点=固定偏移."""
    pts = sorted((a[1], a[0]) for a in anchors) if reverse else sorted(tuple(a) for a in anchors)
    if not pts:
        return x
    if len(pts) == 1:
        return int(x + (pts[0][1] - pts[0][0]))
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x <= x1:
            k = (y1 - y0) / (x1 - x0) if x1 != x0 else 1.0
            return int(round(y0 + (x - x0) * k))
    x0, y0 = pts[-2]
    x1, y1 = pts[-1]
    k = (y1 - y0) / (x1 - x0) if x1 != x0 else 1.0
    return int(round(y1 + (x - x1) * k))


def _load_page_map(conn, doc_id):
    row = conn.execute("SELECT page_map FROM documents WHERE id = ?",
                       (doc_id,)).fetchone()
    if row and row["page_map"]:
        try:
            v = json.loads(row["page_map"])
            if isinstance(v, list) and v:
                return v
            if isinstance(v, dict) and v.get("v") == 2 and v.get("groups"):
                return v
        except ValueError:
            pass
    return None


def _pm_groups(raw) -> list:
    """归一为分组列表 [{label, anchors}]. v1 [[printed,pdf],..] → 单组 label=''.
    多序列: 前言/正文各自从第 1 页起时, printed 会重复, 须分组各存各的."""
    if not raw:
        return []
    if isinstance(raw, dict) and raw.get("v") == 2:
        gs = []
        for g in raw.get("groups") or []:
            a = g.get("anchors") or []
            if a:
                gs.append({"label": str(g.get("label") or ""),
                           "anchors": [[int(x[0]), int(x[1])] for x in a]})
        return gs
    if isinstance(raw, list):
        return [{"label": "", "anchors": [[int(x[0]), int(x[1])] for x in raw]}]
    return []


def _pm_default(raw) -> str:
    if isinstance(raw, dict) and raw.get("v") == 2:
        return str(raw.get("default") or "")
    return ""


def _pm_v2(groups: list, default: str = "") -> dict:
    return {"v": 2, "groups": groups, "default": default}


def _pm_reverse(groups: list, pdf: int) -> int:
    """pdf 物理页 → 原书页 (落库方向). pdf 全书唯一, 各组 pdf 域不重叠:
    取覆盖域命中的组; 无命中取 pdf 距最近锚点的组; 组内分段线性."""
    if not groups:
        return pdf
    best = None
    for g in groups:
        pdfs = [a[1] for a in g["anchors"]]
        lo, hi = min(pdfs), max(pdfs)
        if lo <= pdf <= hi:
            best = g
            break
    if best is None:
        best = min(groups, key=lambda g: min(
            abs(pdf - a[1]) for a in g["anchors"]))
    return _pm_map(best["anchors"], pdf, reverse=True)


def _pm_forward(groups: list, default: str, printed: int) -> int:
    """原书页 → pdf 物理页 (跳转方向). printed 跨组可重复 (序/正文各有 p1):
    候选 = printed 覆盖域命中的组; default 组优先, 无命中组时按 default 外推."""
    if not groups:
        return printed
    cands = [g for g in groups
             if min(a[0] for a in g["anchors"]) <= printed
             <= max(a[0] for a in g["anchors"])]
    g = None
    for c in cands:
        if c["label"] == default:
            g = c
            break
    if g is None:
        g = cands[0] if cands else next(
            (c for c in groups if c["label"] == default), groups[0])
    return _pm_map(g["anchors"], printed)


def _card_fts_sync(conn, card_id: int):
    """卡内容变更后同步 cards_fts (reviewed 的 toc/entity 卡进索引, 其余移除)."""
    from .db import _card_flat_text
    conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card_id,))
    row = conn.execute(
        "SELECT dc.id, dc.doc_id, dc.kind, dc.content, dc.status, d.title "
        "FROM doc_cards dc JOIN documents d ON d.id = dc.doc_id "
        "WHERE dc.id = ?", (card_id,)).fetchone()
    if row and row["status"] == 'reviewed' and row["kind"] in ('toc', 'entity'):
        flat = _card_flat_text(row["kind"], row["content"])
        if flat:
            conn.execute(
                "INSERT INTO cards_fts(card_id, kind, doc_id, title, text) "
                "VALUES(?,?,?,?,?)",
                (row["id"], row["kind"], row["doc_id"], row["title"] or "", flat))


def _q_variants(token: str) -> list[str]:
    """查询段的繁简变体 (数量截断防组合爆炸)."""
    from .search import _all_variants
    vs = _all_variants(token)
    if len(vs) > 32:
        vs = {token}   # 长词放弃笛卡尔积, 依赖 trigram 的繁简不敏感场景靠用户输入形式
    return sorted(vs)


def _card_hits(collection: str, q: str, limit: int = 20) -> list:
    """reviewed 卡命中 (cards_fts, trigram 子串 + 繁简互转).

    整段 ≥3 字走 FTS 短语 (bm25 排序); 含 <3 字段时 INSTR 兜底 (卡表小, 全扫)。
    """
    tokens = [t for t in (q or '').split() if t]
    if not tokens:
        return []
    conn = get_conn(collection)
    try:
        rows, seen = [], set()
        long_tokens = [t for t in tokens if len(t) >= 3]
        if long_tokens:
            match_expr = ' AND '.join(
                '(' + ' OR '.join(f'"{v}"' for v in _q_variants(t)) + ')'
                for t in long_tokens)
            try:
                for r in conn.execute(
                        """SELECT card_id, kind, doc_id, title, text
                           FROM cards_fts WHERE cards_fts MATCH ?
                           ORDER BY bm25(cards_fts) LIMIT ?""",
                        (match_expr, limit * 2)).fetchall():
                    rows.append(r)
                    seen.add(r["card_id"])
            except Exception:
                pass
        if len(tokens) != len(long_tokens) or len(rows) < limit:
            # 短词兜底: 每个 token 至少一个繁简变体出现 (AND 语义)
            try:
                for r in conn.execute(
                        """SELECT card_id, kind, doc_id, title, text
                           FROM cards_fts LIMIT 1000""").fetchall():
                    if r["card_id"] in seen:
                        continue
                    tx = f"{r['title'] or ''} {r['text'] or ''}"
                    if all(any(v in tx for v in _q_variants(t))
                           for t in tokens):
                        rows.append(r)
                        if len(rows) >= limit:
                            break
            except Exception:
                pass
    finally:
        conn.close()
    hits = []
    for row in rows[:limit]:
        text = row["text"] or ""
        pos = -1
        for t in tokens:
            for v in _q_variants(t):
                p = text.find(v)
                if p >= 0:
                    pos = p
                    break
            if pos >= 0:
                break
        snippet = (text[max(0, pos - 40):pos + 120]
                   if pos >= 0 else text[:160])
        hits.append({
            "card_id": row["card_id"], "kind": row["kind"],
            "doc_id": row["doc_id"],
            "title": row["title"], "snippet": snippet[:200]})
    return hits


def _record_search_event(collection: str, query: str, total_docs: int,
                         entity_hits: int, wiki_hits: int, card_hits: int,
                         expanded: bool):
    """记录一次检索事件 (效用度量); 返回 event_id 供点击归因。"""
    try:
        conn = get_conn(collection)
        try:
            cur = conn.execute(
                "INSERT INTO search_events(query,total_docs,entity_hits,"
                " wiki_hits,card_hits,expanded) VALUES(?,?,?,?,?,?)",
                (query, total_docs, entity_hits, wiki_hits, card_hits,
                 1 if expanded else 0))
            eid = cur.lastrowid
            if eid and eid % 50 == 0:      # 轻量修剪: 保留最近 5000 条
                conn.execute(
                    "DELETE FROM search_events WHERE id <= "
                    "(SELECT COALESCE(MAX(id),0)-5000 FROM search_events)")
            conn.commit()
            return eid
        finally:
            conn.close()
    except Exception:
        return None


@app.post("/collections/{collection}/search-events/{eid}/click")
async def search_event_click(collection: str, eid: int):
    """标记该次检索产生了点击 (首次即生效)。"""
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    conn = get_conn(collection)
    try:
        conn.execute("UPDATE search_events SET clicked = 1 WHERE id = ?", (eid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/collections/{collection}/search-stats")
async def api_search_stats(collection: str, limit: int = 2000):
    """检索效用: 点击率、有/无实体命中时的点击率与提升度。"""
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    n = max(1, min(int(limit or 2000), 5000))
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            "SELECT total_docs, entity_hits, wiki_hits, card_hits, expanded, "
            "       clicked FROM search_events ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()
    finally:
        conn.close()

    def rate(sub, key='clicked'):
        if not sub:
            return None
        return round(sum(1 for r in sub if r[key]) / len(sub), 4)

    total = len(rows)
    with_e = [r for r in rows if r['entity_hits'] > 0]
    without_e = [r for r in rows if r['entity_hits'] == 0]
    ctr = rate(rows)
    ctr_e = rate(with_e)
    ctr_n = rate(without_e)
    lift = (round(ctr_e - ctr_n, 4)
            if ctr_e is not None and ctr_n is not None else None)
    return {
        "searches": total,
        "zero_hit": sum(1 for r in rows if r['total_docs'] == 0),
        "clicked": sum(1 for r in rows if r['clicked']),
        "ctr": ctr,
        "with_entity": len(with_e),
        "ctr_with_entity": ctr_e,
        "ctr_without_entity": ctr_n,
        "entity_lift": lift,
        "expanded": sum(1 for r in rows if r['expanded']),
    }


def _record_miss(collection: str, query: str, total_docs: int,
                 source: str = 'web'):
    """零命中查询记入检索问题库 (C1)。绝不抛异常影响检索。"""
    q = (query or '').strip()
    if not q or total_docs > 0:
        return
    try:
        conn = get_conn(collection)
        try:
            conn.execute(
                "INSERT INTO search_misses(query, hits, source) VALUES(?,1,?) "
                "ON CONFLICT(query) DO UPDATE SET "
                "  hits = hits + 1, last_seen = datetime('now')",
                (q, source))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


@app.get("/collections/{collection}/search")
async def api_search(collection: str, q: str, limit: int = 20,
                     offset: int = 0, source_type: str = None,
                     doc_id: int = None):
    """块级搜索 (rank 序, 分页). 响应含 total/has_more 供前端『加载更多』."""
    try:
        results = search(q, collection=collection, limit=limit,
                         offset=offset, source_type=source_type,
                         doc_id=doc_id)
        out = []
        for r in results:
            lines = get_block_lines(
                r["doc_id"], r["page_num"], r["block_num"], collection
            )
            text = " ".join(lines)
            line_id = _first_line_id_for_block(
                r["doc_id"], r["page_num"], r["block_num"], collection
            )
            out.append({
                "doc_id": r["doc_id"],
                "page_num": r["page_num"],
                "block_num": r["block_num"],
                "line_id": line_id,
                "filename": r.get("filename"),
                "title": r.get("title") or r.get("filename"),
                "cite_key": r.get("cite_key"),
                "text": _highlight_keyword(text, q),
            })
        total = count_hits(q, collection=collection,
                           source_type=source_type, doc_id=doc_id)["blocks"]
        if total == 0 and doc_id is None:
            _record_miss(collection, q, 0, 'web')
        return {"query": q, "collection": collection,
                "count": len(out), "results": out,
                "cards": _card_hits(collection, q),
                "wiki": _wiki_hits(collection, q),
                "total": total, "offset": offset, "limit": limit,
                "has_more": offset + len(out) < total}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/collections/{collection}/search-grouped")
async def api_search_grouped(collection: str, q: str,
                             source_type: str = None, limit: int = 200):
    """按文档聚合的搜索结果: 命中文献列表 + 每篇命中块数.

    前端分组视图用: 先展示全部命中文献, 展开单篇再调 /search?doc_id=.
    """
    try:
        groups = search_grouped(q, collection=collection,
                                source_type=source_type, limit=limit)
        stats = count_hits(q, collection=collection, source_type=source_type)
        if stats["docs"] == 0:
            _record_miss(collection, q, 0, 'web')
        from .search import expansions_for
        expanded = expansions_for(q, collection)
        cards = _card_hits(collection, q)
        wiki = _wiki_hits(collection, q)
        entities = _entity_hits(collection, q)
        event_id = _record_search_event(
            collection, q, stats["docs"], len(entities or []),
            len(wiki or []), len(cards or []), bool(expanded))
        return {"query": q, "collection": collection,
                "groups": groups,
                "expanded": expanded,
                "cards": cards,
                "wiki": wiki,
                "entities": entities,
                "event_id": event_id,
                "total_blocks": stats["blocks"], "total_docs": stats["docs"],
                "groups_truncated": len(groups) >= limit}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 检索未命中轨迹 (C1) ────────────────────────────────

@app.get("/collections/{collection}/search-misses")
async def search_misses_list(collection: str, include_resolved: int = 0,
                             limit: int = 200):
    """零命中查询沉淀列表 (未处理优先)。"""
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    conn = get_conn(collection)
    try:
        where = "" if include_resolved else "WHERE resolved = 0"
        rows = conn.execute(
            f"SELECT id, query, hits, source, note, resolved, "
            f"       created_at, last_seen FROM search_misses {where} "
            f"ORDER BY resolved ASC, last_seen DESC LIMIT ?",
            (max(1, min(int(limit or 200), 1000)),)).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/collections/{collection}/search-misses/{mid}/resolve")
async def search_miss_resolve(collection: str, mid: int,
                              resolved: int = Form(1),
                              note: str = Form(None)):
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            "UPDATE search_misses SET resolved = ?, "
            "  note = COALESCE(?, note) WHERE id = ?",
            (1 if resolved else 0, note, mid))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404,
                                content={"error": "记录不存在"})
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/collections/{collection}/search-misses/{mid}")
async def search_miss_delete(collection: str, mid: int):
    conn = get_conn(collection)
    try:
        conn.execute("DELETE FROM search_misses WHERE id = ?", (mid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/collections/{collection}/search-misses/{mid}/add-alias")
async def search_miss_add_alias(collection: str, mid: int,
                                entity: str = Form(...)):
    """把未命中查询词补为某实体的别名 (写 entity_additions, kind=alias)。

    entity 可为实体 ID 或规范名/别名。补录后 C2 别名扩展即可命中。
    """
    ent = (entity or '').strip()
    if not ent:
        return JSONResponse(status_code=400, content={"error": "请填实体名或 ID"})
    conn = get_conn(collection)
    try:
        mrow = conn.execute(
            "SELECT query FROM search_misses WHERE id = ?", (mid,)).fetchone()
        if not mrow:
            return JSONResponse(status_code=404, content={"error": "记录不存在"})
        q = mrow["query"]
        if ent.isdigit():
            er = conn.execute(
                "SELECT id, canonical, aliases FROM entities WHERE id = ?",
                (int(ent),)).fetchone()
        else:
            er = conn.execute(
                "SELECT id, canonical, aliases FROM entities "
                "WHERE canonical = ? OR aliases LIKE ? LIMIT 1",
                (ent, f'%"{ent}"%')).fetchone()
        if not er:
            return JSONResponse(status_code=404,
                                content={"error": "实体未找到: " + ent})
        try:
            aliases = json.loads(er["aliases"] or "[]")
        except (ValueError, TypeError):
            aliases = []
        if not isinstance(aliases, list):
            aliases = []
        changed = False
        if q != er["canonical"] and q not in aliases:
            aliases.append(q)
            conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                         (json.dumps(aliases, ensure_ascii=False), er["id"]))
            changed = True
        conn.execute(
            "INSERT INTO entity_additions(entity_id, kind, value, evidence) "
            "VALUES(?, 'alias', ?, ?)",
            (er["id"], q, f"检索未命中补录: {q}"))
        conn.execute(
            "UPDATE search_misses SET resolved = 1, "
            "  note = COALESCE(note, '') || ? WHERE id = ?",
            (f"已加为 {er['canonical']} 别名", mid))
        conn.commit()
        return {"ok": True, "entity_id": er["id"],
                "canonical": er["canonical"], "alias": q, "changed": changed}
    finally:
        conn.close()


# ── 单 collection 统计 ─────────────────────────────────

@app.get("/collections/{collection}/stats")
async def api_coll_stats(collection: str):
    try:
        conn = get_conn(collection)
        try:
            n_docs = conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            n_lines = conn.execute(
                "SELECT COUNT(*) FROM lines"
            ).fetchone()[0]
            n_blocks = conn.execute(
                "SELECT COUNT(*) FROM blocks_fts"
            ).fetchone()[0]
            n_ocr = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE doc_type = 'ocr'"
            ).fetchone()[0]
            n_bd = conn.execute(
                """SELECT COUNT(*) FROM documents
                   WHERE doc_type = 'born-digital' OR doc_type IS NULL"""
            ).fetchone()[0]
            source_tag_counts = {}
            for row in conn.execute(
                """SELECT je.value AS tag, COUNT(*) AS n
                   FROM documents, json_each(source_tags) je
                   GROUP BY je.value"""
            ):
                source_tag_counts[row["tag"]] = row["n"]
        finally:
            conn.close()
        return {
            "collection": collection,
            "documents": n_docs,
            "blocks": n_blocks,
            "lines": n_lines,
            "ocr_docs": n_ocr,
            "born_digital_docs": n_bd,
            "source_tags": source_tag_counts,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 全库统计 ──────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    """遍历所有 collection DB 求和"""
    try:
        total_docs = 0
        total_lines = 0
        total_blocks = 0
        total_ocr = 0
        total_bd = 0
        total_tags = {}
        coll_breakdown = []

        for coll in list_collections():
            try:
                conn = get_conn(coll)
                try:
                    n_docs = conn.execute(
                        "SELECT COUNT(*) FROM documents"
                    ).fetchone()[0]
                    n_lines = conn.execute(
                        "SELECT COUNT(*) FROM lines"
                    ).fetchone()[0]
                    n_blocks = conn.execute(
                        "SELECT COUNT(*) FROM blocks_fts"
                    ).fetchone()[0]
                    n_ocr = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE doc_type = 'ocr'"
                    ).fetchone()[0]
                    n_bd = conn.execute(
                        """SELECT COUNT(*) FROM documents
                           WHERE doc_type = 'born-digital' OR doc_type IS NULL"""
                    ).fetchone()[0]
                    for row in conn.execute(
                        """SELECT je.value AS tag, COUNT(*) AS n
                           FROM documents, json_each(source_tags) je
                           GROUP BY je.value"""
                    ):
                        total_tags[row["tag"]] = (
                            total_tags.get(row["tag"], 0) + row["n"]
                        )
                    pages = conn.execute(
                        "SELECT COALESCE(SUM(page_count), 0) FROM documents"
                    ).fetchone()[0]
                finally:
                    conn.close()
                total_docs += n_docs
                total_lines += n_lines
                total_blocks += n_blocks
                total_ocr += n_ocr
                total_bd += n_bd
                coll_breakdown.append({
                    "name": coll, "count": n_docs, "pages": pages,
                })
            except Exception:
                continue

        return {
            "documents": total_docs,
            "blocks": total_blocks,
            "lines": total_lines,
            "ocr_docs": total_ocr,
            "born_digital_docs": total_bd,
            "source_tags": total_tags,
            "collections": coll_breakdown,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 上下文 ────────────────────────────────────────────

@app.get("/collections/{collection}/context/{doc_id}/{page}/{block}")
async def api_context(collection: str, doc_id: int, page: int,
                      block: int, radius: int = 1):
    """返回命中块上下文，含 bbox / block_label"""
    try:
        ctx = get_context(doc_id, page, block,
                          radius=radius, collection=collection)
        if not ctx:
            return {"lines": []}

        # get_context 不返回 bbox/block_label，补查
        conn = get_conn(collection)
        try:
            extra = conn.execute(
                """SELECT block_num, line_num, bbox, block_label
                   FROM lines
                   WHERE doc_id = ? AND page_num = ?
                     AND block_num BETWEEN ? AND ?
                   ORDER BY block_num, line_num""",
                (doc_id, page, block - radius, block + radius),
            ).fetchall()
        finally:
            conn.close()

        extra_map = {
            (r["block_num"], r["line_num"]): r for r in extra
        }

        out = []
        for line in ctx:
            ex = extra_map.get(
                (line["block_num"], line["line_num"])
            )
            out.append({
                "block_num": line["block_num"],
                "line_num": line["line_num"],
                "text": line["text"],
                "bbox": ex["bbox"] if ex else None,
                "block_label": ex["block_label"] if ex else None,
            })
        return {"lines": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 上传 ──────────────────────────────────────────────

_SUPPORTED_IMG_EXT = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff', 'tif', 'gif'}
_OFFICE_EXT = {e.lstrip('.') for e in _OFFICE_EXTS}
_SUPPORTED_EXT = {'pdf', 'md', 'markdown'} | _SUPPORTED_IMG_EXT | _OFFICE_EXT


def _file_ext(filename: str) -> str:
    """返回小写扩展名（无点）。无扩展名返回 ''。"""
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[-1].lower()


def _cross_collection_hits(filename: str, current: str) -> list:
    """查同名文件存在于哪些其它书架 (仅提醒, 不阻断; 跨书架共存是设计行为)."""
    hits = []
    for c in list_collections():
        if c == current:
            continue
        try:
            conn = get_conn(c)
            try:
                row = conn.execute(
                    "SELECT id FROM documents WHERE filename = ?",
                    (filename,),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            continue
        if row:
            hits.append(c)
    return hits


@app.post("/collections/{collection}/add")
async def api_add(
    collection: str,
    files: List[UploadFile] = File(...),
    cite_key: str = Form(None),
    title: str = Form(None),
    author: str = Form(None),
    ocr: str = Form('auto'),
    source_tags: str = Form('["primary"]'),
):
    """批量上传：支持 pdf / md / word / 图片（图片自动 OCR）。

    - pdf + ocr 三态 (2026-10-17 智能分流):
      * 'auto' (默认): 探测文本层覆盖率 (前5页+中段3页抽样),
        >= 0.90 走原生管线 ingest_native_pdf (逐页 anydoc, 段落建块),
        否则走扫描件 OCR ingest_scanned_pdf
      * 'force_ocr' (旧值 'true' 兼容): 强制扫描件 OCR
      * 'force_text': 强制原生文本提取 (纯图件会得到空页提示)
    - md/markdown：按段落解析入库（doc_type='markdown'）
    - epub：spine 分页入库（每章 1 页，doc_type='epub'，校对台分章对照）
    - doc/docx/docm：anydoc 转 Markdown 入库（doc_type='office'）
    - 图片（jpg/png/...）：包成单页 PDF 后强制 OCR（doc_type='ocr'）
    - source_tags: JSON 数组字符串，如 '["primary","档案"]'
    返回 {status, results, skipped, errors}：
      - skipped: filename 已在本架的重复文件（不算错误）
      - results/skipped 条目带 cross_collection: 其它书架同名提醒
      - results 条目 pdf 带 note: auto 档分流说明
    """
    try:
        init_db(collection)
        upload_dir = get_upload_dir(collection)

        results = []
        skipped = []
        errors = []
        for file in files:
            fname = file.filename or "unknown"
            ext = _file_ext(fname)

            if ext not in _SUPPORTED_EXT:
                errors.append({
                    "filename": fname,
                    "error": f"不支持的类型 .{ext or '?'}（支持 pdf/md/word/图片）",
                })
                continue

            # 去重检查: 同 filename 已入库则跳过 (不算错误)
            conn = get_conn(collection)
            existing = conn.execute(
                "SELECT id FROM documents WHERE filename = ?",
                (fname,),
            ).fetchone()
            conn.close()
            if existing:
                skipped.append({
                    "filename": fname,
                    "reason": f"'{fname}' 已在书架「{collection}」中",
                    "doc_id": existing["id"],
                    "cross_collection": _cross_collection_hits(
                        fname, collection),
                })
                continue

            # 落盘
            dst = upload_dir / fname
            content = await file.read()
            with open(dst, "wb") as f:
                f.write(content)

            # 分派
            try:
                note = None
                if ext == 'pdf':
                    mode = str(ocr).strip().lower()
                    if mode in ('force_ocr', 'true'):   # 'true': 旧勾选值兼容
                        ingest_func = ingest_scanned_pdf
                    elif mode == 'force_text':
                        ingest_func = ingest_native_pdf
                        note = '强制原生文本提取'
                    else:                               # 'auto' 及其余值
                        ratio = await asyncio.to_thread(
                            probe_pdf_text_layer, dst)
                        if ratio >= 0.90:
                            ingest_func = ingest_native_pdf
                            note = (f'检测到文本层（抽样 {ratio:.0%}），'
                                    '走原生提取（anydoc 段落建块）')
                        else:
                            ingest_func = ingest_scanned_pdf
                            note = (f'未检测到文本层（抽样 {ratio:.0%}），'
                                    '走扫描件 OCR')
                elif ext in ('md', 'markdown'):
                    ingest_func = ingest_markdown
                elif ext == 'epub':
                    ingest_func = ingest_epub
                elif ext in _OFFICE_EXT:
                    ingest_func = ingest_office
                else:  # 图片：强制 OCR
                    ingest_func = ingest_image

                # OCR/解析为分钟级阻塞调用, 放线程池避免卡死事件循环
                r = await asyncio.to_thread(
                    ingest_func, dst,
                    collection=collection,
                    cite_key=cite_key or None,
                    title=title or None,
                    author=author or None,
                    source_tags=source_tags,
                )
                # 原件统一模型 (2026-10-28): 上传入库即把原件移入
                # files/<id>/original.<ext> (移入式, uploads 不留双份)
                from .ingest import attach_original
                await asyncio.to_thread(
                    attach_original, collection, r['doc_id'], dst, True)
                cross = _cross_collection_hits(fname, collection)
                entry = {
                    "filename": fname,
                    "result": r,
                    "cross_collection": cross,
                }
                if note is not None:
                    entry["note"] = note
                results.append(entry)
            except Exception as e:
                errors.append({"filename": fname, "error": str(e)})

        return {
            "status": "ok",
            "results": results,
            "skipped": skipped,
            "errors": errors,
            "total": len(files),
            "succeeded": len(results),
            "skipped_count": len(skipped),
            "failed": len(errors),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 删除 ──────────────────────────────────────────────

@app.delete("/collections/{collection}/doc/{doc_id}")
async def api_remove(collection: str, doc_id: int):
    try:
        remove_doc(doc_id, collection)
        return {"status": "ok", "doc_id": doc_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/purge-missing")
async def api_purge_missing(collection: str, request: Request):
    """批量清理「投放文件已消失」的文献 (2026-09-19, B 方案: 报告+一键清理).

    执行时重新计算缺失清单 (不用扫描周期的陈旧状态); >100 条需 ?confirm=1
    (防目录挪走/挂载脱落导致一键清库).
    """
    try:
        from .folder_scan import _missing
        missing = _missing(collection)
        if len(missing) > 100 and request.query_params.get('confirm') != '1':
            return JSONResponse(status_code=409, content={
                "error": f"缺失 {len(missing)} 条 (>100), 疑似目录不可用; "
                         f"确认无误请加 ?confirm=1 重试"})
        purged, skipped = [], []
        for m in missing:
            try:
                remove_doc(m['id'], collection)
                purged.append(m['filename'])
            except Exception:
                skipped.append(m['filename'])
        return {"status": "ok", "purged": purged,
                "purged_count": len(purged), "skipped": skipped}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})



# 行制推断 (P2 行制扩展): 1=中文单行 / 3=拉丁化三行 / 4=原语四行.
# tri_rows 显式值优先; 否则按语种集推断 (日/俄/韩/希腊等活文字系统 → 4 行).
FOUR_ROW_LANGS = {"ja", "jpn", "ru", "rus", "ko", "kor", "el", "ell", "grc", "gre"}


def _tri_rows_for(doc) -> int:
    if doc is None:
        return 1
    keys = doc.keys()
    lang = (doc["lang"] or "zh") if "lang" in keys else "zh"
    if lang == "zh":
        return 1
    if "tri_rows" in keys and doc["tri_rows"] in (3, 4):
        return doc["tri_rows"]
    return 4 if lang in FOUR_ROW_LANGS else 3

@app.patch("/collections/{collection}/doc/{doc_id}")
async def api_update_doc(collection: str, doc_id: int,
                         source_tags: str = Form(None),
                         bib_type: str = Form(None),
                         bib_data: str = Form(None),
                         lang: str = Form(None),
                         tri_rows: int = Form(None),
                         linked_pdf: str = Form(None),
                         unlink_pdf: bool = Form(False)):
    """更新文献元数据。

    可选字段（至少传一个）:
    - source_tags: JSON 数组字符串（来源标签）
    - bib_type: 文献类型（任意标准 BibTeX 类型, 如 @book/@article/@phdthesis 等 15 类）
    - bib_data: JSON 对象字符串（biblatex 字段）
    - lang: 语种（zh=中文单行制; 其他=非中文行制, 如 eng/mnc/ja, 2026-10-17 P2）
    - tri_rows: 行制显式覆盖（1=单行/3=三行/4=四行; 日/俄类语种默认四行）
    - linked_pdf: 原件相对路径（files/ 前缀, 限本书架内; 置空=解除关联）
    - unlink_pdf: true 表示解除关联（与 linked_pdf 互斥; 空串等同未提供）

    2026-10-31 单布局收敛: linked_pdf 不再接受裸文件名（旧 uploads 语义）;
    补挂/更换原件请走 POST .../doc/{id}/link-pdf（自动存入 files/<id>/）。
    """
    try:
        if (source_tags is None and bib_type is None and bib_data is None
                and lang is None and tri_rows is None and linked_pdf is None
                and not unlink_pdf):
            return JSONResponse(
                status_code=400,
                content={"error": "未提供可更新字段"},
            )

        updates = []
        params = []
        result_extra = {}

        # ── linked_pdf (原件路径可写; 单布局: 仅本书架 files/ 前缀) ──
        if linked_pdf and unlink_pdf:
            return JSONResponse(
                status_code=400,
                content={"error": "linked_pdf 与 unlink_pdf 不能同时提供"},
            )
        if unlink_pdf:
            updates.append("linked_pdf = ?")
            params.append(None)
            result_extra["linked_pdf"] = None
        elif linked_pdf is not None:
            lp = linked_pdf.strip()
            if not lp:
                updates.append("linked_pdf = ?")
                params.append(None)
                result_extra["linked_pdf"] = None
            elif (not lp.startswith("files/") or "\\" in lp
                    or ".." in lp or "/../" in f"/{lp}/"):
                return JSONResponse(
                    status_code=400,
                    content={"error": "linked_pdf 只接受本书架 files/ 前缀的"
                                      "原件路径 (如 files/12/original.pdf);"
                                      "补挂原件请用 link-pdf 端点"},
                )
            else:
                files_root = (get_collections_dir() / collection / "files").resolve()
                lp_path = (get_collections_dir() / collection / lp).resolve()
                if files_root not in lp_path.parents:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "linked_pdf 越出书架 files/ 目录"},
                    )
                if not lp_path.is_file():
                    return JSONResponse(
                        status_code=400,
                        content={"error": f"原件不存在: {lp}"},
                    )
                updates.append("linked_pdf = ?")
                params.append(lp)
                result_extra["linked_pdf"] = lp
                result_extra["linked_pdf_exists"] = True

        # ── lang (P2 两制转录) ──
        if lang is not None:
            lang_v = lang.strip().lower() or "zh"
            if not lang_v.isascii() or not lang_v.replace("-", "").isalnum():
                return JSONResponse(
                    status_code=400,
                    content={"error": "lang 只接受字母/连字符 (如 zh/eng/mnc)"},
                )
            updates.append("lang = ?")
            params.append(lang_v)
            result_extra["lang"] = lang_v

        # ── tri_rows (P2 行制扩展: 1=单行/3=三行/4=四行, 显式覆盖语种推断) ──
        if tri_rows is not None:
            if tri_rows not in (1, 3, 4):
                return JSONResponse(
                    status_code=400,
                    content={"error": "tri_rows 只接受 1(单行)/3(三行)/4(四行)"},
                )
            updates.append("tri_rows = ?")
            params.append(tri_rows)
            result_extra["tri_rows"] = tri_rows

        # ── source_tags ──
        if source_tags is not None:
            tags = json.loads(source_tags)
            if not isinstance(tags, list) or not tags:
                return JSONResponse(
                    status_code=400,
                    content={"error": "source_tags 必须是非空 JSON 数组"},
                )
            tags = [str(t).strip() for t in tags if str(t).strip()]
            if not tags:
                return JSONResponse(
                    status_code=400,
                    content={"error": "source_tags 不能全为空"},
                )
            tags_json = json.dumps(tags)
            is_p = 1 if "primary" in tags else 0
            is_s = 1 if "secondary" in tags else 0
            is_r = 1 if "reference" in tags else 0
            updates.extend([
                "source_tags = ?", "is_primary = ?",
                "is_secondary = ?", "is_reference = ?",
            ])
            params.extend([tags_json, is_p, is_s, is_r])
            result_extra["source_tags"] = tags

        # ── bib_type + bib_data ──
        if bib_type is not None or bib_data is not None:
            conn = get_conn(collection)
            try:
                row = conn.execute(
                    "SELECT cite_key, bib_type, bib_data, filename"
                    " FROM documents WHERE id = ?",
                    (doc_id,),
                ).fetchone()
                if row is None:
                    return JSONResponse(
                        status_code=404,
                        content={"error": f"文献 id={doc_id} 不存在"},
                    )
                cur_bib_type = bib_type if bib_type is not None else row["bib_type"]
                cur_bib_data_str = bib_data if bib_data is not None else row["bib_data"]
                cur_bib_data = parse_bib_data(cur_bib_data_str) or {}

                # 同步 title/author 冗余列
                synced = sync_doc_fields(cur_bib_data)
                updates.extend(["title = ?", "author = ?"])
                params.extend([synced["title"], synced["author"]])
                result_extra["title"] = synced["title"]
                result_extra["author"] = synced["author"]

                # 自动重算 cite_key
                existing_rows = conn.execute(
                    "SELECT cite_key FROM documents WHERE id != ?",
                    (doc_id,),
                ).fetchall()
                existing_keys = {
                    r["cite_key"] for r in existing_rows if r["cite_key"]
                }
                new_cite_key = generate_cite_key(
                    cur_bib_data, existing_keys,
                    exclude_key=row["cite_key"],
                    fingerprint=row["filename"],
                )
                updates.append("cite_key = ?")
                params.append(new_cite_key)
                result_extra["cite_key"] = new_cite_key

                if bib_type is not None:
                    updates.append("bib_type = ?")
                    params.append(cur_bib_type or None)
                    result_extra["bib_type"] = cur_bib_type
                if bib_data is not None:
                    updates.append("bib_data = ?")
                    params.append(json.dumps(cur_bib_data, ensure_ascii=False))
                    result_extra["bib_data"] = cur_bib_data
            finally:
                conn.close()

        params.append(doc_id)
        sql = f"UPDATE documents SET {', '.join(updates)} WHERE id = ?"

        conn = get_conn(collection)
        try:
            cur = conn.execute(sql, params)
            if cur.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"文献 id={doc_id} 不存在"},
                )
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "doc_id": doc_id, **result_extra}
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": "JSON 解析失败"},
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/doc/{doc_id}/link-pdf")
async def api_link_pdf(collection: str, doc_id: int,
                       file: UploadFile = File(None)):
    """为文档补挂原件（2026-10-28 原件统一模型; 兼容旧「关联 PDF」前端）。

    原件存 files/<doc_id>/original.<ext>，linked_pdf 字段记相对路径，
    同时清除 original_virtual。任意常见类型均可（pdf/md/docx/epub/图片）。
    """
    try:
        fname = file.filename or "original.pdf"
        ext = _file_ext(fname)
        allowed = {'pdf', 'md', 'markdown', 'epub', 'doc', 'docx', 'docm',
                   'png', 'jpg', 'jpeg', 'webp', 'tif', 'tiff'}
        if ext not in allowed:
            return JSONResponse(
                status_code=400,
                content={"error": f"不支持的文件类型: {ext}"},
            )

        content = await file.read()
        from .ingest import attach_original as _attach
        from .config import get_original_dir
        dst_dir = get_original_dir(collection, doc_id)
        dst = dst_dir / f"original{'.md' if ext == 'markdown' else '.' + ext}"
        with open(dst, "wb") as f:
            f.write(content)

        page_count = 1
        if dst.suffix.lower() == '.pdf':
            try:
                pdf_doc = pymupdf.open(dst)
                page_count = len(pdf_doc)
                pdf_doc.close()
            except Exception:
                pass

        conn = get_conn(collection)
        try:
            cur = conn.execute(
                "UPDATE documents SET linked_pdf = ?, original_virtual = 0 "
                "WHERE id = ?",
                (f"files/{doc_id}/original{dst.suffix.lower()}", doc_id),
            )
            if cur.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"文献 id={doc_id} 不存在"},
                )
            conn.commit()
        finally:
            conn.close()

        return {
            "status": "ok",
            "doc_id": doc_id,
            "linked_pdf": f"files/{doc_id}/original{dst.suffix.lower()}",
            "page_count": page_count,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/collections/{collection}/doc/{doc_id}/original")
async def api_original_file(collection: str, doc_id: int):
    """返回文献原件（inline 预览; 2026-10-28 原件统一模型）。"""
    from .ingest import get_original_file
    from .config import resolve_original_path
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            "SELECT linked_pdf FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    finally:
        conn.close()
    if doc is None:
        return JSONResponse(status_code=404, content={"error": "文献不存在"})
    p = resolve_original_path(collection, doc["linked_pdf"])
    if not p or not p.exists():
        return JSONResponse(status_code=404, content={"error": "无原件"})
    media = {
        '.pdf': 'application/pdf', '.md': 'text/plain; charset=utf-8',
        '.epub': 'application/epub+zip', '.png': 'image/png',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.html': 'text/html',
    }.get(p.suffix.lower(), 'application/octet-stream')
    return FileResponse(str(p), media_type=media)


# ── 文献列表 ──────────────────────────────────────────

@app.get("/collections/{collection}/docs")
async def api_docs(collection: str, limit: int = 50):
    try:
        conn = get_conn(collection)
        try:
            rows = conn.execute(
                """SELECT id, cite_key, title, author,
                          filename, page_count, doc_type, linked_pdf,
                          is_primary, is_secondary, is_reference,
                          created_at
                   FROM documents
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "count": len(rows),
            "docs": [dict(r) for r in rows],
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/collections/{collection}/docs/list")
async def docs_list_page(request: Request, collection: str):
    """文献列表独立页面。"""
    collections = list_collections()
    if collection not in collections:
        return RedirectResponse(url="/")
    tags = set()
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            "SELECT DISTINCT value FROM documents, "
            "json_each(documents.source_tags)"
        ).fetchall()
        for r in rows:
            tags.add(r[0])
    except Exception:
        pass
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "docs_list.html",
        _nav_ctx("docs", collection,
                 source_tags=sorted(tags),
                 bib_type_labels=BIB_TYPE_LABELS,
                 scan_status=get_scan_status(collection)),
    )


@app.get("/collections/{collection}/upload")
async def upload_page(request: Request, collection: str):
    """资料上传页 (Tab: 上传文件 / 导入数据包)."""
    if collection not in list_collections():
        return RedirectResponse(url="/")
    return templates.TemplateResponse(
        request,
        "upload.html",
        _nav_ctx("upload", collection),
    )


@app.get("/collections/{collection}/doc/{doc_id}/preview")
async def preview_page(request: Request, collection: str, doc_id: int):
    """纯文本预览页 (page→block→line + source-tag, 无 bib)."""
    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                """SELECT id, title, author, filename, page_count, doc_type,
                          source_tags, cite_key, linked_pdf, original_virtual
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return templates.TemplateResponse(
                request,
                "preview.html",
                _nav_ctx("", collection, error="文献不存在"),
                status_code=404,
            )
        doc_d = dict(doc)
        try:
            tags = json.loads(doc_d.get("source_tags") or '["primary"]')
            if not isinstance(tags, list) or not tags:
                tags = ["primary"]
        except (json.JSONDecodeError, TypeError):
            tags = ["primary"]
        doc_d["source_tags"] = tags
        return templates.TemplateResponse(
            request,
            "preview.html",
            _nav_ctx("", collection, doc=doc_d, doc_id=doc_id),
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "preview.html",
            _nav_ctx("", collection, error=str(e)),
            status_code=500,
        )


@app.get("/collections/{collection}/docs/query")
async def api_docs_query(
    collection: str,
    request: Request,
    page: int = 1,
    limit: int = 20,
    sort: str = "created_at",
    order: str = "desc",
    tag: str = None,
    author: str = None,
    bib_type: str = None,
    title: str = None,
    year: str = None,
):
    """分页、排序、筛选文献列表（JSON）。"""
    try:
        conn = get_conn(collection)
        try:
            conditions = ["1=1"]
            params = []

            if tag:
                conditions.append(
                    "EXISTS(SELECT 1 FROM json_each(d.source_tags) "
                    "WHERE value = ?)"
                )
                params.append(tag)
            if author:
                conditions.append("d.author LIKE ?")
                params.append(f"%{author}%")
            if bib_type:
                conditions.append("d.bib_type = ?")
                params.append(bib_type)
            if title:
                conditions.append("d.title LIKE ?")
                params.append(f"%{title}%")
            if year:
                conditions.append(
                    "substr(COALESCE(json_extract(d.bib_data, '$.date'), "
                    "json_extract(d.bib_data, '$.year')), 1, 4) = ?"
                )
                params.append(year)

            where = " AND ".join(conditions)

            valid_sorts = {"created_at", "title", "author", "cite_key",
                           "page_count"}
            sort_col = sort if sort in valid_sorts else "created_at"
            order_dir = "DESC" if order.upper() == "DESC" else "ASC"

            offset = (page - 1) * limit
            if offset < 0:
                offset = 0
            if limit < 1:
                limit = 20

            total = conn.execute(
                f"SELECT COUNT(*) FROM documents d WHERE {where}", params
            ).fetchone()[0]

            sql = f"""SELECT d.id, d.cite_key, d.title, d.author,
                             d.filename, d.page_count, d.doc_type,
                             d.source_tags, d.bib_type, d.bib_data,
                             d.created_at
                      FROM documents d
                      WHERE {where}
                      ORDER BY d.{sort_col} {order_dir}
                      LIMIT ? OFFSET ?"""
            rows = conn.execute(sql, params + [limit, offset]).fetchall()

            # OCR 页状态聚合 (表可能未迁移, 防御)
            state_map = {}
            doc_ids = [r["id"] for r in rows]
            if doc_ids:
                try:
                    ph = ",".join("?" * len(doc_ids))
                    for sr in conn.execute(
                            f"SELECT doc_id, "
                            f"SUM(status='done') AS dn, "
                            f"SUM(status='error') AS en, "
                            f"GROUP_CONCAT(CASE WHEN status='error' "
                            f"THEN page_num END) AS ep "
                            f"FROM ocr_page_state WHERE doc_id IN ({ph}) "
                            f"GROUP BY doc_id", doc_ids):
                        errs = sorted(
                            int(x) for x in (sr["ep"] or "").split(",") if x)
                        state_map[sr["doc_id"]] = {
                            "done": sr["dn"] or 0, "error": sr["en"] or 0,
                            "errors": errs}
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

        docs = []
        for r in rows:
            d = dict(r)
            d["ocr_state"] = state_map.get(d["id"])
            docs.append(d)

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
            "docs": docs,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/export-bib")
async def api_export_bib(collection: str, request: Request):
    """批量导出 BibTeX（ids 为空时导出全部）。"""
    try:
        body = await request.json()
        ids = body.get("ids", [])

        conn = get_conn(collection)
        try:
            if ids:
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"""SELECT id, cite_key, bib_type, bib_data
                        FROM documents
                        WHERE id IN ({placeholders})""",
                    ids,
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, cite_key, bib_type, bib_data
                        FROM documents
                        ORDER BY id"""
                ).fetchall()
        finally:
            conn.close()

        parts = []
        for r in rows:
            bib_data = parse_bib_data(r["bib_data"]) or {}
            parts.append(to_bibtex(r["cite_key"], r["bib_type"], bib_data))

        content = "\n".join(parts)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/x-bibtex",
            headers={
                "Content-Disposition": "attachment; filename=xsf_export.bib"
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _md_frontmatter(doc) -> str:
    """从 doc 行生成 YAML frontmatter (cite_key + bib_type + bib_data 字段).

    无任何书目数据时返回空串 (保持文件干净)。
    值用 json.dumps 序列化 — JSON 字符串是合法 YAML, 自动处理引号/冒号转义。
    """
    fields = {}
    if doc["cite_key"]:
        fields["cite_key"] = doc["cite_key"]
    if doc["bib_type"]:
        fields["bib_type"] = doc["bib_type"]
    bib = parse_bib_data(doc["bib_data"]) or {}
    for k, v in bib.items():
        if v:
            fields[k] = v
    if not fields:
        return ""
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


@app.post("/collections/{collection}/docs/export-md")
async def api_export_md(collection: str, request: Request):
    """批量导出 Markdown（每篇一个文件，打包 zip）。"""
    try:
        body = await request.json()
        ids = body.get("ids", [])
        if not ids:
            return JSONResponse(status_code=400,
                                content={"error": "未选择文献"})

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            conn = get_conn(collection)
            try:
                placeholders = ",".join("?" * len(ids))
                docs = conn.execute(
                    f"""SELECT id, cite_key, title, author,
                               bib_type, bib_data
                        FROM documents
                        WHERE id IN ({placeholders})""",
                    ids,
                ).fetchall()

                for doc in docs:
                    doc_id = doc["id"]
                    rows = conn.execute(
                        """SELECT text, page_num, block_num FROM lines
                           WHERE doc_id = ?
                           ORDER BY page_num, block_num, line_num""",
                        (doc_id,),
                    ).fetchall()
                    blocks = []
                    for r in rows:
                        if not r["text"]:
                            continue
                        key = (r["page_num"], r["block_num"])
                        if not blocks or blocks[-1]["key"] != key:
                            blocks.append({"key": key, "lines": []})
                        blocks[-1]["lines"].append(r["text"])
                    content = "\n\n".join(
                        "\n".join(b["lines"]) for b in blocks
                    )
                    fm = _md_frontmatter(doc)
                    header = (doc["title"] or doc["cite_key"]
                              or f"doc_{doc_id}")
                    md = f"# {header}\n\n"
                    if doc["author"]:
                        md += f"**作者**: {doc['author']}\n\n"
                    md += content + "\n"
                    if fm:
                        md = fm + md
                    filename = (
                        f"{doc['cite_key'] or f'doc_{doc_id}'}.md"
                    )
                    zf.writestr(filename, md.encode("utf-8"))
            finally:
                conn.close()

        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=xsf_docs.zip"
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/match-bib")
async def api_match_bib(collection: str, request: Request):
    """批量匹配: 上传 .bib 文本 + 选中 doc_ids → 返回匹配结果."""
    try:
        body = await request.json()
        bib_text = body.get("bib_text", "")
        doc_ids = body.get("doc_ids", [])
        if not bib_text or not doc_ids:
            return JSONResponse(
                status_code=400,
                content={"error": "需要 bib_text 和 doc_ids"})

        entries = parse_bib_entries(bib_text)
        if not entries:
            return JSONResponse(
                status_code=400,
                content={"error": ".bib 文件中未找到有效条目"})

        conn = get_conn(collection)
        try:
            placeholders = ",".join("?" * len(doc_ids))
            rows = conn.execute(
                f"SELECT id, title, filename FROM documents WHERE id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        finally:
            conn.close()

        docs = [dict(r) for r in rows]
        result = match_docs_to_entries(docs, entries)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/batch-patch")
async def api_batch_patch(collection: str, request: Request):
    """批量应用书目元数据."""
    try:
        body = await request.json()
        updates = body.get("updates", [])
        if not updates:
            return {"applied": 0, "errors": []}

        conn = get_conn(collection)
        errors = []
        applied = 0
        try:
            existing_keys = {
                r[0] for r in conn.execute(
                    "SELECT cite_key FROM documents WHERE cite_key IS NOT NULL"
                ).fetchall()
            }
            filenames = {
                r["id"]: r["filename"] for r in conn.execute(
                    "SELECT id, filename FROM documents"
                ).fetchall()
            }
            for u in updates:
                doc_id = u.get("doc_id")
                bib_type = u.get("bib_type", "")
                bib_data = u.get("bib_data", {})
                if not doc_id or not bib_data:
                    continue
                ck = generate_cite_key(
                    bib_data, existing_keys,
                    exclude_key=u.get("old_cite_key"),
                    fingerprint=filenames.get(doc_id),
                )
                existing_keys.add(ck)
                synced = sync_doc_fields(bib_data)
                conn.execute(
                    """UPDATE documents
                       SET cite_key=?, bib_type=?, bib_data=?,
                           title=COALESCE(?, title), author=COALESCE(?, author)
                       WHERE id=?""",
                    (ck, bib_type, json.dumps(bib_data, ensure_ascii=False),
                     synced["title"], synced["author"], doc_id),
                )
                applied += 1
            conn.commit()
        finally:
            conn.close()
        return {"applied": applied, "errors": errors}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/batch-tags")
async def api_batch_tags(collection: str, request: Request):
    """批量修改来源标签 (source_tags)。

    body: {"ids": [...], "action": "set|add|remove", "tags": [...]}
    - set: 替换整个标签集
    - add: 与原标签并集
    - remove: 从原标签移除; 移空回落 ["primary"] (与 _migrate/_source_flags 兜底一致)
    同步派生 is_primary/is_secondary/is_reference 三列。
    """
    try:
        body = await request.json()
        ids = body.get("ids", [])
        action = body.get("action", "")
        tags = body.get("tags", [])

        if not isinstance(ids, list) or not ids:
            return JSONResponse(status_code=400, content={"error": "未选择文献"})
        if action not in ("set", "add", "remove"):
            return JSONResponse(
                status_code=400,
                content={"error": "action 必须是 set/add/remove"},
            )
        if not isinstance(tags, list) or not tags:
            return JSONResponse(status_code=400, content={"error": "tags 必须是非空数组"})
        tags = [str(t).strip() for t in tags if str(t).strip()]
        if not tags:
            return JSONResponse(status_code=400, content={"error": "tags 不能全为空"})
        tags = list(dict.fromkeys(tags))

        conn = get_conn(collection)
        updated = 0
        try:
            for doc_id in ids:
                row = conn.execute(
                    "SELECT source_tags FROM documents WHERE id = ?",
                    (doc_id,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    current = json.loads(row["source_tags"] or "[]")
                    if not isinstance(current, list):
                        current = ["primary"]
                except (json.JSONDecodeError, TypeError):
                    current = ["primary"]
                current = [str(t).strip() for t in current if str(t).strip()]
                if not current:
                    current = ["primary"]

                if action == "set":
                    new_tags = tags
                elif action == "add":
                    new_tags = current + [t for t in tags if t not in current]
                else:  # remove
                    new_tags = [t for t in current if t not in tags] or ["primary"]

                is_p = 1 if "primary" in new_tags else 0
                is_s = 1 if "secondary" in new_tags else 0
                is_r = 1 if "reference" in new_tags else 0
                conn.execute(
                    """UPDATE documents
                       SET source_tags=?, is_primary=?, is_secondary=?, is_reference=?
                       WHERE id=?""",
                    (json.dumps(new_tags, ensure_ascii=False),
                     is_p, is_s, is_r, doc_id),
                )
                updated += 1
            conn.commit()
        finally:
            conn.close()
        return {"updated": updated}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/export-archive")
async def api_export_archive(collection: str, request: Request):
    """导出选中文献为语料包 (.sqpkg, tar.gz)。

    P3 双通道 (body: {ids, pkg_type}):
    - doc 包: 全量 (documents + lines + blocks_fts + 全部修订史 + uploads/)
    - changeset 包: 变更增量 (documents 元数据 + 仅 in_pkg=1 修订史, 无 lines/uploads)
    manifest v2: {format, version:2, pkg_type, collection, exported_at,
                  docs:[{cite_key,filename,lang,tri_rows,page_count,base_version}],
                  edits_count}; export_log 记台账。
    v1 旧格式 (manifest 无 version) 仍可导入。
    """
    import datetime as _dt

    try:
        body = await request.json()
        ids = body.get("ids", [])
        pkg_type = body.get("pkg_type", "doc")
        if not ids:
            return JSONResponse(status_code=400,
                                content={"error": "未选择文献"})
        if pkg_type not in ("doc", "changeset"):
            return JSONResponse(status_code=400,
                                content={"error": "pkg_type 只接受 doc/changeset"})

        db_path = get_db_path(collection)
        if not db_path.exists():
            return JSONResponse(status_code=404,
                                content={"error": "书架不存在"})

        placeholders = ",".join("?" * len(ids))

        # 1. SQLite backup 到临时 DB，然后按包型清理
        fd, tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        src_conn = get_conn(collection)
        try:
            backup_conn = sqlite3.connect(tmp_db)
            with src_conn:
                src_conn.backup(backup_conn)
            backup_conn.close()

            clean = sqlite3.connect(tmp_db)
            clean.row_factory = sqlite3.Row
            # 文档元信息 (manifest + 均含 documents 表)
            doc_meta = clean.execute(
                f"""SELECT id, cite_key, filename, page_count, linked_pdf
                    FROM documents
                    WHERE id IN ({placeholders}) ORDER BY id""", ids,
            ).fetchall()
            # base_version = 导出时该文献全部修订数 (清理前取, 变更增量基准)
            base_versions = dict(
                (r[0], r[1]) for r in clean.execute(
                    f"""SELECT doc_id, COUNT(*) FROM revision_log
                        WHERE doc_id IN ({placeholders}) GROUP BY doc_id""",
                    ids).fetchall())

            if pkg_type == "changeset":
                # 变更包: 只保留选中文献的 documents + in_pkg=1 修订史
                clean.execute("DELETE FROM lines")
                clean.execute("DELETE FROM blocks_fts")
                clean.execute(
                    f"""DELETE FROM revision_log
                        WHERE doc_id NOT IN ({placeholders})
                           OR in_pkg != 1""", ids)
                clean.execute(
                    f"DELETE FROM documents WHERE id NOT IN ({placeholders})",
                    ids)
            else:
                # 文献包: 全量, 但清到选中文献 (含修订史同步过滤)
                clean.execute(
                    f"DELETE FROM lines WHERE doc_id NOT IN ({placeholders})",
                    ids)
                clean.execute(
                    f"DELETE FROM blocks_fts WHERE doc_id NOT IN ({placeholders})",
                    ids)
                clean.execute(
                    f"DELETE FROM revision_log WHERE doc_id NOT IN ({placeholders})",
                    ids)
                clean.execute(
                    f"DELETE FROM documents WHERE id NOT IN ({placeholders})",
                    ids)
            clean.commit()

            # 单布局收敛 (2026-10-31): 原件从 files/<id>/ 取; 归档仍为
            # uploads/ 平铺 (石渠耦合面不变)。包内 DB 的 linked_pdf 归一化
            # 为归档内实际文件名 (旧裸名语义, 包自洽); 无原件 → NULL+virtual
            # (changeset 包本就无文件, 全部落 virtual)。
            # 扩展名错配 (md 文献挂 pdf 原件等): 按 {id}-{原件名} 归档,
            # 旧消费者配不上 (与 10-28 前一致), 我方导入按裸名优先配对。
            from .config import resolve_original_path
            arc_map = {}   # doc_id → (归档名 | None, 原件路径)
            for r in doc_meta:
                p = resolve_original_path(collection, r["linked_pdf"])
                if p and p.is_file():
                    same_ext = (p.suffix.lstrip('.').lower()
                                == _file_ext(r["filename"] or ""))
                    arc = (r["filename"] if same_ext
                           else f"{r['id']}-{p.name}")
                    arc_map[r["id"]] = (arc, p)
                    clean.execute(
                        "UPDATE documents SET linked_pdf = ? WHERE id = ?",
                        (arc, r["id"]))
                else:
                    arc_map[r["id"]] = (None, None)
                    clean.execute(
                        "UPDATE documents SET linked_pdf = NULL, "
                        "original_virtual = 1 WHERE id = ?",
                        (r["id"],))
            clean.commit()

            edits_count = clean.execute("SELECT COUNT(*) FROM revision_log"
                                        ).fetchone()[0]
            clean.close()
        finally:
            src_conn.close()

        # 2. 打包 .sqpkg (tar.gz)
        fd, tmp_tar = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        doc_count = len(doc_meta)
        with tarfile.open(tmp_tar, "w:gz") as tar:
            tar.add(tmp_db, arcname="export.db")
            manifest = {
                "format": "sqpkg",
                "version": 2,
                "pkg_type": pkg_type,
                "collection": collection,
                "exported_at": _dt.datetime.now().isoformat(),
                "doc_count": doc_count,
                "edits_count": edits_count,
                "docs": [
                    {
                        "id": r["id"],
                        "cite_key": r["cite_key"],
                        "filename": r["filename"],
                        "page_count": r["page_count"],
                        "base_version": base_versions.get(r["id"], 0),
                    }
                    for r in doc_meta
                ],
                "filenames": [r["filename"] for r in doc_meta],
            }
            manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2)
            manifest_bytes = manifest_data.encode("utf-8")
            mi = tarfile.TarInfo(name="manifest.json")
            mi.size = len(manifest_bytes)
            tar.addfile(mi, io.BytesIO(manifest_bytes))
            if pkg_type == "doc":
                for r in doc_meta:
                    arc, p = arc_map.get(r["id"], (None, None))
                    if arc and p:
                        tar.add(str(p), arcname=f"uploads/{arc}")

        os.unlink(tmp_db)

        # 3. 台账
        conn = get_conn(collection)
        try:
            stamp = _dt.datetime.now().strftime("%Y%m%d")
            fname = (f"{collection}_{stamp}_{doc_count}docs.sqpkg"
                     if pkg_type == "doc"
                     else f"{collection}_{stamp}_{edits_count}edits.sqpkg")
            conn.execute(
                """INSERT INTO export_log (pkg_type, doc_ids, filename, edits_count)
                   VALUES (?, ?, ?, ?)""",
                (pkg_type, json.dumps(ids), fname, edits_count),
            )
            conn.commit()
        finally:
            conn.close()

        with open(tmp_tar, "rb") as f:
            content = f.read()
        os.unlink(tmp_tar)

        # Content-Disposition 只允许 latin-1: 中文书架名走 RFC 6266 双写法
        from urllib.parse import quote
        ascii_name = f"xsf_{pkg_type}_{stamp}.sqpkg"
        disp = (f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(fname, safe='')}")
        return Response(
            content=content,
            media_type="application/gzip",
            headers={"Content-Disposition": disp},
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/import-archive")
async def api_import_archive(collection: str, file: UploadFile = File(...),
                             force: int = 0):
    """导入语料包 (.sqpkg / 旧 tar.gz) 到当前书架。

    P3 语义:
    - doc 包 (manifest v2 pkg_type=doc / v1 旧包): 全量导入 (documents+lines
      +blocks_fts+revision_log 联动重映射); **cite_key 幂等** — 同 cite_key
      已在架则拦截 (force=1 覆盖导入为新记录); filename 撞但 cite_key 不同
      → 照旧 _imported 改名共存。
    - changeset 包 (pkg_type=changeset): 变更附件随行, **不自动 apply** —
      同 cite_key 文献在架 → 修订史条目挂到该文献名下 (basis 前缀「变更包·
      {collection}」溯源, in_pkg 置 0); 不在架 → 文献+附件一起导入。
    - 列白名单含 P1/P2 新列 (lang/tri_rows/suspect/gloss/trans/translit)。
    - 单布局 (2026-10-31): 归档 uploads/ 平铺源文件导入即归一化落盘
      files/<新id>/original.<ext> (attach_original), linked_pdf 不直拷;
      归档内文件按裸名 linked_pdf 优先配对, 再回退 filename。
    """
    try:
        init_db(collection)
        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400,
                                content={"error": "空文件"})

        # 1. 解包到临时目录
        tmp_dir = tempfile.mkdtemp(prefix="xsf_import_")
        try:
            tar_io = io.BytesIO(raw)
            with tarfile.open(fileobj=tar_io, mode="r:gz") as tar:
                tar.extractall(tmp_dir)

            tmp_db_path = Path(tmp_dir) / "export.db"
            manifest_path = Path(tmp_dir) / "manifest.json"
            if not tmp_db_path.exists():
                return JSONResponse(
                    status_code=400,
                    content={"error": "数据包缺少 export.db"},
                )
            manifest = {}
            if manifest_path.exists():
                try:
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, ValueError):
                    manifest = {}
            pkg_type = manifest.get("pkg_type", "doc") \
                if manifest.get("version") == 2 else "doc"
            src_collection = manifest.get("collection", "未知书架")

            src_uploads = Path(tmp_dir) / "uploads"

            # 2. 联动导入 (src schema 探测: v1 旧包缺新列/无 revision_log)
            src = sqlite3.connect(str(tmp_db_path))
            src.row_factory = sqlite3.Row
            src_line_cols = {r[1] for r in src.execute(
                "PRAGMA table_info(lines)")}
            src_has_revlog = src.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='revision_log'"""
            ).fetchone() is not None
            LINE_EXTRA = ["suspect", "gloss", "trans", "translit"]
            line_extras = [c for c in LINE_EXTRA if c in src_line_cols]
            dst = get_conn(collection)
            imported = 0
            conflicts = []
            skipped = []

            try:
                doc_rows = src.execute(
                    "SELECT * FROM documents"
                ).fetchall()
                pending_attach = []   # [(new_id, staged_path)] commit 后落盘
                for doc in doc_rows:
                    doc = dict(doc)
                    cite_key = doc.get("cite_key")
                    old_id = doc.pop("id", None)

                    # cite_key 幂等 (doc 包): 同 cite_key 在架 → 拦截/覆盖
                    existing_by_key = None
                    if cite_key:
                        existing_by_key = dst.execute(
                            "SELECT id FROM documents WHERE cite_key = ?",
                            (cite_key,),
                        ).fetchone()

                    if pkg_type == "changeset":
                        # 变更附件随行: 在架 → 挂到该文献; 不在架 → 全量导
                        if existing_by_key and src_has_revlog:
                            new_id = existing_by_key["id"]
                            n = 0
                            for r in src.execute(
                                """SELECT page_num, block_num, old_value,
                                          new_value, basis, created_at
                                   FROM revision_log WHERE doc_id = ?""",
                                (old_id,),
                            ).fetchall():
                                dst.execute(
                                    """INSERT INTO revision_log
                                       (doc_id, page_num, block_num, old_value,
                                        new_value, basis, in_pkg, created_at)
                                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                                    (new_id, r["page_num"], r["block_num"],
                                     r["old_value"], r["new_value"],
                                     f"变更包·{src_collection}·"
                                     f"{r['basis'] or ''}",
                                     r["created_at"]),
                                )
                                n += 1
                            skipped.append({
                                "cite_key": cite_key,
                                "attached_edits": n,
                            })
                            continue

                    elif existing_by_key and not force:
                        skipped.append({
                            "cite_key": cite_key,
                            "reason": "已在架 (cite_key 重复), force=1 可覆盖",
                        })
                        continue

                    filename = doc["filename"]
                    existing = dst.execute(
                        "SELECT id FROM documents WHERE filename = ?",
                        (filename,),
                    ).fetchone()
                    if existing:
                        base, ext = os.path.splitext(filename)
                        filename = f"{base}_imported{ext}"
                        conflicts.append(
                            {"original": doc["filename"],
                             "renamed": filename}
                        )

                    # 源文件配对 (单布局 2026-10-31): 归档内文件按裸名
                    # linked_pdf 优先 (含 {id}-原名 错配归档名), 再回退
                    # filename; 命中则暂存, commit 后统一 attach 落盘
                    # files/<new_id>/ (避免与导入事务抢 SQLite 写锁)。
                    src_name = None
                    lp_bare = (doc.get("linked_pdf") or "").strip()
                    if (lp_bare and "/" not in lp_bare
                            and (src_uploads / lp_bare).is_file()):
                        src_name = lp_bare
                    elif (src_uploads / doc["filename"]).is_file():
                        src_name = doc["filename"]
                    staged = None
                    if src_name:
                        staged = (Path(tmp_dir) / "staged"
                                  / f"{old_id}{Path(src_name).suffix}")
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_uploads / src_name, staged)

                    doc["filename"] = filename
                    # 源库 linked_pdf 路径在新架无意义, 弃置由 attach 回写
                    doc.pop("linked_pdf", None)
                    doc["original_virtual"] = 1 if doc.get("original_virtual") else 0
                    cols = [
                        "cite_key", "title", "author", "filename",
                        "page_count", "doc_type", "is_primary",
                        "is_secondary", "is_reference", "source_tags",
                        "bib_type", "bib_data",
                        "lang", "tri_rows", "original_virtual", "created_at",
                    ]
                    placeholders = ",".join("?" * len(cols))
                    cur = dst.execute(
                        f"INSERT INTO documents ({','.join(cols)}) "
                        f"VALUES ({placeholders})",
                        [doc.get(c) for c in cols],
                    )
                    new_id = cur.lastrowid
                    if staged:
                        pending_attach.append((new_id, staged))

                    line_sel = ("page_num, block_num, line_num, text, "
                                "bbox, block_label, page_w, page_h"
                                + (", " + ", ".join(line_extras)
                                   if line_extras else ""))
                    line_ins = ("doc_id, page_num, block_num, line_num, "
                                "text, bbox, block_label, page_w, page_h"
                                + (", " + ", ".join(line_extras)
                                   if line_extras else ""))
                    for r in src.execute(
                        f"""SELECT {line_sel} FROM lines WHERE doc_id = ?""",
                        (old_id,),
                    ).fetchall():
                        dst.execute(
                            f"""INSERT INTO lines ({line_ins})
                                VALUES ({','.join('?' * (9 + len(line_extras)))})""",
                            (new_id, r["page_num"], r["block_num"],
                             r["line_num"], r["text"], r["bbox"],
                             r["block_label"], r["page_w"], r["page_h"],
                             *[r[c] for c in line_extras]),
                        )

                    for r in src.execute(
                        """SELECT page_num, block_num, text, trans
                           FROM blocks_fts WHERE doc_id = ?""",
                        (old_id,),
                    ).fetchall():
                        dst.execute(
                            """INSERT INTO blocks_fts
                               (doc_id, page_num, block_num, text, trans)
                               VALUES (?, ?, ?, ?, ?)""",
                            (new_id, r["page_num"], r["block_num"],
                             r["text"], r["trans"]),
                        )

                    # 修订史联动 (doc_id 重映射; in_pkg 置 0 = 未再导出)
                    if src_has_revlog:
                        for r in src.execute(
                            """SELECT page_num, block_num, old_value, new_value,
                                      basis, created_at
                               FROM revision_log WHERE doc_id = ?""",
                            (old_id,),
                        ).fetchall():
                            dst.execute(
                                """INSERT INTO revision_log
                                   (doc_id, page_num, block_num, old_value,
                                    new_value, basis, in_pkg, created_at)
                                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                                (new_id, r["page_num"], r["block_num"],
                                 r["old_value"], r["new_value"], r["basis"],
                                 r["created_at"]),
                            )
                    imported += 1

                dst.commit()
                # 原件归一化落盘 (单布局): 暂存文件移入 files/<new_id>/
                # 并回写 linked_pdf; 失败不回滚文档 (doctor 体检可见断链)
                from .ingest import attach_original
                attached = 0
                for new_id, staged in pending_attach:
                    try:
                        attach_original(collection, new_id, staged, move=True)
                        attached += 1
                    except Exception as attach_err:
                        skipped.append({
                            "doc_id": new_id,
                            "reason": f"原件落盘失败: {attach_err}",
                        })
            finally:
                src.close()
                dst.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return {"imported": imported, "conflicts": conflicts,
                "skipped": skipped, "pkg_type": pkg_type}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── P2c AI 填充 (翻译大模型 draft 层) ────────────────────

@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/autofill")
async def api_autofill(collection: str, doc_id: int, page_num: int,
                       request: Request):
    """AI 填充多行制 draft 层 (页级)。

    body: {targets: ['translit','gloss','trans'] 子集, provider_id?: str}
    行制 >=3 才可用。LLM 生成草稿**直接落库**对应列并打 draft_flags 标记
    ({"gloss":"ai",...}); 修订史分层记 ([转]/[对]/[译] 前缀),
    basis='AI草稿·{model}' — AI 披露入史。人工编辑该列后标记自动清除
    (edit_page diff 逻辑)。未配置 llm_chat provider → 503。
    """
    from .llm import LLMNotConfigured
    from .generate import autofill_page

    try:
        body = await request.json()
    except Exception:
        body = {}
    # 配置缺失最优先报 (503): 用户先配模型, 再谈行制/内容
    try:
        from .config import get_llm_provider_cfg as _glc
        _glc(body.get("provider_id"))
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})

    targets = body.get("targets", ["gloss", "trans"])
    provider_id = body.get("provider_id")
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            "SELECT id, lang, tri_rows FROM documents WHERE id = ?",
            (doc_id,)).fetchone()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})
        rows_mode = _tri_rows_for(doc)
        if rows_mode < 3:
            return JSONResponse(
                status_code=400,
                content={"error": "AI 填充仅适用于非中文多行制文献 "
                                  "(三行/四行); 请先切换行制"})
        if rows_mode == 3:
            targets = [t for t in targets if t != "translit"]
        n_blocks = conn.execute(
            "SELECT COUNT(*) c FROM lines WHERE doc_id = ? AND page_num = ?",
            (doc_id, page_num)).fetchone()["c"]
    finally:
        conn.close()
    if not n_blocks:
        return JSONResponse(status_code=400, content={"error": "该页无文本块"})

    def _bg():
        return autofill_page(collection, doc_id, page_num, targets=targets,
                             provider_id=provider_id)

    jid = _bg_job_start("autofill", _bg)
    return {"ok": True, "job": jid, "blocks": n_blocks, "targets": targets}


# ── 文献内容浏览 ────────────────────────────────────────

# ---------- 卡片资产层 (P4, 2026-10-17) ----------
# 设计: mem 小书房/design/完善路线图.md P4 + 石渠 20 号稿
# 三卡 (meta/toc/summary) + 候选队列 (toc/entity), draft→reviewed,
# reviewed 进检索路; 词表延后, category 自由标签起步 (用户裁定)


def _get_card_doc(collection: str, doc_id: int):
    conn = get_conn(collection)
    try:
        doc = conn.execute(
             "SELECT id, title, author, cite_key, filename, page_count, "
             "doc_type, bib_type, bib_data FROM documents WHERE id = ?",
             (doc_id,),
         ).fetchone()
        return doc
    finally:
        conn.close()


def _migrate_legacy_meta(conn, doc_id: int):
    """旧 meta 卡惰性迁移 (2026-10-17 元数据合并裁定).

    元数据编辑统一走 bibEditor (documents.bib_type/bib_data) 后:
    - meta 卡 author/era/note → bib_data 空字段回填, 回填后清旧键
    - meta 卡 entities → 独立 entity 卡 items, 搬完清键
    幂等: 键已清/卡不存在时零动作.
    """
    row = conn.execute(
        "SELECT id, content FROM doc_cards WHERE doc_id = ? AND kind = 'meta'",
        (doc_id,)).fetchone()
    if row is None:
        return
    try:
        content = json.loads(row["content"] or "{}")
    except ValueError:
        return
    changed = False

    # entities → entity 卡
    entities = content.pop("entities", None)
    if entities:
        erow = conn.execute(
            "SELECT id, content FROM doc_cards "
            "WHERE doc_id = ? AND kind = 'entity'", (doc_id,)).fetchone()
        items = []
        if erow is not None:
            try:
                items = json.loads(erow["content"] or "{}").get("items") or []
            except ValueError:
                items = []
        existing = {it.get("label") for it in items}
        for e in entities:
            if e.get("label") and e["label"] not in existing:
                items.append(e)
        payload = json.dumps({"items": items}, ensure_ascii=False)
        if erow is None:
            conn.execute(
                "INSERT INTO doc_cards (doc_id, kind, content, status, updated_at) "
                "VALUES (?, 'entity', ?, 'draft', datetime('now'))",
                (doc_id, payload))
        else:
            conn.execute(
                "UPDATE doc_cards SET content = ?, updated_at = datetime('now') "
                "WHERE id = ?", (payload, erow["id"]))
        changed = True

    # author/era/note → bib_data 空字段回填
    legacy_map = {"author": "author", "era": "year", "note": "note"}
    backfill = {k: v for k, v in
                ((fk, content.pop(lk, None)) for lk, fk in legacy_map.items())
                if v}
    if backfill:
        drow = conn.execute(
            "SELECT bib_data FROM documents WHERE id = ?", (doc_id,)).fetchone()
        try:
            bib = json.loads(drow["bib_data"] or "{}") if drow else {}
        except ValueError:
            bib = {}
        for fk, v in backfill.items():
            if not bib.get(fk):
                bib[fk] = v
        conn.execute(
            "UPDATE documents SET bib_data = ? WHERE id = ?",
            (json.dumps(bib, ensure_ascii=False), doc_id))
        changed = True

    if changed:
        # meta 卡已无编辑职责; 清空旧键后若 content 为空 dict 则整卡删除
        if content:
            conn.execute(
                "UPDATE doc_cards SET content = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (json.dumps(content, ensure_ascii=False), row["id"]))
        else:
            conn.execute("DELETE FROM doc_cards WHERE id = ?", (row["id"],))
        conn.commit()


def _migrate_legacy_summary(conn, doc_id: int):
    """旧 summary 卡 → documents.bib_data.abstract (2026-10-19 摘要降级裁定).

    幂等: 卡不存在或 bib 已有 abstract 时不覆盖.
    """
    row = conn.execute(
        "SELECT id, content FROM doc_cards WHERE doc_id = ? AND kind = 'summary'",
        (doc_id,)).fetchone()
    if row is None:
        return
    try:
        text = (json.loads(row["content"] or "{}") or {}).get("text", "")
    except ValueError:
        text = ""
    text = (text or "").strip()
    drow = conn.execute(
        "SELECT bib_data FROM documents WHERE id = ?", (doc_id,)).fetchone()
    try:
        bib = json.loads(drow["bib_data"] or "{}") if drow else {}
    except ValueError:
        bib = {}
    if text and not bib.get("abstract"):
        bib["abstract"] = text
        conn.execute(
            "UPDATE documents SET bib_data = ? WHERE id = ?",
            (json.dumps(bib, ensure_ascii=False), doc_id))
    conn.execute("DELETE FROM doc_cards WHERE id = ?", (row["id"],))
    conn.commit()


@app.get("/collections/{collection}/doc/{doc_id}/cards")
async def api_cards(collection: str, doc_id: int):
    """三卡 + 候选队列数据."""
    doc = _get_card_doc(collection, doc_id)
    if doc is None:
        return JSONResponse(status_code=404, content={"error": "文献不存在"})
    conn = get_conn(collection)
    try:
        _migrate_legacy_meta(conn, doc_id)
        _migrate_legacy_summary(conn, doc_id)
        cards = conn.execute(
            "SELECT id, kind, content, status, updated_at FROM doc_cards "
            "WHERE doc_id = ?", (doc_id,)).fetchall()
        cands = conn.execute(
            "SELECT id, ctype, label, category, confidence, page_num, "
            "block_num, status, created_at FROM card_candidates "
            "WHERE doc_id = ? ORDER BY status, ctype, id", (doc_id,)).fetchall()
        return {
            "doc": dict(doc),
            "cards": [
                {"id": c["id"], "kind": c["kind"],
                 "content": json.loads(c["content"] or "{}"),
                 "status": c["status"], "updated_at": c["updated_at"]}
                for c in cards],
            "candidates": [dict(c) for c in cands],
        }
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/cards/save")
async def api_card_save(collection: str, doc_id: int,
                        kind: str = Form(...), content: str = Form(...),
                        status: str = Form('draft')):
    """保存/更新单卡 (每 doc 每 kind 一张, upsert)."""
    if kind not in ('meta', 'toc', 'summary', 'entity', 'entity_source'):
        return JSONResponse(status_code=400,
                            content={"error": "kind 只支持 meta/toc/summary/entity/entity_source"})
    if status not in ('draft', 'reviewed'):
        return JSONResponse(status_code=400,
                            content={"error": "status 只支持 draft/reviewed"})
    try:
        json.loads(content)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "content 需 JSON"})
    doc = _get_card_doc(collection, doc_id)
    if doc is None:
        return JSONResponse(status_code=404, content={"error": "文献不存在"})
    conn = get_conn(collection)
    try:
        conn.execute(
            """INSERT INTO doc_cards (doc_id, kind, content, status, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(doc_id, kind) DO UPDATE SET
                 content = excluded.content,
                 status = excluded.status,
                 updated_at = datetime('now')""",
            (doc_id, kind, content, status))
        row = conn.execute(
            "SELECT id FROM doc_cards WHERE doc_id = ? AND kind = ?",
            (doc_id, kind)).fetchone()
        if row:
            _card_fts_sync(conn, row["id"])
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/collections/{collection}/cards/{card_id}/status")
async def api_card_status(collection: str, card_id: int,
                          status: str = Form(...)):
    """draft↔reviewed 切换 (reviewed 卡进检索路)."""
    if status not in ('draft', 'reviewed'):
        return JSONResponse(status_code=400, content={"error": "非法 status"})
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            "UPDATE doc_cards SET status = ?, updated_at = datetime('now') "
            "WHERE id = ?", (status, card_id))
        if cur.rowcount:
            _card_fts_sync(conn, card_id)
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404, content={"error": "卡片不存在"})
        return {"ok": True}
    finally:
        conn.close()


@app.post("/collections/{collection}/candidates/{cand_id}/review")
async def api_candidate_review(collection: str, cand_id: int,
                               action: str = Form(...),
                               label: str = Form(None),
                               category: str = Form(None),
                               mount: str = Form(None)):
    """候选处置: adopt(采纳并入卡)/reject(驳回)/patch(修订字段).

    mount: toc 候选采纳时的挂载路径 (JSON 数组, 沿根 children 索引),
    如 "[0,2]" = 根[0].children[2] 下; 缺省/无效挂根级。"""
    if action not in ('adopt', 'reject', 'patch'):
        return JSONResponse(status_code=400,
                            content={"error": "action 只支持 adopt/reject/patch"})
    conn = get_conn(collection)
    try:
        cand = conn.execute(
            "SELECT * FROM card_candidates WHERE id = ?", (cand_id,)).fetchone()
        if cand is None:
            return JSONResponse(status_code=404, content={"error": "候选不存在"})

        if action == 'patch':
            conn.execute(
                "UPDATE card_candidates SET label = ?, category = ? WHERE id = ?",
                (label or cand["label"], category or cand["category"], cand_id))
            conn.commit()
            return {"ok": True, "status": cand["status"]}

        # adopt / reject: 置状态
        new_status = 'adopted' if action == 'adopt' else 'rejected'
        conn.execute(
            "UPDATE card_candidates SET status = ? WHERE id = ?",
            (new_status, cand_id))

        # adopt 时并入对应卡:
        #   toc 候选 → toc 卡 nodes 数组追加节点
        #   entity 候选 → entity 卡 items 数组追加 (2026-10-17 实体独立成卡)
        if action == 'adopt':
            card_kind = 'toc' if cand["ctype"] == 'toc' else 'entity'
            card = conn.execute(
                "SELECT id, content FROM doc_cards "
                "WHERE doc_id = ? AND kind = ?",
                (cand["doc_id"], card_kind)).fetchone()
            if card is None:
                content = {"tree": []} if card_kind == 'toc' \
                    else {"items": []}
                cur = conn.execute(
                    """INSERT INTO doc_cards
                       (doc_id, kind, content, status, updated_at)
                       VALUES (?, ?, ?, 'draft', datetime('now'))""",
                    (cand["doc_id"], card_kind, json.dumps(content, ensure_ascii=False)))
                card_id, card_content = cur.lastrowid, content
            else:
                card_id = card["id"]
                try:
                    card_content = json.loads(card["content"] or "{}")
                except ValueError:
                    card_content = {}
            if card_kind == 'toc':
                # 旧平铺 nodes 惰性迁移为根级 tree 节点
                if not card_content.get("tree"):
                    card_content["tree"] = [
                        {"label": n.get("label"),
                         "category": n.get("category"),
                         "page_num": n.get("page_num"),
                         "block_num": n.get("block_num"),
                         "from": n.get("from")}
                        for n in card_content.get("nodes") or []
                        if n.get("label")]
                    card_content.pop("nodes", None)
                tree = card_content["tree"]
                target = tree
                if mount:
                    try:
                        for idx in json.loads(mount):
                            node = target[int(idx)]
                            node.setdefault("children", [])
                            target = node["children"]
                    except (ValueError, TypeError, KeyError, IndexError,
                            json.JSONDecodeError):
                        target = tree   # 路径失效挂根级
                target.append({"label": label or cand["label"],
                               "category": category or cand["category"],
                               "page_num": cand["page_num"],
                               "block_num": cand["block_num"],
                               "from": f"candidate#{cand_id}"})
            else:
                items = card_content.setdefault("items", [])
                _lbl = label or cand["label"]
                if any((it.get("label") or "").strip() == (_lbl or "").strip()
                       for it in items):
                    conn.commit()   # 候选处置先落盘
                    return JSONResponse(   # 已存在: 候选仍处置, 卡不重复并入
                        {"ok": True, "status": new_status, "merged": True})
                items.append({"label": _lbl,
                              "category": category or cand["category"],
                              "page_num": cand["page_num"],
                              "from": f"candidate#{cand_id}"})
            conn.execute(
                "UPDATE doc_cards SET content = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (json.dumps(card_content, ensure_ascii=False), card_id))
            _card_fts_sync(conn, card_id)
        conn.commit()
        return {"ok": True, "status": new_status}
    finally:
        conn.close()


@app.get("/collections/{collection}/vocab")
async def api_vocab_get(collection: str, kind: str = "entity_category"):
    """受控词表 (实体类别等), 一行一词."""
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            "SELECT term FROM vocabs WHERE kind = ? ORDER BY id", (kind,)).fetchall()
        return {"ok": True, "kind": kind, "terms": [r["term"] for r in rows]}
    finally:
        conn.close()


@app.post("/collections/{collection}/vocab")
async def api_vocab_import(collection: str, kind: str = Form("entity_category"),
                           terms: str = Form(...),
                           replace: str = Form(None)):
    """导入词表 (多行文本一行一词); replace=1 先清空该 kind."""
    items = [t.strip() for t in (terms or "").splitlines() if t.strip()]
    if not items:
        return JSONResponse({"error": "无有效词条"}, status_code=400)
    conn = get_conn(collection)
    try:
        if replace == "1":
            conn.execute("DELETE FROM vocabs WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT OR IGNORE INTO vocabs (kind, term) VALUES (?, ?)",
            [(kind, t) for t in items])
        n = conn.execute("SELECT COUNT(*) c FROM vocabs WHERE kind = ?",
                         (kind,)).fetchone()["c"]
        conn.commit()
        return {"ok": True, "imported": len(items), "total": n}
    finally:
        conn.close()


@app.get("/collections/{collection}/doc/{doc_id}/page-map")
async def api_page_map_get(collection: str, doc_id: int):
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    conn = get_conn(collection)
    try:
        raw = _load_page_map(conn, doc_id)
        return {"ok": True, "groups": _pm_groups(raw),
                "default": _pm_default(raw)}
    finally:
        conn.close()


def _pm_upsert_anchor(groups: list, label: str, printed: int, pdf: int):
    """组内 printed 去重后并入; 组不存在则新建 (排在末尾)."""
    for g in groups:
        if g["label"] == label:
            g["anchors"] = [a for a in g["anchors"] if a[0] != printed]
            g["anchors"].append([printed, pdf])
            g["anchors"].sort(key=lambda a: a[0])
            return
    groups.append({"label": label, "anchors": [[printed, pdf]]})


@app.post("/collections/{collection}/doc/{doc_id}/page-map")
async def api_page_map_add(collection: str, doc_id: int,
                           printed: int = Form(...), pdf: int = Form(...),
                           label: str = Form(None)):
    """加校准锚点: 翻到 pdf 页, 填该页原书页码 printed.
    label = 页码序列组 (前言/正文各自从 1 起时分组各标各的; 空 = 默认组)."""
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    if printed < 1 or pdf < 1:
        return JSONResponse({"error": "页码须为正整数"}, status_code=400)
    label = (label or "").strip()
    conn = get_conn(collection)
    try:
        if not conn.execute("SELECT 1 FROM documents WHERE id = ?",
                            (doc_id,)).fetchone():
            return JSONResponse({"error": "文献不存在"}, status_code=404)
        raw = _load_page_map(conn, doc_id)
        groups = _pm_groups(raw)
        default = _pm_default(raw) if raw else label
        _pm_upsert_anchor(groups, label, printed, pdf)
        if not any(g["label"] == default for g in groups):
            default = groups[0]["label"]
        conn.execute(
            "UPDATE documents SET page_map = ? WHERE id = ?",
            (json.dumps(_pm_v2(groups, default), ensure_ascii=False), doc_id))
        conn.commit()
        return {"ok": True, "groups": groups, "default": default}
    finally:
        conn.close()


@app.delete("/collections/{collection}/doc/{doc_id}/page-map")
async def api_page_map_del(collection: str, doc_id: int,
                           printed: int = Form(...), label: str = Form(None)):
    conn = get_conn(collection)
    try:
        raw = _load_page_map(conn, doc_id)
        groups = _pm_groups(raw)
        default = _pm_default(raw)
        label = (label or "").strip()
        for g in groups:
            if g["label"] == label:
                g["anchors"] = [a for a in g["anchors"] if a[0] != printed]
        groups = [g for g in groups if g["anchors"]]
        if not any(g["label"] == default for g in groups):
            default = groups[0]["label"] if groups else ""
        conn.execute(
            "UPDATE documents SET page_map = ? WHERE id = ?",
            (json.dumps(_pm_v2(groups, default), ensure_ascii=False)
             if groups else None, doc_id))
        conn.commit()
        return {"ok": True, "groups": groups, "default": default}
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/page-map/default")
async def api_page_map_set_default(collection: str, doc_id: int,
                                   label: str = Form(None)):
    """设默认页码组: 页码跨组歧义时 (序 p1 vs 正文 p1) 按该组跳转."""
    conn = get_conn(collection)
    try:
        raw = _load_page_map(conn, doc_id)
        groups = _pm_groups(raw)
        label = (label or "").strip()
        if not any(g["label"] == label for g in groups):
            return JSONResponse({"error": "组不存在"}, status_code=404)
        conn.execute(
            "UPDATE documents SET page_map = ? WHERE id = ?",
            (json.dumps(_pm_v2(groups, label), ensure_ascii=False), doc_id))
        conn.commit()
        return {"ok": True, "groups": groups, "default": label}
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/page-map/convert")
async def api_page_map_convert(collection: str, doc_id: int):
    """一键反算: 实体 items/待审候选的 page_num 从 pdf 物理页换算为原书页.
    目录节点页码本就是原书页, 不参与换算. 前提 page_map 已校准;
    换算不可逆 (重复执行会二次换算), 前端需双重确认."""
    if collection not in list_collections():
        return JSONResponse({"error": "书架不存在"}, status_code=404)
    conn = get_conn(collection)
    try:
        groups = _pm_groups(_load_page_map(conn, doc_id))
        if not groups:
            return JSONResponse({"error": "尚未校准页码 (page_map 为空)"}, status_code=400)
        ent_n = cand_n = 0

        row = conn.execute(
            "SELECT id, content FROM doc_cards WHERE doc_id = ? AND kind = 'entity'",
            (doc_id,)).fetchone()
        if row:
            try:
                content = json.loads(row["content"] or "{}")
            except ValueError:
                content = {}
            if content.get("items"):
                for it in content["items"]:
                    if it.get("page_num"):
                        it["page_num"] = _pm_reverse(groups, int(it["page_num"]))
                        ent_n += 1
            conn.execute(
                "UPDATE doc_cards SET content = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (json.dumps(content, ensure_ascii=False), row["id"]))
        for c in conn.execute(
                """SELECT id, page_num FROM card_candidates
                   WHERE doc_id = ? AND status = 'pending' AND page_num IS NOT NULL""",
                (doc_id,)).fetchall():
            conn.execute(
                "UPDATE card_candidates SET page_num = ? WHERE id = ?",
                (_pm_reverse(groups, int(c["page_num"])), c["id"]))
            cand_n += 1
        conn.commit()
        return {"ok": True, "entities": ent_n, "candidates": cand_n}
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/candidates/generate")
async def api_candidates_generate(collection: str, doc_id: int,
                                  targets: str = Form(None),
                                  prompt: str = Form(None),
                                  bib_source: str = Form(None),
                                  toc_source: str = Form(None)):
    """LLM 生成候选: 抽样文献前部块文本, 按目标生成目录候选/实体候选/摘要草稿/元数据回填.

    targets 逗号分隔子集 (toc/entities/summary/meta), 默认全部。
    prompt: 自主补充提示 (仅 meta 目标生效, 附加到提取指令后)。
    bib_source: 粘贴/导入的书目数据 (BibTeX/RIS/引文格式), meta 目标下
    由 LLM 解析填入 (优先于文献块样本)。
    toc_source: 粘贴/导入的目录文本 (多级缩进/编号), toc 目标下解析成树;
    树不入库, 经 job result 的 tree 字段由前端确认合并。
    复用 P2c 的 llm_chat provider 配置 (default_llm).
    """
    from .generate import gen_cards_core
    want = {t.strip() for t in (targets or "toc,entities,summary,meta").split(",")
            if t.strip() in ("toc", "entities", "summary", "meta")}
    if not want:
        want = {"toc", "entities", "summary"}
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    try:  # 配置缺失同步快速失败 (503), 不入后台
        from .config import get_llm_provider_cfg as _glc
        _glc(None)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
    doc = _get_card_doc(collection, doc_id)
    if doc is None:
        return JSONResponse(status_code=404, content={"error": "文献不存在"})
    conn = get_conn(collection)
    try:
        has_blocks = conn.execute(
            "SELECT 1 FROM blocks_fts WHERE doc_id = ? LIMIT 1",
            (doc_id,)).fetchone()
    finally:
        conn.close()
    if not has_blocks:
        return JSONResponse(status_code=400,
                            content={"error": "文献无文本块，无法生成"})

    def _bg():
        return gen_cards_core(collection, doc_id, targets=targets, prompt=prompt,
                              toc_source=toc_source, bib_source=bib_source)

    jid = _bg_job_start("cards-gen", _bg)
    return {"ok": True, "job": jid, "targets": sorted(want)}


# ════════════════════════════════════════════════════════════
# 知识地图 Wiki (P6, 2026-10-18): collection 级编译产物
# 模型: LLM 一键生成, 无人审核, 可再生, 不入修订史; stale=资料修订数超基线
# ════════════════════════════════════════════════════════════

WIKI_SYSTEM_PROMPT = """你是研究知识库的编辑。基于给定的文献库资料（文献列表、已审定卡片、正文采样），
为这个文献库编写一组「知识地图」概念页——帮助研究者快速把握库内核心主题与脉络。

要求：
- 输出 3-8 个概念页, 覆盖库内最重要的主题/实体/脉络
- 每页 content 用简体中文 markdown（可用 ## 小节、列表、**加粗**）
- 相关概念用 [[概念页标题]] 互链（必须对应你输出的某个 title）
- 引证具体文献时在 refs 里给 doc_id 与 page_num（正文可提「见某文献」但不要编造页码）
- 不得虚构资料中没有的事实; 资料不足时如实写「资料尚未覆盖」
- 只输出 JSON, 格式:
{"pages": [{"title": "...", "slug": "ascii-kebab", "content": "markdown", "refs": [{"doc_id": 1, "page_num": 2}]}]}"""


def _wiki_llm_payload(conn):
    """组装生成输入: 文献列表 + reviewed 卡 + 每文献前 3 块采样."""
    docs = conn.execute(
        "SELECT id, title, cite_key, filename, page_count, bib_data FROM documents "
        "ORDER BY id").fetchall()
    doc_lines = []
    abstracts = []
    for d in docs:
        doc_lines.append(f"- doc_id={d['id']} 《{d['title'] or d['filename']}》"
                         f" (cite: {d['cite_key'] or '-'}, {d['page_count']}页)")
        bib = parse_bib_data(d["bib_data"]) or {}
        if bib.get("abstract"):
            abstracts.append(
                f"[摘要·{d['title'] or d['filename']}] {bib['abstract'][:200]}")
    cards_txt = []
    for r in conn.execute(
            "SELECT dc.kind, dc.content, dc.status, d.title FROM doc_cards dc "
            "JOIN documents d ON d.id = dc.doc_id "
            "WHERE dc.status = 'reviewed' ORDER BY dc.doc_id, dc.kind").fetchall():
        try:
            c = json.loads(r["content"]) if r["content"] else {}
        except Exception:
            c = {}
        if r["kind"] == "toc" and (c.get("tree") or c.get("nodes")):
            labels = []

            def _walk(nodes):
                for n in nodes:
                    labels.append(
                        f"{n.get('label')}({n.get('category') or '?'})")
                    _walk(n.get("children") or [])

            _walk(c.get("tree") or c.get("nodes") or [])
            nodes = "; ".join(labels[:15])
            cards_txt.append(f"[目录卡·{r['title']}] {nodes}")
        elif r["kind"] == "entity" and c.get("items"):
            items = "; ".join(f"{i.get('label')}({i.get('category') or '?'})"
                              for i in c["items"][:15])
            cards_txt.append(f"[实体卡·{r['title']}] {items}")
    cards_txt.extend(abstracts)   # 摘要已降为元数据字段 (doc.bib_data.abstract)
    samples = []
    for d in docs[:10]:
        rows = conn.execute(
            "SELECT page_num, block_num, text FROM blocks_fts "
            "WHERE doc_id = ? ORDER BY page_num, block_num LIMIT 3",
            (d["id"],)).fetchall()
        if rows:
            frag = " / ".join((r["text"] or "")[:80] for r in rows)
            samples.append(f"《{d['title'] or d['filename']}》(doc_id={d['id']}): {frag}")
    return {"documents": doc_lines, "reviewed_cards": cards_txt[:20],
            "text_samples": samples[:10]}


@app.post("/collections/{collection}/wiki/generate")
async def wiki_generate(collection: str):
    """一键生成知识地图: LLM 编译 → 清旧插新 → ready + 基线戳."""
    from .config import get_llm_provider_cfg
    from .generate import gen_wiki_core
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    try:  # 配置缺失同步快速失败 (503), 不入后台
        get_llm_provider_cfg(None)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            "SELECT status FROM wiki_status WHERE collection = ?",
            (collection,)).fetchone()
        if cur and cur["status"] == 'generating':
            return JSONResponse(status_code=409,
                                content={"error": "正在生成中，请稍候"})
        conn.execute(
            "INSERT INTO wiki_status(collection, status, base_edits, generated_at) "
            "VALUES(?, 'generating', 0, NULL) "
            "ON CONFLICT(collection) DO UPDATE SET status = 'generating'",
            (collection,))
        conn.commit()
    finally:
        conn.close()

    def _bg():
        return gen_wiki_core(collection)

    jid = _bg_job_start("wiki-gen", _bg)
    return {"ok": True, "job": jid}


@app.get("/collections/{collection}/wiki/status")
async def wiki_status(collection: str):
    """状态: ready/generating/none + 页数 + stale 判定(修订数超基线)."""
    conn = get_conn(collection)
    try:
        st = conn.execute("SELECT * FROM wiki_status WHERE collection = ?",
                          (collection,)).fetchone()
        n_pages = conn.execute(
            "SELECT COUNT(*) c FROM wiki_pages WHERE collection = ?",
            (collection,)).fetchone()["c"]
        stale = False
        rev_now = 0
        try:
            rev_now = conn.execute(
                "SELECT COUNT(*) c FROM revision_log").fetchone()["c"]
        except sqlite3.OperationalError:
            pass
        if st and st["status"] == 'ready':
            stale = rev_now > (st["base_edits"] or 0)
        return {"status": (st["status"] if st else ("ready" if n_pages else "none")),
                "pages": n_pages, "stale": stale,
                "generated_at": st["generated_at"] if st else None,
                "base_edits": st["base_edits"] if st else 0, "rev_now": rev_now}
    finally:
        conn.close()


@app.get("/collections/{collection}/wiki/view")
async def wiki_view(request: Request, collection: str):
    """知识地图阅读页: 左概念列表 + 右正文 (ai-generated 标 + 双链 + refs 跳工位)."""
    conn = get_conn(collection)
    try:
        pages = conn.execute(
            "SELECT slug, title, content, refs, position FROM wiki_pages "
            "WHERE collection = ? ORDER BY position, id",
            (collection,)).fetchall()
        st = conn.execute("SELECT * FROM wiki_status WHERE collection = ?",
                          (collection,)).fetchone()
        pages_data = []
        for p in pages:
            try:
                refs = json.loads(p["refs"]) if p["refs"] else []
            except Exception:
                refs = []
            pages_data.append({"slug": p["slug"], "title": p["title"],
                               "content": p["content"], "refs": refs})
        status = (st["status"] if st else ("ready" if pages_data else "none"))
        return templates.TemplateResponse(
            request, "wiki.html",
            _nav_ctx("docs", collection, wiki_pages=pages_data,
                     wiki_status=status,
                     wiki_generated_at=(st["generated_at"] if st else None)))
    finally:
        conn.close()


# ── 实体图层 (P8, 2026-10-18): 书架级编译产物 · ontology RAG 底座 ──

def _entity_mentions(conn, entity_id, doc_id=None):
    sql = ("SELECT m.id, m.doc_id, m.page_num, m.block_num, m.surface, m.valid, "
           "d.title, d.filename FROM entity_mentions m "
           "LEFT JOIN documents d ON d.id = m.doc_id WHERE m.entity_id = ?")
    params = [entity_id]
    if doc_id:
        sql += " AND m.doc_id = ?"
        params.append(doc_id)
    sql += " ORDER BY m.doc_id, m.page_num, m.block_num"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _entity_neighbors(conn, entity_id, limit: int = 30):
    """共现邻居 (同文献同页出现, 一跳) —— M1 的最小可行图. """
    rows = conn.execute(
        "SELECT m2.entity_id AS eid, e.canonical, e.category, COUNT(*) c "
        "FROM entity_mentions m1 "
        "JOIN entity_mentions m2 ON m2.doc_id = m1.doc_id "
        "  AND m2.page_num = m1.page_num "
        "JOIN entities e ON e.id = m2.entity_id "
        "WHERE m1.entity_id = ? AND m2.entity_id != ? "
        "  AND m1.valid = 1 AND m2.valid = 1 "
        "GROUP BY m2.entity_id ORDER BY c DESC LIMIT ?",
        (entity_id, entity_id, limit)).fetchall()
    return [{"id": r["eid"], "canonical": r["canonical"],
             "category": r["category"], "co": r["c"]} for r in rows]


def _entity_hits(collection: str, q: str, limit: int = 10):
    """实体命中组: 检索 canonical/aliases, 供 search-grouped."""
    rows = []
    try:
        conn = get_conn(collection)
        try:
            esc = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
            for r in conn.execute(
                    "SELECT id, canonical, category FROM entities "
                    "WHERE canonical LIKE ? ESCAPE '\\' "
                    "OR aliases LIKE ? ESCAPE '\\' LIMIT ?",
                    (f"%{esc}%", f"%{esc}%", limit)).fetchall():
                n = conn.execute(
                    "SELECT COUNT(*) c FROM entity_mentions "
                    "WHERE entity_id = ? AND valid = 1",
                    (r["id"],)).fetchone()["c"]
                rows.append({"id": r["id"], "canonical": r["canonical"],
                             "category": r["category"], "mentions": n})
        finally:
            conn.close()
    except Exception:
        rows = []
    return rows


@app.post("/collections/{collection}/doc/{doc_id}/entity-source/extract")
async def entity_source_extract(collection: str, doc_id: int,
                                provider_id: str = Form(None),
                                source: str = Form(None)):
    """模型提取实体依据 (关键词/索引) — 返回建议, 不落库 (前端确认后走 cards/save)."""
    from .config import get_llm_provider_cfg
    from . import entity as ent
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    doc = _get_card_doc(collection, doc_id)
    if doc is None:
        return JSONResponse(status_code=404, content={"error": "文献不存在"})
    try:
        get_llm_provider_cfg(None)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
    conn = get_conn(collection)
    try:
        res = ent.extract_seeds(conn, doc_id, provider_id, source=source)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        conn.close()
    return {"ok": True, **res}


@app.post("/collections/{collection}/entitygraph/generate")
async def entitygraph_generate(collection: str, doc_id: int = Form(None)):
    """编译实体图 (全量; doc_id 给定时仅重建该文献的提及)."""
    from .config import get_llm_provider_cfg
    from .generate import gen_entitygraph_core
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    if collection in _ENTITY_RUNNING:
        return JSONResponse(status_code=409,
                            content={"error": "正在编译中，请稍候"})
    try:
        get_llm_provider_cfg(None)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
    _ENTITY_RUNNING.add(collection)
    doc_ids = [int(doc_id)] if doc_id else None

    def _bg():
        try:
            return gen_entitygraph_core(collection, doc_ids)
        finally:
            _ENTITY_RUNNING.discard(collection)

    jid = _bg_job_start("entitygraph-gen", _bg)
    return {"ok": True, "job": jid}


@app.get("/collections/{collection}/entitygraph/status")
async def entitygraph_status(collection: str):
    from . import entity as ent
    try:
        return ent.graph_status(collection)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/entitygraph/migrate-legacy")
async def entitygraph_migrate_legacy(collection: str):
    """一次性: 旧 per-doc 实体卡 items 上卷为提及 (归一到书架实体)."""
    from . import entity as ent
    try:
        n = ent.migrate_legacy(collection)
        return {"ok": True, "mentions": n}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/collections/{collection}/entities/export")
async def entities_export(collection: str):
    """导出实体登记簿 JSON (带 provenance 头) —— 跨书架流通/工具对接."""
    conn = get_conn(collection)
    try:
        st = conn.execute("SELECT * FROM entity_status WHERE collection = ?",
                          (collection,)).fetchone()
        ents = []
        for r in conn.execute(
                "SELECT id, canonical, category, aliases, ext_ids, props, origin "
                "FROM entities ORDER BY id"):
            e = dict(r)
            for k in ("aliases", "ext_ids", "props"):
                try:
                    e[k] = json.loads(e[k] or ('[]' if k == 'aliases' else '{}'))
                except (ValueError, TypeError):
                    e[k] = [] if k == 'aliases' else {}
            ents.append(e)
        return {
            "provenance": {
                "collection": collection,
                "compiled_at": st["generated_at"] if st else None,
                "base_edits": st["base_edits"] if st else 0,
                "model": "xsf-entitygraph-m1",
                "format": "xsf-entities-v1",
            },
            "entities": ents,
        }
    finally:
        conn.close()


@app.get("/collections/{collection}/entities")
async def api_entities(collection: str, q: str = None, category: str = None,
                       limit: int = 500, with_mentions: int = 0,
                       doc_id: int = None, era: str = None,
                       scope: str = None, year: int = None):
    """实体列表 JSON (实体页与 agent/lit-search 共用出口)."""
    conn = get_conn(collection)
    try:
        where, params = [], []
        if q:
            esc = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
            where.append("(canonical LIKE ? ESCAPE '\\' "
                         "OR aliases LIKE ? ESCAPE '\\')")
            params += [f"%{esc}%", f"%{esc}%"]
        if category:
            where.append("category = ?")
            params.append(category)
        sql = ("SELECT id, canonical, category, aliases, ext_ids, props, origin "
               "FROM entities")
        if where:
            sql += " WHERE " + " AND ".join(where)
        filtered = bool(era or scope or (year is not None))
        sql += " ORDER BY canonical"
        if not filtered:            # 有约束过滤时先全取再由 Python 限条
            sql += " LIMIT ?"
            params.append(limit)
        out = []
        for r in conn.execute(sql, params).fetchall():
            e = dict(r)
            for k in ("aliases", "ext_ids", "props"):
                try:
                    e[k] = json.loads(e[k] or ('[]' if k == 'aliases' else '{}'))
                except (ValueError, TypeError):
                    e[k] = [] if k == 'aliases' else {}
            e["mention_count"] = conn.execute(
                "SELECT COUNT(*) c FROM entity_mentions "
                "WHERE entity_id = ? AND valid = 1",
                (r["id"],)).fetchone()["c"]
            # C3 轻量约束过滤 (时代/地域/年份)
            pr = e["props"] if isinstance(e["props"], dict) else {}
            if era:
                hay = str(pr.get("era") or pr.get("朝代") or "")
                if era not in hay:
                    continue
            if scope:
                if scope not in str(pr.get("scope") or ""):
                    continue
            if year is not None:
                ys, ye = pr.get("year_start"), pr.get("year_end")
                if ys is None and ye is None:
                    continue
                if isinstance(ys, int) and year < ys:
                    continue
                if isinstance(ye, int) and year > ye:
                    continue
            if with_mentions or doc_id:
                e["mentions"] = _entity_mentions(conn, r["id"], doc_id)
            out.append(e)
            if len(out) >= limit:
                break
        return {"collection": collection, "entities": out}
    finally:
        conn.close()


@app.get("/collections/{collection}/entities/view")
async def entities_view(request: Request, collection: str):
    """实体聚合页: 左登记簿列表 + 右详情 (提及/邻居/属性/补录)."""
    from . import entity as ent
    st = ent.graph_status(collection)
    return templates.TemplateResponse(
        request, "entities.html",
        _nav_ctx("docs", collection, eg_status=st))


@app.get("/settings/mounts")
async def api_mounts_get():
    """外部权威挂载状态 (显式挂载: 用户决定并告知位置)."""
    from . import entity as ent
    from .config import read_mounts
    return {"mounts": read_mounts().get("mounts", {}),
            "status": ent.mount_status()}


@app.post("/settings/mounts/{name}")
async def api_mounts_set(name: str, enabled: str = Form("1"),
                         path: str = Form(""), cmd: str = Form("")):
    """设置一个外部挂载。enabled=0/1。"""
    from . import entity as ent
    from .config import set_mount
    set_mount(name, enabled not in ("0", "false", "False", ""),
              path=path, cmd=cmd)
    return {"ok": True, "status": ent.mount_status()}


@app.get("/collections/{collection}/entities/suspects")
async def api_entity_suspects(collection: str):
    """编译错误巡查: 重复片段 / 别名撞他人规范名 / 规范名包含。"""
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    from . import entity as ent
    conn = get_conn(collection)
    try:
        items = ent.detect_suspicious(conn)
    finally:
        conn.close()
    return {"items": items, "count": len(items)}


@app.post("/collections/{collection}/entities/{entity_id}/report")
async def api_entity_report(collection: str, entity_id: int,
                            kind: str = Form("other"), note: str = Form(None),
                            doc_id: int = Form(None), page_num: int = Form(None),
                            mention_id: int = Form(None),
                            source: str = Form("web")):
    """消费侧反馈: 报告实体归一错误 (供巡查处置)。"""
    conn = get_conn(collection)
    try:
        if not conn.execute("SELECT id FROM entities WHERE id = ?",
                            (entity_id,)).fetchone():
            return JSONResponse(status_code=404,
                                content={"error": "实体不存在"})
        conn.execute(
            "INSERT INTO entity_reports"
            "(entity_id, kind, note, doc_id, page_num, mention_id, source) "
            "VALUES(?,?,?,?,?,?,?)",
            (entity_id, kind or 'other', note, doc_id, page_num,
             mention_id, source or 'web'))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/collections/{collection}/entity-reports")
async def api_entity_reports(collection: str, include_resolved: int = 0,
                             limit: int = 200):
    if collection not in list_collections():
        return JSONResponse(status_code=404, content={"error": "书架不存在"})
    conn = get_conn(collection)
    try:
        where = "" if include_resolved else "WHERE r.resolved = 0"
        rows = conn.execute(
            f"SELECT r.id, r.entity_id, r.kind, r.note, r.doc_id, r.page_num, "
            f"       r.mention_id, r.source, r.resolved, r.created_at, "
            f"       e.canonical AS canonical "
            f"FROM entity_reports r LEFT JOIN entities e ON e.id = r.entity_id "
            f"{where} ORDER BY r.resolved ASC, r.id DESC LIMIT ?",
            (max(1, min(int(limit or 200), 1000)),)).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/collections/{collection}/entity-reports/{rid}/resolve")
async def api_entity_report_resolve(collection: str, rid: int,
                                    resolved: int = Form(1)):
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            "UPDATE entity_reports SET resolved = ? WHERE id = ?",
            (1 if resolved else 0, rid))
        conn.commit()
        if cur.rowcount == 0:
            return JSONResponse(status_code=404,
                                content={"error": "记录不存在"})
    finally:
        conn.close()
    return {"ok": True}


@app.get("/collections/{collection}/entities/{entity_id}")
async def api_entity_detail(collection: str, entity_id: int):
    conn = get_conn(collection)
    try:
        r = conn.execute(
            "SELECT id, canonical, category, aliases, ext_ids, props, origin "
            "FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"error": "实体不存在"})
        e = dict(r)
        for k in ("aliases", "ext_ids", "props"):
            try:
                e[k] = json.loads(e[k] or ('[]' if k == 'aliases' else '{}'))
            except (ValueError, TypeError):
                e[k] = [] if k == 'aliases' else {}
        e["mentions"] = _entity_mentions(conn, entity_id)
        e["additions"] = [dict(x) for x in conn.execute(
            "SELECT * FROM entity_additions WHERE entity_id = ? ORDER BY id",
            (entity_id,)).fetchall()]
        e["neighbors"] = _entity_neighbors(conn, entity_id)
        return e
    finally:
        conn.close()


@app.post("/collections/{collection}/entities/{entity_id}/merge")
async def api_entity_merge(collection: str, entity_id: int,
                           into_id: int = Form(...)):
    """合并 entity_id → into_id (提及迁移 + 别名并入 + 记录可追溯)."""
    if entity_id == into_id:
        return JSONResponse(status_code=400, content={"error": "不能并入自身"})
    conn = get_conn(collection)
    try:
        src = conn.execute("SELECT * FROM entities WHERE id = ?",
                           (entity_id,)).fetchone()
        dst = conn.execute("SELECT * FROM entities WHERE id = ?",
                           (into_id,)).fetchone()
        if not src or not dst:
            return JSONResponse(status_code=404, content={"error": "实体不存在"})
        conn.execute("UPDATE OR IGNORE entity_mentions SET entity_id = ? "
                     "WHERE entity_id = ?", (into_id, entity_id))
        conn.execute("DELETE FROM entity_mentions WHERE entity_id = ?",
                     (entity_id,))
        try:
            dst_alias = json.loads(dst["aliases"] or '[]')
        except (ValueError, TypeError):
            dst_alias = []
        try:
            src_alias = json.loads(src["aliases"] or '[]')
        except (ValueError, TypeError):
            src_alias = []
        for a in [src["canonical"]] + src_alias:
            if a and a not in dst_alias:
                dst_alias.append(a)
        conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                     (json.dumps(dst_alias, ensure_ascii=False), into_id))
        try:
            de = json.loads(dst["ext_ids"] or '{}')
        except (ValueError, TypeError):
            de = {}
        try:
            se = json.loads(src["ext_ids"] or '{}')
        except (ValueError, TypeError):
            se = {}
        for k, v in se.items():
            de.setdefault(k, v)
        conn.execute("UPDATE entities SET ext_ids = ? WHERE id = ?",
                     (json.dumps(de, ensure_ascii=False), into_id))
        conn.execute("UPDATE entity_additions SET entity_id = ? "
                     "WHERE entity_id = ?", (into_id, entity_id))
        conn.execute(
            "INSERT INTO entity_additions(entity_id, kind, value, evidence) "
            "VALUES(?,?,?,?)",
            (into_id, 'merge', json.dumps(
                {"merged_id": entity_id,
                 "merged_canonical": src["canonical"]}, ensure_ascii=False),
             None))
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        conn.commit()
        return {"ok": True, "into_id": into_id}
    finally:
        conn.close()


@app.post("/collections/{collection}/mentions/{mention_id}/invalidate")
async def api_mention_invalidate(collection: str, mention_id: int,
                                 valid: int = Form(0)):
    """「错了」: 标记提及无效 (valid=0) —— 编译层纠错回流阀."""
    conn = get_conn(collection)
    try:
        conn.execute("UPDATE entity_mentions SET valid = ? WHERE id = ?",
                     (valid, mention_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/collections/{collection}/entities/{entity_id}/additions")
async def api_entity_addition(collection: str, entity_id: int,
                              kind: str = Form(...), value: str = Form(...),
                              evidence: str = Form(None)):
    """人工 patch 补录 (prop/link 必须带证据锚; 重编不丢)."""
    if kind not in ("prop", "alias", "link"):
        return JSONResponse(status_code=400, content={"error": "kind 非法"})
    if kind in ("prop", "link") and not (evidence or "").strip():
        return JSONResponse(
            status_code=400,
            content={"error": "属性/关系补录必须提供证据锚"})
    conn = get_conn(collection)
    try:
        if not conn.execute("SELECT 1 FROM entities WHERE id = ?",
                            (entity_id,)).fetchone():
            return JSONResponse(status_code=404, content={"error": "实体不存在"})
        conn.execute(
            "INSERT INTO entity_additions(entity_id, kind, value, evidence) "
            "VALUES(?,?,?,?)", (entity_id, kind, value, evidence))
        if kind == "alias":
            row = conn.execute("SELECT aliases FROM entities WHERE id = ?",
                               (entity_id,)).fetchone()
            try:
                al = json.loads(row["aliases"] or '[]')
            except (ValueError, TypeError):
                al = []
            av = value.strip()
            if av and av not in al:
                al.append(av)
                conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                             (json.dumps(al, ensure_ascii=False), entity_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/collections/{collection}/entities/{entity_id}/constraint")
async def api_entity_constraint(collection: str, entity_id: int,
                                era: str = Form(None),
                                scope: str = Form(None),
                                year_start: str = Form(None),
                                year_end: str = Form(None)):
    """C3 轻量约束: 写时代/地域/年份区间 (props), 留补录痕迹。"""
    from . import entity as ent
    conn = get_conn(collection)
    try:
        props = ent.set_constraint(conn, entity_id, era=era, scope=scope,
                                   year_start=year_start, year_end=year_end)
        if props is None:
            return JSONResponse(status_code=404,
                                content={"error": "实体不存在"})
        return {"ok": True, "props": props}
    finally:
        conn.close()


def _wiki_hits(collection: str, q: str, limit: int = 10):
    """Wiki 命中组: LIKE 检索概念页 (标题+正文), 供两 search 端点."""
    rows = []
    try:
        conn = get_conn(collection)
        try:
            esc_q = q.replace('\\', '\\\\').replace('%', r'\%') \
                    .replace('_', r'\_')
            for r in conn.execute(
                    "SELECT slug, title, content FROM wiki_pages "
                    "WHERE collection = ? AND (title LIKE ? ESCAPE '\\' "
                    "OR content LIKE ? ESCAPE '\\') LIMIT ?",
                    (collection, f"%{esc_q}%", f"%{esc_q}%", limit)).fetchall():
                pos = (r["content"] or "").lower().find(q.lower())
                snippet = (r["content"] or "")[
                    max(0, pos - 20):max(0, pos - 20) + 80] if pos >= 0 else \
                    (r["content"] or "")[:80]
                rows.append({"slug": r["slug"], "title": r["title"],
                             "snippet": snippet})
        finally:
            conn.close()
    except Exception:
        rows = []
    return rows


@app.get("/collections/{collection}/doc/{doc_id}/content")
async def api_doc_content(collection: str, doc_id: int,
                          page: int = None, limit: int = 0,
                          q: str = None):
    """返回单个文献的内容，按页/块/行组织。

    page: 指定页码则只返回该页；省略则返回所有页。
    limit: 限制返回的最大行数（0 = 不限），用于大文献截断。
    """
    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                """SELECT id, title, author, filename, doc_type, page_count,
                          source_tags, bib_type, bib_data, cite_key, lang, tri_rows
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"文献 id={doc_id} 不存在"},
                )

            query = (
                "SELECT page_num, block_num, line_num, text, bbox, block_label, "
                "translit, gloss, trans "
                "FROM lines WHERE doc_id = ?"
            )
            params = [doc_id]
            if page is not None:
                query += " AND page_num = ?"
                params.append(page)
            query += " ORDER BY page_num, block_num, line_num"
            if limit and limit > 0:
                query += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        # 按 page → block 分组组装
        pages_map = {}
        total_lines = 0
        for r in rows:
            pn = r["page_num"]
            bn = r["block_num"]
            if pn not in pages_map:
                pages_map[pn] = {}
            if bn not in pages_map[pn]:
                try:
                    bbox = json.loads(r["bbox"]) if r["bbox"] else None
                except (json.JSONDecodeError, ValueError):
                    bbox = None
                pages_map[pn][bn] = {
                    "block_num": bn,
                    "block_label": r["block_label"],
                    "bbox": bbox,
                    "lines": [],
                }
            pages_map[pn][bn]["lines"].append({
                "line_num": r["line_num"],
                "text": _highlight_keyword(r["text"], q or ""),
                "translit": r["translit"],
                "gloss": r["gloss"],
                "trans": r["trans"],
            })
            total_lines += 1

        pages = []
        for pn in sorted(pages_map):
            blocks = [pages_map[pn][bn] for bn in sorted(pages_map[pn])]
            pages.append({"page_num": pn, "blocks": blocks})

        doc_data = dict(doc)
        try:
            doc_data["source_tags"] = json.loads(
                doc_data.get("source_tags") or '["primary"]'
            )
        except (json.JSONDecodeError, TypeError):
            doc_data["source_tags"] = ["primary"]
        doc_data["bib_data"] = parse_bib_data(doc_data.get("bib_data")) or {}

        return {
            "doc": doc_data,
            "pages": pages,
            "total_lines": total_lines,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 关键词高亮 helper ───────────────────────────────────

def _highlight_keyword(text: str, keyword: str) -> str:
    """把 keyword 中的空格/空白分隔词在 text 里高亮为 <mark>（繁简互转）。"""
    if not text or not keyword:
        return html.escape(text or "")
    from .search import _all_variants

    tokens = [t for t in keyword.split() if t.strip()]
    if not tokens:
        return html.escape(text)
    result = html.escape(text)
    for tok in tokens:
        variants = _all_variants(tok)
        alt = '|'.join(re.escape(v) for v in sorted(variants))
        pattern = re.compile(f'({alt})', re.IGNORECASE)
        result = pattern.sub(r'<mark>\1</mark>', result)
    return result


# ── OCR 校对页 ──────────────────────────────────────────

@app.get("/collections/{collection}/doc/{doc_id}/proofread")
async def proofread_page(
    request: Request,
    collection: str,
    doc_id: int,
    page: int = 1,
    keyword: str = None,
    block: int = None,
    line: int = None,
):
    """图文对照 OCR 校对页。"""
    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                """SELECT id, title, author, filename, page_count, doc_type,
                          source_tags, bib_type, bib_data, cite_key, linked_pdf,
                          lang, tri_rows, page_map
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return templates.TemplateResponse(
                    request,
                    "proofread.html",
                    _nav_ctx("", collection, error=f"文献 id={doc_id} 不存在"),
                    status_code=404,
                )

            page_count = doc["page_count"] or 1

            # md 文档关联了原件 PDF 时，翻页范围跟随 PDF
            # (2026-10-28 原件统一模型: linked_pdf = files/<id>/ 原件路径,
            #  旧格式 uploads 文件名兼容读取)
            linked_pdf = doc["linked_pdf"] if "linked_pdf" in doc.keys() else None
            if linked_pdf:
                from .config import resolve_original_path
                lp_path = resolve_original_path(collection, linked_pdf)
                if lp_path and lp_path.exists():
                    try:
                        lp_doc = pymupdf.open(lp_path)
                        page_count = max(page_count, len(lp_doc))
                        lp_doc.close()
                    except Exception:
                        pass

            page_num = max(1, min(page, page_count))

            # 卡片组 (2026-10-17 整合裁定): 校对页文本侧内嵌四卡+候选队列
            try:
                _migrate_legacy_meta(conn, doc_id)
                _migrate_legacy_summary(conn, doc_id)
                card_rows = conn.execute(
                    "SELECT kind, content, status FROM doc_cards "
                    "WHERE doc_id = ?", (doc_id,)).fetchall()
                cand_rows = conn.execute(
                    "SELECT id, ctype, label, category, confidence, "
                    "page_num, status FROM card_candidates "
                    "WHERE doc_id = ? ORDER BY status, ctype, id",
                    (doc_id,)).fetchall()
            except sqlite3.OperationalError:
                card_rows, cand_rows = [], []
            # P8: 本文献提及 (实体 tab 数据源, 替代旧 per-doc 实体卡)
            try:
                doc_entities = [dict(r) for r in conn.execute(
                    "SELECT m.id, m.surface, m.page_num, m.block_num, m.valid, "
                    "e.id AS entity_id, e.canonical, e.category, e.ext_ids "
                    "FROM entity_mentions m JOIN entities e ON e.id = m.entity_id "
                    "WHERE m.doc_id = ? AND m.valid = 1 "
                    "ORDER BY m.page_num, m.block_num, e.canonical",
                    (doc_id,)).fetchall()]
            except sqlite3.OperationalError:
                doc_entities = []
            cards_map = {}
            for cr in card_rows:
                try:
                    cards_map[cr["kind"]] = {
                        "content": json.loads(cr["content"] or "{}"),
                        "status": cr["status"]}
                except ValueError:
                    cards_map[cr["kind"]] = {"content": {}, "status": cr["status"]}

            rows = conn.execute(
                """SELECT id, page_num, block_num, line_num, text,
                          bbox, block_label, page_w, page_h, suspect,
                          translit, gloss, trans, draft_flags
                   FROM lines
                   WHERE doc_id = ? AND page_num = ?
                   ORDER BY block_num, line_num""",
                (doc_id, page_num),
            ).fetchall()
        finally:
            conn.close()

        # 按 block 分组
        blocks_map = {}
        for r in rows:
            bn = r["block_num"]
            if bn not in blocks_map:
                block_bbox = None
                if r["bbox"]:
                    try:
                        block_bbox = json.loads(r["bbox"])
                    except (json.JSONDecodeError, ValueError):
                        block_bbox = None
                blocks_map[bn] = {
                    "block_num": bn,
                    "block_label": r["block_label"],
                    "bbox": block_bbox,
                    "lines": [],
                }
            line_bbox = None
            if r["bbox"]:
                try:
                    line_bbox = json.loads(r["bbox"])
                except (json.JSONDecodeError, ValueError):
                    line_bbox = None
            try:
                _df = json.loads(r["draft_flags"]) if r["draft_flags"] else {}
            except (json.JSONDecodeError, ValueError):
                _df = {}
            blocks_map[bn]["lines"].append({
                "id": r["id"],
                "line_num": r["line_num"],
                "text": r["text"],
                "suspect": r["suspect"],
                "bbox": line_bbox,
                "translit": r["translit"],
                "gloss": r["gloss"],
                "trans": r["trans"],
                "draft_flags": _df,
            })
        blocks = [blocks_map[bn] for bn in sorted(blocks_map)]

        # OCR 原始页面尺寸: 优先用 DB 存储的 OCR 返回宽高
        orig_width = 0
        orig_height = 0
        for r in rows:
            if r["page_w"] and r["page_w"] > orig_width:
                orig_width = r["page_w"]
            if r["page_h"] and r["page_h"] > orig_height:
                orig_height = r["page_h"]
        # fallback: 旧数据无 page_w/page_h，用 max bbox 近似
        if not orig_width or not orig_height:
            for b in blocks:
                for ln in b["lines"]:
                    bb = ln.get("bbox")
                    if bb and len(bb) >= 4:
                        orig_width = max(orig_width, bb[2])
                        orig_height = max(orig_height, bb[3])
                bb = b.get("bbox")
                if bb and len(bb) >= 4:
                    orig_width = max(orig_width, bb[2])
                    orig_height = max(orig_height, bb[3])

        # 按 150 DPI 渲染时图片自然尺寸
        render_width = None
        render_height = None
        # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
        from .config import resolve_original_path
        pdf_path = resolve_original_path(
            collection,
            doc["linked_pdf"] if "linked_pdf" in doc.keys() else None)
        if pdf_path and pdf_path.exists():
            try:
                pdf_doc = pymupdf.open(pdf_path)
                page_obj = pdf_doc[page_num - 1]
                rect = page_obj.rect
                dpi = 150
                render_width = rect.width * dpi / 72.0
                render_height = rect.height * dpi / 72.0
                pdf_doc.close()
            except Exception:
                pass

        # 兜底: 无 OCR 数据的页 (空白/封皮/OCR 失败) 用渲染尺寸当原始尺寸,
        # 否则前端 orig_width=0 时缩放静默失效; scale=1 自洽
        if not orig_width and render_width:
            orig_width = int(render_width)
            orig_height = int(render_height or orig_height)

        # 图片到原始 OCR 坐标系的缩放；前端再乘 clientWidth/naturalWidth
        scale = 1.0
        if render_width and orig_width:
            scale = render_width / orig_width

        # 解析 source_tags 供模板显示
        try:
            source_tags = json.loads(doc["source_tags"] or '["primary"]')
            if not isinstance(source_tags, list):
                source_tags = ["primary"]
        except (json.JSONDecodeError, TypeError):
            source_tags = ["primary"]

        # 解析 bib 元数据供模板显示
        bib_data = parse_bib_data(doc["bib_data"]) or {}
        bib_type = doc["bib_type"] or ""

        return templates.TemplateResponse(
            request,
            "proofread.html",
            _nav_ctx("", collection,
                doc=dict(doc),
                page_num=page_num,
                page_count=page_count,
                blocks=blocks,
                keyword=keyword or "",
                target_block=block,
                target_line=line,
                scale=scale,
                render_width=render_width,
                render_height=render_height,
                orig_width=orig_width,
                orig_height=orig_height,
                highlight=_highlight_keyword,
                source_tags=source_tags,
                bib_type=bib_type,
                bib_data=bib_data,
                cards_map=cards_map,
                doc_entities=doc_entities,
                candidates=[dict(cr) for cr in cand_rows],
                bib_type_labels=BIB_TYPE_LABELS,
                bib_field_labels=BIB_FIELD_LABELS,
                bib_type_fields=BIB_TYPE_FIELDS,
                linked_pdf=linked_pdf,
                doc_lang=(doc["lang"] or "zh") if "lang" in doc.keys() else "zh",
                page_map=(lambda raw: {
                    "groups": _pm_groups(raw),
                    "default": _pm_default(raw),
                    "total": sum(len(g["anchors"]) for g in _pm_groups(raw)),
                })(json.loads(doc["page_map"])) if (
                    "page_map" in doc.keys() and doc["page_map"]) else None,
                doc_rows=_tri_rows_for(doc),
            ),
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "proofread.html",
            _nav_ctx("", collection, error=str(e)),
            status_code=500,
        )


# ── 页面图片 ────────────────────────────────────────────

def _epub_doc_src(collection: str, doc_id: int):
    """返回 (epub 路径 | None, 错误响应 | None)。"""
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            "SELECT filename, linked_pdf, doc_type FROM documents WHERE id = ?",
            (doc_id,)
        ).fetchone()
    finally:
        conn.close()
    if doc is None:
        return None, JSONResponse(status_code=404, content={"error": "文献不存在"})
    if doc["doc_type"] != "epub":
        return None, JSONResponse(
            status_code=400, content={"error": "该文献不是 EPUB"})
    # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
    from .config import resolve_original_path
    src = resolve_original_path(collection, doc["linked_pdf"])
    if not src or not src.exists():
        return None, JSONResponse(
            status_code=404, content={"error": "EPUB 文件不存在"})
    return src, None


def _epub_media_type(path: str) -> str:
    import mimetypes
    lower = path.lower()
    if lower.endswith((".xhtml", ".html", ".htm")):
        return "application/xhtml+xml"
    if lower.endswith(".css"):
        return "text/css"
    if lower.endswith(".js"):
        return "application/javascript"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


def _inject_base(content: bytes, base_href: str) -> bytes:
    """在 <head> 开头注入 <base>, 使章节内相对资源引用可解析。"""
    base_tag = ('<base href="%s">' % base_href).encode("utf-8")
    low = content.lower()
    idx = low.find(b"<head")
    if idx != -1:
        gt = content.find(b">", idx)
        if gt != -1:
            return content[:gt + 1] + base_tag + content[gt + 1:]
    idx = low.find(b"<html")
    if idx != -1:
        gt = content.find(b">", idx)
        if gt != -1:
            return (content[:gt + 1] + b"<head>" + base_tag + b"</head>"
                    + content[gt + 1:])
    return b"<head>" + base_tag + b"</head>" + content


@app.get("/collections/{collection}/doc/{doc_id}/page/{page_num}/source")
async def page_source(collection: str, doc_id: int, page_num: int):
    """EPUB 章节源文件 (spine 第 N 个 XHTML), 供校对台影印区 iframe 渲染。

    注入 <base> 指向 /raw/ 资源端点, 使章节内的相对图片/样式引用可解析。
    """
    src, err = _epub_doc_src(collection, doc_id)
    if err is not None:
        return err
    try:
        import posixpath
        from urllib.parse import quote
        spine = _epub_spine_files(src)
        if page_num < 1 or page_num > len(spine):
            return JSONResponse(status_code=404, content={"error": "章节超出范围"})
        chapter = spine[page_num - 1]
        chapter_dir = posixpath.dirname(chapter)
        base_href = ("/collections/" + quote(collection, safe="")
                     + "/doc/" + str(doc_id) + "/raw/"
                     + (quote(chapter_dir, safe="/") + "/" if chapter_dir else ""))
        with zipfile.ZipFile(src) as zf:
            content = zf.read(chapter)
        content = _inject_base(content, base_href)
        return Response(content=content, media_type='application/xhtml+xml')
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"读取章节失败: {e}"})


@app.get("/collections/{collection}/doc/{doc_id}/raw/{path:path}")
async def epub_raw_asset(collection: str, doc_id: int, path: str):
    """EPUB 包内资源 (图片/CSS/字体等), 供章节 iframe 按相对路径引用。"""
    src, err = _epub_doc_src(collection, doc_id)
    if err is not None:
        return err
    import posixpath
    rel = posixpath.normpath(path.lstrip("/"))
    if rel in ("", ".") or rel.startswith("../") or rel == ".." or "\x00" in rel:
        return JSONResponse(status_code=404, content={"error": "资源路径非法"})
    try:
        with zipfile.ZipFile(src) as zf:
            try:
                data = zf.read(rel)
            except KeyError:
                return JSONResponse(
                    status_code=404, content={"error": "资源不存在"})
    except Exception as e:
        return JSONResponse(
            status_code=500, content={"error": f"读取资源失败: {e}"})
    return Response(content=data, media_type=_epub_media_type(rel),
                    headers={"Cache-Control": "max-age=3600"})


@app.get("/collections/{collection}/doc/{doc_id}/page/{page_num}/image")
async def page_image(collection: str, doc_id: int, page_num: int):
    """渲染 PDF 单页为 PNG（page_num 从 1 开始）。"""
    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                "SELECT filename, linked_pdf FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "文献不存在"},
                )
            filename = doc["linked_pdf"] or doc["filename"]
        finally:
            conn.close()

        # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
        from .config import resolve_original_path
        pdf_path = resolve_original_path(collection, doc["linked_pdf"])
        if not pdf_path or not pdf_path.exists():
            return JSONResponse(
                status_code=404,
                content={"error": "PDF 文件不存在"},
            )

        pdf_doc = pymupdf.open(pdf_path)
        try:
            if page_num < 1 or page_num > len(pdf_doc):
                return JSONResponse(
                    status_code=404,
                    content={"error": "页码超出范围"},
                )
            page_obj = pdf_doc[page_num - 1]
            pix = page_obj.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            return StreamingResponse(
                io.BytesIO(img_bytes),
                media_type="image/png",
            )
        finally:
            pdf_doc.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 保存行编辑 ──────────────────────────────────────────

@app.post("/collections/{collection}/doc/{doc_id}/line/{line_id}/edit")
async def edit_line(
    collection: str,
    doc_id: int,
    line_id: int,
    text: str = Form(...),
    basis: str = Form(""),
):
    """更新某行文本，并重聚 block 全文更新 FTS。修订史同记 (P1)。"""
    try:
        conn = get_conn(collection)
        try:
            line = conn.execute(
                """SELECT id, doc_id, page_num, block_num, text
                   FROM lines WHERE id = ?""",
                (line_id,),
            ).fetchone()
            if line is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "行不存在"},
                )
            if line["doc_id"] != doc_id:
                return JSONResponse(
                    status_code=400,
                    content={"error": "行不属于该文献"},
                )

            # 修订史 (P1): 旧值留痕
            if line["text"] != text:
                conn.execute(
                    """INSERT INTO revision_log
                       (doc_id, page_num, block_num, old_value, new_value, basis)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        doc_id,
                        line["page_num"],
                        line["block_num"],
                        line["text"],
                        text,
                        basis or "",
                    ),
                )

            conn.execute(
                "UPDATE lines SET text = ?, suspect = NULL WHERE id = ?",
                (text, line_id),
            )

            # 重新聚合该 block 全文 (含行级 trans 拼接进索引)
            block_rows = conn.execute(
                """SELECT text, trans FROM lines
                   WHERE doc_id = ? AND page_num = ? AND block_num = ?
                   ORDER BY line_num""",
                (line["doc_id"], line["page_num"], line["block_num"]),
            ).fetchall()
            block_text = "\n".join(r["text"] for r in block_rows)
            block_trans = " ".join(
                r["trans"] for r in block_rows
                if (r["trans"] or "").strip())

            # 更新 FTS
            conn.execute(
                """DELETE FROM blocks_fts
                   WHERE doc_id = ? AND page_num = ? AND block_num = ?""",
                (line["doc_id"], line["page_num"], line["block_num"]),
            )
            conn.execute(
                """INSERT INTO blocks_fts(doc_id, page_num, block_num,
                                          text, trans)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    line["doc_id"],
                    line["page_num"],
                    line["block_num"],
                    _tokenize(block_text),
                    block_trans or None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 轻量修订史 (P1, 2026-10-17) ──────────────────────────
# 设计: mem 小书房/design/完善路线图.md P1 (依据最终必填, 入包勾选)


@app.get("/collections/{collection}/doc/{doc_id}/revisions")
async def list_revisions(collection: str, doc_id: int, limit: int = 200):
    """本文修订史列表 (最新在前). needs_basis=依据待补 (红色标记项)."""
    try:
        conn = get_conn(collection)
        try:
            rows = conn.execute(
                """SELECT id, page_num, block_num, old_value, new_value,
                          basis, in_pkg, created_at
                   FROM revision_log WHERE doc_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (doc_id, min(max(limit, 1), 1000)),
            ).fetchall()
            return {
                "ok": True,
                "revisions": [
                    {
                        "id": r["id"],
                        "page_num": r["page_num"],
                        "block_num": r["block_num"],
                        "old_value": r["old_value"],
                        "new_value": r["new_value"],
                        "basis": r["basis"],
                        "needs_basis": not (r["basis"] or "").strip(),
                        "in_pkg": bool(r["in_pkg"]),
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ],
            }
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/revision/{rev_id}/basis")
async def set_revision_basis(
    collection: str,
    rev_id: int,
    basis: str = Form(""),
):
    """补填/修订/清空依据. 依据为选填 (用户裁定 2026-10-17 修订)."""
    try:
        conn = get_conn(collection)
        try:
            cur = conn.execute(
                "UPDATE revision_log SET basis = ? WHERE id = ?",
                (basis.strip(), rev_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(
                    status_code=404,
                    content={"error": "修订记录不存在"},
                )
            return {"ok": True}
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/revision/{rev_id}/toggle-pkg")
async def toggle_revision_pkg(collection: str, rev_id: int):
    """切换「入变更包」勾选 (语料包变更通道, P3 消费)."""
    try:
        conn = get_conn(collection)
        try:
            row = conn.execute(
                "SELECT in_pkg FROM revision_log WHERE id = ?",
                (rev_id,),
            ).fetchone()
            if row is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "修订记录不存在"},
                )
            conn.execute(
                "UPDATE revision_log SET in_pkg = ? WHERE id = ?",
                (0 if row["in_pkg"] else 1, rev_id),
            )
            conn.commit()
            return {"ok": True, "in_pkg": not row["in_pkg"]}
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 智能校对 (双通道分歧校对, pqa design/client/19) ─────────

@app.post("/collections/{collection}/doc/{doc_id}/proofread/{page_num}/smart-check")
async def smart_check(
    collection: str,
    doc_id: int,
    page_num: int,
    channel: str = Form("engine"),        # engine | manual
    transcript_id: int = Form(None),
    provider_id: str = Form(None),
):
    """页级智能校对: 生成第二候选转录并与存量对齐, 分歧写 suspect diff。

    engine 通道后台执行 (~10s/页), 轮询 status; manual 通道同步返回。
    """
    from . import smartcheck
    from .ocr.registry import get_plain_provider

    state = smartcheck._get(collection, doc_id, page_num)
    if state and state.get("state") == "running":
        return JSONResponse(status_code=202, content={"state": "running"})

    smartcheck.pop(collection, doc_id, page_num)
    smartcheck._set(collection, doc_id, page_num, state="running")

    if channel == "manual":
        if not transcript_id:
            smartcheck._set(collection, doc_id, page_num, state="error",
                            reason="缺少 transcript_id")
            return JSONResponse(status_code=400,
                                content={"state": "error", "reason": "缺少 transcript_id"})
        smartcheck.run_check(collection, doc_id, page_num, "manual",
                             transcript_id=transcript_id)
        return _smart_status(collection, doc_id, page_num)

    try:
        provider = get_plain_provider(provider_id)
    except RuntimeError as e:
        smartcheck._set(collection, doc_id, page_num, state="error",
                        reason=str(e))
        return JSONResponse(status_code=400,
                            content={"state": "error", "reason": str(e)})

    def _run():
        try:
            smartcheck.run_check(collection, doc_id, page_num, "engine",
                                 provider=provider)
        except Exception as e:  # 兜底, 防 job 永远卡 running
            logger.exception("smart-check 失败")
            smartcheck._set(collection, doc_id, page_num,
                            state="error", reason=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse(status_code=202, content={"state": "running"})


def _smart_status(collection, doc_id, page_num):
    from . import smartcheck
    job = smartcheck._get(collection, doc_id, page_num)
    if job is None:
        return {"state": "idle"}
    out = {k: v for k, v in job.items() if k != "provider"}
    return out


@app.get("/collections/{collection}/doc/{doc_id}/proofread/{page_num}/smart-check/providers")
async def smart_check_providers(collection: str, doc_id: int, page_num: int):
    """引擎通道可用的 OCR provider (支持整图 plain 且存活)。"""
    from .ocr.registry import list_plain_providers
    try:
        return {"providers": list_plain_providers()}
    except Exception:
        return {"providers": []}


@app.get("/collections/{collection}/doc/{doc_id}/proofread/{page_num}/smart-check/status")
async def smart_check_status(collection: str, doc_id: int, page_num: int):
    return _smart_status(collection, doc_id, page_num)


@app.post("/collections/{collection}/doc/{doc_id}/transcripts")
async def upload_transcript(
    collection: str,
    doc_id: int,
    name: str = Form(None),
    text: str = Form(None),
    file: UploadFile = File(None),
):
    """上传人工录入文本 (粘贴 / .txt / .md)。生成新记录, 永不覆盖旧记录。"""
    if file is not None:
        raw = await file.read()
        suffix = (file.filename or "").lower()
        if not suffix.endswith((".txt", ".md", ".markdown")):
            return JSONResponse(status_code=400,
                                content={"error": "仅支持 .txt / .md 文件"})
        if not name:
            name = Path(file.filename).stem
        # .md 剥常见标记 (# 标记、强调星号、链接括号保留文字)
        if suffix != ".txt":
            raw = re.sub(r"^#{1,6}\s*", "", raw.decode("utf-8", "replace"),
                         flags=re.M)
            raw = re.sub(r"\*\*?([^*]+)\*\*?", r"\1", raw)
            text = raw
        else:
            text = raw.decode("utf-8", "replace")
    else:
        if not text:
            return JSONResponse(status_code=400,
                                content={"error": "text 与 file 至少给一个"})
        name = name or f"录入 {time.strftime('%m-%d %H:%M')}"

    conn = get_conn(collection)
    try:
        cur = conn.execute(
            "INSERT INTO manual_transcripts (doc_id, name, text) VALUES (?,?,?)",
            (doc_id, name, text),
        )
        conn.commit()
        return {"ok": True, "transcript_id": cur.lastrowid, "name": name}
    finally:
        conn.close()


@app.get("/collections/{collection}/doc/{doc_id}/transcripts/{transcript_id}/mapping")
async def transcript_mapping(collection: str, doc_id: int, transcript_id: int):
    """预对齐提案: 人工全文 ↔ OCR 页锚点映射 (含置信度)。"""
    from .collate import map_pages

    conn = get_conn(collection)
    try:
        tr = conn.execute(
            "SELECT text FROM manual_transcripts WHERE id = ? AND doc_id = ?",
            (transcript_id, doc_id),
        ).fetchone()
        if tr is None:
            return JSONResponse(status_code=404,
                                content={"error": "录入文本不存在"})
        rows = conn.execute(
            """SELECT page_num, text FROM lines
               WHERE doc_id = ? AND block_label = 'vertical_text'
               ORDER BY page_num, block_num, line_num""",
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()

    page_texts: dict = {}
    for r in rows:
        page_texts.setdefault(r["page_num"], "")
        page_texts[r["page_num"]] += r["text"]

    result = map_pages(page_texts, tr["text"])
    if result["ok"]:
        # 存提案 (confirmed=0), 不覆盖已确认映射
        conn = get_conn(collection)
        try:
            for p in result["pages"]:
                conn.execute(
                    "INSERT OR IGNORE INTO transcript_pages "
                    "(transcript_id, page_num, char_start, char_end, confidence) "
                    "VALUES (?,?,?,?,?)",
                    (transcript_id, p["page_num"], p["char_start"],
                     p["char_end"], p["confidence"]),
                )
            conn.commit()
        finally:
            conn.close()
    return result


@app.post("/collections/{collection}/doc/{doc_id}/transcripts/{transcript_id}/mapping/confirm")
async def confirm_mapping(
    collection: str,
    doc_id: int,
    transcript_id: int,
    pages: str = Form(...),      # JSON: [{"page_num","char_start","char_end"}]
):
    """确认 (可含人工修正) 页映射。"""
    try:
        items = json.loads(pages)
        assert isinstance(items, list)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "pages 须为 JSON 数组"})
    conn = get_conn(collection)
    try:
        conn.execute("DELETE FROM transcript_pages WHERE transcript_id = ?",
                     (transcript_id,))
        for p in items:
            conn.execute(
                "INSERT INTO transcript_pages "
                "(transcript_id, page_num, char_start, char_end, confidence, confirmed) "
                "VALUES (?,?,?,?,?,1)",
                (transcript_id, p["page_num"], p.get("char_start"),
                 p.get("char_end"), p.get("confidence", 1.0)),
            )
        conn.commit()
        return {"ok": True, "pages": len(items)}
    finally:
        conn.close()


@app.get("/collections/{collection}/doc/{doc_id}/transcripts")
async def list_transcripts(collection: str, doc_id: int):
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            """SELECT id, name, created_at,
                      (SELECT COUNT(*) FROM transcript_pages tp
                        WHERE tp.transcript_id = manual_transcripts.id
                          AND tp.confirmed = 1) AS confirmed_pages
               FROM manual_transcripts WHERE doc_id = ? ORDER BY id DESC""",
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()
    return {"transcripts": [dict(r) for r in rows]}


@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/edit")
async def edit_page(
    collection: str,
    doc_id: int,
    page_num: int,
    text: str = Form(""),
    basis: str = Form(""),
    blocks_json: str = Form(""),
):
    """更新整页文本：以空行分隔 block，整体替换，FTS 同步。

    修订史 (P1): DELETE 前对旧块做快照, 与新块按位置对齐 diff,
    变化块记 revision_log (block 粒度 — 整页重写下行号不稳定).
    basis 可空 (自动保存不打断编辑流), 空值=待补, 导出变更包时强制.

    P2 行制转录: blocks_json 提供时走多行制路径 —
    [{text, translit, gloss, trans}, ...] 逐块写入. 列语义按行制:
    三行(满文类): text=拉丁化原文(OCR 直接产出), 无 translit;
    四行(日/俄类): text=原语, translit=拉丁转写. gloss=词语对照/trans=流畅译文.
    diff 分层记史: text 正常记, translit '[转] '/gloss '[对] '/trans '[译] ' 前缀.
    未提供时走原 text 单行路径 (中文文献兼容).
    """
    import json as _json

    tri_blocks = None
    if blocks_json:
        try:
            tri_blocks = _json.loads(blocks_json)
            assert isinstance(tri_blocks, list)
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": "blocks_json 格式错误"}
            )
        for b in tri_blocks:
            if not isinstance(b, dict) or not str(b.get("text", "")).strip():
                return JSONResponse(
                    status_code=400,
                    content={"error": "blocks_json 每块需含非空 text"},
                )

    try:
        conn = get_conn(collection)
        try:
            # 旧块快照 → 修订史 diff (在任何写操作前).
            # 四元组快照: zh 模式后三列恒 None, 多行制齐取 (P2 行制扩展).
            # draft_flags 随行 (P2c): 人工修订某列 (值变) → 清该列 AI 标记,
            # 未变列的标记跨保存保留。
            def _flags_load(raw):
                try:
                    v = json.loads(raw) if raw else {}
                    return v if isinstance(v, dict) else {}
                except (json.JSONDecodeError, ValueError, TypeError):
                    return {}

            old_blocks = [
                (r["text"], r["translit"], r["gloss"], r["trans"],
                 _flags_load(r["draft_flags"]))
                for r in conn.execute(
                    """SELECT text, translit, gloss, trans, draft_flags
                       FROM lines
                       WHERE doc_id = ? AND page_num = ?
                       ORDER BY block_num, line_num, id""",
                    (doc_id, page_num),
                ).fetchall()
            ]
            if tri_blocks is not None:
                def _tri_cell(b, k):
                    v = str(b.get(k, "") or "").strip()
                    return v or None
                new_blocks = [
                    (
                        str(b.get("text", "")).strip("\n"),
                        _tri_cell(b, "translit"),
                        _tri_cell(b, "gloss"),
                        _tri_cell(b, "trans"),
                    )
                    for b in tri_blocks
                ]
            else:
                new_blocks = [
                    (bt.strip("\n"), None, None, None)
                    for bt in text.split("\n\n")
                    if bt.strip()
                ]
            kept_flags: list[dict] = []
            _FLAG_KEYS = {"translit": 1, "gloss": 2, "trans": 3}
            for i in range(max(len(old_blocks), len(new_blocks))):
                old_t = old_blocks[i] if i < len(old_blocks) else None
                new_t = new_blocks[i] if i < len(new_blocks) else None
                flags = dict(old_t[4]) if old_t else {}
                if old_t is not None and new_t is not None:
                    # 值变列 → 清该列 AI 草稿标记 (人工已修订)
                    for key, idx in _FLAG_KEYS.items():
                        if (old_t[idx] or "") != (new_t[idx] or ""):
                            flags.pop(key, None)
                kept_flags.append(flags)
                if old_t == new_t:
                    continue
                # 分层记史: 正文正常记; 转写/词对照/译文带前缀独立成条 (P2)
                if old_t is None or new_t is None:
                    conn.execute(
                        """INSERT INTO revision_log
                           (doc_id, page_num, block_num, old_value, new_value, basis)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, i + 1,
                         old_t[0] if old_t else "",
                         new_t[0] if new_t else "", basis or ""),
                    )
                    continue
                if old_t[0] != new_t[0]:
                    conn.execute(
                        """INSERT INTO revision_log
                           (doc_id, page_num, block_num, old_value, new_value, basis)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, i + 1,
                         old_t[0], new_t[0], basis or ""),
                    )
                if (old_t[1] or "") != (new_t[1] or ""):
                    conn.execute(
                        """INSERT INTO revision_log
                           (doc_id, page_num, block_num, old_value, new_value, basis)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, i + 1,
                         "[转] " + (old_t[1] or ""),
                         "[转] " + (new_t[1] or ""), basis or ""),
                    )
                if (old_t[2] or "") != (new_t[2] or ""):
                    conn.execute(
                        """INSERT INTO revision_log
                           (doc_id, page_num, block_num, old_value, new_value, basis)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, i + 1,
                         "[对] " + (old_t[2] or ""),
                         "[对] " + (new_t[2] or ""), basis or ""),
                    )
                if (old_t[3] or "") != (new_t[3] or ""):
                    conn.execute(
                        """INSERT INTO revision_log
                           (doc_id, page_num, block_num, old_value, new_value, basis)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, i + 1,
                         "[译] " + (old_t[3] or ""),
                         "[译] " + (new_t[3] or ""), basis or ""),
                    )

            # 继承该页原有正文块标签 (整页重写无法逐行映射,
            # 但标签决定检索过滤/智能校对等下游行为, 不能洗成 NULL)
            row = conn.execute(
                """SELECT block_label FROM lines
                   WHERE doc_id = ? AND page_num = ?
                     AND block_label IS NOT NULL
                     AND block_label NOT IN ('number', 'footer', 'doc_title')
                   ORDER BY id LIMIT 1""",
                (doc_id, page_num),
            ).fetchone()
            carry_label = row["block_label"] if row else None
            conn.execute(
                "DELETE FROM lines WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_num),
            )
            conn.execute(
                "DELETE FROM blocks_fts WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_num),
            )

            if tri_blocks is not None:
                # 多行制: 每块一条 line 记录; 列语义按行制
                # (三行: text=拉丁化原文; 四行: text=原语 + translit=拉丁转写)
                def _cell(b, k):
                    v = str(b.get(k, "") or "").strip()
                    return v or None
                for bn, b in enumerate(tri_blocks, 1):
                    conn.execute(
                        """INSERT INTO lines(doc_id, page_num, block_num, line_num,
                                             block_label, text, translit, gloss, trans,
                                             draft_flags)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, bn, 1, carry_label,
                         str(b.get("text", "")).strip("\n"),
                         _cell(b, "translit"), _cell(b, "gloss"),
                         _cell(b, "trans"),
                         json.dumps(kept_flags[bn - 1], ensure_ascii=False)
                         if kept_flags[bn - 1] else None),
                    )
                    conn.execute(
                        """INSERT INTO blocks_fts(doc_id, page_num, block_num,
                                                  text, trans)
                           VALUES (?, ?, ?, ?, ?)""",
                        (doc_id, page_num, bn,
                         _tokenize(str(b.get("text", "")).strip("\n")),
                         (_cell(b, "trans") or None)),
                    )
            else:
                # 块粒度: \n\n 分块, 每块一条 line 记录 — 2026-09-06 批次2
                for bn, bt in enumerate(text.split("\n\n"), 1):
                    bt = bt.strip("\n")
                    if not bt.strip():
                        continue
                    conn.execute(
                        """INSERT INTO lines(doc_id, page_num, block_num, line_num,
                                             block_label, text)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, bn, 1, carry_label, bt),
                    )
                conn.execute(
                    """INSERT INTO blocks_fts(doc_id, page_num, block_num, text)
                       VALUES (?, ?, ?, ?)""",
                    (doc_id, page_num, bn, _tokenize(bt)),
                )

            conn.commit()
            return {"ok": True}
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 手工分栏重新 OCR ────────────────────────────────────

def _reocr_regions_for_page(provider, src, page_num: int, region_list: list):
    """分栏重 OCR 共享逻辑: 按 provider 能力分派两条路径.

    region 坐标在 150dpi 页面像素空间 (与 page_image/bbox overlay 一致)。
    - structured (paddle_http/aistudio_job): 整页按 300dpi 渲染构造单页 PDF
      (与整本重 OCR 相同几何 — 窄高裁切会触发 PaddleOCR-VL 缩放不稳,
      导致空白/幻觉, 见 2026-08-24 调查), OCR 一次后只保留 bbox 落入某
      region 的文字块 (中心点在 region 内, 或与 region 重叠面积 ≥ 块面积
      50%), 坐标换算回 150dpi 空间。block 按 (region 框选顺序, OCR 原始
      顺序) 排列。
    - plain (openai_chat): 裁切直送 _reocr_regions_plain。
    返回 (new_blocks, page_w, page_h, n_regions);
    无有效 region 时抛 ValueError; 调用方保证 page_num 在 1..len(src) 内。
    """
    import tempfile

    OCR_DPI = 300  # 与整本重 OCR 一致的渲染精度

    page_obj = src[page_num - 1]

    # 150 DPI 页面尺寸（REOCR bbox 坐标系，与 page_image 一致）
    page_w = int(round(page_obj.rect.width * 150 / 72.0))
    page_h = int(round(page_obj.rect.height * 150 / 72.0))

    valid_regions = []
    for reg in region_list:
        try:
            x0, y0, x1, y1 = reg
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        valid_regions.append((float(x0), float(y0), float(x1), float(y1)))

    if not valid_regions:
        raise ValueError("没有有效的裁切区域")

    # 纯文本引擎 (openai_chat) 无 block_bbox, 几何过滤无从归属 —
    # 走裁切直送: 每 region 裁图 → ocr_image_plain → 每 region 一块
    if not getattr(provider, "structured", True):
        return _reocr_regions_plain(provider, page_obj, page_w, page_h,
                                    valid_regions)

    # 整页构造单页 PDF, 一次 OCR (页面尺寸=300dpi 像素数, 同 _run_reocr_doc)
    # 防御: 超大页面 300dpi 渲染可能 OOM, 按面积封顶自适应降 dpi (下限 150)
    ocr_dpi = OCR_DPI
    page_area_pt = page_obj.rect.width * page_obj.rect.height
    if page_area_pt > 0:
        max_area_px = 40_000_000  # ~6.6 个 A4@300dpi
        est_area = page_area_pt * (ocr_dpi / 72.0) ** 2
        if est_area > max_area_px:
            ocr_dpi = max(150, int(ocr_dpi * (max_area_px / est_area) ** 0.5))
    pix = page_obj.get_pixmap(dpi=ocr_dpi)
    out_pdf = pymupdf.open()
    cpage = out_pdf.new_page(width=pix.width, height=pix.height)
    cpage.insert_image(cpage.rect, pixmap=pix)
    fd, tmp_pdf = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    try:
        out_pdf.save(tmp_pdf)
    finally:
        out_pdf.close()
    try:
        pages = provider.ocr(tmp_pdf)
    finally:
        os.unlink(tmp_pdf)

    # paddle bbox 像素空间 → 150dpi 空间换算系数
    # (协议: 响应 width/height 与 block_bbox 同一像素空间)
    page0 = pages[0] if pages else {}
    ow = float(page0.get('width') or 0) or float(pix.width) * 2
    oh = float(page0.get('height') or 0) or float(pix.height) * 2
    kx = page_w / ow
    ky = page_h / oh

    # 收集落入 region 的 blocks: (region_idx, ocr_order, block)
    assigned = []
    for order, block in enumerate(page0.get('parsing_res_list', [])):
        label = block.get('block_label', '')
        if label == 'header':
            continue
        content = block.get('block_content', '')
        if isinstance(content, dict):
            text = content.get('html') or content.get('markdown') or ''
        else:
            text = str(content) if content else ''
        text = text.strip()
        if not text:
            continue
        # 块粒度: 不切行, 整块入库 — 2026-09-06 批次2
        lines = [text]
        bbox = block.get('block_bbox')
        if not bbox or len(bbox) < 4:
            continue  # 无几何信息无法归属 region
        try:
            bx0, by0 = bbox[0] * kx, bbox[1] * ky
            bx1, by1 = bbox[2] * kx, bbox[3] * ky
        except (TypeError, ValueError):
            continue  # bbox 非数值, 无法换算
        barea = max(1e-6, (bx1 - bx0) * (by1 - by0))
        cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
        for ri, (rx0, ry0, rx1, ry1) in enumerate(valid_regions):
            center_in = (rx0 <= cx <= rx1) and (ry0 <= cy <= ry1)
            if not center_in:
                ix = max(0.0, min(bx1, rx1) - max(bx0, rx0))
                iy = max(0.0, min(by1, ry1) - max(by0, ry0))
                center_in = (ix * iy) / barea >= 0.5
            if center_in:
                assigned.append((ri, order, {
                    "block_label": label,
                    "bbox": [bx0, by0, bx1, by1],
                    "lines": lines,
                }))
                break  # 一个 block 只归第一个匹配的 region

    assigned.sort(key=lambda t: (t[0], t[1]))
    new_blocks = [b for _, _, b in assigned]
    return new_blocks, page_w, page_h, len(valid_regions)


def _reocr_regions_plain(provider, page_obj, page_w: int, page_h: int,
                         valid_regions: list):
    """裁切直送路径 (openai_chat 等纯文本引擎, .structured == False)。

    每 region: 150dpi 坐标 → 页面 pt → clip 裁图 (300dpi, 超大面积
    自适应降 dpi 同几何路径) → PNG → provider.ocr_image_plain 纯文本。
    每 region 产一块 (bbox=region 本身 150dpi, label='text'),
    顺序=框选顺序; 空结果 region 跳过。
    返回 (new_blocks, page_w, page_h, n_regions) — 与几何过滤路径同构。
    """
    new_blocks = []
    for (x0, y0, x1, y1) in valid_regions:
        # 150dpi 页面像素 → 页面 pt 坐标, 交页 rect 防越界
        clip = pymupdf.Rect(x0 * 72.0 / 150.0, y0 * 72.0 / 150.0,
                            x1 * 72.0 / 150.0, y1 * 72.0 / 150.0)
        clip = clip & page_obj.rect
        if clip.is_empty:
            continue
        dpi = 300
        est = clip.width * clip.height * (dpi / 72.0) ** 2
        if est > 0:
            max_area_px = 40_000_000  # 同几何路径封顶
            if est > max_area_px:
                dpi = max(150, int(dpi * (max_area_px / est) ** 0.5))
        try:
            pix = page_obj.get_pixmap(dpi=dpi, clip=clip)
            png = pix.tobytes("png")
        except Exception:
            continue
        text = provider.ocr_image_plain(png, "image/png")
        text = (text or "").strip()
        if not text:
            continue
        new_blocks.append({
            "block_label": "text",
            "bbox": [x0, y0, x1, y1],
            "lines": [text],
        })
    return new_blocks, page_w, page_h, len(valid_regions)


def _write_reocr_page(conn, doc_id: int, page_num: int, new_blocks: list,
                      page_w: int, page_h: int, replace: bool = True) -> int:
    """分栏重 OCR 结果写回 DB (短事务: DELETE→INSERT→commit), 返回写入行数."""
    if replace:
        conn.execute(
            "DELETE FROM lines WHERE doc_id = ? AND page_num = ?",
            (doc_id, page_num),
        )
        conn.execute(
            "DELETE FROM blocks_fts WHERE doc_id = ? AND page_num = ?",
            (doc_id, page_num),
        )
        block_num = 0
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(block_num), 0) FROM lines "
            "WHERE doc_id = ? AND page_num = ?",
            (doc_id, page_num),
        ).fetchone()
        block_num = row[0]

    total_lines = 0
    for b in new_blocks:
        block_num += 1
        bbox_json = json.dumps(b["bbox"]) if b["bbox"] else None
        full_text = '\n'.join(b["lines"])
        suspect = detect_suspect(full_text, b["block_label"], bbox_json,
                                 page_w, page_h)
        for ln_num, ln_text in enumerate(b["lines"], 1):
            conn.execute(
                """INSERT INTO lines
                   (doc_id, page_num, block_num, line_num, text,
                    bbox, block_label, page_w, page_h, suspect)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, page_num, block_num, ln_num, ln_text,
                 bbox_json, b["block_label"], page_w, page_h, suspect),
            )
            total_lines += 1
        conn.execute(
            """INSERT INTO blocks_fts
               (doc_id, page_num, block_num, text)
               VALUES (?, ?, ?, ?)""",
            (doc_id, page_num, block_num,
             _tokenize(full_text)),
        )
    conn.commit()
    return total_lines


@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/reocr")
def reocr_page(
    collection: str,
    doc_id: int,
    page_num: int,
    regions: str = Form(...),
    replace: bool = Form(True),
):
    """对页面指定区域（手工分栏）重新 OCR。

    regions: JSON 编码的矩形列表，每个 = [x0,y0,x1,y1]，坐标在「150dpi 页面像素空间」
    （与 page_image 端点渲染的图片及现有 bbox overlay 坐标系一致）。
    整页上下文 OCR 一次后按几何过滤，只保留落入 region 的文字块，
    bbox 换算回 150dpi 页面坐标；block 按 (region 框选顺序, OCR 原序) 排列。
    replace=True 时先清空该页原有 lines/blocks_fts 再写入。
    """
    try:
        try:
            region_list = json.loads(regions)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "regions 不是合法 JSON"},
            )
        if not isinstance(region_list, list) or not region_list:
            return JSONResponse(
                status_code=400,
                content={"error": "regions 为空或格式错误"},
            )

        conn = get_conn(collection)
        try:
            doc = conn.execute(
                "SELECT filename, linked_pdf FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
        from .config import resolve_original_path
        pdf_path = resolve_original_path(collection, doc["linked_pdf"])
        if not pdf_path or not pdf_path.exists():
            return JSONResponse(
                status_code=404,
                content={"error": f"源文件不存在: {doc['filename']}"},
            )

        from .ocr import get_provider

        provider = get_provider()
        src = pymupdf.open(pdf_path)
        try:
            if page_num < 1 or page_num > len(src):
                return JSONResponse(
                    status_code=404, content={"error": "页码超出范围"})
            try:
                new_blocks, reocr_page_w, reocr_page_h, n_crops = \
                    _reocr_regions_for_page(provider, src, page_num, region_list)
            except ValueError as ve:
                return JSONResponse(
                    status_code=400, content={"error": str(ve)})
        finally:
            src.close()

        if not new_blocks:
            return JSONResponse(
                status_code=500,
                content={"error": "重新 OCR 未返回任何文本"},
            )

        # 写回 DB
        conn = get_conn(collection)
        try:
            _write_reocr_page(conn, doc_id, page_num, new_blocks,
                              reocr_page_w, reocr_page_h, replace=replace)
        finally:
            conn.close()

        return {
            "status": "ok",
            "doc_id": doc_id,
            "page_num": page_num,
            "regions": n_crops,
            "new_blocks": len(new_blocks),
            "replaced": replace,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 栏系统: 页面阅读区域定义 + 按栏重组 ────────────────────

@app.get("/collections/{collection}/doc/{doc_id}/page/{page_num}/regions")
async def api_get_regions(collection: str, doc_id: int, page_num: int):
    """读该页栏定义: [{region_idx, bbox: [x0,y0,x1,y1], direction}]。"""
    conn = get_conn(collection)
    try:
        rows = conn.execute(
            "SELECT region_idx, bbox, direction FROM page_regions "
            "WHERE doc_id = ? AND page_num = ? ORDER BY region_idx",
            (doc_id, page_num),
        ).fetchall()
        out = []
        for r in rows:
            try:
                bbox = json.loads(r["bbox"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({"region_idx": r["region_idx"], "bbox": bbox,
                        "direction": r["direction"]})
        return {"regions": out}
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/regions")
async def api_save_regions(collection: str, doc_id: int, page_num: int,
                           regions: str = Form(...)):
    """全量保存该页栏定义。regions = [{bbox, direction}] (JSON, 150dpi 空间)。"""
    try:
        payload = json.loads(regions)
    except json.JSONDecodeError:
        return JSONResponse({"error": "regions 不是合法 JSON"}, status_code=400)
    if not isinstance(payload, list) or len(payload) > 50:
        return JSONResponse({"error": "regions 应为列表 (≤50)"}, status_code=400)
    clean = []
    for i, item in enumerate(payload, 1):
        bbox = item.get("bbox")
        direction = item.get("direction", "h")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(v, (int, float)) for v in bbox)
                or direction not in ("h", "v_rtl", "v_ltr")):
            return JSONResponse(
                {"error": f"第 {i} 个栏格式非法 (bbox 四数值 + direction)"},
                status_code=400,
            )
        clean.append((bbox, direction))
    conn = get_conn(collection)
    try:
        conn.execute(
            "DELETE FROM page_regions WHERE doc_id = ? AND page_num = ?",
            (doc_id, page_num),
        )
        for idx, (bbox, direction) in enumerate(clean, 1):
            conn.execute(
                "INSERT INTO page_regions "
                "(doc_id, page_num, region_idx, bbox, direction) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, page_num, idx, json.dumps(bbox), direction),
            )
        conn.commit()
        return {"ok": True, "count": len(clean)}
    finally:
        conn.close()


@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/reorder")
async def api_reorder_by_regions(collection: str, doc_id: int,
                                 page_num: int):
    """按已存栏定义重排该页 block 阅读顺序 (不重跑 OCR)。

    块 bbox 中心命中栏 → 栏间按 region_idx, 栏内按方向排序
    (h: y→x / v_rtl: -x→y / v_ltr: x→y); 未命中栏的块保持原相对顺序放末尾。
    """
    conn = get_conn(collection)
    try:
        regions = []
        for r in conn.execute(
                "SELECT region_idx, bbox, direction FROM page_regions "
                "WHERE doc_id = ? AND page_num = ? ORDER BY region_idx",
                (doc_id, page_num)).fetchall():
            try:
                regions.append((r["region_idx"], json.loads(r["bbox"]),
                                r["direction"]))
            except (json.JSONDecodeError, TypeError):
                continue
        if not regions:
            return JSONResponse({"error": "本页未定义栏, 请先保存栏定义"},
                                status_code=400)

        rows = conn.execute(
            "SELECT id, block_num, text, bbox, block_label, page_w, page_h, "
            "suspect, translit, gloss, trans FROM lines "
            "WHERE doc_id = ? AND page_num = ? "
            "ORDER BY block_num, line_num",
            (doc_id, page_num),
        ).fetchall()
        if not rows:
            return JSONResponse({"error": "本页无文本块"}, status_code=400)

        # 行归并为块单元 (旧行粒度一块多行; 新粒度一块一行)
        from collections import OrderedDict
        blocks_in = OrderedDict()
        for row in rows:
            blocks_in.setdefault(row["block_num"], []).append(row)

        def _center(bbox_json):
            try:
                b = json.loads(bbox_json)
                return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            except (json.JSONDecodeError, TypeError, IndexError):
                return None

        def _sort_key(direction, cx, cy):
            if direction == "v_rtl":
                return (-cx, cy)
            if direction == "v_ltr":
                return (cx, cy)
            return (cy, cx)

        def _block_center(block_rows):
            c = _center(block_rows[0]["bbox"])
            if c:
                return c
            # 无 bbox: 用块内任一行的近似
            for r in block_rows:
                c = _center(r["bbox"])
                if c:
                    return c
            return None

        assigned = {r[0]: [] for r in regions}   # region_idx -> [block_num]
        unassigned = []
        for bn, block_rows in blocks_in.items():
            c = _block_center(block_rows)
            hit = None
            if c:
                for ridx, bbox, _d in regions:
                    if bbox[0] <= c[0] <= bbox[2] and bbox[1] <= c[1] <= bbox[3]:
                        hit = ridx
                        break
            if hit is None:
                unassigned.append(bn)
            else:
                assigned[hit].append(bn)

        ordered = []   # [block_num] 新阅读顺序
        for ridx, _bbox, direction in regions:
            keyed = []
            for bn in assigned[ridx]:
                c = _block_center(blocks_in[bn]) or (0, 0)
                keyed.append((_sort_key(direction, c[0], c[1]), bn))
            keyed.sort(key=lambda kr: kr[0])
            ordered.extend(bn for _, bn in keyed)
        ordered.extend(unassigned)

        # 短事务重写: 全删重插, 保留文本/几何/疑点; 块内行序不动
        conn.execute("DELETE FROM lines WHERE doc_id = ? AND page_num = ?",
                     (doc_id, page_num))
        conn.execute("DELETE FROM blocks_fts WHERE doc_id = ? AND page_num = ?",
                     (doc_id, page_num))
        for new_bn, old_bn in enumerate(ordered, 1):
            block_rows = blocks_in[old_bn]
            texts, transes = [], []
            for ln, row in enumerate(block_rows, 1):
                conn.execute(
                    """INSERT INTO lines
                       (doc_id, page_num, block_num, line_num, text, bbox,
                        block_label, page_w, page_h, suspect,
                        translit, gloss, trans)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (doc_id, page_num, new_bn, ln, row["text"], row["bbox"],
                     row["block_label"], row["page_w"], row["page_h"],
                     row["suspect"], row["translit"], row["gloss"],
                     row["trans"]),
                )
                texts.append(row["text"])
                if (row["trans"] or "").strip():
                    transes.append(row["trans"])
            conn.execute(
                "INSERT INTO blocks_fts (doc_id, page_num, block_num, "
                "text, trans) VALUES (?, ?, ?, ?, ?)",
                (doc_id, page_num, new_bn, _tokenize("\n".join(texts)),
                 " ".join(transes) or None),
            )
        conn.commit()
        return {"ok": True, "blocks": len(ordered),
                "unassigned": len(unassigned)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        conn.close()


# ── 整本重 OCR: 后台任务 + 进度注册表 ─────────────────────

# (job store 与 _run_reocr_doc 已抽到 xsf/reocr.py, 2026-09-04, 手动/自动 OCR 共用)


@app.post("/collections/{collection}/doc/{doc_id}/reocr")
def reocr_doc(collection: str, doc_id: int):
    """对整个文献重新 OCR（后台线程逐页渲染 → OCR → 写入 DB）。

    fire-and-forget: 立即返回, 进度经 /reocr/status 轮询, 关浏览器不中断.
    原同步阻塞版是 2026-08-21 假死/锁库事故源, 见 histflow-plan 过程日志.
    """
    from .ocr import get_provider

    with _reocr_lock:
        job = _reocr_jobs.get((collection, doc_id))
        if job and job.get("status") == "running":
            return JSONResponse(
                status_code=409,
                content={"error": "该文献正在重 OCR 中"},
            )

    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                "SELECT filename, linked_pdf FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
        from .config import resolve_original_path
        pdf_path = resolve_original_path(collection, doc["linked_pdf"])
        if not pdf_path or not pdf_path.exists():
            return JSONResponse(
                status_code=404, content={"error": f"源文件不存在: {doc['filename']}"}
            )

        src = pymupdf.open(pdf_path)
        total_pages = len(src)
        src.close()

        provider = get_provider()
        with _reocr_lock:
            _reocr_jobs[(collection, doc_id)] = {
                "total": total_pages, "done": 0, "status": "running",
                "error": None, "cancel": False,
                "mode": "full", "page_from": 1, "page_to": total_pages,
            }
        threading.Thread(
            target=_run_reocr_doc,
            args=(collection, doc_id, provider, pdf_path, total_pages),
            daemon=True, name=f"reocr-{collection}-{doc_id}",
        ).start()
        return {"started": True, "doc_id": doc_id, "total": total_pages}
    except Exception as e:
        with _reocr_lock:
            _reocr_jobs.pop((collection, doc_id), None)
        return JSONResponse(status_code=500, content={"error": str(e)})


def _run_reocr_regions(collection: str, doc_id: int, provider, pdf_path: Path,
                       page_from: int, page_to: int,
                       template_w: int, template_h: int,
                       region_list: list) -> None:
    """后台线程: 分栏批量重 OCR — region 从模板页等比缩放到每个目标页.

    每页: 查 cancel → 坐标缩放 → _reocr_regions_for_page → 短事务写入。
    OCR 无结果的页跳过 (保留原文本), 仍计入进度。
    """
    logger = logging.getLogger("xsf.reocr")

    total = page_to - page_from + 1
    total_lines = 0
    total_blocks = 0
    skipped = 0
    src = pymupdf.open(pdf_path)
    conn = get_conn(collection)
    try:
        for idx, page_num in enumerate(range(page_from, page_to + 1), 1):
            with _reocr_lock:
                job = _reocr_jobs[(collection, doc_id)]
                if job.get("cancel"):
                    # 注意: 不能在持锁块内调 _reocr_set (非重入锁, 会死锁)
                    job.update(status="cancelled", error="用户取消")
                    cancelled = True
                else:
                    cancelled = False
            if cancelled:
                logger.info("reocr-range 取消 coll=%s doc=%s 完成 %s/%s",
                            collection, doc_id, idx - 1, total)
                return

            # 150dpi 像素尺寸: 模板页 → 目标页 等比缩放
            page_obj = src[page_num - 1]
            tw = page_obj.rect.width * 150 / 72.0
            th = page_obj.rect.height * 150 / 72.0
            sx = tw / template_w if template_w > 0 else 1.0
            sy = th / template_h if template_h > 0 else 1.0
            itw, ith = max(1, int(tw)), max(1, int(th))
            scaled = []
            for reg in region_list:
                try:
                    x0, y0, x1, y1 = reg
                except (TypeError, ValueError):
                    continue
                nx0 = max(0, min(int(round(x0 * sx)), itw - 1))
                ny0 = max(0, min(int(round(y0 * sy)), ith - 1))
                nx1 = max(1, min(int(round(x1 * sx)), itw))
                ny1 = max(1, min(int(round(y1 * sy)), ith))
                if nx1 <= nx0 or ny1 <= ny0:
                    continue  # 缩放后退化 (如极窄框), 跳过
                scaled.append([nx0, ny0, nx1, ny1])

            try:
                new_blocks, pg_w, pg_h, _ = _reocr_regions_for_page(
                    provider, src, page_num, scaled)
            except ValueError:
                new_blocks = []

            if new_blocks:
                total_lines += _write_reocr_page(
                    conn, doc_id, page_num, new_blocks, pg_w, pg_h)
                total_blocks += len(new_blocks)
            else:
                skipped += 1
                logger.warning(
                    "reocr-range 页 %s 无 OCR 结果, 跳过 (保留原文本)", page_num)
            _reocr_set(collection, doc_id, done=idx)

        _reocr_set(collection, doc_id, status="done", done=total)
        logger.info("reocr-range 完成 coll=%s doc=%s: 页 %s-%s, %s 块 %s 行, 跳过 %s 页",
                    collection, doc_id, page_from, page_to,
                    total_blocks, total_lines, skipped)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        done = (_reocr_get(collection, doc_id) or {}).get("done", 0)
        _reocr_set(collection, doc_id, status="error", error=str(e))
        logger.warning("reocr-range 失败 coll=%s doc=%s @页%s/%s: %s",
                       collection, doc_id, done, total, e)
    finally:
        conn.close()
        src.close()
        # 终态 (done/error/cancelled) 保留 10 分钟供前端查询, 之后回收
        def _cleanup():
            import time
            time.sleep(600)
            with _reocr_lock:
                job = _reocr_jobs.get((collection, doc_id))
                if job and job.get("status") != "running":
                    _reocr_jobs.pop((collection, doc_id), None)
        threading.Thread(target=_cleanup, daemon=True).start()


@app.post("/collections/{collection}/doc/{doc_id}/reocr-range")
def reocr_range(
    collection: str,
    doc_id: int,
    regions: str = Form(...),
    page_from: int = Form(...),
    page_to: int = Form(...),
    template_page: int = Form(...),
):
    """把手工分栏 region 批量应用到页范围 (后台线程逐页 OCR).

    region 坐标在 template_page (150dpi 页面像素空间) 上框选;
    每个目标页按页面尺寸等比缩放坐标后, 整页上下文 OCR + region 几何过滤,
    总是替换目标页原文本。
    fire-and-forget: 立即返回, 进度经 /reocr/status 轮询 (mode=regions)。
    """
    from .ocr import get_provider

    try:
        try:
            region_list = json.loads(regions)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "regions 不是合法 JSON"},
            )
        if not isinstance(region_list, list) or not region_list:
            return JSONResponse(
                status_code=400,
                content={"error": "regions 为空或格式错误"},
            )
        if len(region_list) > 200:
            return JSONResponse(
                status_code=400,
                content={"error": "regions 过多 (单次 ≤200 个)"},
            )

        with _reocr_lock:
            job = _reocr_jobs.get((collection, doc_id))
            if job and job.get("status") == "running":
                return JSONResponse(
                    status_code=409,
                    content={"error": "该文献已有重 OCR 任务进行中"},
                )

        conn = get_conn(collection)
        try:
            doc = conn.execute(
                "SELECT filename, linked_pdf FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        # 原件统一模型: 单布局收敛 (2026-10-31), 只认 files/<id>/ (linked_pdf)
        from .config import resolve_original_path
        pdf_path = resolve_original_path(collection, doc["linked_pdf"])
        if not pdf_path or not pdf_path.exists():
            return JSONResponse(
                status_code=404,
                content={"error": f"源文件不存在: {doc['filename']}"},
            )

        src = pymupdf.open(pdf_path)
        try:
            total_pages = len(src)
            if not (1 <= page_from <= page_to <= total_pages):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"页码范围无效: 需满足 1 ≤ 起 ≤ 止 ≤ {total_pages}"},
                )
            if not (1 <= template_page <= total_pages):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"template_page 超出范围 (1–{total_pages})"},
                )
            t_rect = src[template_page - 1].rect
            template_w = max(1, int(round(t_rect.width * 150 / 72.0)))
            template_h = max(1, int(round(t_rect.height * 150 / 72.0)))
        finally:
            src.close()

        provider = get_provider()
        total = page_to - page_from + 1
        with _reocr_lock:
            _reocr_jobs[(collection, doc_id)] = {
                "total": total, "done": 0, "status": "running",
                "error": None, "cancel": False,
                "mode": "regions", "page_from": page_from, "page_to": page_to,
            }
        threading.Thread(
            target=_run_reocr_regions,
            args=(collection, doc_id, provider, pdf_path,
                  page_from, page_to, template_w, template_h, region_list),
            daemon=True, name=f"reocr-range-{collection}-{doc_id}",
        ).start()
        return {
            "started": True, "doc_id": doc_id, "total": total,
            "page_from": page_from, "page_to": page_to,
        }
    except Exception as e:
        with _reocr_lock:
            _reocr_jobs.pop((collection, doc_id), None)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/collections/{collection}/reocr/status")
async def reocr_status(collection: str):
    """本书架所有重 OCR 任务进度快照 (纯内存读, 不打 DB)."""
    with _reocr_lock:
        jobs = [
            {"doc_id": k[1], **{kk: vv for kk, vv in v.items() if kk != "cancel"}}
            for k, v in _reocr_jobs.items() if k[0] == collection
        ]
    return {"jobs": jobs}


@app.post("/collections/{collection}/doc/{doc_id}/reocr/cancel")
async def reocr_cancel(collection: str, doc_id: int):
    """请求取消: 每页循环开头检查 cancel 标志, 当前页 OCR 完成后停止."""
    with _reocr_lock:
        job = _reocr_jobs.get((collection, doc_id))
        if not job or job.get("status") != "running":
            return JSONResponse(
                status_code=404, content={"error": "无进行中的任务"},
            )
        job["cancel"] = True
    return {"ok": True, "doc_id": doc_id}


# ── 命中文档检索（校对页内搜索）─────────────────────────

@app.get("/collections/{collection}/doc/{doc_id}/hits")
async def doc_hits(collection: str, doc_id: int, keyword: str, limit: int = 300):
    """返回某文档中匹配关键词的所有 page/block/first_line_id + 高亮摘要。"""
    try:
        from .search import _fts_plan, _fts_where, _entity_expansions

        if limit < 1:
            limit = 300

        conn = get_conn(collection)
        try:
            match_expr, like_terms = _fts_plan(
                keyword, _entity_expansions(conn, keyword))
            if not match_expr and not like_terms:
                return {"keyword": keyword, "hits": [], "total": 0,
                        "truncated": False}
            fts_where, fts_params = _fts_where(match_expr, like_terms)
            rows = conn.execute(
                f"""SELECT f.page_num, f.block_num, MIN(l.id) as line_id, l.text as text
                   FROM blocks_fts f
                   JOIN lines l ON l.doc_id = f.doc_id
                               AND l.page_num = f.page_num
                               AND l.block_num = f.block_num
                   WHERE f.doc_id = ? AND {fts_where}
                   GROUP BY f.page_num, f.block_num
                   ORDER BY f.page_num, f.block_num""",
                [doc_id] + fts_params,
            ).fetchall()
        finally:
            conn.close()

        total = len(rows)
        truncated = total > limit
        hits = []
        for r in rows[:limit]:
            text = (r["text"] or "").strip()
            if len(text) > 80:
                idx = max((text.lower().find(v.lower()) for v in {keyword}
                           if text.lower().find(v.lower()) >= 0), default=-1)
                if idx >= 0:
                    start = max(0, idx - 30)
                    text = ("…" if start > 0 else "") + text[start:start + 80] + "…"
                else:
                    text = text[:80] + "…"
            hits.append({
                "page_num": r["page_num"],
                "block_num": r["block_num"],
                "line_id": r["line_id"],
                "snippet": _highlight_keyword(text, keyword),
            })

        return {
            "keyword": keyword,
            "hits": hits,
            "total": total,
            "truncated": truncated,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

