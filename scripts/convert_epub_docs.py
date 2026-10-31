#!/usr/bin/env python3
"""存量 epub 文献转 PDF 归一 (2026-10-31 格式收敛: PDF 为主 + md 例外).

对指定 epub 文献逐本:
  1. Calibre ebook-convert 转 PDF (临时目录)
  2. 安全门: lines 带翻译/注疏 (trans/gloss) 或修订史非零 → 拒绝
     (重抽文本层会抹掉对译劳动), --force 才动
  3. 清旧文本层, 按新 PDF 重抽 lines/blocks_fts (同 ingest_pdf 逻辑)
  4. attach original.pdf (linked_pdf 回写 files/<id>/)
  5. 原 epub 移 ~/xsf-data/backup/epub-retired-<date>/ 留档
  6. doc_type → born-digital, page_count 校正, sag_dirty=1

用法:
    .venv/bin/python scripts/convert_epub_docs.py [--dry-run] [--ids 311,312]
"""
import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.env import load_env  # noqa: E402
load_env()
import pymupdf  # noqa: E402
from xsf.config import get_collections_dir, get_db_dir  # noqa: E402
from xsf.db import get_conn  # noqa: E402
from xsf.ingest import attach_original, _tokenize  # noqa: E402

# (collection, doc_id) — 2026-10-31 盘点: 全库 4 本 epub
TARGETS = [("考据", 6), ("ai与历史研究综合", 311),
           ("ai与历史研究综合", 312), ("ai与历史研究综合", 317)]


def safety_gate(conn, doc_id: int) -> str | None:
    """返回拦截原因, 通过返回 None。"""
    trans = conn.execute(
        "SELECT COUNT(*) FROM lines WHERE doc_id=? AND "
        "(trans IS NOT NULL AND trans!='' OR gloss IS NOT NULL AND gloss!='')",
        (doc_id,)).fetchone()[0]
    if trans:
        return f"{trans} 行带对译/注疏 (trans/gloss), 重抽将抹掉"
    rev = conn.execute(
        "SELECT COUNT(*) FROM revision_log WHERE doc_id=?",
        (doc_id,)).fetchone()[0]
    if rev:
        return f"{rev} 条修订史, 重抽后位置引用将失配"
    return None


def reextract(conn, doc_id: int, pdf_path: Path) -> tuple[int, int]:
    """清旧文本层并按 PDF 重抽 (复刻 ingest_pdf 的抽取循环)。"""
    conn.execute("DELETE FROM lines WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM blocks_fts WHERE doc_id=?", (doc_id,))
    doc = pymupdf.open(pdf_path)
    n_lines = n_blocks = 0
    try:
        for page_num, page in enumerate(doc, 1):
            block_num = 0
            for block in page.get_text('dict').get('blocks', []):
                if block.get('type', 0) != 0:
                    continue
                block_num += 1
                block_lines = []
                line_num = 0
                for line in block.get('lines', []):
                    text = ''.join(s.get('text', '')
                                   for s in line.get('spans', [])).strip()
                    if not text:
                        continue
                    line_num += 1
                    block_lines.append(text)
                    conn.execute(
                        "INSERT INTO lines (doc_id, page_num, block_num, "
                        "line_num, text) VALUES (?, ?, ?, ?, ?)",
                        (doc_id, page_num, block_num, line_num, text))
                    n_lines += 1
                if block_lines:
                    conn.execute(
                        "INSERT INTO blocks_fts (doc_id, page_num, block_num, "
                        "text) VALUES (?, ?, ?, ?)",
                        (doc_id, page_num, block_num,
                         _tokenize('\n'.join(block_lines))))
                    n_blocks += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        doc.close()
    return n_lines, n_blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--ids', help='只转指定 doc_id, 逗号分隔 (默认全部候选)')
    ap.add_argument('--force', action='store_true',
                    help='越过安全门 (会抹掉对译/修订, 慎用)')
    args = ap.parse_args()
    wanted = ({int(x) for x in args.ids.split(',')}
              if args.ids else {d for _c, d in TARGETS})

    backup = (Path.home() / 'xsf-data' / 'backup'
              / 'epub-retired-20261031')
    for col, did in TARGETS:
        if did not in wanted:
            continue
        dbp = get_db_dir() / col / 'xsf.db'
        conn = sqlite3.connect(dbp)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, title, doc_type, linked_pdf FROM documents "
            "WHERE id=?", (did,)).fetchone()
        if not row or row['doc_type'] != 'epub':
            print(f"[{col}#{did}] 非 epub 或不存在, 跳过")
            conn.close()
            continue
        gate = safety_gate(conn, did)
        if gate and not args.force:
            print(f"[{col}#{did}] {row['title'][:20]} — 安全门拦截: {gate}")
            conn.close()
            continue
        epub_path = (get_collections_dir() / col
                     / (row['linked_pdf'] or ''))
        if not epub_path.is_file():
            print(f"[{col}#{did}] 原件缺失: {epub_path}")
            conn.close()
            continue

        if args.dry_run:
            print(f"[{col}#{did}] {row['title'][:20]} — 可转 "
                  f"({epub_path.name})")
            conn.close()
            continue

        with tempfile.TemporaryDirectory(
                prefix=f"epubconv-{did}-") as td:
            pdf_out = Path(td) / "conv.pdf"
            r = subprocess.run(
                ['ebook-convert', str(epub_path), str(pdf_out)],
                capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not pdf_out.is_file():
                print(f"[{col}#{did}] ebook-convert 失败:\n"
                      f"{r.stderr[-500:]}")
                conn.close()
                continue
            # 抽取健全性: 页数与文本量
            chk = pymupdf.open(pdf_out)
            pages = len(chk)
            chars = sum(len(p.get_text() or '') for p in chk)
            chk.close()
            if pages < 1 or chars < 200:
                print(f"[{col}#{did}] 转换产物可疑 (pages={pages}, "
                      f"chars={chars}), 跳过")
                conn.close()
                continue

            try:
                n_lines, n_blocks = reextract(conn, did, pdf_out)
                doc_cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(documents)")}
                sets = ["doc_type='born-digital'", "page_count=?"]
                params = [pages]
                if 'sag_dirty' in doc_cols:
                    sets.append("sag_dirty=1")
                conn.execute(
                    f"UPDATE documents SET {', '.join(sets)} WHERE id=?",
                    params + [did])
                conn.commit()
                attach_original(col, did, pdf_out, move=False)
            except Exception as e:
                print(f"[{col}#{did}] 重抽失败 (已回滚): {e}")
                conn.close()
                continue
        conn.close()

        # 原 epub 移 backup 留档
        backup.mkdir(parents=True, exist_ok=True)
        dst_epub = backup / f"{col}-{did}{epub_path.suffix}"
        shutil.move(str(epub_path), dst_epub)
        print(f"[{col}#{did}] {row['title'][:20]} ✓ "
              f"pages={pages} lines={n_lines} blocks={n_blocks}; "
              f"epub→{dst_epub}")


if __name__ == '__main__':
    main()
