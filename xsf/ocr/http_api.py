"""generic_http sync OCR 客户端.

唯一协议 (2026-08-14 决策, 见 histflow-plan 14-ocr-pipeline.md §3.3):
  POST <url>                          # multipart/form-data
    file:  PDF/图片
    model: 可选表单字段
  Headers: Authorization: Bearer <api_key>   # 可选
  → 200 {pages: [{page_index, parsing_res_list, width, height}]}

响应归一化兼容三种形态:
  {"pages": [...]}                          # 标准形态 (本地 server.py)
  {"result": {"pages": [...]}}              # 常见包装
  {"layoutParsingResults": [{prunedResult}]} # PaddleX/aistudio 单页形态

parsing_res_list 元素 = {block_label, block_content, block_bbox, block_order}
坐标系铁律: width/height 与 block_bbox 同一像素空间。
"""
import time

import requests

MAX_RETRIES = 3
RETRY_BASE_WAIT = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
HTTP_TIMEOUT = 900  # 大 PDF 本地 GPU 推理可达数分钟


def _is_retryable(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        return True
    return any(k in str(exc).lower() for k in ("timeout", "connection"))


def _normalize_pages(data) -> list[dict]:
    """把三种响应形态归一化为 pages 列表，并补齐 page_index。"""
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        if isinstance(data.get('pages'), list):
            pages = data['pages']
        elif isinstance(data.get('result'), dict) and \
                isinstance(data['result'].get('pages'), list):
            pages = data['result']['pages']
        elif isinstance(data.get('layoutParsingResults'), list):
            pages = [lr['prunedResult'] for lr in data['layoutParsingResults']
                     if isinstance(lr, dict) and 'prunedResult' in lr]
        else:
            raise ValueError(
                f"响应不是已知的 pages 形态: 顶层键 {list(data)[:8]}"
            )
    else:
        raise ValueError(f"响应不是 JSON 对象: {type(data).__name__}")

    for i, p in enumerate(pages):
        if not isinstance(p, dict):
            raise ValueError(f"page[{i}] 不是 dict")
        if p.get('page_index') is None:
            p['page_index'] = i
    return pages


def ocr_file(path, url, api_key=None, model=None, timeout=HTTP_TIMEOUT):
    """同步 OCR: POST 文件 → pages 列表。带指数退避重试。

    返回 list[{page_index, parsing_res_list, width, height}]。
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    form = {}
    if model:
        form["model"] = model

    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            with open(path, "rb") as f:
                r = requests.post(url, headers=headers, data=form,
                                  files={"file": f}, timeout=timeout)
            if r.status_code in RETRYABLE_STATUS:
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                print(f"  [ocr] HTTP {r.status_code}, "
                      f"{wait}s 后重试 ({attempt + 1}/{MAX_RETRIES})")
                last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"OCR 服务拒绝访问 (HTTP {r.status_code}): "
                    f"检查 api_key 是否有效"
                )
            r.raise_for_status()
            pages = _normalize_pages(r.json())
            print(f"  [ocr] 完成, 共 {len(pages)} 页")
            return pages
        except (requests.ConnectionError, requests.Timeout,
                ValueError, RuntimeError) as e:
            # 4xx/5xx 已处理; 响应形态错误 (ValueError) 与网络错误可重试
            if isinstance(e, RuntimeError):
                raise
            last_exc = e
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            print(f"  [ocr] 错误 {e}, {wait}s 后重试 "
                  f"({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    raise last_exc
