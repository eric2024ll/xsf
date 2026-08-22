"""xsf FastAPI Web 界面"""

import asyncio
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import List

import pymupdf
from fastapi import FastAPI, UploadFile, File, Form, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    get_collections_dir, list_collections, get_auth_token, get_db_path,
)
from . import users as user_store
from .db import init_db, get_conn
from .search import search, get_block_lines, get_context, get_highlight_terms
from .bib_utils import (
    generate_cite_key, sync_doc_fields, parse_bib_data, to_bibtex,
    parse_bib_entries, match_docs_to_entries,
    BIB_TYPE_FIELDS, BIB_TYPE_LABELS, BIB_FIELD_LABELS,
)
from .ingest import (
    ingest_pdf, ingest_scanned_pdf, ingest_markdown, ingest_image,
    remove_doc, _tokenize,
)

_VERSION = "0.1.0"

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


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
    """启动时初始化所有已有 collection 的 DB"""
    for coll in list_collections():
        init_db(coll)
    (get_collections_dir() / "uploads").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="小書房：放書架的地方，也能看書", version=_VERSION, lifespan=lifespan)

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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ── Auth Middleware (admin + 临时用户双角色) ──────────
#
# 角色:
#   admin  — cookie xsf_auth == XSF_AUTH_TOKEN (无状态, mcp_adapter 兼容, 重启不掉线)
#   guest  — cookie xsf_session = 内存 session id (登录时签发, 重启需重登)
#   开发模式 — 未设 XSF_AUTH_TOKEN 时全部视为 admin (沿用旧语义)

_PUBLIC_PATHS = {'/login', '/logout'}
_PUBLIC_PREFIXES = ('/login', '/static', '/favicon')
_GUEST_PAGE_BLOCK_RE = re.compile(r'^/collections/[^/]+/upload$')

_SESSION_TTL = 7 * 24 * 3600  # guest session 7 天
_sess_lock = threading.Lock()
_sessions: dict = {}  # sid -> {"username": str, "expires": float}


def _sess_prune():
    now = time.time()
    for sid in [s for s, v in _sessions.items() if v["expires"] <= now]:
        del _sessions[s]


def _sess_create(username: str) -> str:
    sid = secrets.token_urlsafe(32)
    with _sess_lock:
        _sess_prune()
        _sessions[sid] = {"username": username, "expires": time.time() + _SESSION_TTL}
    return sid


def _sess_get(sid: str):
    if not sid:
        return None
    with _sess_lock:
        _sess_prune()
        return _sessions.get(sid)


def _get_role(request: Request) -> str | None:
    """解析请求角色: 'admin' | 'guest' | None(未认证)."""
    token = get_auth_token()
    if token is None:
        return 'admin'  # 开发模式
    legacy = request.cookies.get('xsf_auth', '')
    if legacy and secrets.compare_digest(legacy, token):
        return 'admin'
    sess = _sess_get(request.cookies.get('xsf_session', ''))
    if sess is not None:
        return 'guest'
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        role = _get_role(request)
        request.state.role = role or ''

        path = request.url.path

        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        if role is None:
            is_api = path.startswith('/api/') or path.startswith('/collections/')
            if is_api:
                return JSONResponse(
                    status_code=401,
                    content={"error": "未认证，请先登录"},
                )
            return RedirectResponse('/login', status_code=303)

        if role == 'guest':
            # 写操作一律拒绝 (导出类 POST 端点含在内, 裁定: 临时用户仅搜索+浏览)
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                return JSONResponse(
                    status_code=403,
                    content={"error": "临时用户无此权限，请联系管理员"},
                )
            # 管理页 / 上传页禁入
            if path == '/users' or path.startswith('/api/users'):
                return JSONResponse(status_code=403, content={"error": "需要管理员权限"})
            if _GUEST_PAGE_BLOCK_RE.match(path):
                return RedirectResponse('/', status_code=303)

        return await call_next(request)


app.add_middleware(AuthMiddleware)


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


# ── Auth: login / logout ──────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error},
    )


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(''), password: str = Form(...)):
    token = get_auth_token()
    if token is None:
        return RedirectResponse('/', status_code=303)  # 开发模式

    username = (username or '').strip()

    def _err(msg):
        return templates.TemplateResponse(request, "login.html", {"error": msg})

    # ── 管理员分支 ──
    if not username or username.lower() == 'admin':
        if secrets.compare_digest(password, token):
            resp = RedirectResponse('/', status_code=303)
            resp.set_cookie(
                'xsf_auth', token,
                httponly=True,
                max_age=7 * 24 * 3600,
                samesite='lax',
            )
            return resp
        return _err("管理员密码错误")

    # ── 临时用户分支 ──
    status, _user = user_store.verify_user(username, password)
    if status == 'ok':
        sid = _sess_create(username)
        resp = RedirectResponse('/', status_code=303)
        resp.set_cookie(
            'xsf_session', sid,
            httponly=True,
            max_age=_SESSION_TTL,
            samesite='lax',
        )
        return resp
    if status == 'disabled':
        return _err("账号已停用，请联系管理员")
    if status == 'expired':
        return _err("账号已过期，请联系管理员")
    return _err("用户名或密码错误")


@app.get("/logout")
async def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie('xsf_auth')
    resp.delete_cookie('xsf_session')
    return resp


# ── 用户管理 (仅 admin; guest 被 middleware 拦截) ──────

@app.get("/users")
async def users_page(request: Request):
    return templates.TemplateResponse(
        request,
        "users.html",
        _nav_ctx("users") | {"users": user_store.list_users()},
    )


@app.get("/api/users")
async def api_list_users(request: Request):
    return {"users": user_store.list_users()}


@app.post("/api/users")
async def api_create_user(request: Request,
                          username: str = Form(...),
                          password: str = Form(''),
                          days: str = Form(''),
                          note: str = Form('')):
    days_val = days.strip() if days and days.strip() else None
    err, user, plain = user_store.create_user(username, password or None, days_val, note)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return {"ok": True, "user": user, "password": plain}  # 密码仅此一次返回


@app.post("/api/users/{username}/toggle")
async def api_toggle_user(request: Request, username: str):
    infos = {u["username"]: u for u in user_store.list_users()}
    if username not in infos:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    err = user_store.set_enabled(username, not infos[username]["enabled"])
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return {"ok": True}


@app.delete("/api/users/{username}")
async def api_delete_user(request: Request, username: str):
    err, deleted = user_store.delete_user(username)
    if err:
        return JSONResponse(status_code=404, content={"error": err})
    return {"ok": True, "deleted": deleted}


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
    name = (collection or "").strip()
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    if "/" in name or "\\" in name or name in (".", ".."):
        return JSONResponse({"error": "名称包含非法字符"}, status_code=400)
    if name in list_collections():
        return JSONResponse({"error": "书架已存在"}, status_code=409)
    init_db(name)
    return {"ok": True, "name": name}


@app.delete("/api/collections/{collection}")
async def api_delete_collection(collection: str):
    """删除书架: DB 目录 + 源文件目录."""
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
    if not new_name:
        return JSONResponse({"error": "新名称不能为空"}, status_code=400)
    if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        return JSONResponse({"error": "名称包含非法字符"}, status_code=400)
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
    """provider 列表 (api_key 打码) + default。"""
    from .config import (get_ocr_providers, get_default_ocr_provider_id)
    providers = []
    for p in get_ocr_providers():
        providers.append({
            'id': p['id'],
            'name': p.get('name', p['id']),
            'type': p.get('type', 'generic_http'),
            'url': p.get('url', ''),
            'model': p.get('model') or '',
            'has_key': bool(p.get('api_key')),
            'updated_at': p.get('updated_at') or p.get('created_at'),
        })
    return {
        'providers': providers,
        'default': get_default_ocr_provider_id(),
    }


@app.post("/api/ocr-config/provider")
async def api_save_ocr_provider(name: str = Form(...), url: str = Form(''),
                                id: str = Form(None),
                                api_key: str = Form(None),
                                model: str = Form(None),
                                type: str = Form('generic_http')):
    """新增/编辑 provider。api_key 留空且为编辑 → 保留旧值。"""
    from .config import save_ocr_provider
    try:
        p = save_ocr_provider(name=name, url=url, pid=id,
                              api_key=(api_key or '').strip() or None,
                              model=model, type=type)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if p.get('type') == 'aistudio' and not p.get('api_key'):
        return JSONResponse(
            {"error": "aistudio 类型必须填写 token (API Key)"}, status_code=400
        )
    return {"ok": True, "id": p['id']}


@app.delete("/api/ocr-config/provider/{pid}")
async def api_delete_ocr_provider(pid: str):
    from .config import delete_ocr_provider
    if not delete_ocr_provider(pid):
        return JSONResponse({"error": f"provider 不存在: {pid}"},
                            status_code=404)
    return {"ok": True}


@app.post("/api/ocr-config/default")
async def api_set_ocr_default(id: str = Form(...)):
    from .config import set_default_ocr_provider
    try:
        set_default_ocr_provider(id)
    except KeyError:
        return JSONResponse({"error": f"provider 不存在: {id}"},
                            status_code=404)
    return {"ok": True, "default": id}


@app.post("/api/ocr-config/test")
def api_test_ocr_config(id: str = Form(None), url: str = Form(None),
                        api_key: str = Form(None),
                        model: str = Form(None)):
    """测试 provider 连通: 发 1 页空白 PDF, 校验 200 + pages 结构。

    sync 端点 (线程池执行): 内部 requests.post/aistudio 轮询为阻塞调用.
    id 非空 → 用已保存配置 (url/api_key 参数可覆盖);
    无 id → 用表单传入的 url/api_key (添加前预检)。
    """
    import tempfile
    import requests as _req
    from .ocr.http_api import _normalize_pages

    ptype = 'generic_http'
    if id:
        try:
            from .config import get_ocr_provider_cfg
            cfg = get_ocr_provider_cfg(id)
            ptype = cfg.get('type', 'generic_http')
            test_url = url or cfg['url']
            test_key = api_key or cfg.get('api_key')
            test_model = model if model is not None else cfg.get('model')
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)},
                                status_code=400)
    else:
        test_url = (url or '').strip()
        test_key = (api_key or '').strip() or None
        test_model = (model or '').strip() or None
        if not test_url and ptype == 'generic_http':
            return JSONResponse({"ok": False, "error": "未指定 provider id 或 url"},
                                status_code=400)

    fd, tmp_pdf = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    try:
        doc = pymupdf.open()
        doc.new_page(width=72, height=72)
        doc.save(tmp_pdf)
        doc.close()

        if ptype == 'aistudio':
            from .ocr.aistudio_api import ocr_file_aistudio
            try:
                pages = ocr_file_aistudio(tmp_pdf, token=test_key or '',
                                          model=test_model)
                return {"ok": True, "pages": len(pages)}
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)},
                                    status_code=502)

        headers = {"Authorization": f"Bearer {test_key}"} if test_key else {}
        form = {"model": test_model} if test_model else {}
        r = _req.post(test_url, headers=headers, data=form,
                      files={"file": open(tmp_pdf, 'rb')}, timeout=120)
        if r.status_code == 200:
            try:
                pages = _normalize_pages(r.json())
                return {"ok": True, "pages": len(pages)}
            except (ValueError, Exception) as e:
                return JSONResponse(
                    {"ok": False, "error": f"响应不是 pages 形态: {e}"},
                    status_code=502,
                )
        if r.status_code in (401, 403):
            return JSONResponse(
                {"ok": False, "error": f"鉴权失败 (HTTP {r.status_code}): 检查 api_key"},
                status_code=r.status_code,
            )
        return JSONResponse(
            {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"},
            status_code=502,
        )
    except _req.RequestException as e:
        return JSONResponse(
            {"ok": False, "error": f"网络错误: {e}"}, status_code=504
        )
    finally:
        try:
            os.unlink(tmp_pdf)
        except OSError:
            pass


# ── 搜索 ──────────────────────────────────────────────

@app.get("/collections/{collection}/search")
async def api_search(collection: str, q: str, limit: int = 20,
                     source_type: str = None):
    try:
        results = search(q, collection=collection, limit=limit,
                         source_type=source_type)
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
        return {"query": q, "collection": collection,
                "count": len(out), "results": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── SAG 语义搜索 ───────────────────────────────────────

@app.get("/collections/{collection}/sag-search")
async def api_sag_search(collection: str, q: str, limit: int = 10,
                         mode: str = "vector"):
    """SAG 语义搜索 (mode: vector|multi). SAG 不可用自动降级 FTS5.

    响应带 engine 字段 ("sag" | "fts5") 供前端区分.
    """
    from . import sag_integration
    from .search import get_highlight_terms

    if mode not in ("vector", "multi"):
        mode = "vector"

    try:
        try:
            results = sag_integration.search(q, collection,
                                             mode=mode, top_k=limit)
            out = []
            for r in results:
                out.append({
                    "doc_id": r["doc_id"],
                    "page_num": None,
                    "block_num": None,
                    "line_id": None,
                    "filename": r.get("filename"),
                    "title": r.get("title"),
                    "cite_key": r.get("cite_key"),
                    "score": r.get("score"),
                    "text": _highlight_keyword(r["text"], q),
                })
            return {"query": q, "collection": collection, "engine": "sag",
                    "mode": mode, "count": len(out), "results": out}
        except sag_integration.SagUnavailable:
            pass  # 降级 FTS5

        # ── 降级: FTS5 ──
        fts_results = search(q, collection=collection, limit=limit)
        out = []
        for r in fts_results:
            lines_list = get_block_lines(
                r["doc_id"], r["page_num"], r["block_num"], collection
            )
            text = " ".join(lines_list)
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
                "score": None,
                "text": _highlight_keyword(text, q),
            })
        return {"query": q, "collection": collection, "engine": "fts5",
                "mode": mode, "count": len(out), "results": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/sag-status")
async def api_sag_status():
    """SAG 可用性探测 (前端切换搜索模式用)."""
    from . import sag_integration
    enabled = sag_integration.sag_base_url() is not None
    return {"enabled": enabled, "healthy": sag_integration.health() if enabled else False}


# ── SAG 手动同步 (脏标记 + 并发去重) ─────────────────────

_sag_sync_lock = threading.Lock()
_sag_syncing: set = set()


@contextmanager
def _sag_sync_guard(collection: str, doc_id: int):
    """(collection, doc_id) 粒度的同步互斥; 已在同步中时 yield True."""
    key = (collection, doc_id)
    with _sag_sync_lock:
        busy = key in _sag_syncing
        if not busy:
            _sag_syncing.add(key)
    try:
        yield busy
    finally:
        if not busy:
            with _sag_sync_lock:
                _sag_syncing.discard(key)


@app.post("/collections/{collection}/doc/{doc_id}/sag-sync")
def api_sag_sync(collection: str, doc_id: int):
    """手动同步单篇文档到 SAG (幂等: 删旧版→重 ingest), 成功清脏标记.

    sync 端点: SAG ingest 大文档需数分钟, 走线程池不阻塞事件循环.
    """
    from . import sag_integration
    logger = logging.getLogger("xsf.sag")

    if not sag_integration.sag_base_url():
        return JSONResponse(
            status_code=503,
            content={"error": "SAG 未配置 (XSF_SAG_URL)"},
        )
    # 并发去重: 同一篇正在同步中直接拒绝 (幂等重写若交错会产生 SAG 重复条目)
    with _sag_sync_guard(collection, doc_id) as busy:
        if busy:
            return JSONResponse(
                status_code=409,
                content={"error": "该文档正在同步中"},
            )
        try:
            result = sag_integration.sync_doc(doc_id, collection)
        except Exception as e:
            logger.warning("SAG sync 失败 coll=%s doc=%s: %s", collection, doc_id, e)
            try:
                conn = get_conn(collection)
                try:
                    conn.execute(
                        "UPDATE documents SET sag_dirty = 1 WHERE id = ?",
                        (doc_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
            return JSONResponse(status_code=502, content={"error": str(e)})
        if not result.get("synced"):
            return JSONResponse(
                status_code=404,
                content={"error": f"文档不可同步: {result.get('reason')}"},
            )
        conn = get_conn(collection)
        try:
            conn.execute(
                "UPDATE documents SET sag_dirty = 0 WHERE id = ?", (doc_id,),
            )
            conn.commit()
        finally:
            conn.close()
    logger.info("SAG sync 完成 coll=%s doc=%s (%s)",
                collection, doc_id, result.get("title"))
    return {"ok": True, "doc_id": doc_id, "title": result.get("title")}


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
_SUPPORTED_EXT = {'pdf', 'md', 'markdown'} | _SUPPORTED_IMG_EXT


def _file_ext(filename: str) -> str:
    """返回小写扩展名（无点）。无扩展名返回 ''。"""
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[-1].lower()


@app.post("/collections/{collection}/add")
async def api_add(
    collection: str,
    files: List[UploadFile] = File(...),
    cite_key: str = Form(None),
    title: str = Form(None),
    author: str = Form(None),
    ocr: bool = Form(False),
    source_tags: str = Form('["primary"]'),
):
    """批量上传：支持 pdf / md / 图片（图片自动 OCR）。

    - pdf：born-digital 解析；ocr=True 则走 PaddleOCR-VL
    - md/markdown：按段落解析入库（doc_type='markdown'）
    - 图片（jpg/png/...）：包成单页 PDF 后强制 OCR（doc_type='ocr'）
    - source_tags: JSON 数组字符串，如 '["primary","档案"]'
    返回 {status, results:[...], errors:[...]}。
    """
    try:
        init_db(collection)
        upload_dir = get_collections_dir() / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        results = []
        errors = []
        for file in files:
            fname = file.filename or "unknown"
            ext = _file_ext(fname)

            if ext not in _SUPPORTED_EXT:
                errors.append({
                    "filename": fname,
                    "error": f"不支持的类型 .{ext or '?'}（支持 pdf/md/图片）",
                })
                continue

            # 去重检查: 同 filename 已入库则跳过
            conn = get_conn(collection)
            existing = conn.execute(
                "SELECT id FROM documents WHERE filename = ?",
                (fname,),
            ).fetchone()
            conn.close()
            if existing:
                errors.append({
                    "filename": fname,
                    "error": f"'{fname}' 已在书架「{collection}」中",
                    "doc_id": existing["id"],
                    "duplicate": True,
                })
                continue

            # 落盘
            dst = upload_dir / fname
            content = await file.read()
            with open(dst, "wb") as f:
                f.write(content)

            # 分派
            try:
                if ext == 'pdf':
                    ingest_func = ingest_scanned_pdf if ocr else ingest_pdf
                elif ext in ('md', 'markdown'):
                    ingest_func = ingest_markdown
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
                results.append({"filename": fname, "result": r})
            except Exception as e:
                errors.append({"filename": fname, "error": str(e)})

        return {
            "status": "ok",
            "results": results,
            "errors": errors,
            "total": len(files),
            "succeeded": len(results),
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


@app.patch("/collections/{collection}/doc/{doc_id}")
async def api_update_doc(collection: str, doc_id: int,
                         source_tags: str = Form(None),
                         bib_type: str = Form(None),
                         bib_data: str = Form(None)):
    """更新文献元数据。

    可选字段（至少传一个）:
    - source_tags: JSON 数组字符串（来源标签）
    - bib_type: 文献类型（任意标准 BibTeX 类型, 如 @book/@article/@phdthesis 等 15 类）
    - bib_data: JSON 对象字符串（biblatex 字段）
    """
    try:
        if source_tags is None and bib_type is None and bib_data is None:
            return JSONResponse(
                status_code=400,
                content={"error": "未提供可更新字段"},
            )

        updates = []
        params = []
        result_extra = {}

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
        updates.append("sag_dirty = 1")  # 元数据变更 → SAG 待重同步
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
                       file: UploadFile = File(...)):
    """为文本文档（如 md）关联 PDF 文件（纯展示，不 OCR）。"""
    try:
        fname = file.filename or "linked.pdf"
        ext = _file_ext(fname)
        if ext != 'pdf':
            return JSONResponse(
                status_code=400,
                content={"error": "仅支持 PDF 文件"},
            )

        upload_dir = get_collections_dir() / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dst = upload_dir / fname
        content = await file.read()
        with open(dst, "wb") as f:
            f.write(content)

        page_count = 1
        try:
            pdf_doc = pymupdf.open(dst)
            page_count = len(pdf_doc)
            pdf_doc.close()
        except Exception:
            pass

        conn = get_conn(collection)
        try:
            cur = conn.execute(
                "UPDATE documents SET linked_pdf = ? WHERE id = ?",
                (fname, doc_id),
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
            "linked_pdf": fname,
            "page_count": page_count,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 文献列表 ──────────────────────────────────────────

@app.get("/collections/{collection}/docs")
async def api_docs(collection: str, limit: int = 50):
    try:
        conn = get_conn(collection)
        try:
            rows = conn.execute(
                """SELECT id, cite_key, title, author,
                          filename, page_count, doc_type,
                          is_primary, is_secondary, is_reference,
                          sag_dirty, created_at
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
                 bib_type_labels=BIB_TYPE_LABELS),
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
                          source_tags, cite_key
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
                    "substr(json_extract(d.bib_data, '$.date'), 1, 4) = ?"
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
        finally:
            conn.close()

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
            "docs": [dict(r) for r in rows],
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
                           title=COALESCE(?, title), author=COALESCE(?, author),
                           sag_dirty=1
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


@app.post("/collections/{collection}/docs/export-archive")
async def api_export_archive(collection: str, request: Request):
    """导出选中文献为数据包 (tar.gz)：含 export.db + uploads/ + manifest.json。"""
    try:
        body = await request.json()
        ids = body.get("ids", [])
        if not ids:
            return JSONResponse(status_code=400,
                                content={"error": "未选择文献"})

        db_path = get_db_path(collection)
        if not db_path.exists():
            return JSONResponse(status_code=404,
                                content={"error": "书架不存在"})

        placeholders = ",".join("?" * len(ids))

        # 1. SQLite backup 到临时 DB，然后只保留选中文献
        fd, tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        src_conn = get_conn(collection)
        try:
            backup_conn = sqlite3.connect(tmp_db)
            with src_conn:
                src_conn.backup(backup_conn)
            backup_conn.close()

            # 删除非选中文献
            clean = sqlite3.connect(tmp_db)
            clean.row_factory = sqlite3.Row
            clean.execute(f"DELETE FROM lines WHERE doc_id NOT IN ({placeholders})", ids)
            clean.execute(f"DELETE FROM blocks_fts WHERE doc_id NOT IN ({placeholders})", ids)
            clean.execute(f"DELETE FROM documents WHERE id NOT IN ({placeholders})", ids)
            clean.commit()
            filenames = [r[0] for r in clean.execute(
                "SELECT filename FROM documents"
            ).fetchall()]
            doc_count = clean.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            clean.close()
        finally:
            src_conn.close()

        # 2. 打包 tar.gz
        fd, tmp_tar = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        uploads_dir = get_collections_dir() / "uploads"
        with tarfile.open(tmp_tar, "w:gz") as tar:
            tar.add(tmp_db, arcname="export.db")
            manifest = {
                "collection": collection,
                "exported_at": __import__("datetime").datetime.now().isoformat(),
                "doc_count": doc_count,
                "filenames": filenames,
            }
            manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2)
            manifest_bytes = manifest_data.encode("utf-8")
            mi = tarfile.TarInfo(name="manifest.json")
            mi.size = len(manifest_bytes)
            tar.addfile(mi, io.BytesIO(manifest_bytes))
            for fn in filenames:
                fp = uploads_dir / fn
                if fp.exists():
                    tar.add(fp, arcname=f"uploads/{fn}")

        os.unlink(tmp_db)

        with open(tmp_tar, "rb") as f:
            content = f.read()
        os.unlink(tmp_tar)

        fname = f"{collection}_export_{doc_count}docs.tar.gz"
        return Response(
            content=content,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"'
            },
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/docs/import-archive")
async def api_import_archive(collection: str, file: UploadFile = File(...)):
    """导入数据包 (tar.gz)：三表联动导入到当前 collection。"""
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
            if not tmp_db_path.exists():
                return JSONResponse(
                    status_code=400,
                    content={"error": "数据包缺少 export.db"},
                )

            src_uploads = Path(tmp_dir) / "uploads"
            dst_uploads = get_collections_dir() / "uploads"
            dst_uploads.mkdir(parents=True, exist_ok=True)

            # 2. 三表联动导入
            src = sqlite3.connect(str(tmp_db_path))
            src.row_factory = sqlite3.Row
            dst = get_conn(collection)
            imported = 0
            conflicts = []

            try:
                doc_rows = src.execute(
                    "SELECT * FROM documents"
                ).fetchall()
                for doc in doc_rows:
                    doc = dict(doc)
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

                    # 拷贝源文件
                    src_fp = src_uploads / doc["filename"]
                    dst_fp = dst_uploads / filename
                    if src_fp.exists() and not dst_fp.exists():
                        shutil.copy2(src_fp, dst_fp)

                    old_id = doc.pop("id", None)
                    doc["filename"] = filename
                    cols = [
                        "cite_key", "title", "author", "filename",
                        "page_count", "doc_type", "is_primary",
                        "is_secondary", "is_reference", "source_tags",
                        "bib_type", "bib_data", "linked_pdf",
                        "created_at",
                    ]
                    placeholders = ",".join("?" * len(cols))
                    cur = dst.execute(
                        f"INSERT INTO documents ({','.join(cols)}) "
                        f"VALUES ({placeholders})",
                        [doc.get(c) for c in cols],
                    )
                    new_id = cur.lastrowid

                    for r in src.execute(
                        """SELECT page_num, block_num, line_num, text,
                                  bbox, block_label, page_w, page_h
                           FROM lines WHERE doc_id = ?""",
                        (old_id,),
                    ).fetchall():
                        dst.execute(
                            """INSERT INTO lines
                               (doc_id, page_num, block_num, line_num,
                                text, bbox, block_label, page_w, page_h)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (new_id, r["page_num"], r["block_num"],
                             r["line_num"], r["text"], r["bbox"],
                             r["block_label"], r["page_w"], r["page_h"]),
                        )

                    for r in src.execute(
                        """SELECT page_num, block_num, text
                           FROM blocks_fts WHERE doc_id = ?""",
                        (old_id,),
                    ).fetchall():
                        dst.execute(
                            """INSERT INTO blocks_fts
                               (doc_id, page_num, block_num, text)
                               VALUES (?, ?, ?, ?)""",
                            (new_id, r["page_num"], r["block_num"],
                             r["text"]),
                        )
                    imported += 1

                dst.commit()
            finally:
                src.close()
                dst.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return {"imported": imported, "conflicts": conflicts}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 文献内容浏览 ────────────────────────────────────────

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
                          source_tags, bib_type, bib_data, cite_key
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"文献 id={doc_id} 不存在"},
                )

            query = (
                "SELECT page_num, block_num, line_num, text, bbox, block_label "
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
                          source_tags, bib_type, bib_data, cite_key, linked_pdf
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

            # md 文档关联了 PDF 时，翻页范围跟随 PDF
            linked_pdf = doc["linked_pdf"] if "linked_pdf" in doc.keys() else None
            if linked_pdf:
                lp_path = get_collections_dir() / "uploads" / linked_pdf
                if lp_path.exists():
                    try:
                        lp_doc = pymupdf.open(lp_path)
                        page_count = max(page_count, len(lp_doc))
                        lp_doc.close()
                    except Exception:
                        pass

            page_num = max(1, min(page, page_count))

            rows = conn.execute(
                """SELECT id, page_num, block_num, line_num, text,
                          bbox, block_label, page_w, page_h
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
            blocks_map[bn]["lines"].append({
                "id": r["id"],
                "line_num": r["line_num"],
                "text": r["text"],
                "bbox": line_bbox,
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
        pdf_path = get_collections_dir() / "uploads" / doc["filename"]
        if pdf_path.exists():
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
                bib_type_labels=BIB_TYPE_LABELS,
                bib_field_labels=BIB_FIELD_LABELS,
                bib_type_fields=BIB_TYPE_FIELDS,
                linked_pdf=linked_pdf,
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

        pdf_path = get_collections_dir() / "uploads" / filename
        if not pdf_path.exists():
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
):
    """更新某行文本，并重聚 block 全文更新 FTS。"""
    try:
        conn = get_conn(collection)
        try:
            line = conn.execute(
                """SELECT id, doc_id, page_num, block_num
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

            conn.execute(
                "UPDATE lines SET text = ? WHERE id = ?",
                (text, line_id),
            )
            # 校对改动 → SAG 待重同步
            conn.execute(
                "UPDATE documents SET sag_dirty = 1 WHERE id = ?", (doc_id,),
            )

            # 重新聚合该 block 全文
            block_rows = conn.execute(
                """SELECT text FROM lines
                   WHERE doc_id = ? AND page_num = ? AND block_num = ?
                   ORDER BY line_num""",
                (line["doc_id"], line["page_num"], line["block_num"]),
            ).fetchall()
            block_text = "\n".join(r["text"] for r in block_rows)

            # 更新 FTS
            conn.execute(
                """DELETE FROM blocks_fts
                   WHERE doc_id = ? AND page_num = ? AND block_num = ?""",
                (line["doc_id"], line["page_num"], line["block_num"]),
            )
            conn.execute(
                """INSERT INTO blocks_fts(doc_id, page_num, block_num, text)
                   VALUES (?, ?, ?, ?)""",
                (
                    line["doc_id"],
                    line["page_num"],
                    line["block_num"],
                    _tokenize(block_text),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/edit")
async def edit_page(
    collection: str,
    doc_id: int,
    page_num: int,
    text: str = Form(...),
):
    """更新整页文本：以空行分隔 block，整体替换，FTS 同步。"""
    try:
        conn = get_conn(collection)
        try:
            conn.execute(
                "DELETE FROM lines WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_num),
            )
            conn.execute(
                "DELETE FROM blocks_fts WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_num),
            )
            # 校对改动 → SAG 待重同步
            conn.execute(
                "UPDATE documents SET sag_dirty = 1 WHERE id = ?", (doc_id,),
            )

            for bn, bt in enumerate(text.split("\n\n"), 1):
                lines = [l for l in bt.split("\n") if l]
                if not lines:
                    continue
                for ln, line_text in enumerate(lines, 1):
                    conn.execute(
                        """INSERT INTO lines(doc_id, page_num, block_num, line_num, text)
                           VALUES (?, ?, ?, ?, ?)""",
                        (doc_id, page_num, bn, ln, line_text),
                    )
                conn.execute(
                    """INSERT INTO blocks_fts(doc_id, page_num, block_num, text)
                       VALUES (?, ?, ?, ?)""",
                    (doc_id, page_num, bn, _tokenize("\n".join(lines))),
                )

            conn.commit()
            return {"ok": True}
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 手工分栏重新 OCR ────────────────────────────────────

def _reocr_regions_for_page(provider, src, page_num: int, region_list: list):
    """分栏重 OCR 共享逻辑: 按 region 裁切页面 → 拼临时 PDF 一次 OCR → 收集 blocks.

    region 坐标在 150dpi 页面像素空间 (与 page_image/bbox overlay 一致)。
    返回 (new_blocks, page_w, page_h, n_crops), block bbox 已偏移回页面坐标。
    无有效裁切区域时抛 ValueError; 调用方保证 page_num 在 1..len(src) 内。
    """
    import tempfile

    PT_PER_PX = 72.0 / 150.0  # 150dpi 像素 → PDF 点
    OCR_DPI = 300              # 裁切渲染精度（高清→paddle 取清晰像素）

    page_obj = src[page_num - 1]

    # 150 DPI 页面尺寸（REOCR bbox 坐标系，与 page_image 一致）
    page_w = int(round(page_obj.rect.width * 150 / 72.0))
    page_h = int(round(page_obj.rect.height * 150 / 72.0))

    # 每个 region 裁切为一张图，按顺序拼成多页 PDF
    out_pdf = pymupdf.open()
    crop_meta = []  # (x0, y0, log_w, log_h)
    for reg in region_list:
        try:
            x0, y0, x1, y1 = reg
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        clip = pymupdf.Rect(x0 * PT_PER_PX, y0 * PT_PER_PX,
                            x1 * PT_PER_PX, y1 * PT_PER_PX)
        cpix = page_obj.get_pixmap(dpi=OCR_DPI, clip=clip)
        if cpix.width <= 0 or cpix.height <= 0:
            continue
        # 逻辑页面尺寸 = 150dpi 空间像素数（paddle bbox 空间不变），
        # 但 image object 是 OCR_DPI 高清 → 精度提升
        log_w = max(1, int(round(x1 - x0)))
        log_h = max(1, int(round(y1 - y0)))
        cpage = out_pdf.new_page(width=log_w, height=log_h)
        cpage.insert_image(cpage.rect, pixmap=cpix)
        crop_meta.append((float(x0), float(y0), log_w, log_h))

    if not crop_meta:
        out_pdf.close()
        raise ValueError("没有有效的裁切区域")

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

    # 收集 OCR 结果，按 region 顺序，bbox 偏移回原页面坐标
    new_blocks = []  # {block_label, bbox, lines:[str,...]}
    for i, page in enumerate(pages):
        if i >= len(crop_meta):
            break
        ox, oy, _, _ = crop_meta[i]
        parsing_res_list = page.get('parsing_res_list', [])
        for block in parsing_res_list:
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
            # paddle-VL 整段识别 → 按 \n 切行（无独立行框，bbox 用 block 近似）
            lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
            if not lines:
                continue
            bbox = block.get('block_bbox')
            # bbox 偏移：crop 内坐标 + region 左上角偏移
            shifted = None
            if bbox and len(bbox) >= 4:
                shifted = [bbox[0] + ox, bbox[1] + oy,
                           bbox[2] + ox, bbox[3] + oy]
            new_blocks.append({
                "block_label": label,
                "bbox": shifted,
                "lines": lines,
            })
    return new_blocks, page_w, page_h, len(crop_meta)


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
        for ln_num, ln_text in enumerate(b["lines"], 1):
            conn.execute(
                """INSERT INTO lines
                   (doc_id, page_num, block_num, line_num, text,
                    bbox, block_label, page_w, page_h)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, page_num, block_num, ln_num, ln_text,
                 bbox_json, b["block_label"], page_w, page_h),
            )
            total_lines += 1
        conn.execute(
            """INSERT INTO blocks_fts
               (doc_id, page_num, block_num, text)
               VALUES (?, ?, ?, ?)""",
            (doc_id, page_num, block_num,
             _tokenize(full_text)),
        )
    conn.execute(
        "UPDATE documents SET sag_dirty = 1 WHERE id = ?", (doc_id,),
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
    每个 region 裁切为一张图，按顺序拼成多页 PDF 一次提交 OCR；
    返回结果按 region 顺序追加为新 block，bbox 偏移回原页面坐标。
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
                "SELECT filename FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        pdf_path = get_collections_dir() / "uploads" / doc["filename"]
        if not pdf_path.exists():
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


# ── 整本重 OCR: 后台任务 + 进度注册表 ─────────────────────

_reocr_lock = threading.Lock()
# {(collection, doc_id): {"total", "done", "status", "error", "cancel"}}
_reocr_jobs: dict = {}


def _reocr_get(collection: str, doc_id: int) -> dict | None:
    with _reocr_lock:
        job = _reocr_jobs.get((collection, doc_id))
        return dict(job) if job else None


def _reocr_set(collection: str, doc_id: int, **kw) -> None:
    with _reocr_lock:
        _reocr_jobs[(collection, doc_id)].update(kw)


def _run_reocr_doc(collection: str, doc_id: int, provider, pdf_path: Path,
                   total_pages: int) -> None:
    """后台线程: 逐页 OCR → 每页短事务写入 (进度 = 已提交页数)."""
    logger = logging.getLogger("xsf.reocr")
    import tempfile

    total_lines = 0
    total_blocks = 0
    src = pymupdf.open(pdf_path)
    conn = get_conn(collection)
    try:
        for page_idx in range(total_pages):
            with _reocr_lock:
                job = _reocr_jobs[(collection, doc_id)]
                if job.get("cancel"):
                    # 注意: 不能在持锁块内调 _reocr_set (非重入锁, 会死锁)
                    job.update(status="cancelled", error="用户取消")
                    cancelled = True
                else:
                    cancelled = False
            if cancelled:
                logger.info("reocr 取消 coll=%s doc=%s 完成 %s/%s",
                            collection, doc_id, page_idx, total_pages)
                return
            page_obj = src[page_idx]
            pix = page_obj.get_pixmap(dpi=300)

            cpdf = pymupdf.open()
            cpage = cpdf.new_page(width=pix.width, height=pix.height)
            cpage.insert_image(cpage.rect, pixmap=pix)
            fd, tmp_pdf = tempfile.mkstemp(suffix='.pdf')
            os.close(fd)
            cpdf.save(tmp_pdf)
            cpdf.close()

            try:
                result = provider.ocr(tmp_pdf)
            finally:
                os.unlink(tmp_pdf)

            conn.execute(
                "DELETE FROM lines WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_idx + 1),
            )
            conn.execute(
                "DELETE FROM blocks_fts WHERE doc_id = ? AND page_num = ?",
                (doc_id, page_idx + 1),
            )
            page_num = page_idx + 1
            block_num = 0
            for p in result:
                for block in p.get('parsing_res_list', []):
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
                    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
                    if not lines:
                        continue

                    block_num += 1
                    bbox = block.get('block_bbox')
                    bbox_json = json.dumps(bbox) if bbox else None
                    page_w = p.get('width') or pix.width
                    page_h = p.get('height') or pix.height

                    for ln_num, ln_text in enumerate(lines, 1):
                        conn.execute(
                            """INSERT INTO lines
                               (doc_id, page_num, block_num, line_num, text,
                                bbox, block_label, page_w, page_h)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (doc_id, page_num, block_num, ln_num, ln_text,
                             bbox_json, label, page_w, page_h),
                        )
                        total_lines += 1
                    conn.execute(
                        """INSERT INTO blocks_fts
                           (doc_id, page_num, block_num, text)
                           VALUES (?, ?, ?, ?)""",
                        (doc_id, page_num, block_num, _tokenize(text)),
                    )
                    total_blocks += 1
            conn.commit()  # 每页提交, 释放写锁
            _reocr_set(collection, doc_id, done=page_idx + 1)

        # 清理超出新页数残留 + 收尾
        conn.execute(
            "DELETE FROM lines WHERE doc_id = ? AND page_num > ?",
            (doc_id, total_pages),
        )
        conn.execute(
            "DELETE FROM blocks_fts WHERE doc_id = ? AND page_num > ?",
            (doc_id, total_pages),
        )
        conn.execute(
            "UPDATE documents SET doc_type = 'ocr', sag_dirty = 1 WHERE id = ?",
            (doc_id,),
        )
        conn.commit()
        _reocr_set(collection, doc_id, status="done", done=total_pages)
        logger.info("reocr 完成 coll=%s doc=%s: %s 页 %s 块 %s 行",
                    collection, doc_id, total_pages, total_blocks, total_lines)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        done = (_reocr_get(collection, doc_id) or {}).get("done", 0)
        _reocr_set(collection, doc_id, status="error", error=str(e))
        logger.warning("reocr 失败 coll=%s doc=%s @页%s/%s: %s",
                       collection, doc_id, done, total_pages, e)
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
                "SELECT filename FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        pdf_path = get_collections_dir() / "uploads" / doc["filename"]
        if not pdf_path.exists():
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
        # 先置脏: 逐页提交中途崩溃也能在 SAG 同步 tab 看到待重同步
        conn = get_conn(collection)
        try:
            conn.execute(
                "UPDATE documents SET sag_dirty = 1 WHERE id = ?", (doc_id,),
            )
            conn.commit()
        finally:
            conn.close()
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
    每个目标页按页面尺寸等比缩放坐标后裁切 OCR, 总是替换目标页原文本。
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
                "SELECT filename FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if doc is None:
            return JSONResponse(status_code=404, content={"error": "文献不存在"})

        pdf_path = get_collections_dir() / "uploads" / doc["filename"]
        if not pdf_path.exists():
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
        # 先置脏: 逐页提交中途崩溃也能在 SAG 同步 tab 看到待重同步
        conn = get_conn(collection)
        try:
            conn.execute(
                "UPDATE documents SET sag_dirty = 1 WHERE id = ?", (doc_id,),
            )
            conn.commit()
        finally:
            conn.close()
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
        from .search import _fts_query

        fts_q = _fts_query(keyword)
        if not fts_q:
            return {"keyword": keyword, "hits": [], "total": 0, "truncated": False}

        if limit < 1:
            limit = 300

        conn = get_conn(collection)
        try:
            rows = conn.execute(
                """SELECT f.page_num, f.block_num, MIN(l.id) as line_id, l.text as text
                   FROM blocks_fts f
                   JOIN lines l ON l.doc_id = f.doc_id
                               AND l.page_num = f.page_num
                               AND l.block_num = f.block_num
                   WHERE f.doc_id = ? AND blocks_fts MATCH ?
                   GROUP BY f.page_num, f.block_num
                   ORDER BY f.page_num, f.block_num""",
                (doc_id, fts_q),
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

