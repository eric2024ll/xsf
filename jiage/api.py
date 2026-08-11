"""jiage FastAPI Web 界面"""

import html
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import pymupdf
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_collections_dir, list_collections, get_auth_token
from .db import init_db, get_conn
from .search import search, get_block_lines, get_context
from .bib_utils import (
    generate_cite_key, sync_doc_fields, parse_bib_data,
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


app = FastAPI(title="架閣：放書架的地方，也能看書", version=_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="4" fill="#3b5998"/>'
    '<text x="16" y="23" font-size="18" text-anchor="middle" '
    'fill="#fff" font-family="serif">架</text></svg>'
).encode('utf-8')


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


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

                r = ingest_func(
                    dst,
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
    - bib_type: 文献类型（@book/@article/@online/@manuscript/@incollection）
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
                    "SELECT cite_key, bib_type, bib_data FROM documents WHERE id = ?",
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
                "text": r["text"],
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
                """SELECT id, title, author, filename, page_count, doc_type,
                          source_tags, bib_type, bib_data, cite_key
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
                "source_tags": source_tags,
                "bib_type": bib_type,
                "bib_data": bib_data,
                "bib_type_labels": BIB_TYPE_LABELS,
                "bib_field_labels": BIB_FIELD_LABELS,
                "bib_type_fields": BIB_TYPE_FIELDS,
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


# ── 手工分栏重新 OCR ────────────────────────────────────

@app.post("/collections/{collection}/doc/{doc_id}/page/{page_num}/reocr")
async def reocr_page(
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

        import os
        import tempfile
        from .ocr import get_provider

        PT_PER_PX = 72.0 / 150.0  # 150dpi 像素 → PDF 点
        OCR_DPI = 300              # 裁切渲染精度（高清→paddle 取清晰像素）

        src = pymupdf.open(pdf_path)
        try:
            if page_num < 1 or page_num > len(src):
                return JSONResponse(
                    status_code=404, content={"error": "页码超出范围"})
            page_obj = src[page_num - 1]

            # 每个 region 裁切为一张图，按顺序拼成多页 PDF
            out_pdf = pymupdf.open()
            crop_meta = []  # (x0, y0, pix_w, pix_h)
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
                return JSONResponse(
                    status_code=400,
                    content={"error": "没有有效的裁切区域"},
                )

            fd, tmp_pdf = tempfile.mkstemp(suffix='.pdf')
            os.close(fd)
            out_pdf.save(tmp_pdf)
            out_pdf.close()
        finally:
            src.close()

        try:
            provider = get_provider()
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

        if not new_blocks:
            return JSONResponse(
                status_code=500,
                content={"error": "重新 OCR 未返回任何文本"},
            )

        # 写回 DB
        conn = get_conn(collection)
        try:
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
                            bbox, block_label)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (doc_id, page_num, block_num, ln_num, ln_text,
                         bbox_json, b["block_label"]),
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
        finally:
            conn.close()

        return {
            "status": "ok",
            "doc_id": doc_id,
            "page_num": page_num,
            "regions": len(crop_meta),
            "new_blocks": len(new_blocks),
            "replaced": replace,
        }
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

