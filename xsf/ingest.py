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


# ── 语种判定 (2026-11-07) ──────────────────────────────
# documents.lang 默认 'zh'，此前入库从不判定语种：英文 PDF 入库后既不算外文
# (翻译批处理按 lang!='zh' 筛选)，界面也只按中文单行渲染 —— 用户看到「没有
# 翻译」的 3 篇英文论文就是这么漏的。入库后按正文拉丁/中日韩字符占比判定，
# 仅当「明确拉丁主导」时才改标 eng (保守：样本不足或中英混排一律留 zh)。

_CJK_CHAR = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
LANG_DETECT_MIN_ALPHA = 400      # 字母样本下限 (太小不判)
LANG_DETECT_CJK_RATIO = 0.02     # 中日韩字符占比低于此值视为拉丁主导


def detect_lang(conn, doc_id: int) -> str | None:
    """按正文判定语种: 明确拉丁主导返回 'eng', 否则 None (留默认 zh)。

    只读, 不改库 —— 入库流程判定后自行 UPDATE。
    """
    cjk = ascii_alpha = 0
    for row in conn.execute(
            "SELECT text FROM lines WHERE doc_id = ? "
            "ORDER BY page_num, block_num, line_num LIMIT 400", (doc_id,)):
        t = row[0] or ''
        cjk += len(_CJK_CHAR.findall(t))
        ascii_alpha += sum(1 for ch in t if ch.isascii() and ch.isalpha())
    total = cjk + ascii_alpha
    if total < LANG_DETECT_MIN_ALPHA:
        return None
    if cjk / total < LANG_DETECT_CJK_RATIO:
        return 'eng'
    return None


def _apply_detected_lang(conn, doc_id: int) -> str | None:
    """入库收尾: 判出外文则回写 documents.lang。返回写入语种或 None。"""
    lang = detect_lang(conn, doc_id)
    if lang:
        conn.execute("UPDATE documents SET lang = ? WHERE id = ?", (lang, doc_id))
    return lang


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

        _apply_detected_lang(conn, doc_id)
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

        _apply_detected_lang(conn, doc_id)
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

# ── anydoc 词首拆分修复 (2026-11-01) ──
# CyberLeninka 类 PDF (子集字体, 大写字母右侧留白偏大) 下, anydoc 的切词阈值
# 会把词首大写字母当词边界切出去: 'А нализ' / 'Р ис. 1' / 'С анкт-Петербург'。
# 以同页 PyMuPDF 词表为 oracle 确定性回接, 三类形态:
#   ① 首大写 + 空格 + 词  合并形在该页出现过 → 回接
#   ② 首大写 + 空格 + 连字符复合词 (С анкт-Петербург) → 该复合词在 PDF 某行内
#      整体出现过才回接
#   ③ 复合词内的空格 (Санкт- Петербург) → 同上
# ②③ 以「行内整体出现」为依据, 跨行软连字符 (attri- butes) 不会被误合成
# 'attri-butes'; ① 另要求两段不是该页合法相邻词, 保护 'В статье' 等虚词。
_SPLIT_SPACE_RE = re.compile(r'(?<![\w])([А-ЯЁA-Z]) ([а-яёa-z]{2,})')
_SPLIT_HYPHEN_WORD_RE = re.compile(
    r'(?<![\w])([А-ЯЁA-Z]) ([А-Яа-яЁёA-Za-z]+-[А-Яа-яЁёA-Za-z]+)')
_SPLIT_HYPHEN_RE = re.compile(r'(\w+)- (\w+)')
_WORD_TRIM = '.,;:!?()[]«»"\'\u2013\u2014'


def page_word_oracle(page):
    """页面 oracle: (词集合, 相邻词二元组集合, 行内连字符复合词集合)。"""
    words, bigrams, hyphenated, prev = set(), set(), set(), None
    for w in page.get_text('words'):
        core = w[4].strip().strip(_WORD_TRIM)
        if not core:
            continue
        words.add(core)
        if prev is not None:
            bigrams.add((prev, core))
        prev = core
    for line in page.get_text().splitlines():
        for tok in re.findall(r'\w+-\w+', line):
            hyphenated.add(tok)
            # 复合词各段并入词表: 'Н овиков-Д аурский' 两段都要能回接
            for part in tok.split('-'):
                if len(part) >= 2:
                    words.add(part)
    return words, bigrams, hyphenated


def repair_split_words(text: str, words: set, bigrams: set,
                       hyphenated: set = frozenset()) -> str:
    """回接 anydoc 误拆的词首大写字母与行内连字符复合词 (判定依据见上)。"""
    if not words or not text:
        return text

    def _join(m):
        merged = m.group(1) + m.group(2)
        if merged in words and (m.group(1), m.group(2)) not in bigrams:
            return merged
        return m.group(0)

    def _join_hyph(m):
        merged = f'{m.group(1)}-{m.group(2)}'
        return merged if merged in hyphenated else m.group(0)

    # 顺序: 先补复合词内空格 (Санкт- Петербург) → 再回接词首大写 (С анкт-Петербург),
    # 否则第二条规则看到的不是完整复合词
    text = _SPLIT_HYPHEN_RE.sub(_join_hyph, text)
    text = _SPLIT_HYPHEN_WORD_RE.sub(
        lambda m: m.group(1) + m.group(2)
        if (m.group(1) + m.group(2)) in hyphenated else m.group(0), text)
    return _SPLIT_SPACE_RE.sub(_join, text)


def _native_extract_pages(pdf_path: str | Path, conn, doc_id: int):
    """逐页 anydoc 抽取写 lines/blocks_fts (born-digital 文本层管线唯一实现).

    - anydoc 整篇输出无页界标记, 故按页拆转; 页归属 100% 保真,
      跨页段落按页硬切 (校对工作台按页工作, 页内完整可接受)
    - 每段落一个 block (单 line), 段内折行合并; 剥 md 标记前缀
    - 单页 anydoc 无法转换 (无文本层: 封面/插图/空白页): 该页留空
      (记入 empty_pages, 页图仍可看; 需要文本用校对台 reocr), 不中断整篇
    - 事务 commit/rollback 由调用方负责

    返回 (n_lines, n_blocks, empty_pages).
    """
    import tempfile
    src = pymupdf.open(pdf_path)
    try:
        total_lines = total_blocks = 0
        empty_pages = []
        with tempfile.TemporaryDirectory(prefix='xsf_native_') as td:
            for pno in range(len(src)):
                one = pymupdf.open()
                try:
                    one.insert_pdf(src, from_page=pno, to_page=pno)
                    tmp_pdf = Path(td) / f'p{pno + 1}.pdf'
                    one.save(tmp_pdf)
                finally:
                    one.close()
                try:
                    md = _office_to_markdown(tmp_pdf)
                except Exception:
                    empty_pages.append(pno + 1)
                    continue
                words, bigrams, hyphenated = page_word_oracle(src[pno])
                block_num = 0
                for para in md.split('\n\n'):
                    para = _MD_PREFIX_RE.sub('', para.strip()).strip()
                    para = ' '.join(para.split('\n')).strip()
                    para = para.replace('**', '').replace('`', '')
                    para = repair_split_words(para, words, bigrams,
                                              hyphenated)
                    if not para:
                        continue
                    block_num += 1
                    conn.execute(
                        '''INSERT INTO lines
                           (doc_id, page_num, block_num, line_num, text)
                           VALUES (?, ?, ?, ?, ?)''',
                        (doc_id, pno + 1, block_num, 1, para))
                    conn.execute(
                        '''INSERT INTO blocks_fts
                           (doc_id, page_num, block_num, text)
                           VALUES (?, ?, ?, ?)''',
                        (doc_id, pno + 1, block_num, _tokenize(para)))
                    total_lines += 1
                    total_blocks += 1
                if block_num == 0:
                    empty_pages.append(pno + 1)
        return total_lines, total_blocks, empty_pages
    finally:
        src.close()


def ingest_native_pdf(pdf_path: str | Path, collection: str,
                      cite_key: str = None, title: str = None,
                      author: str = None,
                      source_tags: str = '["primary"]') -> dict:
    """数字原生 PDF 入库: 逐页 anydoc 转 Markdown, 按段落建块 (文本层 PDF 唯一管线).

    - 2026-10-31 管线收敛: 文本层 PDF 一律走 anydoc (旧 PyMuPDF 直提管线退役);
      无文本层的页留空 (页图仍可看), doc_type='pdf_native',
      与 OCR 管线 (doc_type='ocr') 区分
    - 整篇抽不出行 (全部页不可转): 抛错回滚, 调用方回退扫描件 OCR 管线
    - 已知限制: md 表格语法字符 (| 等) 原样入库, 表格件建议强制 OCR
    """
    pdf_path = Path(pdf_path)
    src = pymupdf.open(pdf_path)
    page_count = len(src)
    meta = src.metadata or {}
    if not title:
        title = meta.get('title') or None
    if not author:
        author = meta.get('author') or None
    src.close()

    is_primary, is_secondary, is_reference = _source_flags(source_tags)
    conn = get_conn(collection)
    try:
        cur = conn.execute(
            '''INSERT INTO documents
               (cite_key, title, author, filename, page_count, doc_type,
                is_primary, is_secondary, is_reference, source_tags)
               VALUES (?, ?, ?, ?, ?, 'pdf_native', ?, ?, ?, ?)''',
            (cite_key, title, author, pdf_path.name, page_count,
             is_primary, is_secondary, is_reference, source_tags))
        doc_id = cur.lastrowid
        total_lines, total_blocks, empty_pages = _native_extract_pages(
            pdf_path, conn, doc_id)
        if total_lines == 0:
            raise RuntimeError(
                '整篇无可转文本层 (全部页需 OCR), 请用扫描件 OCR 管线')
        _apply_detected_lang(conn, doc_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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
