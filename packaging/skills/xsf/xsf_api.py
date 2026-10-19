#!/usr/bin/env python3
import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URLS = [
    "http://127.0.0.1:8090",     # ① 本机服务, 优先
    "http://192.168.1.113:8090", # ② 内网 GPU 机, 顺延
]
PROBE_TIMEOUT = 2
_active_url = None


def _candidates():
    override = os.environ.get("XSF_URL")
    if override:
        return [override.rstrip("/")]
    return list(DEFAULT_URLS)


def _url_alive(url, timeout=PROBE_TIMEOUT):
    """TCP 可达性探测, 避免对死服务长时间挂起"""
    try:
        parsed = urllib.parse.urlparse(url)
        sock = socket.create_connection(
            (parsed.hostname, parsed.port or 80), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def enc(name):
    return urllib.parse.quote(str(name), safe="")


def strip_marks(obj):
    if isinstance(obj, str):
        return re.sub(r"</?mark\s*/?>", "", obj)
    if isinstance(obj, list):
        return [strip_marks(x) for x in obj]
    if isinstance(obj, dict):
        return {k: strip_marks(v) for k, v in obj.items()}
    return obj


def get(path, params=None):
    global _active_url
    attempted = []
    tried = set()
    while True:
        if _active_url and _active_url not in tried:
            base = _active_url
        else:
            base = next((u for u in _candidates() if u not in tried), None)
        if base is None:
            break
        tried.add(base)
        if not _url_alive(base):
            attempted.append(f"{base}  连接失败")
            if _active_url == base:
                _active_url = None
            continue
        url = base + path
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != ""}
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                _active_url = base
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            sys.stderr.write(f"HTTP {e.code} (服务 {base}): {body}\n")
            sys.exit(1)
        except urllib.error.URLError as e:
            attempted.append(f"{base}  {e.reason}")
            _active_url = None
            continue
    sys.stderr.write(
        "全部 xsf 服务不可达. 服务链 (按顺延顺序):\n  "
        + "\n  ".join(_candidates())
        + "\n尝试记录:\n  " + "\n  ".join(attempted)
        + "\n可用 XSF_URL 环境变量整体覆盖地址链.\n")
    sys.exit(1)


def out(data, pretty):
    print(json.dumps(data, ensure_ascii=False, indent=2 if pretty else None))


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    common.add_argument("--mark", action="store_true",
                        help="保留 <mark> 高亮标签 (默认剥除)")
    p = argparse.ArgumentParser(
        prog="xsf_api.py",
        description="小書房 (xsf) 只读 API 客户端",
        parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("collections", parents=[common],
                   help="列出所有书架")

    s = sub.add_parser("search", parents=[common], help="FTS5 关键词检索")
    s.add_argument("q")
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--source-type", help="按来源标签过滤")

    s = sub.add_parser("context", parents=[common], help="命中块的上下文行")
    s.add_argument("doc_id", type=int)
    s.add_argument("page", type=int)
    s.add_argument("block", type=int)
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("-r", "--radius", type=int, default=1)

    s = sub.add_parser("content", parents=[common], help="文献全文 (页/块/行结构)")
    s.add_argument("doc_id", type=int)
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("--page", type=int, help="只返回该页 (省略=全部页)")
    s.add_argument("--limit", type=int, default=0, help="最大行数, 0=不限")
    s.add_argument("-q", "--highlight", help="高亮词 (输出加 <mark>, 需配合 --mark 保留)")

    s = sub.add_parser("hits", parents=[common], help="关键词在文内的全部命中位置")
    s.add_argument("doc_id", type=int)
    s.add_argument("keyword")
    s.add_argument("-c", "--collection", required=True, help="书架名")

    s = sub.add_parser("docs", parents=[common], help="最近入库文献列表")
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("-n", "--limit", type=int, default=50)

    s = sub.add_parser("query", parents=[common], help="文献元数据筛选/分页/排序")
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("--tag", help="source_tags 精确匹配")
    s.add_argument("--author", help="作者模糊匹配")
    s.add_argument("--title", help="标题模糊匹配")
    s.add_argument("--year", help="出版年 (bib_data.date 前 4 位)")
    s.add_argument("--bib-type", help="文献类型精确匹配")
    s.add_argument("--sort", default="created_at",
                   choices=["created_at", "title", "author",
                            "cite_key", "page_count"])
    s.add_argument("--order", choices=["asc", "desc"], default="desc")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("-n", "--limit", type=int, default=20)

    s = sub.add_parser("stats", parents=[common], help="统计 (带书架名=该书架, 省略=全库)")
    s.add_argument("collection", nargs="?")

    s = sub.add_parser("entities", parents=[common],
                       help="实体图谱检索 (书架级归一实体列表)")
    s.add_argument("q", nargs="?", help="检索词 (canonical/别名匹配; 省略=全部)")
    s.add_argument("-c", "--collection", required=True, help="书架名")
    s.add_argument("--category", help="类别过滤 (如 人名/地名/物产)")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("--doc", type=int, help="只列该文献提及的实体 (doc_id)")

    s = sub.add_parser("entity", parents=[common],
                       help="实体详情 (提及锚点+补录+共现邻居)")
    s.add_argument("entity_id", type=int)
    s.add_argument("-c", "--collection", required=True, help="书架名")

    return p


def main():
    a = build_parser().parse_args()
    if a.cmd == "collections":
        data = get("/api/collections")
    elif a.cmd == "search":
        data = get(f"/collections/{enc(a.collection)}/search",
                   {"q": a.q, "limit": a.limit, "source_type": a.source_type})
    elif a.cmd == "context":
        data = get(f"/collections/{enc(a.collection)}"
                   f"/context/{a.doc_id}/{a.page}/{a.block}",
                   {"radius": a.radius})
    elif a.cmd == "content":
        data = get(f"/collections/{enc(a.collection)}/doc/{a.doc_id}/content",
                   {"page": a.page, "limit": a.limit, "q": a.highlight})
    elif a.cmd == "hits":
        data = get(f"/collections/{enc(a.collection)}/doc/{a.doc_id}/hits",
                   {"keyword": a.keyword})
    elif a.cmd == "docs":
        data = get(f"/collections/{enc(a.collection)}/docs",
                   {"limit": a.limit})
    elif a.cmd == "query":
        data = get(f"/collections/{enc(a.collection)}/docs/query",
                   {"page": a.page, "limit": a.limit, "sort": a.sort,
                    "order": a.order, "tag": a.tag, "author": a.author,
                    "bib_type": a.bib_type, "title": a.title, "year": a.year})
    elif a.cmd == "stats":
        if a.collection:
            data = get(f"/collections/{enc(a.collection)}/stats")
        else:
            data = get("/api/stats")
    elif a.cmd == "entities":
        data = get(f"/collections/{enc(a.collection)}/entities",
                   {"q": a.q, "category": a.category,
                    "limit": a.limit, "doc_id": a.doc})
    elif a.cmd == "entity":
        data = get(f"/collections/{enc(a.collection)}/entities/{a.entity_id}")
    else:
        build_parser().error(f"未知命令: {a.cmd}")
    if not a.mark:
        data = strip_marks(data)
    out(data, a.pretty)


if __name__ == "__main__":
    main()
