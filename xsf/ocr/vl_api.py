"""统一 vl_api adapter (v3 架构, 2026-09-06).

一个类覆盖三种 endpoint profile, 能力二分:
  structured (有 block 坐标+标签) → 首次入库 / 画框几何过滤重 OCR
  plain      (纯文本)             → 整页对照 / 栏裁切路径 (无需坐标)

profiles:
  paddle_http  POST <base_url>/ocr multipart(file) → {pages:[...]}   structured
  aistudio_job aistudio 云端 submit→poll→fetch (api_key=token)       structured
  openai_chat  POST <base_url>/chat/completions (OpenAI 兼容视觉)    plain
               适配 Ollama / vLLM / LM Studio / 本地 paddle-vl server.py
  glm_ocr      POST <base_url>/layout_parsing (智谱 GLM-OCR MaaS)    structured
               0.9B OmniDocBench V1.5 94.62; 返回 md_results + layout_details
               (type+bbox); 0.2 元/百万 token, 逐页 PNG base64 直传 (2026-11-03)
  lighton_grounding  POST <base_url>/v1/chat/completions (本机 vLLM)  structured
               LightOnOCR-3-4B; prompt="grounding" → 每块前置
               ![label](x1,y1,x2,y2) (归一 0-1000); 空 prompt=纯转录
               (2026-11-03)

设计依据: histflow-plan/system/tools/14-ocr-pipeline.md §3。
"""
import base64
import os
import re
import tempfile

import requests

from .http_api import ocr_file, HTTP_TIMEOUT, MAX_RETRIES, \
    RETRY_BASE_WAIT, RETRYABLE_STATUS

# openai_chat 纯文本转录默认 prompt (用户自定义 prompt 留空时的回退值;
# 可在 OCR 设置中按文献类型改写, 疑字标记体系与校对界面约定一致)
DEFAULT_PLAIN_PROMPT = """逐字转录图中全部文字为纯文本，规则：
1. 竖排：从最右列起，逐列自上而下转录，每列转为一行，列序从右到左；横排按自然行序
2. 逐字照录：不增、不删、不注释、不翻译、不校对补正
3. 疑字标记：无法辨认→[?]；原件缺损/污损→[缺损]；疑为某字→[疑作:甲|乙]；疑有脱文→[疑脱]
4. 异体字、俗字、避讳字照原样转录，不转通行字；数字、西文、假名照原样保留
5. 表格：逐行转录，单元格间用 " | " 分隔，表头在最前
6. 眉批、夹注转录后以（批：…）标注；印章文字以（印：…）标注，置于所在位置
7. 只输出转录文本，不输出任何说明"""


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
        self.prompt = (cfg.get('prompt') or '').strip()
        self.name = cfg.get('name') or cfg.get('id', 'vl_api')
        if self.endpoint not in ('paddle_http', 'aistudio_job', 'openai_chat',
                                 'glm_ocr', 'lighton_grounding'):
            raise RuntimeError(f'未知 vl_api endpoint: {self.endpoint}')
        if self.endpoint == 'glm_ocr' and not self.base_url:
            self.base_url = 'https://open.bigmodel.cn/api/paas/v4'
        if self.endpoint == 'lighton_grounding' and not self.base_url:
            self.base_url = 'http://127.0.0.1:8023/v1'

    @property
    def structured(self) -> bool:
        return self.endpoint in ('paddle_http', 'aistudio_job', 'glm_ocr',
                                 'lighton_grounding')

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
        if self.endpoint == 'glm_ocr':
            return self._glm_ocr_pages(pdf_path)
        if self.endpoint == 'lighton_grounding':
            return self._lighton_pages(pdf_path)
        return ocr_file(pdf_path, self._ocr_url(), api_key=self.api_key,
                        model=self.model)

    def _pdf_to_pages(self, pdf_path):
        """openai_chat: PDF 逐页渲染 → 整页文本 → 伪 pages 结构。"""
        import pymupdf

        pages = []
        with pymupdf.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                png = pix.tobytes('png')
                text = self._openai_chat(png, 'image/png')
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

    # ── glm_ocr 路径 (智谱 GLM-OCR MaaS, 结构化) ──────

    GLM_OCR_DPI = 200   # API 侧会再缩放; 200dpi 兼顾小字与 token

    def _glm_layout_parsing(self, image_bytes: bytes) -> dict:
        """POST /layout_parsing 单页。返回原始 JSON。"""
        url = self.base_url.rstrip('/')
        if not url.endswith('/layout_parsing'):
            url += '/layout_parsing'
        b64 = base64.b64encode(image_bytes).decode('ascii')
        body = {'model': self.model or 'glm-ocr',
                'file': f'data:image/png;base64,{b64}'}
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
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
                        f'GLM-OCR 拒绝访问 (HTTP {r.status_code}): '
                        f'检查 bigmodel API key')
                if r.status_code >= 400:
                    detail = ''
                    try:
                        err = r.json().get('error') or {}
                        detail = err.get('message') or r.text[:200]
                    except ValueError:
                        detail = r.text[:200]
                    raise RuntimeError(
                        f'HTTP {r.status_code}: {detail}'
                        if detail else f'HTTP {r.status_code} ({url})')
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout,
                    ValueError) as e:
                import time
                last_exc = e
                time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
        raise last_exc

    def _glm_ocr_pages(self, pdf_path):
        """glm_ocr: 逐页渲染 PNG → layout_parsing → parsing_res_list。

        layout_details 形状防御式兼容两态: 扁平 [elem,...] (单图调用常态)
        或嵌套 [[elem,...], ...] (多页 PDF 时按页分组)。元素取
        type/label + bbox + text/content/markdown; 缺 layout_details 时
        回退 md_results 单块伪结构 (可入库不可几何过滤)。
        bbox 单位按「所发图片像素」处理 (与 width/height 同空间)。
        """
        import pymupdf

        pages = []
        with pymupdf.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                # 长边封顶 2400px: 重OCR 路径喂进来的是「300dpi 像素当 pt」的
                # 单页 PDF (2388×3568pt), 直接按 200dpi 渲染会变成 6637×9914
                # 的 65MP 图 — 白烧 token 且被服务端缩回。按 72dpi≈原始像素
                # 折算, 普通 A4 仍取满 200dpi。
                dpi = self.GLM_OCR_DPI
                max_pt = max(page.rect.width, page.rect.height)
                if max_pt > 0:
                    dpi = max(72, min(dpi, int(2400 / max_pt * 72)))
                pix = page.get_pixmap(dpi=dpi)
                png = pix.tobytes('png')
                try:
                    data = self._glm_layout_parsing(png)
                except RuntimeError as e:
                    # 单页失败不中断整本 (空页/配额等), 与其他引擎一致
                    import logging
                    logging.getLogger('xsf.ocr').warning(
                        'glm_ocr 第 %d 页失败: %s', i + 1, e)
                    continue
                blocks = self._glm_parse_details(data, pix.width, pix.height)
                if blocks:
                    pages.append({
                        'page_index': i,
                        'width': pix.width,
                        'height': pix.height,
                        'parsing_res_list': blocks,
                    })
        return pages

    @staticmethod
    def _glm_parse_details(data: dict, width: int, height: int) -> list:
        """layout_details + md_results → parsing_res_list。"""
        ld = data.get('layout_details')
        # 嵌套形态: 按页分组 → 取第一组 (我们是单页调用)
        if isinstance(ld, list) and ld and isinstance(ld[0], list):
            ld = ld[0] if ld else []
        if not isinstance(ld, list):
            ld = []
        out = []
        for el in ld:
            if not isinstance(el, dict):
                continue
            label = (el.get('type') or el.get('label') or 'text')
            text = (el.get('text') or el.get('content')
                    or el.get('markdown') or el.get('md') or '')
            if isinstance(text, dict):
                text = text.get('markdown') or text.get('html') or ''
            text = str(text).strip()
            # GLM-OCR 实际字段是 bbox_2d; 兼容旧版 bbox/box
            bbox = el.get('bbox_2d') or el.get('bbox') or el.get('box')
            if not text or not bbox or len(bbox) < 4:
                continue
            try:
                bbox = [float(bbox[0]), float(bbox[1]),
                        float(bbox[2]), float(bbox[3])]
            except (TypeError, ValueError):
                continue
            # 每块自带 width/height 时, bbox 在该块坐标系 → 换算到本页像素
            try:
                ew = float(el.get('width') or 0)
                eh = float(el.get('height') or 0)
            except (TypeError, ValueError):
                ew = eh = 0.0
            if ew > 0 and eh > 0 and (ew != width or eh != height):
                sx, sy = width / ew, height / eh
                bbox = [bbox[0] * sx, bbox[1] * sy,
                        bbox[2] * sx, bbox[3] * sy]
            out.append({'block_label': str(label),
                        'block_content': text,
                        'block_bbox': bbox})
        if not out:
            md = (data.get('md_results') or data.get('text') or '').strip()
            if md:
                out = [{'block_label': 'text', 'block_content': md,
                        'block_bbox': None}]
        return out

    # ── lighton_grounding 路径 (LightOnOCR-3, vLLM OpenAI 兼容) ──

    LIGHTON_MAX_EDGE = 2048   # 官方: 400dpi 渲染后缩到长边 2048
    _LIGHTON_MARK_RE = re.compile(
        r'!\[([A-Za-z0-9_+]+)\]\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,'
        r'\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)')

    def _lighton_chat(self, image_bytes: bytes, prompt: str,
                      max_tokens: int = 4096) -> str:
        """POST <base_url>/chat/completions (vLLM OpenAI 兼容)。

        prompt='grounding' 走版面模式; 空串=纯转录 (只发图, 不发 text)。
        """
        url = self.base_url
        if not url.endswith('/chat/completions'):
            if not url.endswith('/v1'):
                url += '/v1'
            url += '/chat/completions'
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        content = [{'type': 'image_url',
                    'image_url': {'url': 'data:image/png;base64,'
                                         + base64.b64encode(image_bytes).decode('ascii')}}]
        if prompt:
            content.append({'type': 'text', 'text': prompt})
        body = {
            'model': self.model or 'lightonocr',
            'messages': [{'role': 'user', 'content': content}],
            'max_tokens': max_tokens,
            'temperature': 0.2,
            'top_p': 0.9,
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
                if r.status_code >= 400:
                    detail = ''
                    try:
                        err = r.json().get('error') or {}
                        detail = err.get('message') if isinstance(err, dict) else str(err)
                    except ValueError:
                        detail = r.text[:200]
                    raise RuntimeError(
                        f'LightOnOCR HTTP {r.status_code}: {detail or url}')
                data = r.json()
                return ((data.get('choices') or [{}])[0]
                        .get('message', {}).get('content') or '')
            except (requests.ConnectionError, requests.Timeout, ValueError) as e:
                import time
                last_exc = e
                time.sleep(RETRY_BASE_WAIT * (2 ** attempt))
        raise last_exc

    def _lighton_pages(self, pdf_path):
        """逐页渲染到长边 2048 → grounding → 0-1000 归一 bbox 块。"""
        import pymupdf

        pages = []
        with pymupdf.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                longest = max(page.rect.width, page.rect.height) or 1.0
                dpi = int(min(400.0, max(96.0,
                                        self.LIGHTON_MAX_EDGE * 72.0 / longest)))
                pix = page.get_pixmap(dpi=dpi)
                png = pix.tobytes('png')
                try:
                    md = self._lighton_chat(png, 'grounding')
                except RuntimeError as e:
                    import logging
                    logging.getLogger('xsf.ocr').warning(
                        'lighton_grounding 第 %d 页失败: %s', i + 1, e)
                    continue
                blocks = self._lighton_parse_blocks(md)
                if blocks:
                    pages.append({
                        'page_index': i,
                        'width': 1000,
                        'height': 1000,
                        'parsing_res_list': blocks,
                    })
        return pages

    _LIGHTON_MD_LABELS = {'text', 'title', 'list', 'header', 'footer',
                          'page_number', 'footnote', 'caption', 'aside_text'}

    @classmethod
    def _lighton_clean(cls, content: str, label: str) -> str:
        """剥掉模型自带 Markdown 标记 (标题 #/加粗 **), 保留表格 HTML。

        检索文本层不该带排版记号; 块类型已由 label 表达。
        """
        if label not in cls._LIGHTON_MD_LABELS:
            return content
        out = []
        for ln in content.splitlines():
            s = ln.strip()
            if not s:
                continue
            s = re.sub(r'^#{1,6}\s+', '', s)
            s = s.replace('**', '')
            out.append(s)
        return '\n'.join(out) if out else content

    @classmethod
    def _lighton_parse_blocks(cls, md: str) -> list:
        """grounding 标记 → parsing_res_list。

        坐标归一 0-1000, 故 page_w/h 固定 1000 (下游按 bbox/page_w 归一)。
        无标记时回退单块纯文本 (bbox None, 可入库不可定位)。
        """
        text = (md or '').strip()
        if not text:
            return []
        marks = list(cls._LIGHTON_MARK_RE.finditer(text))
        if not marks:
            return [{'block_label': 'text', 'block_content': text,
                     'block_bbox': None}]
        out = []
        for idx, m in enumerate(marks):
            label = (m.group(1) or 'text').rstrip('+') or 'text'
            try:
                bbox = [float(m.group(j)) for j in range(2, 6)]
            except ValueError:
                continue
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            content = text[m.end():end].strip()
            if not content:
                continue
            out.append({'block_label': label,
                        'block_content': cls._lighton_clean(content, label),
                        'block_bbox': bbox})
        if not out:
            return [{'block_label': 'text', 'block_content': text,
                     'block_bbox': None}]
        return out

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
        if self.endpoint == 'glm_ocr':
            data = self._glm_layout_parsing(image_bytes)
            md = (data.get('md_results') or data.get('text') or '').strip()
            if not md:
                blocks = self._glm_parse_details(data, 1, 1)
                md = '\n'.join(b['block_content'] for b in blocks)
            return md
        if self.endpoint == 'lighton_grounding':
            raw = (self._lighton_chat(image_bytes, '') or '').strip()
            return self._lighton_clean(raw, 'text')
        raise NotImplementedError(
            'aistudio_job 暂不支持整图纯文本路径 (用于整本入库即可)')

    def _openai_chat(self, image_bytes: bytes, mime: str,
                     prompt: str = None, max_tokens: int = None) -> str:
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
                     'text': prompt or self.prompt or DEFAULT_PLAIN_PROMPT},
                ],
            }],
            'stream': False,
        }
        if max_tokens:
            body['max_tokens'] = max_tokens
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
                if r.status_code >= 400:
                    # 提取服务端错误详情 (OpenAI 风格 error.message)
                    detail = ''
                    try:
                        err = r.json().get('error') or {}
                        detail = err.get('message') or r.text[:200]
                    except ValueError:
                        detail = r.text[:200]
                    raise RuntimeError(
                        f'HTTP {r.status_code}: {detail}'
                        if detail else f'HTTP {r.status_code} ({url})')
                r.raise_for_status()
                data = r.json()
                content = (data.get('choices') or [{}])[0] \
                    .get('message', {}).get('content', '')
                if not content:
                    # 空响应 (如空白图/低 max_tokens) 重试无意义, 直接返回
                    return ''
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
            if self.endpoint == 'glm_ocr':
                # 空白 800×1000 PNG 直调 layout_parsing: 验 key/配额/路径。
                # 注意: 服务端对过小图 (1×1 / 320×240 / 512×512) 一律回
                # HTTP 400「格式不支持」, 与 key 无关 → 探活图必须够大。
                try:
                    import pymupdf
                    pm = pymupdf.Pixmap(pymupdf.csRGB,
                                        pymupdf.IRect(0, 0, 800, 1000), 0)
                    tiny = pm.tobytes('png')
                except Exception:
                    tiny = base64.b64decode(
                        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'
                        'AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5E'
                        'rkJggg==')
                try:
                    data = self._glm_layout_parsing(tiny)
                    has_md = bool((data.get('md_results') or '').strip())
                    return {'ok': True,
                            'detail': 'glm-ocr 可调用'
                                      + ('' if has_md else ' (md_results 空, 疑非文档图, 正常)')}
                except RuntimeError as e:
                    return {'ok': False, 'detail': str(e)[:200]}
            if self.endpoint == 'lighton_grounding':
                url = self.base_url
                if url.endswith('/chat/completions'):
                    url = url[:-len('/chat/completions')]
                if not url.endswith('/v1'):
                    url += '/v1'
                try:
                    r = requests.get(url.rstrip('/') + '/models', timeout=10)
                    if r.status_code != 200:
                        return {'ok': False,
                                'detail': f'HTTP {r.status_code}'}
                    ids = [m.get('id') for m in r.json().get('data', [])]
                    return {'ok': True,
                            'detail': '已加载: ' + ', '.join(ids[:3])}
                except (requests.ConnectionError, requests.Timeout) as e:
                    return {'ok': False, 'detail': f'连不上 {url}: {e}'}
            if self.endpoint == 'paddle_http':
                r = requests.get(f"{self._root_url()}/health",
                                 timeout=10)
                ok = r.status_code == 200
                return {'ok': ok,
                        'detail': r.json().get('model', 'ok') if ok
                        else f'HTTP {r.status_code}'}
            if self.endpoint == 'openai_chat':
                # 纯文本 ping 验证 model 可调用 (无图最鲁棒:
                # 避开小图/低 max_tokens 被网关拒绝的问题)
                headers = {'Content-Type': 'application/json'}
                if self.api_key:
                    headers['Authorization'] = f'Bearer {self.api_key}'
                url = self.base_url
                if not url.endswith('/chat/completions'):
                    url += '/chat/completions'
                try:
                    r = requests.post(url, headers=headers, timeout=30, json={
                        'model': self.model,
                        'messages': [{'role': 'user',
                                      'content': 'ping'}],
                        'max_tokens': 4,
                        'stream': False,
                    })
                    if r.status_code == 200:
                        return {'ok': True,
                                'detail': f'model 可调用: {self.model}'}
                    detail = ''
                    try:
                        err = r.json().get('error') or {}
                        detail = err.get('message') or r.text[:200]
                    except ValueError:
                        detail = r.text[:200]
                    # 部分视觉服务强制要图 (如本机 paddle-vl): 退回小图探测
                    if r.status_code == 400 and detail and any(
                            k in detail.lower() for k in
                            ('图像', 'image', 'base64')):
                        try:
                            tiny_png = base64.b64decode(
                                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA'
                                'fFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQA'
                                'AAABJRU5ErkJggg==')
                            self._openai_chat(tiny_png, 'image/png', '')
                            return {'ok': True,
                                    'detail': f'model 可调用: {self.model}'}
                        except Exception as e2:
                            return {'ok': False, 'detail': str(e2)[:250]}
                    if r.status_code == 404:
                        return {'ok': False,
                                'detail': f'model 不存在: {self.model} '
                                          f'({detail})' if detail
                                else f'model 不存在: {self.model}'}
                    return {'ok': False,
                            'detail': f'HTTP {r.status_code}: {detail}'
                            if detail else f'HTTP {r.status_code}'}
                except requests.RequestException as e:
                    return {'ok': False, 'detail': str(e)[:250]}
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
