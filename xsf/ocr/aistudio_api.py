"""aistudio PaddleOCR-VL 云端客户端 (内置 provider 类型).

三阶段协议: submit → poll → fetch JSONL, 折叠为一次同步调用。
token 来自 provider 配置 (api_key), 不依赖环境变量。
返回 list[dict]: {page_index, parsing_res_list, width, height}。
"""
import json
import time

import requests

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL_DEFAULT = "PaddleOCR-VL-1.6"
OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
POLL_TIMEOUT = 600
POLL_INTERVAL = 3
MAX_RETRIES = 3
RETRY_BASE_WAIT = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _do_request(method, url, name, timeout, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"bearer {token}"
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            if r.status_code in RETRYABLE_STATUS:
                last_exc = RuntimeError(f"上游 HTTP {r.status_code}")
                time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
                continue
            if r.status_code in (401, 403):
                raise RuntimeError("aistudio token 无效或已过期")
            if r.status_code >= 400:
                raise RuntimeError(f"上游 HTTP {r.status_code}: {r.text[:300]}")
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = RuntimeError(f"网络错误 ({name}): {e}")
            time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
    raise last_exc


def ocr_file_aistudio(pdf_path, token: str, model: str = None) -> list[dict]:
    """端到端 OCR: submit → poll → fetch。token 必填。"""
    token = (token or '').strip()
    if not token:
        raise RuntimeError('aistudio provider 缺少 token (api_key)')
    model = (model or '').strip() or MODEL_DEFAULT

    with open(pdf_path, 'rb') as f:
        r = _do_request(
            "POST", JOB_URL, "submit", timeout=120, token=token,
            data={"model": model, "optionalPayload": json.dumps(OPTIONAL_PAYLOAD)},
            files={"file": f},
        )
    job_id = r.json().get("data", {}).get("jobId")
    if not job_id:
        raise RuntimeError(f"响应无 jobId: {r.text[:300]}")

    deadline = time.time() + POLL_TIMEOUT
    err_streak = 0
    jsonl_url = None
    while time.time() < deadline:
        try:
            r = _do_request("GET", f"{JOB_URL}/{job_id}", "poll", timeout=30, token=token)
            data = r.json()["data"]
            state = data.get("state", "")
            if state == "done":
                jsonl_url = data["resultUrl"]["jsonUrl"]
                break
            if state == "failed":
                raise RuntimeError(f"OCR 任务失败: {json.dumps(data)[:300]}")
            err_streak = 0
            time.sleep(POLL_INTERVAL)
        except RuntimeError:
            raise
        except Exception:
            err_streak += 1
            if err_streak >= 5:
                raise
            time.sleep(POLL_INTERVAL)
    if not jsonl_url:
        raise TimeoutError(f"轮询超时 ({POLL_TIMEOUT}s), job={job_id}")

    r = _do_request("GET", jsonl_url, "fetch", timeout=300, token=None)
    pages = []
    for line in r.text.strip().split("\n"):
        if not line.strip():
            continue
        obj = json.loads(line)
        for lr in obj["result"]["layoutParsingResults"]:
            pruned = lr["prunedResult"]
            pruned["page_index"] = len(pages)
            pages.append(pruned)
    return pages
