import os
from pathlib import Path


def get_data_dir() -> Path:
    """JIAGE_DATA 环境变量 → 默认 ~/jiage-data/"""
    data_dir = os.environ.get('JIAGE_DATA')
    path = Path(data_dir) if data_dir else Path.home() / 'jiage-data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    return get_data_dir() / 'jiage.db'


def get_collections_dir() -> Path:
    """书架目录。可通过 JIAGE_COLLECTIONS_DIR 指向 OSS（服务器）。

    默认 JIAGE_DATA/collections/（本地）；服务器设 /mnt/oss/sources/jiage/collections/。
    jiage.db 必须留本地（ossfs 不支持 SQLite 文件锁）。
    """
    d = Path(os.environ.get('JIAGE_COLLECTIONS_DIR', str(get_data_dir() / 'collections')))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_ocr_token() -> str:
    """PADDLE_OCR_TOKEN 环境变量 (强制)。"""
    token = os.environ.get('PADDLE_OCR_TOKEN')
    if not token:
        raise RuntimeError(
            'PADDLE_OCR_TOKEN 环境变量未设置。'
            '请在 aistudio 获取 bearer token 后设置。'
        )
    return token


def get_ocr_method() -> str:
    """JIAGE_OCR_METHOD 环境变量，默认 paddle_api。"""
    return os.environ.get('JIAGE_OCR_METHOD', 'paddle_api')
