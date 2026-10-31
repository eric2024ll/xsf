import json
import re
import shutil
import posixpath

import pymupdf
import jieba
from pathlib import Path
from .db import get_conn
from .config import get_collections_dir
from .suspect import detect_suspect

_CJK_SEG = re.compile(r'([\u4e00-\u9fff]+)')


def attach_original(collection: str, doc_id: int, src_path: str | Path,
                    move: bool = False) -> Path:
    """入库即存原件（2026-10-28 原件统一模型）。

    把原件存到 files/<doc_id>/original.<ext> 并回写 linked_pdf
    （字段语义 = 原件相对路径），同时清除 original_virtual。
    move=True 用于 uploads 投放流（移入式，避免双份存储）；
    CLI/上传自用户路径时用默认复制。

    设计: mem 小书房/完善路线图 (2026-10-28 用户裁定)
    """
    src = Path(src_path)
    dst_dir = get_collections_dir() / collection / 'files' / str(doc_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    rel = f'files/{doc_id}/original{src.suffix.lower()}'
    dst = get_collections_dir() / collection / rel
    if move:
        shutil.move(str(src), str(dst))
    else:
        shutil.copy2(src, dst)
    conn = get_conn(collection)
    try:
        conn.execute(
            'UPDATE documents SET linked_pdf = ?, original_virtual = 0 '
            'WHERE id = ?',
            (rel, doc_id),
        )
        conn.commit()
    finally:
        conn.close()
    return dst


def get_original_file(collection: str, doc_id: int) -> Path | None:
    """读某文献的原件绝对路径（不存在返回 None）。"""
    from .config import resolve_original_path
    conn = get_conn(collection)
    try:
        r = conn.execute(
            'SELECT linked_pdf FROM documents WHERE id = ?', (doc_id,)
        ).fetchone()
    finally:
        conn.close()
    if not r or not r['linked_pdf']:
        return None
    p = resolve_original_path(collection, r['linked_pdf'])
    return p if (p and p.exists()) else None


def _tokenize(text: str) -> str:
    """分词，空格连接，供 FTS5 trigram 索引。

    仅 CJK 连续段走 jieba；非 CJK 段（西里尔/拉丁等）原样保留——
    jieba 会把西里尔文本切成单字，打散 trigram 索引（2026-10-26 修，
    《Историческая информатика》PDF 入库检索全空暴露）。
    """
    parts = _CJK_SEG.split(text)
    out = []
    for i, p in enumerate(parts):
        if not p.strip():
            continue
        if i % 2 == 1:  # 捕获组 = CJK 连续段
            out.extend(t for t in jieba.cut_for_search(p) if t.strip())
        else:
            out.append(p)
    return ' '.join(out)


def _source_flags(source_tags: str):
    """从 source_tags JSON 推导旧三列布尔值（向后兼容 is_primary 等）"""
    try:
        tags = json.loads(source_tags)
        if not isinstance(tags, list):
            tags = ["primary"]
    except (json.JSONDecodeError, TypeError):
        tags = ["primary"]
    return (
        1 if "primary" in tags else 0,
        1 if "secondary" in tags else 0,
        1 if "reference" in tags else 0,
    )


def ingest_pdf(pdf_path: str | Path, collection: str,
               cite_key: str = None, title: str = None,
               author: str = None,
               source_tags: str = '["primary"]') -> dict:
    """将 PDF 导入数据库，返回统计信息"""
    pdf_path = Path(pdf_path)
    doc = pymupdf.open(pdf_path)

    # 从 PDF 元数据提取标题/作者（如果未手动指定）
    meta = doc.metadata or {}
    if not title:
        title = meta.get('title') or None
    if not author:
        author = meta.get('author') or None

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (cite_key, title, author,
             pdf_path.name, len(doc),
             is_primary, is_secondary, is_reference, source_tags)
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
                       author: str = None,
                       source_tags: str = '["primary"]',
                       provider_id: str = None) -> dict:
    """扫描件 OCR 入库（vl_api provider）。bbox+block_label 入库，doc_type='ocr'。"""
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

    # 调 OCR provider (默认走全局 default, provider_id 可显式指定)
    provider = get_provider(provider_id)
    pages = provider.ocr(str(pdf_path))

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, ?, 'ocr', ?, ?, ?, ?)''',
            (cite_key, title, author, pdf_path.name, page_count,
             is_primary, is_secondary, is_reference, source_tags)
        )
        doc_id = cur.lastrowid

        total_lines = 0
        total_blocks = 0

        for page in pages:
            page_num = page.get('page_index', 0) + 1
            parsing_res_list = page.get('parsing_res_list', [])
            page_w = page.get('width')
            page_h = page.get('height')

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

                # 块粒度: 一块一行记录 (行内 \n 保留, bbox 即块框) — 2026-09-06 批次2
                block_num += 1
                bbox_json = json.dumps(bbox) if bbox else None
                suspect = detect_suspect(text, label, bbox_json, page_w, page_h)

                conn.execute(
                    '''INSERT INTO lines
                       (doc_id, page_num, block_num, line_num, text, bbox, block_label, page_w, page_h, suspect)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (doc_id, page_num, block_num, 1, text, bbox_json, label, page_w, page_h, suspect)
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


def ingest_markdown(md_path: str | Path, collection: str,
                    cite_key: str = None, title: str = None,
                    author: str = None,
                    source_tags: str = '["primary"]') -> dict:
    """Markdown 文本入库。整篇当 1 个 block，内部按行存储。

    doc_type='markdown'，page_count=1（md 无页概念，统一页 1）。
    """
    md_path = Path(md_path)
    text = md_path.read_text(encoding='utf-8', errors='replace')
    if not title:
        title = md_path.stem

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, 1, 'markdown', ?, ?, ?, ?)''',
            (cite_key, title, author, md_path.name,
             is_primary, is_secondary, is_reference, source_tags)
        )
        doc_id = cur.lastrowid

        total_lines = 0
        block_num = 1
        block_texts = []
        for ln in text.split('\n'):
            ln = ln.rstrip()
            if not ln.strip():
                continue
            line_num = total_lines + 1
            conn.execute(
                '''INSERT INTO lines
                   (doc_id, page_num, block_num, line_num, text)
                   VALUES (?, ?, ?, ?, ?)''',
                (doc_id, 1, block_num, line_num, ln)
            )
            block_texts.append(ln)
            total_lines += 1

        if block_texts:
            conn.execute(
                '''INSERT INTO blocks_fts
                   (doc_id, page_num, block_num, text)
                   VALUES (?, ?, ?, ?)''',
                (doc_id, 1, block_num, _tokenize('\n'.join(block_texts)))
            )

        conn.commit()
    finally:
        conn.close()

    return {
        'doc_id': doc_id,
        'pages': 1,
        'blocks': 1 if total_lines else 0,
        'lines': total_lines,
        'title': title,
        'doc_type': 'markdown',
    }


_OFFICE_EXTS = {'.doc', '.docx', '.docm', '.epub'}


def _epub_spine_files(zip_path: str | Path) -> list[str]:
    """EPUB spine 顺序的 XHTML 文件列表（解析失败回退文件名排序）。"""
    import zipfile
    from xml.etree import ElementTree as ET

    NS = {
        'cn': 'urn:oasis:names:tc:opendocument:xmlns:container',
        'opf': 'http://www.idpf.org/2007/opf',
    }
    with zipfile.ZipFile(zip_path) as zf:
        fallback = sorted(
            n for n in zf.namelist()
            if n.lower().endswith(('.xhtml', '.html', '.htm'))
        )
        try:
            container = ET.fromstring(zf.read('META-INF/container.xml'))
            opf_path = container.find('.//cn:rootfile', NS).get('full-path')
            opf = ET.fromstring(zf.read(opf_path))
            base = posixpath.dirname(opf_path)
            manifest = {
                i.get('id'): i.get('href')
                for i in opf.findall('.//opf:manifest/opf:item', NS)
            }
            hrefs = [
                manifest[ir.get('idref')]
                for ir in opf.findall('.//opf:spine/opf:itemref', NS)
                if ir.get('idref') in manifest
            ]
            spine = [
                posixpath.normpath(posixpath.join(base, h)) if base else h
                for h in hrefs
                if h and h.lower().endswith(('.xhtml', '.html', '.htm'))
            ]
            if spine:
                return spine
        except Exception:
            pass
    return fallback


def _epub_meta(zip_path: str | Path) -> tuple:
    """EPUB OPF 元数据 (dc:title, dc:creator), 无则 (None, None)。"""
    import zipfile
    from xml.etree import ElementTree as ET

    NS = {
        'cn': 'urn:oasis:names:tc:opendocument:xmlns:container',
        'opf': 'http://www.idpf.org/2007/opf',
        'dc': 'http://purl.org/dc/elements/1.1/',
    }
    try:
        with zipfile.ZipFile(zip_path) as zf:
            container = ET.fromstring(zf.read('META-INF/container.xml'))
            opf_path = container.find('.//cn:rootfile', NS).get('full-path')
            opf = ET.fromstring(zf.read(opf_path))
            t = opf.find('.//dc:title', NS)
            a = opf.find('.//dc:creator', NS)
            return (t.text.strip() if t is not None and t.text else None,
                    a.text.strip() if a is not None and a.text else None)
    except Exception:
        return (None, None)


def _xhtml_paragraphs(data: bytes) -> list[str]:
    """XHTML 提段: p/h1-h6/blockquote/li + 脚注 div (calibre 模式
    div.note / epub:type=note 等, 脚注常独立成文件且无 p/li)。"""
    from html.parser import HTMLParser

    VOID = {'br', 'hr', 'img', 'meta', 'link', 'input', 'source'}

    class _ParaParser(HTMLParser):
        BLOCKS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                  'blockquote', 'li'}
        NOTE_CLS = {'note', 'notes', 'footnote', 'footnotes'}
        NOTE_TYPES = {'note', 'footnote', 'footnotes', 'endnote',
                      'rearnote'}

        def __init__(self):
            super().__init__()
            self.paras = []
            self._stack = []          # [(is_block, chunks)]

        def _note_div(self, attrs) -> bool:
            d = dict(attrs)
            cls = {c.lower() for c in (d.get('class') or '').split()}
            et = {c.lower() for c in (d.get('epub:type') or '').split()}
            return bool(cls & self.NOTE_CLS or et & self.NOTE_TYPES)

        def handle_starttag(self, tag, attrs):
            if tag in VOID:
                return
            block = (tag in self.BLOCKS
                     or (tag == 'div' and self._note_div(attrs)))
            self._stack.append([block, []])

        def handle_endtag(self, tag):
            if not self._stack:
                return
            block, chunks = self._stack.pop()
            text = ' '.join(''.join(chunks).split())
            if block:
                if text:
                    self.paras.append(text)
            elif self._stack and text:
                # 普通容器: 文本上浮给父块 (不产段, 兄弟 note-div 各自成段)
                self._stack[-1][1].append(text)

        def handle_data(self, d):
            if self._stack:
                self._stack[-1][1].append(d)

    p = _ParaParser()
    p.feed(data.decode('utf-8', errors='replace'))
    return p.paras


def _epub_toc_tree(zip_path: str | Path,
                   spine: list[str]) -> list[dict]:
    """从 nav.xhtml (EPUB3) / toc.ncx (EPUB2) 提取目录树。

    节点 {label, page_num, from:'epub-toc'}; page_num = href 对应 spine 页号
    (1 起, 锚部分忽略, 不在 spine 的文件页号为 None)。深度 ≤3。失败返 []。
    """
    import zipfile
    from xml.etree import ElementTree as ET

    NS = {
        'cn': 'urn:oasis:names:tc:opendocument:xmlns:container',
        'opf': 'http://www.idpf.org/2007/opf',
        'epub': 'http://www.idpf.org/2007/ops',
        'ncx': 'http://www.daisy.org/z3986/2005/ncx/',
        'x': 'http://www.w3.org/1999/xhtml',
    }
    page_of = {name: i + 1 for i, name in enumerate(spine)}

    def _norm(name: str, base: str) -> str:
        return posixpath.normpath(posixpath.join(base, name)) if base else name

    def _mk_page(base: str):
        def _page(href: str):
            href = (href or '').split('#')[0]
            return page_of.get(href) or page_of.get(_norm(href, base))
        return _page

    def _cap(nodes: list[dict], depth: int = 1) -> list[dict]:
        out = []
        for n in nodes:
            if not n.get('label'):
                continue
            node = {'label': n['label'], 'category': None,
                    'page_num': n.get('page_num'), 'from': 'epub-toc'}
            if depth < 3 and n.get('children'):
                kids = _cap(n['children'], depth + 1)
                if kids:
                    node['children'] = kids
            out.append(node)
        return out

    try:
        with zipfile.ZipFile(zip_path) as zf:
            container = ET.fromstring(zf.read('META-INF/container.xml'))
            opf_path = container.find('.//cn:rootfile', NS).get('full-path')
            opf = ET.fromstring(zf.read(opf_path))
            base = posixpath.dirname(opf_path)
            manifest = opf.findall('.//opf:manifest/opf:item', NS)

            # ① EPUB3 nav (manifest properties 含 nav)
            nav_href = next(
                (i.get('href') for i in manifest
                 if 'nav' in (i.get('properties') or '')), None)
            if not nav_href:
                nav_href = next(
                    (i.get('href') for i in manifest
                     if (i.get('href') or '').lower() in
                     ('nav.xhtml', 'toc.xhtml', 'nav.html')), None)
            if nav_href:
                nav_name = _norm(nav_href, base)
                try:
                    root = ET.fromstring(zf.read(nav_name))
                except KeyError:
                    root = None
                if root is not None:
                    _page = _mk_page(posixpath.dirname(nav_name))
                    nav_el = None
                    for nv in root.iter(f'{{{NS["x"]}}}nav'):
                        t = nv.get(f'{{{NS["epub"]}}}type', '')
                        if t == 'toc' or not t:
                            nav_el = nv
                            if t == 'toc':
                                break
                    if nav_el is not None:
                        def _parse_ol(ol, depth=1):
                            nodes = []
                            for li in ol.findall(f'{{{NS["x"]}}}li'):
                                a = li.find(f'{{{NS["x"]}}}a')
                                node = {
                                    'label': ''.join(
                                        a.itertext()).strip() if a is not None
                                    else ''.join(li.itertext()).strip(),
                                    'page_num': _page(a.get('href'))
                                    if a is not None else None,
                                    'children': []}
                                sub = li.find(f'{{{NS["x"]}}}ol')
                                if sub is not None and depth < 4:
                                    node['children'] = _parse_ol(sub, depth + 1)
                                nodes.append(node)
                            return nodes
                        ol = nav_el.find(f'{{{NS["x"]}}}ol')
                        if ol is not None:
                            return _cap(_parse_ol(ol))

            # ② EPUB2 toc.ncx (spine@toc → manifest id)
            spine_el = opf.find('opf:spine', NS)
            ncx_id = spine_el.get('toc') if spine_el is not None else None
            ncx_href = next(
                (i.get('href') for i in manifest
                 if i.get('id') == ncx_id), None)
            if not ncx_href:
                ncx_href = next(
                    (i.get('href') for i in manifest
                     if (i.get('href') or '').lower().endswith('.ncx')), None)
            if ncx_href:
                ncx_name = _norm(ncx_href, base)
                try:
                    root = ET.fromstring(zf.read(ncx_name))
                except KeyError:
                    root = None
                if root is not None:
                    _page = _mk_page(posixpath.dirname(ncx_name))
                    navmap = root.find('ncx:navMap', NS)
                    if navmap is not None:
                        def _parse_np(np, depth=1):
                            label_el = np.find(
                                'ncx:navLabel/ncx:text', NS)
                            content = np.find('ncx:content', NS)
                            node = {
                                'label': (label_el.text or '').strip()
                                if label_el is not None else '',
                                'page_num': _page(content.get('src'))
                                if content is not None else None,
                                'children': []}
                            for sub in np.findall('ncx:navPoint', NS):
                                if depth < 4:
                                    node['children'].append(
                                        _parse_np(sub, depth + 1))
                            return node
                        nodes = [_parse_np(np)
                                 for np in navmap.findall('ncx:navPoint', NS)]
                        while (len(nodes) == 1 and nodes[0]['children']
                               and len(nodes[0]['children']) > 1):
                            # calibre 单根壳 (书名 navPoint 包全书): 下钻
                            nodes = nodes[0]['children']
                        return _cap(nodes)
    except Exception:
        pass
    return []


def ingest_epub(epub_path: str | Path, collection: str,
                cite_key: str = None, title: str = None,
                author: str = None,
                source_tags: str = '["primary"]') -> dict:
    """EPUB 分页入库: spine 每个 XHTML 文档 = 1 页, 段落建块。

    分页对照语义: 页 = 章节 (spine 文档); 校对台影印区经
    page/{n}/source 端点渲染章节 XHTML 原文, 转录区显示提取段落。
    doc_type='epub', 与 office (整篇单块) 管线区分。
    """
    import zipfile
    epub_path = Path(epub_path)
    spine = _epub_spine_files(epub_path)
    if not spine:
        raise RuntimeError('EPUB 无正文 XHTML 文件（spine 为空）')
    if not title or not author:
        m_title, m_author = _epub_meta(epub_path)
        title = title or m_title or epub_path.stem
        author = author or m_author

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, ?, 'epub', ?, ?, ?, ?)''',
            (cite_key, title, author, epub_path.name, len(spine),
             is_primary, is_secondary, is_reference, source_tags)
        )
        doc_id = cur.lastrowid

        total_lines = 0
        total_blocks = 0
        empty_pages = []
        with zipfile.ZipFile(epub_path) as zf:
            for pno, name in enumerate(spine, 1):
                paras = _xhtml_paragraphs(zf.read(name))
                block_num = 0
                for para in paras:
                    block_num += 1
                    conn.execute(
                        '''INSERT INTO lines
                           (doc_id, page_num, block_num, line_num, text)
                           VALUES (?, ?, ?, ?, ?)''',
                        (doc_id, pno, block_num, 1, para)
                    )
                    conn.execute(
                        '''INSERT INTO blocks_fts
                           (doc_id, page_num, block_num, text)
                           VALUES (?, ?, ?, ?)''',
                        (doc_id, pno, block_num, _tokenize(para))
                    )
                    total_lines += 1
                    total_blocks += 1
                if block_num == 0:
                    empty_pages.append(pno)

        # 目录卡: nav/ncx 提取 (anydoc 与 spine 管线都不含 EPUB 目录, 2026-10-19)
        toc_tree = _epub_toc_tree(epub_path, spine)
        if toc_tree:
            conn.execute(
                "INSERT INTO doc_cards (doc_id, kind, content, status) "
                "VALUES (?, 'toc', ?, 'draft')",
                (doc_id, json.dumps({'tree': toc_tree}, ensure_ascii=False)))

        conn.commit()
    finally:
        conn.close()

    return {
        'doc_id': doc_id,
        'pages': len(spine),
        'blocks': total_blocks,
        'lines': total_lines,
        'title': title,
        'mode': 'epub (spine 分页)',
        'empty_pages': empty_pages,
        'toc_nodes': len(toc_tree),
    }


def _office_to_markdown(path: Path) -> str:
    """anydoc 转 GitHub-Flavored Markdown（Word 三件套 + EPUB）。

    import 优先（pyproject [office] 可选组），CLI 兜底（PATH 上的 anydoc），
    两者皆无时报错引导安装；转换失败（加密/损坏/超大）抛 RuntimeError。
    """
    try:
        import anydoc
    except ImportError:
        anydoc = None

    if anydoc is not None:
        try:
            return anydoc.to_markdown(str(path))
        except anydoc.ConvertError as e:
            raise RuntimeError(f"anydoc 转换失败: {e}") from e

    import shutil
    import subprocess
    cli = shutil.which('anydoc')
    if not cli:
        # systemd 服务 PATH 不含 ~/.local/bin (本机 anydoc CLI 安装位)
        candidate = Path.home() / '.local' / 'bin' / 'anydoc'
        if candidate.is_file():
            cli = str(candidate)
    if not cli:
        raise RuntimeError(
            "未安装 anydoc（Word/EPUB 转换依赖）: "
            "pip install firecrawl-anydoc 或将 anydoc CLI 加入 PATH")

    try:
        r = subprocess.run([cli, str(path)], capture_output=True,
                           text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("anydoc 转换超时 (300s)，文件过大或异常") from e
    if r.returncode != 0:
        msg = r.stderr.strip().removeprefix('anydoc:').strip() \
            or f'退出码 {r.returncode}'
        raise RuntimeError(f"anydoc 转换失败: {msg}")
    return r.stdout


_MD_PREFIX_RE = re.compile(r'^(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s+)+')


def probe_pdf_text_layer(pdf_path: str | Path, min_chars: int = 20) -> float:
    """探测 PDF 文本层覆盖率: 抽样前 5 页 + 中段 3 页, 返回有文本层页占比。

    born-digital PDF 与扫描件的物理判据是文本层字符量, 而非人肉勾选。
    占比 >= 0.90 判定数字原生 (阈值用户裁定 2026-10-17, 保守取向:
    混合件倾向走 OCR)。
    """
    doc = pymupdf.open(pdf_path)
    try:
        n = len(doc)
        if n == 0:
            return 0.0
        idxs = set(range(min(n, 5)))
        if n > 8:
            mid = n // 2
            idxs.update(i for i in (mid - 1, mid, mid + 1) if 0 <= i < n)
        idxs = sorted(idxs)
        hits = sum(
            1 for i in idxs
            if len(doc[i].get_text().strip()) >= min_chars
        )
        return hits / len(idxs)
    finally:
        doc.close()


def ingest_native_pdf(pdf_path: str | Path, collection: str,
                      cite_key: str = None, title: str = None,
                      author: str = None,
                      source_tags: str = '["primary"]') -> dict:
    """数字原生 PDF 入库: 逐页拆分 -> anydoc 转 Markdown -> 按段落建块。

    - anydoc 整篇输出无页界标记, 故按页拆转; 页归属 100% 保真,
      跨页段落按页硬切 (校对工作台按页工作, 页内完整可接受)
    - 每段落一个 block (单 line), 段内折行合并; 剥 md 标记前缀
    - doc_type='pdf_native', 与 ingest_pdf (PyMuPDF 直提, 无段落感)
      及 OCR 管线 (doc_type='ocr') 区分
    - 已知限制: md 表格语法字符 (| 等) 原样入库, 表格件建议强制 OCR
    """
    import tempfile
    pdf_path = Path(pdf_path)
    src = pymupdf.open(pdf_path)
    page_count = len(src)
    meta = src.metadata or {}
    if not title:
        title = meta.get('title') or None
    if not author:
        author = meta.get('author') or None

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, ?, 'pdf_native', ?, ?, ?, ?)''',
            (cite_key, title, author, pdf_path.name, page_count,
             is_primary, is_secondary, is_reference, source_tags)
        )
        doc_id = cur.lastrowid

        total_lines = 0
        total_blocks = 0
        empty_pages = []
        with tempfile.TemporaryDirectory(prefix='xsf_native_') as td:
            for pno in range(page_count):
                one = pymupdf.open()
                one.insert_pdf(src, from_page=pno, to_page=pno)
                tmp_pdf = Path(td) / f'p{pno + 1}.pdf'
                one.save(tmp_pdf)
                one.close()

                md = _office_to_markdown(tmp_pdf)
                block_num = 0
                for para in md.split('\n\n'):
                    para = _MD_PREFIX_RE.sub('', para.strip()).strip()
                    para = ' '.join(para.split('\n')).strip()
                    para = para.replace('**', '').replace('`', '')
                    if not para:
                        continue
                    block_num += 1
                    conn.execute(
                        '''INSERT INTO lines
                           (doc_id, page_num, block_num, line_num, text)
                           VALUES (?, ?, ?, ?, ?)''',
                        (doc_id, pno + 1, block_num, 1, para)
                    )
                    conn.execute(
                        '''INSERT INTO blocks_fts
                           (doc_id, page_num, block_num, text)
                           VALUES (?, ?, ?, ?)''',
                        (doc_id, pno + 1, block_num, _tokenize(para))
                    )
                    total_lines += 1
                    total_blocks += 1
                if block_num == 0:
                    empty_pages.append(pno + 1)

        conn.commit()
    finally:
        src.close()
        conn.close()

    return {
        'doc_id': doc_id,
        'pages': page_count,
        'blocks': total_blocks,
        'lines': total_lines,
        'title': title,
        'mode': 'native (anydoc)',
        'empty_pages': empty_pages,
    }


def ingest_office(office_path: str | Path, collection: str,
                  cite_key: str = None, title: str = None,
                  author: str = None,
                  source_tags: str = '["primary"]') -> dict:
    """Word/EPUB 文档（.doc/.docx/.docm/.epub）入库。anydoc 转 Markdown 后复用
    ingest_markdown 的行/块管线（ingest_image 同款「转格式再复用」手法）。

    doc_type='office'，page_count=1（无页概念），filename 记原文件名。
    原文件照常存 uploads/；转换文本只进 DB 不落盘（2026-09-29 决策）。
    """
    import os
    import tempfile

    office_path = Path(office_path)
    md_text = _office_to_markdown(office_path)

    fd, tmp_md = tempfile.mkstemp(suffix='.md')
    os.close(fd)
    Path(tmp_md).write_text(md_text, encoding='utf-8')

    try:
        result = ingest_markdown(
            tmp_md,
            collection=collection,
            cite_key=cite_key,
            title=title or office_path.stem,
            author=author,
            source_tags=source_tags,
        )
        doc_id = result['doc_id']
        conn = get_conn(collection)
        try:
            conn.execute(
                "UPDATE documents SET filename = ?, doc_type = 'office' "
                "WHERE id = ?",
                (office_path.name, doc_id),
            )
            conn.commit()
        finally:
            conn.close()
        result['filename'] = office_path.name
        result['doc_type'] = 'office'
    finally:
        os.unlink(tmp_md)

    return result


def ingest_image(img_path: str | Path, collection: str,
                 cite_key: str = None, title: str = None,
                 author: str = None,
                 source_tags: str = '["primary"]') -> dict:
    """图片 OCR 入库。把图片包成单页 PDF，复用 ingest_scanned_pdf 的 OCR 流程。

    doc_type='ocr'，page_count=1，filename 记原图片名（非临时 PDF 名）。
    """
    import os
    import tempfile

    img_path = Path(img_path)

    # 1. 用 PyMuPDF 把图片包成单页 PDF（保留原始像素尺寸）
    src = pymupdf.open(str(img_path))
    rect = src[0].rect
    pdf = pymupdf.open()
    page = pdf.new_page(width=rect.width, height=rect.height)
    page.insert_image(rect, filename=str(img_path))
    src.close()

    fd, tmp_pdf = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    pdf.save(tmp_pdf)
    pdf.close()

    try:
        result = ingest_scanned_pdf(
            tmp_pdf,
            collection=collection,
            cite_key=cite_key,
            title=title or img_path.stem,
            author=author,
            source_tags=source_tags,
        )
        # 2. 修正 documents.filename 为原图片名（ingest_scanned_pdf 记的是临时 pdf 名）
        doc_id = result['doc_id']
        conn = get_conn(collection)
        try:
            conn.execute(
                "UPDATE documents SET filename = ? WHERE id = ?",
                (img_path.name, doc_id),
            )
            conn.commit()
        finally:
            conn.close()
        result['filename'] = img_path.name
    finally:
        os.unlink(tmp_pdf)

    return result


def remove_doc(doc_id: int, collection: str):
    """删除文献（数据库 + 原件目录 files/<id>/, 单布局 2026-10-31）"""
    import shutil

    from .config import get_collections_dir

    filename = None
    conn = get_conn(collection)
    try:
        row = conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row:
            filename = row["filename"]
        conn.execute('DELETE FROM lines WHERE doc_id = ?', (doc_id,))
        conn.execute(
            'DELETE FROM blocks_fts WHERE doc_id = ?', (doc_id,)
        )
        # 卡片/候选/提及随文献清理 (避免孤儿数据进检索)
        conn.execute('DELETE FROM doc_cards WHERE doc_id = ?', (doc_id,))
        conn.execute('DELETE FROM card_candidates WHERE doc_id = ?', (doc_id,))
        conn.execute('DELETE FROM cards_fts WHERE doc_id = ?', (doc_id,))
        conn.execute('DELETE FROM entity_mentions WHERE doc_id = ?', (doc_id,))
        conn.execute('DELETE FROM documents WHERE id = ?', (doc_id,))
        conn.commit()
    finally:
        conn.close()

    # 原件目录整体清除 (id 键控, 无误伤); 目录不在不算错
    orig_dir = get_collections_dir() / collection / 'files' / str(doc_id)
    if orig_dir.is_dir():
        shutil.rmtree(orig_dir, ignore_errors=True)
    # 历史布局兜底: 旧数据原件可能仍滞留 uploads/<filename>
    if filename:
        from .config import get_upload_path
        fp = get_upload_path(collection, filename)
        if fp.exists():
            fp.unlink()
