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
import os
import time

import requests

MAX_RETRIES = 3
RETRY_BASE_WAIT = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _http_timeout(default: int = 900) -> int:
    """单请求超时(秒): 大 PDF 本地 GPU 推理可达数分钟, 大部头扫描书
    可超 900s。可由 XSF_OCR_HTTP_TIMEOUT 覆盖; 非法值回退默认。
    需与服务端 POCR_TASK_TIMEOUT(看门狗) 协同: 客户端超时应略大于
    服务端看门狗, 避免客户端先断而服务端白算。"""
    try:
        return int(os.environ.get("XSF_OCR_HTTP_TIMEOUT", default))
    except (TypeError, ValueError):
        return default


HTTP_TIMEOUT = _http_timeout()  # 运行时可覆盖, 见 _http_timeout()
# 503 (服务忙) 快速失败: 固定短等待 + 独立次数上限, 避免排队风暴时
# 每页原地拉锯 MAX_RETRIES × (排队 120s + 退避), 页 error 交给断点补跑
BUSY_WAIT = 60
BUSY_MAX_ATTEMPTS = 2


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
    busy_attempts = 0
    for attempt in range(MAX_RETRIES):
        try:
            try:
                f = open(path, "rb")
            except FileNotFoundError as e:
                # 源文件在重试间隔被移走 (并发流程/手工清理): 快速失败,
                # 不再重试也不吐裸 ENOENT
                raise RuntimeError(
                    f"源文件已不存在 (疑被并发流程移走): {path}") from e
            with f:
                r = requests.post(url, headers=headers, data=form,
                                  files={"file": f}, timeout=timeout)
            if r.status_code == 503:
                busy_attempts += 1
                if busy_attempts >= BUSY_MAX_ATTEMPTS:
                    raise requests.HTTPError(
                        f"HTTP 503: OCR 服务持续忙 (已尝试 "
                        f"{busy_attempts} 次), 本页落 error 待断点补跑"
                    )
                print(f"  [ocr] HTTP 503 服务忙, {BUSY_WAIT}s 后重试 "
                      f"({busy_attempts}/{BUSY_MAX_ATTEMPTS})")
                time.sleep(BUSY_WAIT)
                continue
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
