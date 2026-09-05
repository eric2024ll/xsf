"""xsf MCP 路由适配器 — AI agent 的统一搜索入口.

MCP server, 背后走 xsf FTS5 关键词检索 (毫秒级, 零 LLM 成本).

运行 (stdio transport, 供 opencode/Claude Code 等接入):
    xsf-mcp

环境变量:
    XSF_BASE_URL     xsf Web 基址 (默认 http://localhost:8090)
    XSF_AUTH_TOKEN   xsf 登录密码 (未设则无认证)
"""

import os

import requests

from mcp.server import MCPServer

mcp = MCPServer("xsf-search")

_XSF_BASE = (os.environ.get('XSF_BASE_URL')
             or 'http://localhost:8090').rstrip('/')
_AUTH_TOKEN = os.environ.get('XSF_AUTH_TOKEN') or None


def _xsf_get(path: str, **kw) -> requests.Response:
    cookies = {'xsf_auth': _AUTH_TOKEN} if _AUTH_TOKEN else None
    return requests.get(f'{_XSF_BASE}{path}', cookies=cookies,
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
        collection: 书架名. 空串 = 搜索全部书架
        limit: 返回条数 (默认 10)

    Returns:
        JSON: {engine, collection, count, results: [{doc_id, title, cite_key,
        page_num, text}]}
    """
    import json as _json

    if not query.strip():
        return _json.dumps({'error': 'query 为空'}, ensure_ascii=False)

    if not collection:
        r = _xsf_get('/api/collections')
        r.raise_for_status()
        colls = r.json().get('collections') or []
        if not colls:
            return _json.dumps({'error': '没有书架'}, ensure_ascii=False)
        collection = colls[0]

    try:
        data = _fts5_search(query, collection, limit)
    except requests.RequestException as e:
        return _json.dumps({'error': f'xsf 不可达: {e}'}, ensure_ascii=False)

    data['engine'] = 'fts5'
    return _json.dumps(data, ensure_ascii=False)


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
