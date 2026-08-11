"""jiage FastAPI Web 界面"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from .config import get_data_dir, get_collections_dir
from .db import init_db, get_conn
from .search import search, get_block_lines, get_context
from .ingest import ingest_pdf, ingest_scanned_pdf, remove_doc

_VERSION = "0.1.0"

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    (get_data_dir() / "uploads").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="籍 jiAge 文献池", version=_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 页面 ──────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": _VERSION},
    )


# ── 搜索 ──────────────────────────────────────────────

@app.get("/api/search")
async def api_search(q: str, collection: str = None, limit: int = 20):
    try:
        results = search(q, collection=collection, limit=limit)
        out = []
        for r in results:
            lines = get_block_lines(r["doc_id"], r["page_num"], r["block_num"])
            text = " ".join(lines)
            out.append({
                "doc_id": r["doc_id"],
                "page_num": r["page_num"],
                "block_num": r["block_num"],
                "collection": r["collection"],
                "filename": r.get("filename"),
                "title": r.get("title") or r.get("filename"),
                "cite_key": r.get("cite_key"),
                "text": text,
            })
        return {"query": q, "count": len(out), "results": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 统计 ──────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    try:
        conn = get_conn()
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
            colls = conn.execute(
                """SELECT collection, COUNT(*) as cnt, SUM(page_count) as pages
                   FROM documents GROUP BY collection ORDER BY cnt DESC"""
            ).fetchall()
        finally:
            conn.close()
        return {
            "documents": n_docs,
            "blocks": n_blocks,
            "lines": n_lines,
            "ocr_docs": n_ocr,
            "born_digital_docs": n_bd,
            "collections": [
                {"name": c["collection"], "count": c["cnt"],
                 "pages": c["pages"] or 0}
                for c in colls
            ],
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 上下文 ────────────────────────────────────────────

@app.get("/api/context/{doc_id}/{page}/{block}")
async def api_context(doc_id: int, page: int, block: int, radius: int = 1):
    """返回命中块上下文，含 bbox / block_label"""
    try:
        ctx = get_context(doc_id, page, block, radius=radius)
        if not ctx:
            return {"lines": []}

        # get_context 不返回 bbox/block_label，补查
        conn = get_conn()
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

@app.post("/api/add")
async def api_add(
    file: UploadFile = File(...),
    collection: str = Form(...),
    cite_key: str = Form(None),
    title: str = Form(None),
    author: str = Form(None),
    ocr: bool = Form(False),
):
    try:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            return JSONResponse(
                status_code=400,
                content={"error": "仅支持 PDF 文件"},
            )

        upload_dir = get_data_dir() / "uploads"
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
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 删除 ──────────────────────────────────────────────

@app.delete("/api/doc/{doc_id}")
async def api_remove(doc_id: int):
    try:
        remove_doc(doc_id)
        return {"status": "ok", "doc_id": doc_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── 文献列表 ──────────────────────────────────────────

@app.get("/api/docs")
async def api_docs(collection: str = None, limit: int = 50):
    try:
        conn = get_conn()
        try:
            if collection:
                rows = conn.execute(
                    """SELECT id, collection, cite_key, title, author,
                              filename, page_count, doc_type, created_at
                       FROM documents
                       WHERE collection = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (collection, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, collection, cite_key, title, author,
                              filename, page_count, doc_type, created_at
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
