import pymupdf
import jieba
from pathlib import Path
from .db import get_conn


def _tokenize(text: str) -> str:
    """jieba 分词，空格连接，供 FTS5 索引"""
    return ' '.join(jieba.cut_for_search(text))


def ingest_pdf(pdf_path: str | Path, collection: str,
               cite_key: str = None, title: str = None,
               author: str = None) -> dict:
    """将 PDF 导入数据库，返回统计信息"""
    pdf_path = Path(pdf_path)
    doc = pymupdf.open(pdf_path)

    # 从 PDF 元数据提取标题/作者（如果未手动指定）
    meta = doc.metadata or {}
    if not title:
        title = meta.get('title') or None
    if not author:
        author = meta.get('author') or None

    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count)
               VALUES (?, ?, ?, ?, ?)''',
            (cite_key, title, author,
             pdf_path.name, len(doc))
        )
        doc_id = cur.lastrowid

        total_lines = 0
        total_blocks = 0

        for page_num, page in enumerate(doc, 1):
            page_dict = page.get_text('dict')
            block_num = 0

            for block in page_dict.get('blocks', []):
                if block.get('type', 0) != 0:
                    continue

                block_num += 1
                block_lines = []
                line_num = 0

                for line in block.get('lines', []):
                    spans = line.get('spans', [])
                    line_text = ''.join(
                        span.get('text', '') for span in spans
                    ).strip()
                    if not line_text:
                        continue

                    line_num += 1
                    block_lines.append(line_text)
                    conn.execute(
                        '''INSERT INTO lines
                           (doc_id, page_num, block_num, line_num, text)
                           VALUES (?, ?, ?, ?, ?)''',
                        (doc_id, page_num, block_num, line_num, line_text)
                    )
                    total_lines += 1

                if block_lines:
                    block_text = '\n'.join(block_lines)
                    conn.execute(
                        '''INSERT INTO blocks_fts
                           (doc_id, page_num, block_num, text)
                           VALUES (?, ?, ?, ?)''',
                        (doc_id, page_num, block_num,
                         _tokenize(block_text))
                    )
                    total_blocks += 1

        conn.commit()
    finally:
        page_count = len(doc)
        conn.close()
        doc.close()

    return {
        'doc_id': doc_id,
        'pages': page_count,
        'blocks': total_blocks,
        'lines': total_lines,
        'title': title,
    }


def ingest_scanned_pdf(pdf_path: str | Path, collection: str,
                       cite_key: str = None, title: str = None,
                       author: str = None) -> dict:
    """扫描件 OCR 入库（PaddleOCR-VL）。bbox+block_label 入库，doc_type='ocr'。"""
    import json
    from .ocr import get_provider

    pdf_path = Path(pdf_path)

    # 获取页数（扫描件 metadata 通常为空）
    doc = pymupdf.open(pdf_path)
    page_count = len(doc)
    meta = doc.metadata or {}
    if not title:
        title = meta.get('title') or None
    if not author:
        author = meta.get('author') or None
    doc.close()

    # 调 OCR provider
    provider = get_provider()
    pages = provider.ocr(str(pdf_path))

    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type)
               VALUES (?, ?, ?, ?, ?, 'ocr')''',
            (cite_key, title, author, pdf_path.name, page_count)
        )
        doc_id = cur.lastrowid

        total_lines = 0
        total_blocks = 0

        for page in pages:
            page_num = page.get('page_index', 0) + 1
            parsing_res_list = page.get('parsing_res_list', [])

            block_num = 0
            for block in parsing_res_list:
                label = block.get('block_label', '')
                content = block.get('block_content', '')
                bbox = block.get('block_bbox')

                if label == 'header':
                    continue

                # content 可能是 dict (table) 或 str
                if isinstance(content, dict):
                    text = content.get('html') or content.get('markdown') or ''
                else:
                    text = str(content) if content else ''

                text = text.strip()
                if not text:
                    continue

                block_num += 1
                bbox_json = json.dumps(bbox) if bbox else None

                conn.execute(
                    '''INSERT INTO lines
                       (doc_id, page_num, block_num, line_num, text, bbox, block_label)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (doc_id, page_num, block_num, 1, text, bbox_json, label)
                )
                total_lines += 1

                conn.execute(
                    '''INSERT INTO blocks_fts
                       (doc_id, page_num, block_num, text)
                       VALUES (?, ?, ?, ?)''',
                    (doc_id, page_num, block_num, _tokenize(text))
                )
                total_blocks += 1

        conn.commit()
    finally:
        conn.close()

    return {
        'doc_id': doc_id,
        'pages': page_count,
        'blocks': total_blocks,
        'lines': total_lines,
        'title': title,
        'doc_type': 'ocr',
    }


def remove_doc(doc_id: int, collection: str):
    """删除文献及其所有行和 FTS 条目"""
    conn = get_conn(collection)
    try:
        conn.execute('DELETE FROM lines WHERE doc_id = ?', (doc_id,))
        conn.execute(
            'DELETE FROM blocks_fts WHERE doc_id = ?', (doc_id,)
        )
        conn.execute('DELETE FROM documents WHERE id = ?', (doc_id,))
        conn.commit()
    finally:
        conn.close()
