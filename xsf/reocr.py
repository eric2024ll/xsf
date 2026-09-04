"""整本重 OCR: job 注册表 + 后台执行器.

2026-09-04 从 api.py 抽出, 供手动 reocr 端点与 folder_scan 自动 OCR 共用.
api.py 里的 _run_reocr_regions (分栏重 OCR) 也 import 本模块的 job store.
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import pymupdf

from .db import get_conn
from .ingest import _tokenize

logger = logging.getLogger("xsf.reocr")

_reocr_lock = threading.Lock()
# {(collection, doc_id): {"total", "done", "status", "error", "cancel", ...}}
_reocr_jobs: dict = {}


def _reocr_get(collection: str, doc_id: int) -> dict | None:
    with _reocr_lock:
        job = _reocr_jobs.get((collection, doc_id))
        return dict(job) if job else None


def _reocr_set(collection: str, doc_id: int, **kw) -> None:
    with _reocr_lock:
        _reocr_jobs[(collection, doc_id)].update(kw)


def register_job(collection: str, doc_id: int, total_pages: int,
                 **extra) -> bool:
    """注册 OCR job (手动/自动共用的互斥入口).

    已有 running 中 job → 返回 False 不覆盖; 否则写入新 job 返回 True.
    extra 透传进 job dict (如 mode="full" / source="auto").
    """
    with _reocr_lock:
        job = _reocr_jobs.get((collection, doc_id))
        if job and job.get("status") == "running":
            return False
        entry = {
            "total": total_pages, "done": 0, "status": "running",
            "error": None, "cancel": False,
        }
        entry.update(extra)
        _reocr_jobs[(collection, doc_id)] = entry
        return True


def pop_job(collection: str, doc_id: int) -> None:
    with _reocr_lock:
        _reocr_jobs.pop((collection, doc_id), None)


def _run_reocr_doc(collection: str, doc_id: int, provider, pdf_path: Path,
                   total_pages: int) -> None:
    """后台线程: 逐页 OCR → 每页短事务写入 (进度 = 已提交页数)."""
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
            "UPDATE documents SET doc_type = 'ocr' WHERE id = ?",
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
