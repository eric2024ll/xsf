#!/usr/bin/env python3
"""存量文献批量 OCR (2026-10-31 用户裁定: 重转残留 21 本全量 OCR).

复刻 reocr_doc 端点编排 (job dict + _run_reocr_doc), 串行逐本:
  注册 job → _run_reocr_doc 后台线程 → join 等终态 → 下一本。
_run_reocr_doc 页级断点续跑 (ocr_page_state=done 跳过, 中断重跑省 API) +
单页失败不中断整本; 终态 done/done_with_errors (doc_type→'ocr') 或
error (doc_type 不变, 本子记录失败原因).

用法:
    .venv/bin/python scripts/reocr_docs.py --dry-run
    .venv/bin/python scripts/reocr_docs.py
    .venv/bin/python scripts/reocr_docs.py --ids 107,112
"""
import argparse
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.env import load_env  # noqa: E402
load_env()
import pymupdf  # noqa: E402
from xsf.config import resolve_original_path  # noqa: E402
from xsf.db import get_conn  # noqa: E402
from xsf.ocr import get_provider  # noqa: E402
from xsf.reocr import _reocr_jobs, _run_reocr_doc  # noqa: E402

# 2026-10-31 重转残留: 3 损坏 PDF + 16 零行 (扫描件误分流) + 2 竖排古籍
TARGETS = [
    ('ai与历史研究综合', 107), ('ai与历史研究综合', 112),
    ('ai与历史研究综合', 172), ('ai与历史研究综合', 130),
    ('ai与历史研究综合', 140), ('ai与历史研究综合', 135),
    ('ai与历史研究综合', 150), ('ai与历史研究综合', 151),
    ('ai与历史研究综合', 152), ('ai与历史研究综合', 154),
    ('ai与历史研究综合', 160), ('ai与历史研究综合', 164),
    ('ai与历史研究综合', 165), ('ai与历史研究综合', 224),
    ('ai与历史研究综合', 245), ('ai与历史研究综合', 256),
    ('ai与历史研究综合', 257), ('ai与历史研究综合', 258),
    ('ai与历史研究综合', 313),
    ('晚清乡土志', 301), ('晚清乡土志', 302),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='只盘点清单与页数, 不调 OCR')
    ap.add_argument('--ids', help='只跑指定 doc_id, 逗号分隔')
    args = ap.parse_args()
    wanted = ({int(x) for x in args.ids.split(',')} if args.ids else None)
    targets = [t for t in TARGETS if wanted is None or t[1] in wanted]

    provider = None
    total_pages = 0
    failed_docs = []
    for col, did in targets:
        conn = get_conn(col)
        row = conn.execute(
            "SELECT title, page_count, linked_pdf, doc_type FROM documents "
            "WHERE id=?", (did,)).fetchone()
        conn.close()
        if row is None:
            print(f"[{col}#{did}] 不存在, 跳过", flush=True)
            continue
        t = (row['title'] or '')[:24]
        orig = resolve_original_path(col, row['linked_pdf'])
        if not orig or not Path(orig).is_file():
            print(f"[{col}#{did}] {t} — 原件缺失, 跳过", flush=True)
            failed_docs.append((col, did, '原件缺失'))
            continue

        if args.dry_run:
            print(f"[{col}#{did}] {t} — pages={row['page_count']} "
                  f"doc_type={row['doc_type']}")
            total_pages += row['page_count'] or 0
            continue

        src = pymupdf.open(orig)
        npages = len(src)
        src.close()
        if provider is None:
            provider = get_provider()
        _reocr_jobs[(col, did)] = {
            "total": npages, "done": 0, "status": "running",
            "error": None, "cancel": False,
            "mode": "full", "page_from": 1, "page_to": npages,
        }
        th = threading.Thread(
            target=_run_reocr_doc,
            args=(col, did, provider, orig, npages),
            daemon=True, name=f"reocr-{col}-{did}")
        th.start()
        print(f"[{col}#{did}] {t} OCR 开始 ({npages} 页)", flush=True)
        th.join()  # 线程自管终态写入 job dict
        job = _reocr_jobs.get((col, did), {})
        status = job.get('status')
        print(f"[{col}#{did}] {t} 终态={status} "
              f"失败页={job.get('failed_pages') or '无'} "
              f"err={job.get('error')}", flush=True)
        _reocr_jobs.pop((col, did), None)
        if status not in ('done', 'done_with_errors'):
            failed_docs.append((col, did, f"终态 {status}: {job.get('error')}"))

    if args.dry_run:
        print(f"\n== 盘点: {len(targets)} 本 / {total_pages} 页 ==")
    else:
        print(f"\n== 批量 OCR 完成: 失败 {len(failed_docs)} 本 ==")
        for col, did, why in failed_docs:
            print(f"  [{col}#{did}] {why}")


if __name__ == '__main__':
    main()
