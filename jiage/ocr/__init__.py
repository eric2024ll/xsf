"""OCR provider 子包 (generic_http 同步协议).

统一入口: get_provider().ocr() — 按配置 (ocr-config.json v2) 构建 adapter。
"""
from .adapter import DirectAdapter
from .http_api import ocr_file
from .registry import get_provider, list_providers

__all__ = ["ocr_file", "DirectAdapter", "get_provider", "list_providers"]
