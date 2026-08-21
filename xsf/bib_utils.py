"""BibTeX 元数据辅助函数。

cite_key 自动生成 + documents 冗余列同步 + biblatex 字段预设。

cite_key 生成 (2026-08-21 起):
    姓拼音+年+标题前3字拼音 [- v卷次] → 冲突加 b/c/d → 指纹兜底.
    套书 (书名/作者/年相同) 靠卷次 token 消歧: volume/number 字段优先,
    否则从 title 挖卷次模式 (第X卷/卷X/X册/上中下/(二)等), 纯数字 v2 式.
"""
import hashlib
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
    "@inbook": ["author", "title", "chapter", "pages", "publisher", "year", "address"],
    "@inproceedings": ["author", "title", "booktitle", "editor", "publisher", "year", "address", "pages"],
    "@proceedings": ["editor", "title", "publisher", "year", "address", "volume"],
    "@phdthesis": ["author", "title", "school", "year", "address"],
    "@mastersthesis": ["author", "title", "school", "year", "address"],
    "@techreport": ["author", "title", "institution", "year", "number", "address"],
    "@manual": ["author", "title", "organization", "address", "edition", "year"],
    "@booklet": ["author", "title", "howpublished", "address", "month", "year"],
    "@unpublished": ["author", "title", "note", "month", "year"],
    "@manuscript": ["author", "title", "year", "institution"],
    "@misc": ["author", "title", "howpublished", "month", "year", "note"],
    "@online": ["author", "title", "year", "url"],
}

BIB_TYPE_LABELS = {
    "@book": "图书",
    "@article": "期刊/报纸",
    "@incollection": "析出文献",
    "@inbook": "书中章节",
    "@inproceedings": "会议论文",
    "@proceedings": "会议录",
    "@phdthesis": "博士论文",
    "@mastersthesis": "硕士论文",
    "@techreport": "科技报告",
    "@manual": "技术手册",
    "@booklet": "小册子",
    "@unpublished": "未刊文献",
    "@manuscript": "手稿/档案",
    "@online": "电子出版物",
    "@misc": "其他",
}

BIB_TYPE_ALIASES = {
    "@conference": "@inproceedings",
    "@electronic": "@online",
    "@www": "@online",
}


def normalize_bib_type(bib_type: str, fields: dict | None = None) -> str:
    """归一化 BibTeX 类型: 别名表 + @thesis 嗅探.

    @thesis 按 type 字段判断: 含 master/mathesis → @mastersthesis;
    含 phd/dissertation 或缺省 → @phdthesis. 其余别名走
    BIB_TYPE_ALIASES; 未知类型原样返回.
    """
    t = (bib_type or "").strip().lower()
    if not t:
        return t
    if not t.startswith("@"):
        t = "@" + t
    if t == "@thesis":
        tv = str((fields or {}).get("type", "") or "").lower()
        if "master" in tv or "mathesis" in tv:
            return "@mastersthesis"
        return "@phdthesis"
    return BIB_TYPE_ALIASES.get(t, t)

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
    "chapter": "章节",
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


_VOL_MARKS = "卷冊册集辑輯篇函帙"
_NUM_RE = r"[0-9零一二三四五六七八九十百]+"
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}
_SHANG_ZHONG_XIA = {"上": 1, "中": 2, "下": 3}


def _cn_to_int(s: str):
    """中文/阿拉伯数字串 → int, 解析失败返回 None. 支持到百位."""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (num or 1) * unit
            num = 0
        else:
            return None
    total += num
    return total or None


def _volume_token(bib_data: dict, title: str) -> str:
    """提取卷次判别词 → 'v2' 式纯数字 token, 无则 ''。

    优先级: volume/number 字段 > title 卷次模式。
    title 模式按顺序: 第X卷/X册 → 卷X → (上) → 卷上/上册 → (二)。
    数值上限 99 (过滤年份等误配, 如 '(1924)')。
    """
    for field in ("volume", "number"):
        raw = str(bib_data.get(field) or "").strip()
        if not raw:
            continue
        m = re.search(_NUM_RE, raw)
        n = _cn_to_int(m.group(0)) if m else None
        if n and 0 < n <= 99:
            return f"v{n}"

    if title:
        patterns = [
            rf"第?\s*({_NUM_RE})\s*[{_VOL_MARKS}]",
            rf"[{_VOL_MARKS}]\s*({_NUM_RE})",
            r"[（(]\s*([上中下])\s*[)）]",
            rf"(?:[{_VOL_MARKS}]\s*([上中下])|([上中下])\s*[{_VOL_MARKS}])",
            rf"[（(]\s*({_NUM_RE})\s*[)）]",
        ]
        for p in patterns:
            m = re.search(p, title)
            if not m:
                continue
            g = next((x for x in m.groups() if x), "")
            n = _SHANG_ZHONG_XIA.get(g) or _cn_to_int(g)
            if n and 0 < n <= 99:
                return f"v{n}"
    return ""


def generate_cite_key(bib_data: dict, existing_keys: set[str],
                      exclude_key: str = None, fingerprint: str = None) -> str:
    """从 bib_data 生成 cite_key: 姓+年+标题首词[-v卷次]。冲突加 b/c/d, 指纹兜底。

    existing_keys: 该 collection 已有的 cite_key 集合。
    exclude_key: 更新时排除自身的 cite_key（避免自己和自己冲突）。
    fingerprint: 兜底判别源 (通常传 filename), b-z 后缀耗尽时取其 sha1 前 4 位。
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
    vol = _volume_token(bib_data, title)
    if vol:
        base = f"{base}-{vol}"

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

    if fingerprint:
        fp = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:4]
        cand = f"{base}-{fp}"
        if cand not in existing_keys:
            return cand
        if exclude_key and cand == exclude_key:
            return cand

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
            'type': normalize_bib_type('@' + entry_type, fields),
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
