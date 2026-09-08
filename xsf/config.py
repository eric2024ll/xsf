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


def get_upload_dir(collection: str) -> Path:
    """某书架的源文件目录: <collections>/{collection}/uploads/

    2026-09-04 起按书架独立（原全局平铺 collections/uploads/ 已迁移），
    跨书架同名文件不再互相覆盖。目录不存在时自动创建。
    """
    d = get_collections_dir() / collection / 'uploads'
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_upload_path(collection: str, filename: str) -> Path:
    """某书架内一份源文件的完整路径（纯拼接，不创建目录）。"""
    return get_collections_dir() / collection / 'uploads' / filename


def get_ocr_config_path() -> Path:
    """OCR 配置文件路径: <XSF_DATA>/ocr-config.json"""
    return get_data_dir() / 'ocr-config.json'


def _read_ocr_config() -> dict:
    """读取 OCR 配置文件。损坏/不存在返回 {}。

    v2 → v3 一次性原地迁移 (2026-09-06):
      generic_http → {type: vl_api, endpoint: paddle_http, url→base_url}
      aistudio     → {type: vl_api, endpoint: aistudio_job}
      local_merged → 删除 (p3 已裁撤)
    """
    p = get_ocr_config_path()
    try:
        cfg = json.loads(p.read_text('utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if cfg.get('version') == 2:
        providers = []
        for pr in cfg.get('providers', []):
            if not isinstance(pr, dict) or not pr.get('id'):
                continue
            ptype = pr.get('type', 'generic_http')
            if ptype == 'local_merged':
                continue
            np = dict(pr)
            np['type'] = 'vl_api'
            if ptype == 'aistudio':
                np['endpoint'] = 'aistudio_job'
            elif ptype == 'vl_api':
                pass
            else:
                np['endpoint'] = 'paddle_http'
            if 'url' in np:
                np['base_url'] = np.pop('url')
            providers.append(np)
        cfg['providers'] = providers
        cfg['version'] = 3
        try:
            _write_ocr_config(cfg)
        except OSError:
            pass  # 迁移写失败不阻塞读 (下次再迁)
    return cfg


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


# ── OCR provider 配置 (v3, 统一 vl_api 架构) ────────
# schema: {version: 3, providers: [{id, name, type: 'vl_api',
#          endpoint: 'paddle_http'|'aistudio_job'|'openai_chat',
#          base_url?, api_key?, model?}], default?}
#   paddle_http  POST <base_url>/ocr multipart(file) [+ Bearer]
#                → {pages: [{page_index, parsing_res_list, ...}]}  [结构化]
#   aistudio_job PaddleOCR aistudio 云端 (submit→poll→fetch, api_key=token) [结构化]
#   openai_chat  POST <base_url>/chat/completions (OpenAI 兼容视觉)       [纯文本]
#                Ollama / vLLM / LM Studio / 本地 paddle-vl 均适用
# 旧 v2 已自动迁移; v1 ({token, provider}) 不迁移, 读作空列表。


def get_ocr_providers() -> list[dict]:
    """v3 providers 列表 (深拷贝)。旧版/损坏配置返回 []。"""
    cfg = _read_ocr_config()
    if cfg.get('version') != 3:
        return []
    providers = cfg.get('providers', [])
    return [dict(p) for p in providers if isinstance(p, dict) and p.get('id')]


def get_default_ocr_provider_id() -> str | None:
    """默认 provider id: 配置 default > XSF_OCR_METHOD (匹配 id) > 首个。"""
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 3 else []
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
            '(vl_api: paddle_http / aistudio_job / openai_chat)，'
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
                       endpoint: str = 'paddle_http',
                       prompt: str = '') -> dict:
    """新增 (pid 为空) / 编辑 (pid 已存在) provider (v3, 统一 vl_api).

    endpoint:
      'paddle_http'  (本地/自建 PaddleOCR-VL HTTP, 需 base_url)
      'aistudio_job' (aistudio 云端, api_key=token)
      'openai_chat'  (OpenAI 兼容视觉端点, 需 base_url; Ollama/vLLM/LM Studio)
    api_key 传 None/空 且为编辑 → 保留旧值。
    prompt: openai_chat 专属自定义转录 prompt (可空=用内置默认; 其他 endpoint 忽略)。
    返回写入后的完整 provider dict。
    """
    ENDPOINTS = ('paddle_http', 'aistudio_job', 'openai_chat')
    name = (name or '').strip()
    url = (url or '').strip()
    ep = (endpoint or 'paddle_http').strip() or 'paddle_http'
    if ep not in ENDPOINTS:
        raise ValueError(f'未知 endpoint: {ep} (可选: {", ".join(ENDPOINTS)})')
    if not name:
        raise ValueError('name 不能为空')
    if ep in ('paddle_http', 'openai_chat'):
        if not url:
            raise ValueError(f'{ep} 类型必须填 base_url')
        if not (url.startswith('http://') or url.startswith('https://')):
            raise ValueError('base_url 必须以 http:// 或 https:// 开头')

    cfg = _read_ocr_config()
    if cfg.get('version') != 3:
        cfg = {'version': 3, 'providers': []}
    providers = cfg.get('providers', [])

    if pid:
        target = next((p for p in providers if p['id'] == pid), None)
        if target is None:
            raise KeyError(f'provider 不存在: {pid}')
        target['name'] = name
        target['type'] = 'vl_api'
        target['endpoint'] = ep
        target['base_url'] = url
        if api_key:                       # 空 = 保留旧值
            target['api_key'] = api_key.strip()
        if model is not None:
            target['model'] = model.strip() or None
        if ep == 'openai_chat':
            target['prompt'] = (prompt or '').strip()
        else:
            target.pop('prompt', None)
        target['updated_at'] = datetime.now().isoformat(timespec='seconds')
        result = dict(target)
    else:
        pid = _next_provider_id(providers)
        entry = {
            'id': pid,
            'name': name,
            'type': 'vl_api',
            'endpoint': ep,
            'base_url': url,
            'model': (model or '').strip() or None,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        if api_key:
            entry['api_key'] = api_key.strip()
        if ep == 'openai_chat':
            entry['prompt'] = (prompt or '').strip()
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
    if cfg.get('version') != 3:
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
    providers = cfg.get('providers', []) if cfg.get('version') == 3 else []
    ids = [p.get('id') for p in providers]
    if pid not in ids:
        raise KeyError(f'provider 不存在: {pid}')
    cfg['default'] = pid
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)


def get_auth_token() -> str | None:
    """XSF_AUTH_TOKEN 环境变量。未设返回 None（开发模式，跳过认证）。"""
    return os.environ.get('XSF_AUTH_TOKEN') or None
