"""OCR provider 子包 (统一 vl_api 架构).

统一入口: get_provider() — 按配置 (ocr-config.json v3) 构建 VLApiAdapter。
structured profile (paddle_http / aistudio_job) 提供 .ocr() 结构化识别;
openai_chat 提供 .ocr_image_plain() 纯文本识别 (整页对照 / 栏裁切)。
"""
from .http_api import ocr_file
from .registry import get_provider, list_providers
from .vl_api import VLApiAdapter

__all__ = ["ocr_file", "VLApiAdapter", "get_provider", "list_providers"]
