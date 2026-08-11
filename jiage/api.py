"""jiage FastAPI Web 界面"""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_data_dir, get_collections_dir, list_collections, get_auth_token
from .db import init_db, get_conn
from .search import search, get_block_lines, get_context
from .ingest import ingest_pdf, ingest_scanned_pdf, remove_doc

_VERSION = "0.1.0"

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化所有已有 collection 的 DB"""
    for coll in list_collections():
        init_db(coll)
    (get_collections_dir() / "uploads").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="籍 jiAge 文献池", version=_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth Middleware ────────────────────────────────────

_PUBLIC_PATHS = {'/login', '/logout'}
_PUBLIC_PREFIXES = ('/login', '/static', '/favicon')


def _is_authenticated(request: Request) -> bool:
    token = get_auth_token()
    if token is None:
        return True
    cookie_val = request.cookies.get('jiage_auth', '')
    return secrets.compare_digest(cookie_val, token)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = get_auth_token()
        if token is None:
            return await call_next(request)

        path = request.url.path

        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        if _is_authenticated(request):
            return await call_next(request)

        is_api = path.startswith('/api/') or path.startswith('/collections/')
        if is_api:
            return JSONResponse(
                status_code=401,
                content={"error": "未认证，请先登录"},
            )
        return RedirectResponse('/login', status_code=303)


app.add_middleware(AuthMiddleware)


# ── Auth: login / logout ──────────────────────────────

@app.get("/login")
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error},
    )


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    token = get_auth_token()
    if token is not None and secrets.compare_digest(password, token):
        resp = RedirectResponse('/', status_code=303)
        resp.set_cookie(
            'jiage_auth', token,
            httponly=True,
            max_age=7 * 24 * 3600,
            samesite='lax',
        )
        return resp
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "密码错误"},
    )


@app.get("/logout")
async def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie('jiage_auth')
    return resp


# ── 页面 ──────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": _VERSION},
    )


# ── Collection 列表 ────────────────────────────────────

@app.get("/api/collections")
async def api_collections():
    return {"collections": list_collections()}


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
            out.append({
                "doc_id": r["doc_id"],
                "page_num": r["page_num"],
                "block_num": r["block_num"],
                "filename": r.get("filename"),
                "title": r.get("title") or r.get("filename"),
                "cite_key": r.get("cite_key"),
                "text": text,
            })
        return {"query": q, "collection": collection,
                "count": len(out), "results": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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
            n_primary = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_primary = 1"
            ).fetchone()[0]
            n_secondary = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_secondary = 1"
            ).fetchone()[0]
            n_reference = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_reference = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "collection": collection,
            "documents": n_docs,
            "blocks": n_blocks,
            "lines": n_lines,
            "ocr_docs": n_ocr,
            "born_digital_docs": n_bd,
            "source_type": {
                "primary": n_primary,
                "secondary": n_secondary,
                "reference": n_reference,
            },
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
        total_primary = 0
        total_secondary = 0
        total_reference = 0
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
                    n_primary = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE is_primary = 1"
                    ).fetchone()[0]
                    n_secondary = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE is_secondary = 1"
                    ).fetchone()[0]
                    n_reference = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE is_reference = 1"
                    ).fetchone()[0]
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
                total_primary += n_primary
                total_secondary += n_secondary
                total_reference += n_reference
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
            "source_type": {
                "primary": total_primary,
                "secondary": total_secondary,
                "reference": total_reference,
            },
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

@app.post("/collections/{collection}/add")
async def api_add(
    collection: str,
    file: UploadFile = File(...),
    cite_key: str = Form(None),
    title: str = Form(None),
    author: str = Form(None),
    ocr: bool = Form(False),
    source_type: str = Form('primary'),
):
    try:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            return JSONResponse(
                status_code=400,
                content={"error": "仅支持 PDF 文件"},
            )

        st_map = {
            'primary':   {'is_primary': True,   'is_secondary': False, 'is_reference': False},
            'secondary': {'is_primary': False,  'is_secondary': True,  'is_reference': False},
            'reference': {'is_primary': False,  'is_secondary': False, 'is_reference': True},
            'all':       {'is_primary': True,   'is_secondary': True,  'is_reference': True},
        }
        st = st_map.get(source_type, st_map['primary'])

        init_db(collection)

        # 去重检查: 同 filename 已入库则直接返回
        conn = get_conn(collection)
        existing = conn.execute(
            "SELECT id, title FROM documents WHERE filename = ?",
            (file.filename,),
        ).fetchone()
        conn.close()
        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "error": f"'{file.filename}' 已在书架「{collection}」中 (id={existing['id']})",
                    "doc_id": existing["id"],
                },
            )

        upload_dir = get_collections_dir() / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = upload_dir / file.filename
        with open(pdf_path, "wb") as f:
            content = await file.read()
            f.write(content)

        ingest_func = ingest_scanned_pdf if ocr else ingest_pdf
        result = ingest_func(
            pdf_path,
            collection=collection,
            cite_key=cite_key or None,
            title=title or None,
            author=author or None,
            **st,
        )
        return {"status": "ok", "result": result}
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
