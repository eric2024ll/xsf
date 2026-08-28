import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def get_data_dir() -> Path:
    """XSF_DATA 环境变量 → 默认 ~/xsf-data/"""
    data_dir = os.environ.get('XSF_DATA')
    path = Path(data_dir) if data_dir else Path.home() / 'xsf-data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_dir() -> Path:
    """数据库目录（本地磁盘）。

    ossfs 不支持 SQLite 文件锁+随机写，xsf.db 必须留本地磁盘。
    XSF_DB_DIR 环境变量 → 默认 XSF_DATA/db/。
    服务器上保持 ~/xsf-data/db/，不要指向 OSS。
    """
    d = Path(os.environ.get('XSF_DB_DIR', str(get_data_dir() / 'db')))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_db_path(collection: str) -> Path:
    """每个 collection 独立 DB: db/<collection>/xsf.db（本地磁盘）"""
    return get_db_dir() / collection / 'xsf.db'


def list_collections() -> list[str]:
    """扫描 db 目录下含非空 xsf.db 的子目录，返回 collection 名称列表.

    跳过 0 字节空壳 (历史 bug: get_conn 曾静默创建空 DB)。
    """
    db_dir = get_db_dir()
    result = []
    for child in sorted(db_dir.iterdir()):
        db_file = child / 'xsf.db'
        if child.is_dir() and db_file.exists() and db_file.stat().st_size > 0:
            result.append(child.name)
    return result


def get_collections_dir() -> Path:
    """书架目录（源文件/uploads）。可通过 XSF_COLLECTIONS_DIR 指向 OSS（服务器）。

    默认 XSF_DATA/collections/（本地）；服务器设 /mnt/oss/sources/xsf/collections/。
    注意: xsf.db 不放这里，放 get_db_dir()（本地磁盘）。
    """
    d = Path(os.environ.get('XSF_COLLECTIONS_DIR', str(get_data_dir() / 'collections')))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_ocr_config_path() -> Path:
    """OCR 配置文件路径: <XSF_DATA>/ocr-config.json"""
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


# ── OCR provider 配置 (v2, generic_http 同步协议) ────────
# schema: {version: 2, providers: [{id, name, url, api_key?, model?}], default?}
# 唯一协议: POST <url> multipart(file[, model]) [+ Bearer api_key]
#           → {pages: [{page_index, parsing_res_list, width, height}]}
# 旧 v1 ({token, provider}) 无对应协议, 不迁移, 读作空列表。


def get_ocr_providers() -> list[dict]:
    """v2 providers 列表 (深拷贝)。v1/损坏配置返回 []。"""
    cfg = _read_ocr_config()
    if cfg.get('version') != 2:
        return []
    providers = cfg.get('providers', [])
    return [dict(p) for p in providers if isinstance(p, dict) and p.get('id')]


def get_default_ocr_provider_id() -> str | None:
    """默认 provider id: 配置 default > XSF_OCR_METHOD (匹配 id) > 首个。"""
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 2 else []
    ids = [p.get('id') for p in providers if p.get('id')]
    default = cfg.get('default')
    if default in ids:
        return default
    env = os.environ.get('XSF_OCR_METHOD')
    if env in ids:
        return env
    return ids[0] if ids else None


def get_ocr_provider_cfg(provider_id: str = None) -> dict:
    """解析 provider 配置: 显式 id > default 解析链。找不到 raise RuntimeError。"""
    providers = get_ocr_providers()
    if not providers:
        raise RuntimeError(
            '未配置 OCR provider。请在前端「OCR 设置」添加 '
            '(generic_http: POST 文件 → {pages:[...]})，'
            '或设置 XSF_OCR_METHOD 环境变量。'
        )
    want = provider_id or get_default_ocr_provider_id()
    for p in providers:
        if p['id'] == want:
            return p
    avail = ', '.join(p['id'] for p in providers)
    raise RuntimeError(f'未知 OCR provider: {want}。已配置: {avail}')


def _next_provider_id(providers: list[dict]) -> str:
    n = 1
    existing = {p['id'] for p in providers}
    while f'p{n}' in existing:
        n += 1
    return f'p{n}'


def save_ocr_provider(name: str, url: str = None, pid: str = None,
                      api_key: str = None, model: str = None,
                      type: str = 'generic_http',
                      urls: list[str] = None) -> dict:
    """新增 (pid 为空) / 编辑 (pid 已存在) provider。

    type:
      'generic_http' (自定义同步端点, 需 url)
      'aistudio'     (内置云端, 无需 url)
      'local_merged' (本地合并: PP-OCRv66 + PP-StructureV3, 需 urls 列表)
    api_key 传 None/空 且为编辑 → 保留旧值。
    urls 仅用于 local_merged 类型, 双 URL 列表。
    返回写入后的完整 provider dict。
    """
    PROVIDER_TYPES = ('generic_http', 'aistudio', 'local_merged')
    name = (name or '').strip()
    url = (url or '').strip()
    ptype = (type or 'generic_http').strip() or 'generic_http'
    if ptype not in PROVIDER_TYPES:
        raise ValueError(f'未知 provider 类型: {ptype} (可选: {", ".join(PROVIDER_TYPES)})')
    if not name:
        raise ValueError('name 不能为空')
    if ptype == 'generic_http':
        if not url:
            raise ValueError('generic_http 类型必须填 url')
        if not (url.startswith('http://') or url.startswith('https://')):
            raise ValueError('url 必须以 http:// 或 https:// 开头')

    cfg = _read_ocr_config()
    if cfg.get('version') != 2:
        cfg = {'version': 2, 'providers': []}
    providers = cfg.get('providers', [])

    if pid:
        target = next((p for p in providers if p['id'] == pid), None)
        if target is None:
            raise KeyError(f'provider 不存在: {pid}')
        target['name'] = name
        target['type'] = ptype
        if urls is not None:
            target['urls'] = urls
        elif ptype == 'local_merged':
            target.pop('url', None)
        else:
            target['url'] = url
        if api_key:                       # 空 = 保留旧值
            target['api_key'] = api_key.strip()
        if model is not None:
            target['model'] = model.strip() or None
        target['updated_at'] = datetime.now().isoformat(timespec='seconds')
        result = dict(target)
    else:
        pid = _next_provider_id(providers)
        entry = {
            'id': pid,
            'name': name,
            'type': ptype,
            'model': (model or '').strip() or None,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        if ptype == 'local_merged':
            if not urls or len(urls) < 2:
                raise ValueError('local_merged 类型必须提供 urls 列表 (至少 2 个 URL)')
            entry['urls'] = list(urls)
        else:
            entry['url'] = url
        if api_key:
            entry['api_key'] = api_key.strip()
        providers.append(entry)
        result = dict(entry)

    cfg['providers'] = providers
    if not cfg.get('default'):
        cfg['default'] = pid
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)
    return result


def delete_ocr_provider(pid: str) -> bool:
    """删除 provider。若它是 default 则清空 default。返回是否删除。"""
    cfg = _read_ocr_config()
    if cfg.get('version') != 2:
        return False
    providers = cfg.get('providers', [])
    remaining = [p for p in providers if p.get('id') != pid]
    if len(remaining) == len(providers):
        return False
    cfg['providers'] = remaining
    if cfg.get('default') == pid:
        cfg['default'] = remaining[0]['id'] if remaining else None
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)
    return True


def set_default_ocr_provider(pid: str) -> None:
    """设置全局默认 provider。"""
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 2 else []
    ids = [p.get('id') for p in providers]
    if pid not in ids:
        raise KeyError(f'provider 不存在: {pid}')
    cfg['default'] = pid
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)


def get_auth_token() -> str | None:
    """XSF_AUTH_TOKEN 环境变量。未设返回 None（开发模式，跳过认证）。"""
    return os.environ.get('XSF_AUTH_TOKEN') or None
