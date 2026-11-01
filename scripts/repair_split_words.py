#!/usr/bin/env python3
"""修复 anydoc 词首大写字母被误切的问题 (2026-11-01).

背景: CyberLeninka 类 PDF (子集字体, 大写字母右侧留白偏大) 下, anydoc 的
切词阈值把词首大写字母当词边界切出去 ('А ктивный', 'Р ис. 1')。管线侧已在
xsf/ingest.py 的 _native_extract_pages 内接入 page_word_oracle + repair_split_words;
本脚本对**存量** lines 做原位修复 (块结构不变, 只回接空格):

  1. 逐页取 PyMuPDF 词表 oracle (同页词集合 + 相邻词二元组)
  2. 对每行套 repair_split_words; 有变化才 UPDATE lines.text
  3. 受影响 block 重建 blocks_fts (全文含 trans 拼接, 与校对台编辑同规则)
  4. trans/gloss/修订史/实体提及锚点不动 (页-块定位不因去空格变化)

用法:
    .venv/bin/python scripts/repair_split_words.py --dry-run -c ai与历史研究综合 --ids 330,331
    .venv/bin/python scripts/repair_split_words.py -c ai与历史研究综合 --ids 330,331
    .venv/bin/python scripts/repair_split_words.py            # 全部书架全部 pdf_native
"""
import argparse
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.env import load_env  # noqa: E402
load_env()
from xsf.config import get_collections_dir  # noqa: E402
from xsf.db import get_conn  # noqa: E402
from xsf.ingest import (  # noqa: E402
    page_word_oracle, repair_split_words, _tokenize, get_original_file,
)


def iter_collections():
    root = get_collections_dir()
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and (p / 'files').is_dir():
            out.append(p.name)
    return out


def repair_doc(conn, collection: str, doc_id: int, dry_run: bool) -> dict:
    """返回 {'lines': 改动行数, 'chars': 增删字符数, 'blocks': 受影响块数}."""
    orig = get_original_file(collection, doc_id)
    if not orig:
        return {'skip': '无原件'}
    pdf = pymupdf.open(str(orig))
    try:
        cache = {}
        changed = 0
        delta = 0
        blocks = set()
        rows = conn.execute(
            "SELECT id, page_num, block_num, text FROM lines WHERE doc_id=?",
            (doc_id,)).fetchall()
        for r in rows:
            pno = r['page_num'] - 1
            if pno < 0 or pno >= len(pdf):
                continue
            if pno not in cache:
                cache[pno] = page_word_oracle(pdf[pno])
            words, bigrams, hyphenated = cache[pno]
            new = repair_split_words(r['text'] or '', words, bigrams,
                                     hyphenated)
            if new == (r['text'] or ''):
                continue
            changed += 1
            delta += len(r['text'] or '') - len(new)
            blocks.add((r['page_num'], r['block_num']))
            if not dry_run:
                conn.execute("UPDATE lines SET text=? WHERE id=?", (new, r['id']))
        if not dry_run:
            for (pg, bn) in sorted(blocks):
                block_rows = conn.execute(
                    """SELECT text, trans FROM lines
                       WHERE doc_id=? AND page_num=? AND block_num=?
                       ORDER BY line_num""",
                    (doc_id, pg, bn)).fetchall()
                block_text = '\n'.join(x['text'] for x in block_rows)
                block_trans = ' '.join(
                    x['trans'] for x in block_rows if (x['trans'] or '').strip())
                conn.execute(
                    """DELETE FROM blocks_fts
                       WHERE doc_id=? AND page_num=? AND block_num=?""",
                    (doc_id, pg, bn))
                conn.execute(
                    """INSERT INTO blocks_fts(doc_id, page_num, block_num,
                                              text, trans)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, pg, bn, _tokenize(block_text), block_trans or None))
            conn.commit()
        return {'lines': changed, 'chars': delta, 'blocks': len(blocks),
                'pages': len(pdf)}
    finally:
        pdf.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-c', '--collection', action='append',
                    help='书架名 (可重复; 省略=全部)')
    ap.add_argument('--ids', help='文献 id, 逗号分隔')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(',')] if args.ids else None
    collections = args.collection or iter_collections()
    total = 0
    for c in collections:
        try:
            conn = get_conn(c)
        except Exception as e:      # 书架无 DB
            print(f'[跳过] {c}: {e}')
            continue
        try:
            sql = "SELECT id, title FROM documents WHERE doc_type='pdf_native'"
            params = ()
            if ids:
                sql += f" AND id IN ({','.join('?' * len(ids))})"
                params = tuple(ids)
            docs = conn.execute(sql + ' ORDER BY id', params).fetchall()
            for d in docs:
                res = repair_doc(conn, c, d['id'], args.dry_run)
                if 'skip' in res:
                    print(f"[跳过] {c}#{d['id']}: {res['skip']}")
                    continue
                if res['lines']:
                    total += res['lines']
                    print(f"[{'试算' if args.dry_run else '修复'}] {c}#{d['id']} "
                          f"{(d['title'] or '')[:40]}: 行 {res['lines']} / "
                          f"块 {res['blocks']} / 去掉空格 {res['chars']} "
                          f"(共 {res['pages']} 页)")
        finally:
            conn.close()
    print(f"{'试算' if args.dry_run else '完成'}: 共修复 {total} 行")


if __name__ == '__main__':
    main()
