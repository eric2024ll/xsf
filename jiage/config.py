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
    d = get_data_dir() / 'collections'
    d.mkdir(parents=True, exist_ok=True)
    return d
