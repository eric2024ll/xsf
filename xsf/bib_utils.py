"""BibTeX 元数据辅助函数。

cite_key 自动生成 + documents 冗余列同步 + biblatex 字段预设。
"""
import json
import re
from pathlib import Path

from opencc import OpenCC
from pypinyin import lazy_pinyin, Style

_t2s = OpenCC('t2s')

BIB_TYPE_FIELDS = {
    "@article": ["author", "title", "journal", "year", "volume", "number", "pages"],
    "@book": ["author", "title", "publisher", "year", "address", "edition"],
    "@incollection": ["author", "title", "booktitle", "editor", "publisher", "year", "address", "pages"],
    "@manuscript": ["author", "title", "year", "institution"],
    "@online": ["author", "title", "year", "url"],
}

BIB_TYPE_LABELS = {
    "@book": "图书",
    "@article": "期刊/报纸",
    "@manuscript": "手稿/档案",
    "@online": "电子出版物",
    "@incollection": "析出文献",
}

BIB_FIELD_LABELS = {
    "author": "作者",
    "title": "标题",
    "year": "年份",
    "publisher": "出版者",
    "address": "出版地",
    "edition": "版次",
    "journal": "期刊名",
    "volume": "卷",
    "number": "期",
    "pages": "页码",
    "editor": "编者",
    "institution": "机构",
    "booktitle": "所在文献标题",
    "url": "URL",
    "doi": "DOI",
    "note": "备注",
    "month": "月份",
    "series": "丛书",
    "school": "学校",
    "organization": "组织",
    "howpublished": "出版方式",
}


def _slugify(text: str) -> str:
    """中文→全拼(小写), 英文取小写, 去非字母数字。"""
    if not text:
        return ""
    has_cjk = re.search(r"[\u4e00-\u9fff]", text)
    if has_cjk:
        parts = lazy_pinyin(text, style=Style.NORMAL, errors="default")
        s = "".join(parts).lower()
    else:
        s = text.lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def _title_slug(title: str) -> str:
    """标题 → 拼音关键词（取前 3 字全拼，与 ref.bib 惯例一致）。"""
    if not title:
        return "untitled"
    has_cjk = re.search(r"[\u4e00-\u9fff]", title)
    if has_cjk:
        snippet = title[:3]
        parts = lazy_pinyin(snippet, style=Style.NORMAL, errors="default")
        s = "".join(parts).lower()
    else:
        first_word = re.split(r"[\s:,.;]", title.strip())[0]
        s = first_word.lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s or "untitled"


def _first_author_surname(author_str: str) -> str:
    """从 author 字符串提取第一作者的姓 → 拼音。

    支持: "王明珂" / "Wang, Ming-ke" / "张三 and 李四" / "A. Smith and B. Jones"
    """
    if not author_str:
        return "anon"
    first = re.split(r"\s+and\s+|;|，|,", author_str.strip())[0].strip()
    if not first:
        return "anon"
    if re.search(r"[\u4e00-\u9fff]", first):
        return _slugify(first[0]) or "anon"
    parts = first.split()
    surname = parts[-1] if parts else first
    return re.sub(r"[^a-z]", "", surname.lower()) or "anon"


def generate_cite_key(bib_data: dict, existing_keys: set[str],
                      exclude_key: str = None) -> str:
    """从 bib_data 生成 cite_key: 姓+年+标题首词。冲突加 b/c/d。

    existing_keys: 该 collection 已有的 cite_key 集合。
    exclude_key: 更新时排除自身的 cite_key（避免自己和自己冲突）。
    """
    author = bib_data.get("author", "")
    year = ""
    if bib_data.get("year"):
        year = str(bib_data["year"]).strip()
    else:
        date = str(bib_data.get("date", "")).strip()
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    surname = _first_author_surname(author)
    title = bib_data.get("title", "")
    title_slug = _title_slug(title)

    base = f"{surname}{year}{title_slug}".lower()

    if exclude_key and base == exclude_key:
        return base

    if base not in existing_keys:
        return base

    for suffix in "bcdefghijklmnopqrstuvwxyz":
        candidate = base + suffix
        if candidate not in existing_keys:
            return candidate
        if exclude_key and candidate == exclude_key:
            return candidate

    return base + "x"


def sync_doc_fields(bib_data: dict) -> dict:
    """从 bib_data 提取 title/author 同步到 documents 冗余列。"""
    return {
        "title": (bib_data.get("title") or "").strip() or None,
        "author": (bib_data.get("author") or "").strip() or None,
    }


def parse_bib_data(raw: str) -> dict | None:
    """解析 bib_data JSON 字符串 → dict，失败返回 None。"""
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def to_bibtex(cite_key: str, bib_type: str, bib_data: dict) -> str:
    """将单条文献转为 BibTeX 格式字符串。"""
    ck = cite_key or "untitled"
    if not bib_type or not bib_data:
        title = (bib_data or {}).get("title", "") if bib_data else ""
        return f"@misc{{{ck},\n  title = {{{title}}},\n}}\n"
    lines = [f"{bib_type}{{{ck},"]
    for key, val in bib_data.items():
        if val:
            lines.append(f"  {key} = {{{val}}},")
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ── 批量 .bib 解析 + 匹配 ────────────────────────────────

def parse_bib_entries(text: str) -> list[dict]:
    """解析 .bib 文本 → 条目列表.

    每条: {type: '@article', cite_key: '...', fields: {title: ..., author: ...}}
    跳过 @string/@comment/@preamble.
    """
    entries = []
    i, n = 0, len(text)
    while i < n:
        at = text.find('@', i)
        if at == -1:
            break
        j = at + 1
        while j < n and (text[j].isalnum() or text[j] in '_-'):
            j += 1
        entry_type = text[at + 1:j].lower()
        while j < n and text[j] in ' \t\n\r':
            j += 1
        if j >= n or text[j] not in '{(':
            i = at + 1
            continue
        opener = text[j]
        closer = '}' if opener == '{' else ')'
        depth, k = 1, j + 1
        while k < n and depth > 0:
            if text[k] == opener:
                depth += 1
            elif text[k] == closer:
                depth -= 1
            k += 1
        if depth != 0:
            break
        body = text[j + 1:k - 1]
        i = k
        if entry_type in ('string', 'comment', 'preamble'):
            continue
        comma = _find_top_comma(body)
        if comma == -1:
            cite_key, fields_text = body.strip(), ''
        else:
            cite_key, fields_text = body[:comma].strip(), body[comma + 1:]
        fields = _parse_bib_fields(fields_text)
        entries.append({
            'type': '@' + entry_type,
            'cite_key': cite_key,
            'fields': fields,
        })
    return entries


def _find_top_comma(s: str) -> int:
    """找第一层级的逗号位置 (不被花括号/引号包裹)."""
    depth, in_quote = 0, False
    for i, ch in enumerate(s):
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == ',' and depth == 0:
                return i
    return -1


def _parse_bib_fields(text: str) -> dict:
    """解析 field = {value} / "value" / bare 对."""
    fields = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in ' \t\n\r,':
            i += 1
        if i >= n:
            break
        start = i
        while i < n and (text[i].isalnum() or text[i] in '_-'):
            i += 1
        if i == start:
            i += 1
            continue
        key = text[start:i].lower()
        while i < n and text[i] in ' \t\n\r':
            i += 1
        if i >= n or text[i] != '=':
            continue
        i += 1
        while i < n and text[i] in ' \t\n\r':
            i += 1
        if i >= n:
            break
        if text[i] == '{':
            depth, i = 1, i + 1
            start = i
            while i < n and depth > 0:
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                if depth > 0:
                    i += 1
            val = text[start:i]
            i += 1
        elif text[i] == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                i += 1
            val = text[start:i]
            i += 1
        else:
            start = i
            while i < n and text[i] not in ',\n':
                i += 1
            val = text[start:i].strip()
        val = val.strip()
        if val:
            fields[key] = val
    return fields


def normalize_title(s: str) -> str:
    """标题归一化: 繁→简, 仅保留 CJK+字母+数字, 小写."""
    if not s:
        return ''
    s = _t2s.convert(s)
    return re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbfa-z0-9]', '', s.lower())


def match_docs_to_entries(docs: list[dict], entries: list[dict]) -> dict:
    """将文献匹配到 .bib 条目.

    docs: [{id, title, filename}, ...]
    entries: [{type, cite_key, fields}, ...]

    返回 {entries, matched, ambiguous, unmatched_docs}
    """
    for e in entries:
        e['_norm'] = normalize_title(e.get('fields', {}).get('title', ''))

    used = set()
    matched = []
    ambiguous = []
    unmatched_docs = []

    for doc in docs:
        dt = normalize_title(doc.get('title') or '')
        fn = doc.get('filename') or ''
        df = normalize_title(Path(fn).stem if fn else '')

        exact_hits, sub_hits = [], []
        for idx, e in enumerate(entries):
            et = e['_norm']
            if not et:
                continue
            if dt and dt == et:
                exact_hits.append((idx, 'exact', 'title'))
            elif df and df == et:
                exact_hits.append((idx, 'exact', 'filename'))
            elif dt and len(dt) >= 2 and (dt in et or et in dt):
                sub_hits.append((idx, 'substring', 'title'))
            elif df and len(df) >= 2 and (df in et or et in df):
                sub_hits.append((idx, 'substring', 'filename'))

        doc_title = doc.get('title') or doc.get('filename') or '(无标题)'
        all_hits = exact_hits + sub_hits
        if len(all_hits) == 0:
            unmatched_docs.append({
                'doc_id': doc['id'],
                'doc_title': doc_title,
                'filename': doc.get('filename', ''),
            })
        elif len(all_hits) == 1:
            h = all_hits[0]
            used.add(h[0])
            matched.append({
                'doc_id': doc['id'],
                'doc_title': doc_title,
                'entry_idx': h[0],
                'match_type': h[1],
                'match_source': h[2],
            })
        else:
            ambiguous.append({
                'doc_id': doc['id'],
                'doc_title': doc_title,
                'candidates': all_hits,
            })

    for e in entries:
        e.pop('_norm', None)

    return {
        'entries': entries,
        'matched': matched,
        'ambiguous': ambiguous,
        'unmatched_docs': unmatched_docs,
    }
