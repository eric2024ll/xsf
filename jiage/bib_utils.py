"""BibTeX 元数据辅助函数。

cite_key 自动生成 + documents 冗余列同步 + biblatex 字段预设。
"""
import json
import re

from pypinyin import lazy_pinyin, Style

BIB_TYPE_FIELDS = {
    "@book": ["author", "title", "date", "publisher", "location", "edition"],
    "@article": ["author", "title", "date", "journaltitle", "volume", "number", "pages"],
    "@manuscript": ["author", "title", "date", "repository"],
    "@online": ["author", "title", "date", "url", "urldate"],
    "@incollection": ["author", "title", "booktitle", "bookauthor", "date", "publisher", "pages"],
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
    "date": "日期",
    "publisher": "出版者",
    "location": "出版地",
    "edition": "版次",
    "journaltitle": "期刊名",
    "volume": "卷",
    "number": "期",
    "pages": "页码",
    "repository": "馆藏机构",
    "url": "URL",
    "urldate": "访问日期",
    "booktitle": "所在文献标题",
    "bookauthor": "所在文献作者",
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
    date = str(bib_data.get("date", "")).strip()
    year = ""
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
