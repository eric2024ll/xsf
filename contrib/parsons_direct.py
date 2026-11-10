#!/usr/bin/env python3
"""doc132 parsons ClearScan PDF 直提 (anydoc 判 needs OCR, pymupdf 文本层可用).

复用 xsf 逻辑: detect_suspect / _tokenize / _apply_detected_lang。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.path.insert(0, '/home/eric/xsf')
import os
os.environ.setdefault('XSF_COLLECTIONS_DIR', '/mnt/nas/xsf-collections')

import pymupdf
from xsf.db import get_conn
from xsf.config import resolve_original_path
from xsf.ingest import detect_suspect, _tokenize, _apply_detected_lang

COLL, DID = '互嵌式', 132
conn = get_conn(COLL)
row = conn.execute('SELECT linked_pdf FROM documents WHERE id=?', (DID,)).fetchone()
pdf_path = resolve_original_path(COLL, row['linked_pdf'])

conn.execute('DELETE FROM lines WHERE doc_id=?', (DID,))
conn.execute('DELETE FROM blocks_fts WHERE doc_id=?', (DID,))
conn.execute('DELETE FROM ocr_page_state WHERE doc_id=?', (DID,))
conn.commit()

src = pymupdf.open(pdf_path)
n_lines = n_blocks = empty = 0
for pno in range(len(src)):
    page = src[pno]
    w, h = page.rect.width, page.rect.height
    blocks = [b for b in page.get_text('blocks') if b[6] == 0 and b[4].strip()]
    if not blocks:
        empty += 1
        continue
    for bn, b in enumerate(sorted(blocks, key=lambda x: (x[1], x[0])), 1):
        text = ' '.join(b[4].split())
        bbox = json.dumps([b[0], b[1], b[2], b[3]])
        suspect = detect_suspect(text, 'text', bbox, w, h)
        conn.execute(
            'INSERT INTO lines (doc_id, page_num, block_num, line_num, text, '
            'bbox, block_label, page_w, page_h, suspect) '
            'VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)',
            (DID, pno + 1, bn, text, bbox, 'text', w, h, suspect))
        conn.execute(
            'INSERT INTO blocks_fts (doc_id, page_num, block_num, text) '
            'VALUES (?, ?, ?, ?)', (DID, pno + 1, bn, _tokenize(text)))
        n_blocks += 1
        n_lines += 1
src.close()
_apply_detected_lang(conn, DID)
conn.commit()
chk = conn.execute('SELECT count(*) FROM lines WHERE doc_id=?', (DID,)).fetchone()[0]
conn.close()
print(f'直提完成: {n_lines} 行 / {n_blocks} 块 / 空页 {empty} / DB 复核 {chk}')
