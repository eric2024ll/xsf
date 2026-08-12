import json
import os
import tempfile
from datetime import datetime
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


def get_ocr_config_path() -> Path:
    """OCR 配置文件路径: <JIAGE_DATA>/ocr-config.json"""
    return get_data_dir() / 'ocr-config.json'


def _read_ocr_config() -> dict:
    """读取 OCR 配置文件。损坏/不存在返回 {}。"""
    p = get_ocr_config_path()
    try:
        return json.loads(p.read_text('utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_ocr_config(data: dict) -> None:
    """原子写 OCR 配置文件 (tempfile + rename)，权限 600。"""
    p = get_ocr_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_ocr_token() -> str:
    """OCR bearer token: 配置文件优先，环境变量 PADDLE_OCR_TOKEN fallback。"""
    cfg = _read_ocr_config()
    token = cfg.get('token', '').strip()
    if token:
        return token
    token = os.environ.get('PADDLE_OCR_TOKEN')
    if not token:
        raise RuntimeError(
            'OCR token 未设置。请在前端「OCR 设置」填入，'
            '或设置 PADDLE_OCR_TOKEN 环境变量。'
        )
    return token


def save_ocr_config(token: str, provider: str = 'paddle_api') -> dict:
    """保存 OCR 配置。返回写入的完整 dict。"""
    cfg = _read_ocr_config()
    cfg['provider'] = provider
    if token:
        cfg['token'] = token.strip()
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)
    return cfg


def get_ocr_method() -> str:
    """OCR provider 名: 配置文件优先，环境变量 JIAGE_OCR_METHOD fallback。"""
    cfg = _read_ocr_config()
    return cfg.get('provider') or os.environ.get('JIAGE_OCR_METHOD', 'paddle_api')


def get_auth_token() -> str | None:
    """JIAGE_AUTH_TOKEN 环境变量。未设返回 None（开发模式，跳过认证）。"""
    return os.environ.get('JIAGE_AUTH_TOKEN') or None
