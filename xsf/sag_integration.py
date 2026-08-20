"""SAG (SQL-Retrieval Augmented Generation) 集成层.

xsf 入库后把文档同步到 SAG (source 命名 xsf-{collection}), 搜索时走 SAG 语义检索
(vector/multi), SAG 不可用自动降级 FTS5. 所有同步失败静默降级, 不阻塞主流程.

真实 API (Zleap-AI/SAG, compose 部署):
    POST /api/v1/auth/login        {name, password} → {access_token, ...}
    POST /api/v1/auth/register     {name, email, password} → {access_token, ...}
                                   (注册关闭时仅放行首用户, 即首次自动引导)
    GET  /api/v1/system/ready      免认证健康探测
    GET/POST /api/v1/sources       source 列表 / 创建
    POST /api/v1/sources/{id}/documents/ingest   {title, text} → DocumentOut
                                   (统一写入接口, 自动分块 + event/entity 抽取)
    GET  /api/v1/sources/{id}/documents          文档列表 (含抽取进度 status)
    DELETE /api/v1/sources/{id}/documents/{doc}  删文档
    POST /api/v1/sources/{id}/search  {query, strategy: vector|multi, top_k}
                                   → {sections[], events[], entities[], summary, ...}

环境变量:
    XSF_SAG_URL        SAG API 基址 (如 http://localhost:8000). 未设 = 禁用同步
    XSF_SAG_USER       登录用户名 (默认 xsf; 实例无用户时自动注册首用户)
    XSF_SAG_PASSWORD   登录密码 (默认 xsf12345; 注册要求 >= 8 字符)
    XSF_SAG_TIMEOUT    常规请求超时秒数 (默认 15)

doc_id 回溯约定: ingest 的 title 固定为 `doc{doc_id} · {title}`, 正文首行
`# doc{doc_id} · {title}` + frontmatter `doc_id: N`. 搜索结果从 heading/
content 正则解析 doc_id 回查 xsf DB.
"""

import json
import os
import re
import threading

import requests

from .db import get_conn


class SagUnavailable(Exception):
    """SAG 未启用 / 不可达 / 认证失败 / 响应异常."""


def sag_base_url() -> str | None:
    url = (os.environ.get('XSF_SAG_URL') or '').strip().rstrip('/')
    return url or None


def _timeout(default: int = 15) -> int:
    try:
        return int(os.environ.get('XSF_SAG_TIMEOUT', default))
    except ValueError:
        return default


def _sag_user() -> str:
    return (os.environ.get('XSF_SAG_USER') or 'xsf').strip() or 'xsf'


def _sag_password() -> str:
    return os.environ.get('XSF_SAG_PASSWORD') or 'xsf12345'


# ── 认证 (Bearer JWT, 模块级 token 缓存 + 401 重登) ──────

_token_lock = threading.Lock()
_token_cache: str | None = None


def _login() -> str:
    """登录换 JWT; 登录失败 (实例无用户) 时尝试注册首用户."""
    url = sag_base_url()
    if not url:
        raise SagUnavailable('XSF_SAG_URL 未设置')
    user, pwd = _sag_user(), _sag_password()
    try:
        r = requests.post(
            f'{url}/api/v1/auth/login',
            json={'name': user, 'password': pwd},
            timeout=_timeout(),
        )
        if r.status_code == 200:
            tok = (r.json() or {}).get('access_token')
            if tok:
                return tok
        # 登录失败 → 注册 (SAG 无用户时放行首用户注册, 已有用户则 403)
        reg = requests.post(
            f'{url}/api/v1/auth/register',
            json={'name': user, 'email': f'{user}@xsf.local', 'password': pwd},
            timeout=_timeout(),
        )
        if reg.status_code in (200, 201):
            tok = (reg.json() or {}).get('access_token')
            if tok:
                return tok
        detail = ''
        for resp in (r, reg):
            try:
                detail = (resp.json() or {}).get('detail') or detail
            except (ValueError, AttributeError):
                pass
        raise SagUnavailable(
            f'SAG 登录/注册失败 (login {r.status_code}, register {reg.status_code})'
            f'{": " + str(detail) if detail else ""}'
        )
    except SagUnavailable:
        raise
    except requests.RequestException as e:
        raise SagUnavailable(f'SAG 认证请求失败: {e}') from e


def _auth_headers() -> dict:
    global _token_cache
    with _token_lock:
        if _token_cache is None:
            _token_cache = _login()
        return {'Authorization': f'Bearer {_token_cache}'}


def _invalidate_token() -> None:
    global _token_cache
    with _token_lock:
        _token_cache = None


def _request(method: str, path: str, *, timeout: int | None = None,
             **kw) -> requests.Response:
    """带认证请求; 401 时重登一次重试 (token 过期自愈)."""
    url = sag_base_url()
    if not url:
        raise SagUnavailable('XSF_SAG_URL 未设置')
    t = timeout or _timeout()
    r = requests.request(method, f'{url}{path}', headers=_auth_headers(),
                         timeout=t, **kw)
    if r.status_code == 401:
        _invalidate_token()
        r = requests.request(method, f'{url}{path}', headers=_auth_headers(),
                             timeout=t, **kw)
    return r


def health() -> bool:
    """SAG 可达性探测 (免认证 /api/v1/system/ready, 2s 超时)."""
    url = sag_base_url()
    if not url:
        return False
    try:
        r = requests.get(f'{url}/api/v1/system/ready', timeout=2)
        if r.status_code != 200:
            return False
        try:
            return (r.json() or {}).get('status', 'ready') == 'ready'
        except ValueError:
            return True
    except requests.RequestException:
        return False


# ── source 管理 ────────────────────────────────────────

def _source_name(collection: str) -> str:
    """collection → SAG source 名: xsf-{collection}."""
    return f'xsf-{collection}'


def get_source_id(collection: str, create: bool = True) -> str | None:
    """找 (或建) collection 对应的 SAG source, 返回 source id."""
    name = _source_name(collection)
    r = _request('GET', '/api/v1/sources')
    r.raise_for_status()
    data = r.json()
    sources = data if isinstance(data, list) else (data.get('sources') or [])
    for s in sources:
        if s.get('name') == name:
            return s.get('id')
    if not create:
        return None
    r = _request(
        'POST', '/api/v1/sources',
        json={'name': name, 'description': f'xsf 书架「{collection}」全文'},
    )
    r.raise_for_status()
    return (r.json() or {}).get('id')


# ── 文档导出 (doc_id 回溯约定见模块 docstring) ──────────

def _ingest_title(doc_id: int, title: str) -> str:
    return f'doc{doc_id} · {title or f"文档{doc_id}"}'


def _doc_export(doc_id: int, collection: str) -> dict | None:
    """从 SQLite 导出文档为 markdown 文本. 文档不存在返回 None."""
    conn = get_conn(collection)
    try:
        doc = conn.execute(
            'SELECT id, cite_key, title, author FROM documents WHERE id = ?',
            (doc_id,),
        ).fetchone()
        if doc is None:
            return None
        rows = conn.execute(
            '''SELECT page_num, block_num, text FROM lines
               WHERE doc_id = ?
               ORDER BY page_num, block_num, line_num''',
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()

    blocks = []
    for r in rows:
        if not r['text']:
            continue
        key = (r['page_num'], r['block_num'])
        if not blocks or blocks[-1]['key'] != key:
            blocks.append({'key': key, 'lines': []})
        blocks[-1]['lines'].append(r['text'])
    body = '\n\n'.join('\n'.join(b['lines']) for b in blocks)

    title = doc['title'] or doc['cite_key'] or f'文档{doc_id}'
    ingest_title = _ingest_title(doc_id, title)
    fm = {
        'doc_id': doc_id,
        'collection': collection,
        'cite_key': doc['cite_key'] or '',
        'title': doc['title'] or '',
    }
    if doc['author']:
        fm['author'] = doc['author']
    text = (
        '---\n'
        + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}'
                    for k, v in fm.items())
        + '\n---\n\n'
        + f'# {ingest_title}\n\n'
        + body
        + '\n'
    )
    return {'doc_id': doc_id, 'ingest_title': ingest_title, 'text': text}


# ── 同步 / 删除 ────────────────────────────────────────

# doc_id 前缀: doc3 · xxx / doc3-xxx / doc3:xxx / doc_id: 3
_HEAD_RE = re.compile(r'^doc(\d+)(?:[-\s·:]|$)')
_FM_RE = re.compile(r'^doc_id:\s*"?(\d+)"?', re.M)


def _doc_id_from_str(val) -> int | None:
    m = _HEAD_RE.match(str(val or ''))
    return int(m.group(1)) if m else None


def _list_remote_docs(source_id: str) -> list[dict]:
    r = _request('GET', f'/api/v1/sources/{source_id}/documents')
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get('documents') or [])


def _remote_doc_id(d: dict) -> int | None:
    """从 SAG 文档条目解析 xsf doc_id (filename/title 前缀)."""
    return (_doc_id_from_str(d.get('filename'))
            or _doc_id_from_str(d.get('title'))
            or _doc_id_from_str(d.get('name')))


def _delete_remote(source_id: str, document_id) -> None:
    _request('DELETE', f'/api/v1/sources/{source_id}/documents/{document_id}') \
        .raise_for_status()


def _try_delete_doc(source_id: str, doc_id: int) -> None:
    """删 SAG 上的旧版文档 (幂等重同步用, best effort)."""
    try:
        for d in _list_remote_docs(source_id):
            if _remote_doc_id(d) == doc_id:
                did = d.get('id') or d.get('document_id')
                if did:
                    _delete_remote(source_id, did)
    except (requests.RequestException, SagUnavailable, ValueError):
        pass


def sync_doc(doc_id: int, collection: str) -> dict:
    """上传文档到 SAG (JSON ingest, 自动分块+抽取). 失败抛 SagUnavailable."""
    if not sag_base_url():
        return {'synced': False, 'reason': 'disabled'}
    try:
        export = _doc_export(doc_id, collection)
        if export is None:
            return {'synced': False, 'reason': 'doc_not_found'}
        source_id = get_source_id(collection)
        if not source_id:
            raise SagUnavailable('create source failed')
        # 幂等: 先删旧版再写入
        _try_delete_doc(source_id, doc_id)
        r = _request(
            'POST', f'/api/v1/sources/{source_id}/documents/ingest',
            json={'title': export['ingest_title'], 'text': export['text']},
            timeout=_timeout(300),
        )
        r.raise_for_status()
        return {'synced': True, 'source_id': source_id,
                'title': export['ingest_title']}
    except SagUnavailable:
        raise
    except requests.RequestException as e:
        raise SagUnavailable(str(e)) from e
    except Exception as e:
        raise SagUnavailable(str(e)) from e


def remove_doc(doc_id: int, collection: str) -> None:
    """删除文献时同步删 SAG (best effort, 失败静默)."""
    if not sag_base_url():
        return
    try:
        source_id = get_source_id(collection, create=False)
        if not source_id:
            return
        for d in _list_remote_docs(source_id):
            if _remote_doc_id(d) == doc_id:
                did = d.get('id') or d.get('document_id')
                if did:
                    _delete_remote(source_id, did)
    except (requests.RequestException, SagUnavailable, ValueError):
        pass


# ── 搜索 ───────────────────────────────────────────────

def search(query: str, collection: str, mode: str = 'vector',
           top_k: int = 10) -> list[dict]:
    """SAG source 内搜索. 返回标准化结果列表.

    mode: 'vector' (语义) | 'multi' (实体关系 + LLM rerank).
    解析 SearchResponse.sections[]; doc_id 从 heading 前缀 / 正文 frontmatter
    解析, title/cite_key 回查 xsf DB (保证与 FTS5 结果一致).
    每条: {doc_id, score, text, title, cite_key, filename, heading}
    """
    source_id = get_source_id(collection, create=False)
    if not source_id:
        raise SagUnavailable(f'SAG source 不存在: xsf-{collection}')
    strategy = 'multi' if mode == 'multi' else 'vector'
    try:
        r = _request(
            'POST', f'/api/v1/sources/{source_id}/search',
            json={'query': query, 'strategy': strategy, 'top_k': top_k},
            timeout=_timeout(120),
        )
        r.raise_for_status()
    except SagUnavailable:
        raise
    except requests.RequestException as e:
        raise SagUnavailable(str(e)) from e

    data = r.json() or {}
    sections = data.get('sections') or []

    out = []
    doc_ids = set()
    for s in sections:
        if not isinstance(s, dict):
            continue
        content = s.get('content') or ''
        heading = s.get('heading') or ''
        m = _HEAD_RE.match(heading) or _FM_RE.search(content)
        if not m:
            continue  # 非 xsf 同步的文档, 跳过
        doc_id = int(m.group(1))
        doc_ids.add(doc_id)
        out.append({
            'doc_id': doc_id,
            'score': s.get('score'),
            'rank': s.get('rank'),
            'text': content,
            'heading': heading,
        })

    if doc_ids:
        conn = get_conn(collection)
        try:
            placeholders = ','.join('?' * len(doc_ids))
            rows = conn.execute(
                f'''SELECT id, title, cite_key, filename FROM documents
                    WHERE id IN ({placeholders})''',
                sorted(doc_ids),
            ).fetchall()
        finally:
            conn.close()
        meta = {r['id']: r for r in rows}
        for o in out:
            m = meta.get(o['doc_id'])
            o['title'] = m['title'] if m else None
            o['cite_key'] = m['cite_key'] if m else None
            o['filename'] = m['filename'] if m else None
            if not o['title']:
                o['title'] = o['heading'] or f"文档{o['doc_id']}"
    return out
