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


def _endpoint_alive(cfg) -> bool:
    """paddle_http 做 1.5s TCP 探活跳过死端点; 其余类型 (云 API) 不探。"""
    if cfg['endpoint'] != 'paddle_http':
        return True
    import socket
    from urllib.parse import urlparse
    u = urlparse(cfg.get('base_url') or '')
    if not u.hostname:
        return True
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=1.5):
            return True
    except OSError:
        return False


def list_plain_providers():
    """支持整图 plain 且端点存活的 provider 列表 [{id, name, default}]。"""
    from ..config import get_default_ocr_provider_id
    default = get_default_ocr_provider_id()
    out = []
    for p in get_ocr_providers():
        if p['endpoint'] == 'aistudio_job' or not _endpoint_alive(p):
            continue
        out.append({'id': p['id'], 'name': p.get('name', p['id']),
                    'default': p['id'] == default})
    return out


def get_plain_provider(method=None) -> VLApiAdapter:
    """取支持整图纯文本路径 (ocr_image_plain) 的 adapter。

    method 解析链同 get_provider, 但 aistudio_job 不支持 plain 路径,
    显式指定时直接报错; 未指定时在配置中顺延找第一个支持的
    (openai_chat / paddle_http), 全不支持才报错。
    """
    pid = method or get_default_ocr_provider_id()
    if method is not None:
        cfg = get_ocr_provider_cfg(pid)
        if cfg['endpoint'] == 'aistudio_job':
            raise RuntimeError(
                f'provider {pid} (aistudio_job) 不支持整图纯文本路径, '
                '请换 openai_chat 或 paddle_http provider')
        return _build_adapter(pid)
    # 未显式指定 (含默认 provider 不支持 plain): 顺延找第一个支持且存活的
    if pid is not None:
        cfg = get_ocr_provider_cfg(pid)
        if cfg['endpoint'] != 'aistudio_job' and _endpoint_alive(cfg):
            return _build_adapter(pid)
    for p in get_ocr_providers():
        if p['endpoint'] != 'aistudio_job' and _endpoint_alive(p):
            return _build_adapter(p['id'])
    raise RuntimeError(
        '无支持整图纯文本路径的 provider (需 openai_chat 或 paddle_http)')
