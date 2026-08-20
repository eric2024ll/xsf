"""xsf MCP 路由适配器 — AI agent 的统一搜索入口.

一个 MCP server, 背后按查询类型自动路由:
    简单关键词 → xsf FTS5 (毫秒级, 零 LLM 成本)
    语义/多跳   → SAG vector / multi (事件-实体 + LLM rerank)
    FTS5 无结果 → 自动补 SAG 语义召回

运行 (stdio transport, 供 opencode/Claude Code 等接入):
    xsf-mcp

环境变量:
    XSF_BASE_URL     xsf Web 基址 (默认 http://localhost:8090)
    XSF_AUTH_TOKEN   xsf 登录密码 (未设则无认证)
    XSF_SAG_URL      SAG API 基址 (默认 http://localhost:8000; 未设 = 仅 FTS5)
"""

import os
import re

import requests

from mcp.server import MCPServer

mcp = MCPServer("xsf-search")

_XSF_BASE = (os.environ.get('XSF_BASE_URL')
             or 'http://localhost:8090').rstrip('/')
_SAG_BASE = (os.environ.get('XSF_SAG_URL')
             or 'http://localhost:8000').rstrip('/')
_AUTH_TOKEN = os.environ.get('XSF_AUTH_TOKEN') or None

# 问题式/语义式查询的特征 — 命中即直走 SAG
_SEMANTIC_MARKERS = re.compile(
    r'[?？]|什么|为何|为什么|如何|怎么|怎样|哪些|谁|何时|关系|比较|区别|'
    r'分析|解释|告诉我|影响|原因|背景'
)


def _xsf_get(path: str, **kw) -> requests.Response:
    cookies = {'xsf_auth': _AUTH_TOKEN} if _AUTH_TOKEN else None
    return requests.get(f'{_XSF_BASE}{path}', cookies=cookies,
                        timeout=kw.pop('timeout', 30), **kw)


def _is_semantic(query: str) -> bool:
    """路由启发: 含疑问词/关系词, 或长查询 (>12 字) 视为语义式."""
    return bool(_SEMANTIC_MARKERS.search(query)) or len(query.strip()) > 12


def _fts5_search(query: str, collection: str, limit: int) -> dict:
    r = _xsf_get(
        f'/collections/{collection}/search',
        params={'q': query, 'limit': limit},
    )
    r.raise_for_status()
    return r.json()


def _sag_search(query: str, collection: str, limit: int,
                mode: str = 'vector') -> dict:
    r = _xsf_get(
        f'/collections/{collection}/sag-search',
        params={'q': query, 'limit': limit, 'mode': mode},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()


# ── MCP tools ──────────────────────────────────────────

@mcp.tool()
def search(query: str, collection: str = "", mode: str = "auto",
           limit: int = 10) -> str:
    """混合路由搜索小书房文献库.

    Args:
        query: 搜索词. 关键词 (如 "噶玛兰") 或自然语言问题 (如 "噶玛兰与汉人通婚的原因")
        collection: 书架名. 空串 = 搜索全部书架
        mode: auto (默认, 自动路由) | fts5 (关键词) | sag (语义) | multi (实体关系+rerank)
        limit: 返回条数 (默认 10)

    Returns:
        JSON: {engine, collection, count, results: [{doc_id, title, cite_key,
        page_num, text, score}]}
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

    engine_used = None
    data = None

    if mode == 'fts5':
        data = _fts5_search(query, collection, limit)
        engine_used = data.get('engine', 'fts5')
    elif mode in ('sag', 'multi'):
        sag_mode = 'multi' if mode == 'multi' else 'vector'
        data = _sag_search(query, collection, limit, sag_mode)
        engine_used = data.get('engine', 'sag')
    else:  # auto 路由
        semantic = _is_semantic(query)
        if not semantic:
            try:
                data = _fts5_search(query, collection, limit)
                engine_used = data.get('engine', 'fts5')
            except requests.RequestException as e:
                return _json.dumps({'error': f'xsf 不可达: {e}'},
                                   ensure_ascii=False)
        if semantic or not data.get('results'):
            try:
                sag_data = _sag_search(query, collection, limit, 'vector')
                if engine_used == 'fts5' and not sag_data.get('results'):
                    pass  # SAG 也没结果, 保留 FTS5 空结果
                elif engine_used is None or engine_used == 'fts5':
                    # 语义优先 / FTS5 无结果时 SAG 有结果 → 用 SAG
                    if semantic or sag_data.get('results'):
                        data = sag_data
                        engine_used = sag_data.get('engine', 'sag')
            except requests.RequestException:
                if data is None:
                    data = {'query': query, 'collection': collection,
                            'count': 0, 'results': []}
                    engine_used = 'unavailable'

    data['engine'] = engine_used
    data['routed_from'] = mode
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
    """两个引擎的健康状态: xsf Web (FTS5) + SAG (语义)."""
    import json as _json
    out = {'xsf': {'base_url': _XSF_BASE}, 'sag': {'base_url': _SAG_BASE}}
    try:
        r = _xsf_get('/api/stats', timeout=5)
        out['xsf']['healthy'] = r.status_code == 200
    except requests.RequestException:
        out['xsf']['healthy'] = False
    try:
        r = requests.get(f'{_SAG_BASE}/health', timeout=3)
        out['sag']['healthy'] = r.status_code == 200
    except requests.RequestException:
        out['sag']['healthy'] = False
    return _json.dumps(out, ensure_ascii=False)


def main():
    mcp.run()


if __name__ == '__main__':
    main()
