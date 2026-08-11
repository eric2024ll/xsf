"""jiage FastAPI Web 界面"""

import html
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_collections_dir, list_collections, get_auth_token
from .db import init_db, get_conn
from .search import search, get_block_lines, get_context
from .ingest import ingest_pdf, ingest_scanned_pdf, remove_doc, _tokenize

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


# ── 文献内容浏览 ────────────────────────────────────────

@app.get("/collections/{collection}/doc/{doc_id}/content")
async def api_doc_content(collection: str, doc_id: int,
                          page: int = None, limit: int = 0):
    """返回单个文献的内容，按页/块/行组织。

    page: 指定页码则只返回该页；省略则返回所有页。
    limit: 限制返回的最大行数（0 = 不限），用于大文献截断。
    """
    try:
        conn = get_conn(collection)
        try:
            doc = conn.execute(
                """SELECT id, title, filename, doc_type, page_count
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
                "text": r["text"],
            })
            total_lines += 1

        pages = []
        for pn in sorted(pages_map):
            blocks = [pages_map[pn][bn] for bn in sorted(pages_map[pn])]
            pages.append({"page_num": pn, "blocks": blocks})

        return {
            "doc": dict(doc),
            "pages": pages,
            "total_lines": total_lines,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 关键词高亮 helper ───────────────────────────────────

def _highlight_keyword(text: str, keyword: str) -> str:
    """把 keyword 中的空格/空白分隔词在 text 里高亮为 <mark>。"""
    if not text or not keyword:
        return html.escape(text or "")
    tokens = [t for t in keyword.split() if t.strip()]
    if not tokens:
        return html.escape(text)
    result = html.escape(text)
    for tok in tokens:
        pattern = re.compile(
            f'({re.escape(tok)})',
            re.IGNORECASE,
        )
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
                """SELECT id, title, filename, page_count, doc_type
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return templates.TemplateResponse(
                    request,
                    "proofread.html",
                    {
                        "error": f"文献 id={doc_id} 不存在",
                        "collection": collection,
                    },
                    status_code=404,
                )

            page_count = doc["page_count"] or 1
            page_num = max(1, min(page, page_count))

            rows = conn.execute(
                """SELECT id, page_num, block_num, line_num, text,
                          bbox, block_label
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

        # 从 bbox 推断 OCR 原始页面尺寸
        orig_width = 0
        orig_height = 0
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

        # 图片到原始 OCR 坐标系的缩放；前端再乘 clientWidth/naturalWidth
        scale = 1.0
        if render_width and orig_width:
            scale = render_width / orig_width

        return templates.TemplateResponse(
            request,
            "proofread.html",
            {
                "collection": collection,
                "doc": dict(doc),
                "page_num": page_num,
                "page_count": page_count,
                "blocks": blocks,
                "keyword": keyword or "",
                "target_block": block,
                "target_line": line,
                "scale": scale,
                "render_width": render_width,
                "render_height": render_height,
                "orig_width": orig_width,
                "orig_height": orig_height,
                "highlight": _highlight_keyword,
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "proofread.html",
            {
                "error": str(e),
                "collection": collection,
            },
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
                "SELECT filename FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": "文献不存在"},
                )
            filename = doc["filename"]
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


# ── 命中文档检索（校对页内搜索）─────────────────────────

@app.get("/collections/{collection}/doc/{doc_id}/hits")
async def doc_hits(collection: str, doc_id: int, keyword: str):
    """返回某文档中匹配关键词的所有 page/block/first_line_id。"""
    try:
        from .search import _fts_query

        fts_q = _fts_query(keyword)
        if not fts_q:
            return {"keyword": keyword, "hits": []}

        conn = get_conn(collection)
        try:
            rows = conn.execute(
                """SELECT f.page_num, f.block_num, MIN(l.id) as line_id
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

        return {
            "keyword": keyword,
            "hits": [dict(r) for r in rows],
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

