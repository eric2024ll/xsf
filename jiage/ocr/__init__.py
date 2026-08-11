"""OCR provider 子包.

统一入口: ocr_pdf() 或 get_provider().ocr().
"""
from .paddle_api import ocr_pdf
from .adapter import DirectAdapter
from .registry import get_provider, list_providers, register

__all__ = ["ocr_pdf", "DirectAdapter", "get_provider", "list_providers", "register"]
