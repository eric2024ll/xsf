"""统一 vl_api adapter (v3 架构, 2026-09-06).

一个类覆盖三种 endpoint profile, 能力二分:
  structured (有 block 坐标+标签) → 首次入库 / 画框几何过滤重 OCR
  plain      (纯文本)             → 整页对照 / 栏裁切路径 (无需坐标)

profiles:
  paddle_http  POST <base_url>/ocr multipart(file) → {pages:[...]}   structured
  aistudio_job aistudio 云端 submit→poll→fetch (api_key=token)       structured
  openai_chat  POST <base_url>/chat/completions (OpenAI 兼容视觉)    plain
               适配 Ollama / vLLM / LM Studio / 本地 paddle-vl server.py

设计依据: histflow-plan/system/tools/14-ocr-pipeline.md §3。
"""
import base64
import os
import tempfile

import requests

from .http_api import ocr_file, HTTP_TIMEOUT, MAX_RETRIES, \
    RETRY_BASE_WAIT, RETRYABLE_STATUS


class VLApiAdapter:
    """vl_api 统一适配器。

    .structured  → bool (True: .ocr() 可用; False: 仅 plain 路径)
    .ocr(pdf_path) → list[{page_index, parsing_res_list, width, height}]
    .ocr_image_plain(image_bytes, mime, prompt) → str (整图纯文本)
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.endpoint = cfg.get('endpoint', 'paddle_http')
        self.base_url = (cfg.get('base_url') or '').rstrip('/')
        self.model = cfg.get('model')
        self.api_key = cfg.get('api_key')
        self.name = cfg.get('name') or cfg.get('id', 'vl_api')
        if self.endpoint not in ('paddle_http', 'aistudio_job', 'openai_chat'):
            raise RuntimeError(f'未知 vl_api endpoint: {self.endpoint}')

    @property
    def structured(self) -> bool:
        return self.endpoint in ('paddle_http', 'aistudio_job')

    def _ocr_url(self) -> str:
        """结构化 /ocr 端点 (base_url 容忍带 /ocr 尾巴)。"""
        url = self.base_url
        if url.endswith('/ocr'):
            url = url[:-len('/ocr')]
        return url + '/ocr'

    def _root_url(self) -> str:
        """服务根 (health/models 探活用): 剥掉误带的 /ocr 尾巴。"""
        url = self.base_url
        if url.endswith('/ocr'):
            url = url[:-len('/ocr')]
        return url

    # ── structured 路径 ──────────────────────────────

    def ocr(self, pdf_path):
        """结构化 OCR: 文件 → pages 列表。

        paddle_http/aistudio_job: 真结构化 (bbox+label)。
        openai_chat: 每页渲染 PNG → 整页纯文本 → 伪 pages 结构
        (block_label='text', block_bbox=None) — 逐页对照形态, 无块坐标。
        """
        if self.endpoint == 'aistudio_job':
            from .aistudio_api import ocr_file_aistudio
            return ocr_file_aistudio(pdf_path, token=self.api_key,
                                     model=self.model)
        if self.endpoint == 'openai_chat':
            return self._pdf_to_pages(pdf_path)
        return ocr_file(pdf_path, self._ocr_url(), api_key=self.api_key,
                        model=self.model)

    def _pdf_to_pages(self, pdf_path):
        """openai_chat: PDF 逐页渲染 → 整页文本 → 伪 pages 结构。"""
        import pymupdf

        prompt = ('逐页转录图片中全部文字，按阅读顺序输出纯文本，'
                  '不要添加任何解释或标注')
        pages = []
        with pymupdf.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                png = pix.tobytes('png')
                text = self._openai_chat(png, 'image/png', prompt)
                text = (text or '').strip()
                if not text:
                    continue
                pages.append({
                    'page_index': i,
                    'width': pix.width,
                    'height': pix.height,
                    'parsing_res_list': [{
                        'block_label': 'text',
                        'block_content': text,
                        'block_bbox': None,
                    }],
                })
        return pages

    # ── plain 路径 ───────────────────────────────────

    def ocr_image_plain(self, image_bytes: bytes, mime: str = 'image/png',
                        prompt: str = None) -> str:
        """整图 → 纯文本。openai_chat 真调用; paddle_http 走 /ocr 转文本。"""
        if self.endpoint == 'openai_chat':
            return self._openai_chat(image_bytes, mime, prompt)
        if self.endpoint == 'paddle_http':
            fd, tmp = tempfile.mkstemp(suffix='.png')
            try:
                os.write(fd, image_bytes)
                os.close(fd)
                pages = ocr_file(tmp, self._ocr_url(), api_key=self.api_key,
                                 model=self.model)
                return _pages_to_text(pages)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        raise NotImplementedError(
            'aistudio_job 暂不支持整图纯文本路径 (用于整本入库即可)')

    def _openai_chat(self, image_bytes: bytes, mime: str,
                     prompt: str = None) -> str:
        """POST <base_url>/chat/completions, OpenAI 视觉消息格式。"""
        url = self.base_url
        if not url.endswith('/chat/completions'):
            url += '/chat/completions'
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        b64 = base64.b64encode(image_bytes).decode('ascii')
        body = {
            'model': self.model or 'paddleocr-vl',
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image_url',
                     'image_url': {'url': f'data:{mime};base64,{b64}'}},
                    {'type': 'text',
                     'text': prompt or '请识别图中全部文字, 按阅读顺序输出'},
                ],
            }],
            'stream': False,
        }
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.post(url, headers=headers, json=body,
                                  timeout=HTTP_TIMEOUT)
                if r.status_code in RETRYABLE_STATUS:
                    import time
                    last_exc = requests.HTTPError(f'HTTP {r.status_code}')
                    time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
                    continue
                if r.status_code in (401, 403):
                    raise RuntimeError(
                        f'OCR 服务拒绝访问 (HTTP {r.status_code}): '
                        f'检查 api_key 是否有效')
                r.raise_for_status()
                data = r.json()
                content = (data.get('choices') or [{}])[0] \
                    .get('message', {}).get('content', '')
                if not content:
                    raise ValueError(f'响应无 content: {str(data)[:200]}')
                return content
            except (requests.ConnectionError, requests.Timeout,
                    ValueError) as e:
                import time
                last_exc = e
                time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
        raise last_exc

    # ── 连接测试 (设置界面「测试」按钮) ───────────────

    def test_connection(self) -> dict:
        """轻量探活, 不跑大推理。返回 {ok, detail}。"""
        try:
            if self.endpoint == 'paddle_http':
                r = requests.get(f"{self._root_url()}/health",
                                 timeout=10)
                ok = r.status_code == 200
                return {'ok': ok,
                        'detail': r.json().get('model', 'ok') if ok
                        else f'HTTP {r.status_code}'}
            if self.endpoint == 'openai_chat':
                headers = {}
                if self.api_key:
                    headers['Authorization'] = f'Bearer {self.api_key}'
                r = requests.get(f"{self._root_url()}/models",
                                 headers=headers, timeout=10)
                ok = r.status_code == 200
                detail = 'ok'
                if ok:
                    try:
                        ids = [m.get('id') for m in r.json().get('data', [])]
                        detail = f"models: {', '.join(filter(None, ids)) or '无'}"
                    except ValueError:
                        pass
                else:
                    detail = f'HTTP {r.status_code}'
                return {'ok': ok, 'detail': detail}
            if self.endpoint == 'aistudio_job':
                # aistudio 无免费探活端点: 只验证 token 已配置,
                # 真实任务首次调用时才验证有效性 (避免测试也烧配额)
                if (self.api_key or '').strip():
                    return {'ok': True,
                            'detail': 'token 已配置 (有效性在首次任务时验证)'}
                return {'ok': False, 'detail': '缺少 token (api_key)'}
        except Exception as e:
            return {'ok': False, 'detail': str(e)[:300]}
        return {'ok': False, 'detail': '未知 endpoint'}


def _pages_to_text(pages: list) -> str:
    """结构化 pages → 纯文本 (title 加 ##, 表格取 markdown, 跳过页眉脚)."""
    parts = []
    for p in pages:
        for b in p.get('parsing_res_list', []):
            label = b.get('block_label') or 'text'
            content = b.get('block_content')
            if isinstance(content, dict):
                text = content.get('markdown') or content.get('html') or ''
            else:
                text = str(content) if content else ''
            text = text.strip()
            if not text:
                continue
            if label in ('title', 'doc_title'):
                parts.append(f'\n## {text}\n')
            elif label == 'table':
                parts.append(f'\n{text}\n')
            elif label in ('image', 'separator', 'header', 'footer'):
                continue
            else:
                parts.append(text)
    return '\n'.join(parts).strip()


def build_vl_adapter(provider_cfg: dict) -> VLApiAdapter:
    """工厂入口。"""
    return VLApiAdapter(provider_cfg)
