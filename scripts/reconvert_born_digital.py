#!/usr/bin/env python3
"""存量 born-digital 文献重转 anydoc 管线 (2026-10-31 管线收敛).

旧管线 (PyMuPDF 直提, doc_type='born-digital') 退役: 文本层 PDF 一律
anydoc 逐页转 Markdown 段落建块 (doc_type='pdf_native'). 逐本:
  1. 清旧 lines/blocks_fts
  2. _native_extract_pages 重抽 (与 ingest_native_pdf 同一抽取实现)
  3. 无文本层的页留空 (记 empty_pages; 页图仍可看, 校对台可 reocr)
  4. doc_type→'pdf_native'; sag_dirty=1 (仅有该列的库)
  5. entity_mentions 锚点失效 → valid=0 (留痕不删, 聚合查询已按 valid 过滤)
  6. 整篇零行 → 回滚保留旧文本并报告 (不静默清空, 人工补 OCR)

trans/gloss: 随 lines 重建抹掉 (2026-10-31 裁定); revision_log 留痕不动.

用法:
    .venv/bin/python scripts/reconvert_born_digital.py --dry-run
    .venv/bin/python scripts/reconvert_born_digital.py
    .venv/bin/python scripts/reconvert_born_digital.py --ids 311,312
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.env import load_env  # noqa: E402
load_env()
from xsf.config import get_db_dir  # noqa: E402
from xsf.db import get_conn  # noqa: E402
from xsf.ingest import _native_extract_pages, get_original_file  # noqa: E402


def doc_counts(conn, doc_id: int):
    """(旧行数, trans/gloss 行数, 修订史条数, 有效 mention 数)."""
    old = conn.execute(
        "SELECT COUNT(*) FROM lines WHERE doc_id=?", (doc_id,)).fetchone()[0]
    tg = conn.execute(
        "SELECT COUNT(*) FROM lines WHERE doc_id=? AND "
        "(trans IS NOT NULL AND trans!='' OR gloss IS NOT NULL AND gloss!='')",
        (doc_id,)).fetchone()[0]
    rev = conn.execute(
        "SELECT COUNT(*) FROM revision_log WHERE doc_id=?",
        (doc_id,)).fetchone()[0]
    men = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE doc_id=? AND valid=1",
        (doc_id,)).fetchone()[0]
    return old, tg, rev, men


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='只盘点可转清单, 不动库')
    ap.add_argument('--ids',
                    help='只转指定 doc_id, 逗号分隔 (默认全部 born-digital)')
    args = ap.parse_args()
    wanted = ({int(x) for x in args.ids.split(',')} if args.ids else None)

    tot = {'docs': 0, 'pages': 0, 'old_lines': 0, 'new_lines': 0,
           'tg': 0, 'rev': 0, 'men': 0, 'empty': []}
    failed = []

    for dbp in sorted(get_db_dir().glob('*/xsf.db')):
        col = dbp.parent.name
        conn = get_conn(col)
        rows = conn.execute(
            "SELECT id, title, page_count FROM documents "
            "WHERE doc_type='born-digital' ORDER BY id").fetchall()
        if wanted is not None:
            rows = [r for r in rows if r['id'] in wanted]
        if not rows:
            conn.close()
            continue

        for r in rows:
            did = r['id']
            t = (r['title'] or '')[:24]
            old, tg, rev, men = doc_counts(conn, did)
            orig = get_original_file(col, did)
            if orig is None or not Path(orig).is_file():
                print(f"[{col}#{did}] {t} — 原件缺失, 跳过")
                failed.append((col, did, '原件缺失'))
                continue

            if args.dry_run:
                print(f"[{col}#{did}] {t} — 可转 pages={r['page_count']} "
                      f"旧行={old} trans/gloss={tg} rev={rev} mentions={men}")
                tot['docs'] += 1
                tot['pages'] += r['page_count'] or 0
                tot['old_lines'] += old
                tot['tg'] += tg
                tot['rev'] += rev
                tot['men'] += men
                continue

            try:
                conn.execute("DELETE FROM lines WHERE doc_id=?", (did,))
                conn.execute("DELETE FROM blocks_fts WHERE doc_id=?", (did,))
                n_lines, n_blocks, empty = _native_extract_pages(
                    orig, conn, did)
                if n_lines == 0:
                    conn.rollback()
                    print(f"[{col}#{did}] {t} — 整篇零行可转, 回滚保留旧文本 "
                          f"(需人工走 OCR)")
                    failed.append((col, did, '整篇零行'))
                    continue
                conn.execute(
                    "UPDATE entity_mentions SET valid=0 "
                    "WHERE doc_id=? AND valid=1", (did,))
                doc_cols = {c[1] for c in conn.execute(
                    "PRAGMA table_info(documents)")}
                sets = ["doc_type='pdf_native'"]
                if 'sag_dirty' in doc_cols:
                    sets.append("sag_dirty=1")
                conn.execute(
                    f"UPDATE documents SET {', '.join(sets)} WHERE id=?",
                    (did,))
                conn.commit()
                print(f"[{col}#{did}] {t} ✓ pages={r['page_count']} "
                      f"行 {old}→{n_lines} blocks={n_blocks} "
                      f"留空页={empty} 抹 trans/gloss={tg} "
                      f"rev 留痕={rev} mentions 失效={men}")
                tot['docs'] += 1
                tot['pages'] += r['page_count'] or 0
                tot['old_lines'] += old
                tot['new_lines'] += n_lines
                tot['tg'] += tg
                tot['rev'] += rev
                tot['men'] += men
                tot['empty'].extend(f"{col}#{did}p{p}" for p in empty)
            except Exception as e:
                conn.rollback()
                print(f"[{col}#{did}] {t} — 失败 (已回滚): {e}")
                failed.append((col, did, str(e)))
        conn.close()

    print()
    print(f"== {'盘点 (dry-run)' if args.dry_run else '重转'} 完成 ==")
    print(f"docs={tot['docs']} pages={tot['pages']} 旧行={tot['old_lines']}"
          + (f" 新行={tot['new_lines']}" if not args.dry_run else ""))
    print(f"抹 trans/gloss 行={tot['tg']} | revision_log 留痕={tot['rev']} "
          f"| mentions 失效={tot['men']}")
    if tot['empty']:
        print(f"留空页 ({len(tot['empty'])}): " + ", ".join(tot['empty']))
    if failed:
        print("失败:")
        for col, did, why in failed:
            print(f"  [{col}#{did}] {why}")


if __name__ == '__main__':
    main()
