"""xsf MCP 路由适配器 — AI agent 的统一搜索入口.

MCP server, 背后走 xsf FTS5 关键词检索 (毫秒级, 零 LLM 成本).

运行 (stdio transport, 供 opencode/Claude Code 等接入):
    xsf-mcp

环境变量:
    XSF_BASE_URL     xsf Web 基址 (默认 http://localhost:8090)
"""

import os

import requests

from mcp.server import MCPServer

mcp = MCPServer("xsf-search")

_XSF_BASE = (os.environ.get('XSF_BASE_URL')
             or 'http://localhost:8090').rstrip('/')


def _xsf_get(path: str, **kw) -> requests.Response:
    return requests.get(f'{_XSF_BASE}{path}',
                        timeout=kw.pop('timeout', 30), **kw)


def _fts5_search(query: str, collection: str, limit: int) -> dict:
    r = _xsf_get(
        f'/collections/{collection}/search',
        params={'q': query, 'limit': limit},
    )
    r.raise_for_status()
    return r.json()


# ── MCP tools ──────────────────────────────────────────

@mcp.tool()
def search(query: str, collection: str = "", limit: int = 10) -> str:
    """FTS5 关键词搜索小书房文献库.

    Args:
        query: 搜索词 (如 "噶玛兰")
        collection: 书架名. 空串 = 遍历全部书架合并 (A4, 2026-11-07):
            各书架按 rank 序取候选后轮询交织, 每条结果带 collection 字段 —
            后续 get_context/get_doc 必须用它回指书架
        limit: 返回条数 (默认 10)

    Returns:
        JSON: {engine, collection, count, results: [{doc_id, title, cite_key,
        page_num, text, collection}], totals_per_shelf}
        单书架时行为不变; 实体/wiki 命中组不进本工具 (保持书架域, 见检索设计 3.0)
    """
    import json as _json

    if not query.strip():
        return _json.dumps({'error': 'query 为空'}, ensure_ascii=False)

    try:
        if collection:
            data = _fts5_search(query, collection, limit)
            data['engine'] = 'fts5'
            for r in data.get('results') or []:
                r.setdefault('collection', collection)
            return _json.dumps(data, ensure_ascii=False)

        # ── 全书架: 遍历 merge, 书架轮询交织 (防单书架排序偏置) ──
        r = _xsf_get('/api/collections')
        r.raise_for_status()
        colls = r.json().get('collections') or []
        if not colls:
            return _json.dumps({'error': '没有书架'}, ensure_ascii=False)
        per, totals = [], {}
        for c in colls:
            try:
                d = _fts5_search(query, c, limit)
                res = d.get('results') or []
                for x in res:
                    x['collection'] = c
                per.append(res)
                totals[c] = d.get('total', 0)
            except requests.RequestException:
                totals[c] = None   # 单书架失败不拖垮全书架
        merged, i = [], 0
        while len(merged) < limit:
            added = False
            for res in per:
                if i < len(res):
                    merged.append(res[i])
                    added = True
                    if len(merged) >= limit:
                        break
            if not added:
                break
            i += 1
        return _json.dumps({
            'engine': 'fts5', 'collection': '__all__',
            'collections': colls, 'count': len(merged),
            'results': merged, 'totals_per_shelf': totals,
            'total_all': sum(v for v in totals.values() if v),
        }, ensure_ascii=False)
    except requests.RequestException as e:
        return _json.dumps({'error': f'xsf 不可达: {e}'}, ensure_ascii=False)


@mcp.tool()
def list_collections() -> str:
    """列出小书房全部书架 (含文献统计)."""
    import json as _json
    r = _xsf_get('/api/stats')
    r.raise_for_status()
    data = r.json()
    colls = data.get('collections') or []
    return _json.dumps({
        'total_documents': data.get('documents'),
        'collections': [c['name'] for c in colls],
        'detail': colls,
    }, ensure_ascii=False)


@mcp.tool()
def get_context(collection: str, doc_id: int, page: int, block: int,
                radius: int = 1) -> str:
    """取命中块的上下文 (前后 radius 个块的全部行, 含 bbox/block_label).

    搜索结果跳转原文核对用.
    """
    import json as _json
    r = _xsf_get(
        f'/collections/{collection}/context/{doc_id}/{page}/{block}',
        params={'radius': radius},
    )
    r.raise_for_status()
    return _json.dumps(r.json(), ensure_ascii=False)


@mcp.tool()
def get_doc(collection: str, doc_id: int) -> str:
    """取文献全文内容 (页 → 块 → 行结构)."""
    import json as _json
    r = _xsf_get(f'/collections/{collection}/doc/{doc_id}/content')
    r.raise_for_status()
    return _json.dumps(r.json(), ensure_ascii=False)


@mcp.tool()
def status() -> str:
    """xsf Web (FTS5) 健康状态."""
    import json as _json
    out = {'xsf': {'base_url': _XSF_BASE}}
    try:
        r = _xsf_get('/api/stats', timeout=5)
        out['xsf']['healthy'] = r.status_code == 200
    except requests.RequestException:
        out['xsf']['healthy'] = False
    return _json.dumps(out, ensure_ascii=False)


def main():
    from .env import load_env
    load_env()
    mcp.run()


if __name__ == '__main__':
    main()
