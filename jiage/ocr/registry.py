"""OCR provider 注册表。

用户可自主添加 provider (见 14-ocr-pipeline.md §3.3)。
兼容性门槛: 必须能输出 parsing_res_list 中间格式
(含 block_bbox + block_label + block_content)。

三类 provider:
- DirectAdapter (封装流水线): PaddleOCR-VL / GLM-OCR MaaS / Folio-OCR 本地
- HybridAdapter (裸 VL + 检测器): Qwen-OCR / DeepSeek-OCR / 任意通用 VL
- 坐标源 (仅检测器): PP-OCRv5 / PP-DocLayout-V3 (辅助)
"""
import os

from .adapter import DirectAdapter
from .paddle_api import ocr_pdf as _paddle_ocr

_REGISTRY = {
    "paddle_api": DirectAdapter(_paddle_ocr, "paddle_api"),
}


def get_provider(method=None):
    """按 method 名取 provider。默认 paddle_api。"""
    method = method or os.environ.get("JIAGE_OCR_METHOD", "paddle_api")
    if method not in _REGISTRY:
        raise KeyError(
            f"未知 OCR method: {method}。已注册: {list(_REGISTRY)}"
        )
    return _REGISTRY[method]


def list_providers():
    """列出所有已注册 provider。"""
    return {
        name: type(adapter).__name__ for name, adapter in _REGISTRY.items()
    }


def register(name, adapter):
    """用户自主注册新 provider。

    Example:
        from jiage.ocr import DirectAdapter, register
        register("my_ocr", DirectAdapter(my_ocr_func, "my_ocr"))
    """
    _REGISTRY[name] = adapter
