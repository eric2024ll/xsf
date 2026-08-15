"""OCR provider 注册表 (generic_http 同步协议, 配置驱动).

2026-08-14 重构: 注册表从代码注册升级为配置注册 (ocr-config.json v2).
provider = {id, name, url, api_key?, model?}, 用户在 Web 界面自助添加.
兼容性门槛不变: 端点必须返回 parsing_res_list 中间格式
(含 block_bbox + block_label + block_content).

设计依据: histflow-plan system/tools/14-ocr-pipeline.md §3.3 修订注
"""
from ..config import get_ocr_provider_cfg, get_default_ocr_provider_id, \
    get_ocr_providers
from .adapter import DirectAdapter
from .http_api import ocr_file


def _build_adapter(provider_id: str = None) -> DirectAdapter:
    """按配置构建 adapter (type: generic_http | aistudio)。找不到配置 raise RuntimeError。"""
    cfg = get_ocr_provider_cfg(provider_id)
    pid = cfg['id']
    ptype = cfg.get('type', 'generic_http')

    if ptype == 'aistudio':
        from .aistudio_api import ocr_file_aistudio

        def _ocr(pdf_path, _cfg=cfg):
            return ocr_file_aistudio(
                pdf_path,
                token=_cfg.get('api_key'),
                model=_cfg.get('model'),
            )
    else:
        def _ocr(pdf_path, _cfg=cfg):
            return ocr_file(
                pdf_path,
                url=_cfg['url'],
                api_key=_cfg.get('api_key'),
                model=_cfg.get('model'),
            )

    return DirectAdapter(_ocr, pid)


def get_provider(method=None):
    """按 provider id 取 adapter。

    method 解析链: 显式 id > 配置 default > XSF_OCR_METHOD(匹配 id) > 首个。
    """
    pid = method or get_default_ocr_provider_id()
    if pid is None:
        raise RuntimeError(
            '未配置 OCR provider。请在前端「OCR 设置」添加 generic_http '
            'provider (POST 文件 → {pages:[...]})。'
        )
    return _build_adapter(pid)


def list_providers():
    """列出已配置 provider: {id: name}。"""
    return {p['id']: p.get('name', p['id']) for p in get_ocr_providers()}


def register(name, adapter):
    """代码注册 (保留扩展点, 当前无使用方)。"""
    raise NotImplementedError(
        'provider 已改为配置注册 (ocr-config.json v2), '
        '请在前端「OCR 设置」添加, 不再支持代码注册'
    )
