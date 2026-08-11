import os
from pathlib import Path


def get_data_dir() -> Path:
    """JIAGE_DATA 环境变量 → 默认 ~/jiage-data/"""
    data_dir = os.environ.get('JIAGE_DATA')
    path = Path(data_dir) if data_dir else Path.home() / 'jiage-data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_dir() -> Path:
    """数据库目录（本地磁盘）。

    ossfs 不支持 SQLite 文件锁+随机写，jiage.db 必须留本地磁盘。
    JIAGE_DB_DIR 环境变量 → 默认 JIAGE_DATA/db/。
    服务器上保持 ~/jiage-data/db/，不要指向 OSS。
    """
    d = Path(os.environ.get('JIAGE_DB_DIR', str(get_data_dir() / 'db')))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path(collection: str) -> Path:
    """每个 collection 独立 DB: db/<collection>/jiage.db（本地磁盘）"""
    return get_db_dir() / collection / 'jiage.db'


def list_collections() -> list[str]:
    """扫描 db 目录下含 jiage.db 的子目录，返回 collection 名称列表"""
    db_dir = get_db_dir()
    result = []
    for child in sorted(db_dir.iterdir()):
        if child.is_dir() and (child / 'jiage.db').exists():
            result.append(child.name)
    return result


def get_collections_dir() -> Path:
    """书架目录（源文件/uploads）。可通过 JIAGE_COLLECTIONS_DIR 指向 OSS（服务器）。

    默认 JIAGE_DATA/collections/（本地）；服务器设 /mnt/oss/sources/jiage/collections/。
    注意: jiage.db 不放这里，放 get_db_dir()（本地磁盘）。
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


def get_auth_token() -> str | None:
    """JIAGE_AUTH_TOKEN 环境变量。未设返回 None（开发模式，跳过认证）。"""
    return os.environ.get('JIAGE_AUTH_TOKEN') or None
