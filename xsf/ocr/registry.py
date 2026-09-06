"""OCR provider 注册表 (统一 vl_api 架构, 配置驱动).

2026-09-06 v3 重构: 三种 endpoint profile (paddle_http / aistudio_job /
openai_chat) 统一为 VLApiAdapter, 能力二分 structured/plain。
provider = {id, name, type: 'vl_api', endpoint, base_url?, api_key?, model?}。

调用方约定:
  首次入库 / 画框几何过滤重 OCR → adapter.ocr() (需 .structured == True)
  整页对照 / 栏裁切            → adapter.ocr_image_plain()

设计依据: histflow-plan system/tools/14-ocr-pipeline.md §3.3
"""
from ..config import get_ocr_provider_cfg, get_default_ocr_provider_id, \
    get_ocr_providers
from .vl_api import VLApiAdapter


def _build_adapter(provider_id: str = None) -> VLApiAdapter:
    """按配置构建 VLApiAdapter。找不到配置 raise RuntimeError。"""
    cfg = get_ocr_provider_cfg(provider_id)
    return VLApiAdapter(cfg)


def get_provider(method=None) -> VLApiAdapter:
    """按 provider id 取 adapter。

    method 解析链: 显式 id > 配置 default > XSF_OCR_METHOD(匹配 id) > 首个。
    """
    pid = method or get_default_ocr_provider_id()
    if pid is None:
        raise RuntimeError(
            '未配置 OCR provider。请在前端「OCR 设置」添加 vl_api '
            'provider (paddle_http / aistudio_job / openai_chat)。'
        )
    return _build_adapter(pid)


def list_providers():
    """列出已配置 provider: {id: name}。"""
    return {p['id']: p.get('name', p['id']) for p in get_ocr_providers()}
