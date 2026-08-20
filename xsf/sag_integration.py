"""SAG (SQL-Retrieval Augmented Generation) 集成层.

xsf 入库后把文档同步到 SAG (source 命名 xsf-{collection}), 搜索时走 SAG 语义检索
(vector/multi), SAG 不可用自动降级 FTS5. 所有同步失败静默降级, 不阻塞主流程.

环境变量:
    XSF_SAG_URL      SAG API 基址 (如 http://localhost:8000). 未设 = 禁用同步
    XSF_SAG_TIMEOUT  常规请求超时秒数 (默认 15)
"""

import json
import os
import re

import requests

from .db import get_conn


class SagUnavailable(Exception):
    """SAG 未启用 / 不可达 / 响应异常."""


def sag_base_url() -> str | None:
    url = (os.environ.get('XSF_SAG_URL') or '').strip().rstrip('/')
    return url or None


def _timeout(default: int = 15) -> int:
    try:
        return int(os.environ.get('XSF_SAG_TIMEOUT', default))
    except ValueError:
        return default


def health() -> bool:
    """SAG 可达性探测 (2s 超时, 任何异常都视为不可用)."""
    url = sag_base_url()
    if not url:
        return False
    try:
        r = requests.get(f'{url}/health', timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ── source 管理 ────────────────────────────────────────

def _source_name(collection: str) -> str:
    """collection → SAG source 名: xsf-{collection}."""
    return f'xsf-{collection}'


def _list_sources() -> list[dict]:
    url = sag_base_url()
    if not url:
        raise SagUnavailable('XSF_SAG_URL 未设置')
    r = requests.get(f'{url}/api/v1/sources', timeout=_timeout())
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get('sources') or []


def get_source_id(collection: str, create: bool = True) -> str | None:
    """找 (或建) collection 对应的 SAG source, 返回 source id."""
    name = _source_name(collection)
    for s in _list_sources():
        if s.get('name') == name:
            return s.get('id') or s.get('source_id')
    if not create:
        return None
    url = sag_base_url()
    r = requests.post(
        f'{url}/api/v1/sources',
        json={'name': name, 'description': f'xsf 书架「{collection}」全文'},
        timeout=_timeout(),
    )
    r.raise_for_status()
    data = r.json()
    src = data.get('source') if isinstance(data, dict) else None
    if src:
        return src.get('id') or src.get('source_id')
    return data.get('id') or data.get('source_id')


# ── 文档导出 ───────────────────────────────────────────

def _doc_to_markdown(doc_id: int, collection: str) -> tuple[str, str] | None:
    """从 SQLite 导出文档为 markdown (frontmatter 带 doc_id/cite_key 供回溯).

    返回 (filename, content); 文档不存在返回 None.
    文件名前缀 doc{doc_id}- 保证 SAG 结果能映射回 xsf.
    """
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

    fm = {
        'doc_id': doc_id,
        'collection': collection,
        'cite_key': doc['cite_key'] or '',
        'title': doc['title'] or '',
    }
    content = (
        '---\n'
        + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}'
                    for k, v in fm.items())
        + '\n---\n\n'
        + body
        + '\n'
    )
    stem = doc['cite_key'] or (doc['title'] or f'doc{doc_id}')[:40]
    stem = re.sub(r'[\\/:*?"<>|\s]+', '_', stem)
    filename = f'doc{doc_id}-{stem}.md'
    return filename, content


# ── 同步 / 删除 ────────────────────────────────────────

def sync_doc(doc_id: int, collection: str) -> dict:
    """上传文档到 SAG 并触发抽取. 任何失败抛 SagUnavailable (调用方捕获降级)."""
    url = sag_base_url()
    if not url:
        return {'synced': False, 'reason': 'disabled'}
    try:
        export = _doc_to_markdown(doc_id, collection)
        if export is None:
            return {'synced': False, 'reason': 'doc_not_found'}
        filename, content = export

        source_id = get_source_id(collection)
        if not source_id:
            raise SagUnavailable('create source failed')

        # 先删旧版本 (重同步幂等), 失败忽略
        _try_delete_doc(source_id, filename)

        r = requests.post(
            f'{url}/api/v1/sources/{source_id}/ingest',
            files={'file': (filename, content.encode('utf-8'),
                            'text/markdown')},
            timeout=_timeout(120),
        )
        r.raise_for_status()

        # 触发 event-entity 抽取 (LLM 慢, 只触发不等待结果)
        try:
            requests.post(
                f'{url}/api/v1/sources/{source_id}/extract',
                json={},
                timeout=_timeout(30),
            )
        except requests.RequestException:
            pass
        return {'synced': True, 'source_id': source_id, 'filename': filename}
    except SagUnavailable:
        raise
    except requests.RequestException as e:
        raise SagUnavailable(str(e)) from e
    except Exception as e:
        raise SagUnavailable(str(e)) from e


def _try_delete_doc(source_id: str, filename: str) -> None:
    """按文件名删 SAG 上的旧文档 (best effort)."""
    url = sag_base_url()
    if not url:
        return
    try:
        r = requests.get(
            f'{url}/api/v1/sources/{source_id}/documents',
            timeout=_timeout(),
        )
        r.raise_for_status()
        data = r.json()
        docs = data if isinstance(data, list) else data.get('documents') or []
        for d in docs:
            name = d.get('filename') or d.get('name') or ''
            if name == filename:
                did = d.get('id') or d.get('doc_id')
                if did:
                    requests.delete(
                        f'{url}/api/v1/sources/{source_id}/documents/{did}',
                        timeout=_timeout(),
                    )
                break
    except requests.RequestException:
        pass


def remove_doc(doc_id: int, collection: str) -> None:
    """删除文献时同步删 SAG (best effort, 失败静默)."""
    url = sag_base_url()
    if not url:
        return
    try:
        source_id = get_source_id(collection, create=False)
        if not source_id:
            return
        prefix = f'doc{doc_id}-'
        r = requests.get(
            f'{url}/api/v1/sources/{source_id}/documents',
            timeout=_timeout(),
        )
        r.raise_for_status()
        data = r.json()
        docs = data if isinstance(data, list) else data.get('documents') or []
        for d in docs:
            name = d.get('filename') or d.get('name') or ''
            if name.startswith(prefix):
                did = d.get('id') or d.get('doc_id')
                if did:
                    requests.delete(
                        f'{url}/api/v1/sources/{source_id}/documents/{did}',
                        timeout=_timeout(),
                    )
    except (requests.RequestException, SagUnavailable):
        pass


# ── 搜索 ───────────────────────────────────────────────

def search(query: str, collection: str, mode: str = 'vector',
           top_k: int = 10) -> list[dict]:
    """SAG source 内搜索. 返回标准化结果列表.

    mode: 'vector' (语义) | 'multi' (实体关系 + LLM rerank).
    每条: {doc_id, score, text, title, cite_key} — doc_id 从文件名解析,
    title/cite_key 回查 xsf DB (保证与 FTS5 结果一致).
    """
    url = sag_base_url()
    if not url:
        raise SagUnavailable('XSF_SAG_URL 未设置')
    source_id = get_source_id(collection, create=False)
    if not source_id:
        raise SagUnavailable(f'SAG source 不存在: xsf-{collection}')
    try:
        r = requests.post(
            f'{url}/api/v1/sources/{source_id}/search',
            json={'query': query, 'mode': mode, 'top_k': top_k},
            timeout=_timeout(60),
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise SagUnavailable(str(e)) from e

    data = r.json()
    raw = (data.get('results') if isinstance(data, dict) else data) or []

    out = []
    doc_ids = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = (item.get('content') or item.get('text')
                or item.get('chunk') or '')
        score = (item.get('score') if item.get('score') is not None
                 else item.get('similarity'))
        name = (item.get('filename') or item.get('source_file')
                or item.get('doc_name') or item.get('name') or '')
        m = re.match(r'doc(\d+)-', name)
        if not m:
            continue  # 非 xsf 同步的文档, 跳过
        doc_id = int(m.group(1))
        doc_ids.add(doc_id)
        out.append({'doc_id': doc_id, 'score': score,
                    'text': text, 'filename': name})

    if doc_ids:
        conn = get_conn(collection)
        try:
            placeholders = ','.join('?' * len(doc_ids))
            rows = conn.execute(
                f'''SELECT id, title, cite_key FROM documents
                    WHERE id IN ({placeholders})''',
                sorted(doc_ids),
            ).fetchall()
        finally:
            conn.close()
        meta = {r['id']: r for r in rows}
        for o in out:
            m = meta.get(o['doc_id'])
            o['title'] = (m['title'] if m else None) or o['filename']
            o['cite_key'] = m['cite_key'] if m else None
    return out
