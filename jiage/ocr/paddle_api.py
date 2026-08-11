"""PaddleOCR-VL 云端 API 客户端.

基于 mzyj-kb-code/experiments/e2/ocr_api.py 改写:
- 删除硬编码默认 token, 强制环境变量 PADDLE_OCR_TOKEN
- 保留 async job 三阶段 + 双层重试逻辑

设计依据: histflow-plan/system/tools/14-ocr-pipeline.md
"""
import os
import json
import time

import requests

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"
DEFAULT_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
MAX_RETRIES = 3
RETRY_BASE_WAIT = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _get_token():
    token = os.environ.get("PADDLE_OCR_TOKEN")
    if not token:
        raise RuntimeError(
            "PADDLE_OCR_TOKEN 环境变量未设置。"
            "请在 aistudio 获取 bearer token 后设置。"
        )
    return token


def _is_retryable(exc):
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "connection", "httperror"))


def _do_request(method, url, name, timeout, need_auth=True, **kwargs):
    """带重试的 HTTP 请求。对 RETRYABLE_STATUS + 网络错误指数退避。

    need_auth=False 用于 BCE 存储下载（认证在 URL query 参数，不需 bearer header）。
    """
    headers = kwargs.pop("headers", {})
    if need_auth:
        token = _get_token()
        headers["Authorization"] = f"bearer {token}"
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
            if r.status_code in RETRYABLE_STATUS:
                wait = RETRY_BASE_WAIT * (2 ** attempt)
                print(
                    f"  [{name}] HTTP {r.status_code}, "
                    f"{wait}s 后重试 ({attempt + 1}/{MAX_RETRIES})"
                )
                last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            print(
                f"  [{name}] 网络错误 {e}, "
                f"{wait}s 后重试 ({attempt + 1}/{MAX_RETRIES})"
            )
            time.sleep(wait)
    raise last_exc


def _submit(pdf_path):
    """阶段 1: 提交 OCR 任务，返回 jobId。"""
    with open(pdf_path, "rb") as f:
        r = _do_request(
            "POST",
            JOB_URL,
            "submit",
            timeout=120,
            data={
                "model": MODEL,
                "optionalPayload": json.dumps(DEFAULT_PAYLOAD),
            },
            files={"file": f},
        )
    return r.json()["data"]["jobId"]


def _poll(job_id, timeout=600, interval=3):
    """阶段 2: 轮询任务状态，返回 jsonUrl。"""
    url = f"{JOB_URL}/{job_id}"
    deadline = time.time() + timeout
    err_streak = 0
    while time.time() < deadline:
        try:
            r = _do_request("GET", url, "poll", timeout=30)
            data = r.json()["data"]
            state = data.get("state", "")
            if state == "done":
                return data["resultUrl"]["jsonUrl"]
            if state == "failed":
                raise RuntimeError(f"OCR 任务失败: {data}")
            err_streak = 0
            time.sleep(interval)
        except Exception:
            err_streak += 1
            if err_streak >= 5:
                raise
            time.sleep(interval)
    raise TimeoutError(f"轮询超时 ({timeout}s), job={job_id}")


def _fetch_pages(jsonl_url):
    """阶段 3: 下载 JSONL 结果，返回 pages list。

    每行一个 JSON，取 result.layoutParsingResults[].prunedResult。
    """
    r = _do_request("GET", jsonl_url, "fetch", timeout=300, need_auth=False)
    pages = []
    for line in r.text.strip().split("\n"):
        if not line.strip():
            continue
        obj = json.loads(line)
        layout_results = obj["result"]["layoutParsingResults"]
        for lr in layout_results:
            pruned = lr["prunedResult"]
            pruned["page_index"] = len(pages)
            pages.append(pruned)
    return pages


def ocr_pdf(pdf_path):
    """端到端 OCR: submit → poll → fetch，带整体重试。

    返回 list[dict]，每个 = {page_index, parsing_res_list, width, height}。
    parsing_res_list 元素 = {block_label, block_content, block_bbox, block_order}。
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            job_id = _submit(pdf_path)
            print(f"  OCR 任务已提交: {job_id}")
            jsonl_url = _poll(job_id)
            pages = _fetch_pages(jsonl_url)
            print(f"  OCR 完成，共 {len(pages)} 页")
            return pages
        except Exception as e:
            if not _is_retryable(e) or attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BASE_WAIT * (2 ** attempt)
            print(
                f"  OCR 整体重试 ({attempt + 1}/{MAX_RETRIES}): "
                f"{e}, {wait}s 后重提交"
            )
            last_exc = e
            time.sleep(wait)
    raise last_exc
