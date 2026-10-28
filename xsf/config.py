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
    """数据库目录（必须本地磁盘）。

    网络文件系统 (NFS/ossfs) 不支持 SQLite 文件锁+随机写，xsf.db 必须留本地磁盘。
    XSF_DB_DIR 环境变量 → 默认 XSF_DATA/db/。
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


# 路径分隔符 (跨平台) + Windows 保留 + URL/cookie 层风险字符 (# % & + 会让
# /collections/{名}/... 链接与 last_collection cookie 出现截断/解码错乱)
_COLL_FORBIDDEN = set('/\\:*?"<>|#%&+')


def validate_collection_name(name: str) -> str | None:
    """校验书架名. 合法返回 None; 非法返回中文错误消息.

    Web (POST/PATCH /api/collections) 与 CLI (add) 共用,
    防 AI/脚本建出链接层打不开或文件系统层歧义的书架名
    (如含 # 的名字入库正常但书架页 404).
    """
    raw = name or ''
    if not raw.strip():
        return '名称不能为空'
    if raw != raw.strip():
        return '名称首尾不能含空白'
    if len(raw) > 64:
        return '名称过长（最多 64 字符）'
    if raw in ('.', '..'):
        return '名称不能为 . 或 ..'
    bad = sorted(set(raw) & _COLL_FORBIDDEN)
    if bad:
        return f'名称包含非法字符: {"".join(bad)}'
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return '名称包含控制字符'
    return None


def find_orphan_collections() -> tuple[list[str], list[str]]:
    """探测两类「消失的书架」. 返回 (db_orphans, dir_orphans):

    - db_orphans: db/ 下有目录但 xsf.db 缺失或 0 字节 → list_collections
      跳过 → 书架列表看不到 (常为删除残留或创建中断)
    - dir_orphans: collections/ 下有书架目录但 db/ 无对应 DB →
      folder_scan 只扫已有 DB 的书架, 投放文件永不入库且无任何提示
      (# 开头如 NAS 回收站 #recycle、. 开头隐藏目录不算)
    """
    known = set(list_collections())
    db_dir = get_db_dir()
    db_orphans = sorted(
        c.name for c in db_dir.iterdir()
        if c.is_dir() and c.name not in known
    )
    coll_dir = get_collections_dir()
    dir_orphans = sorted(
        c.name for c in coll_dir.iterdir()
        if c.is_dir() and c.name not in known
        and not c.name.startswith(('#', '.'))
    )
    return db_orphans, dir_orphans


def get_collections_dir() -> Path:
    """书架目录（源文件/uploads）。可通过 XSF_COLLECTIONS_DIR 指向 NAS 等外部存储。

    默认 XSF_DATA/collections/（本地）；本机 NAS 部署用 /mnt/nas/xsf-collections/。
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


def get_original_dir(collection: str, doc_id: int) -> Path:
    """某文献的原件目录: <collections>/{collection}/files/{doc_id}/

    2026-10-28 原件统一模型: 入库即存原件, 文本层是派生物。
    目录不存在时自动创建。
    """
    d = get_collections_dir() / collection / 'files' / str(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_original_path(collection: str, linked_pdf: str | None) -> Path | None:
    """解析 linked_pdf 字段 → 原件绝对路径（纯拼接，不检查存在性）。

    - 新格式: files/<id>/original.<ext>（相对书架目录）
    - 旧格式: 纯文件名（历史 .snap-N.pdf / uploads 投放名, 兼容读取）
    - 空 → None
    """
    if not linked_pdf:
        return None
    v = linked_pdf.strip()
    if v.startswith('files/'):
        return get_collections_dir() / collection / v
    return get_upload_path(collection, v)


def get_ocr_config_path() -> Path:
    """OCR 配置文件路径: <XSF_DATA>/ocr-config.json"""
    return get_data_dir() / 'ocr-config.json'


# ── 外部权威挂载 (显式, 用户决定挂载并告知位置) ──
# 原则: 不自动探测路径。用户显式声明挂载哪个外部库、位于何处; 未挂载即
# 该能力降级 (实体归一退化为纯 LLM)。
# 配置: <XSF_DATA>/mounts.json
#   {"version":1, "mounts": {"cbdb": {"enabled":true, "path":"...", "cmd":"python3"}}}
def get_mounts_config_path() -> Path:
    return get_data_dir() / 'mounts.json'


def read_mounts() -> dict:
    """读取挂载配置; 缺失/损坏 → 空配置。"""
    p = get_mounts_config_path()
    try:
        d = json.loads(p.read_text('utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'version': 1, 'mounts': {}}
    if not isinstance(d, dict):
        return {'version': 1, 'mounts': {}}
    d.setdefault('version', 1)
    if not isinstance(d.get('mounts'), dict):
        d['mounts'] = {}
    return d


def write_mounts(data: dict) -> None:
    """原子写挂载配置 (tempfile + rename)。"""
    p = get_mounts_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data or {})
    data.setdefault('version', 1)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_mount(name: str) -> dict | None:
    """返回已启用且路径存在的挂载 {path, cmd, ...} 或 None。

    未挂载 / 未启用 / 路径不存在 → None (能力降级)。
    """
    m = (read_mounts().get('mounts') or {}).get(name)
    if not isinstance(m, dict) or not m.get('enabled'):
        return None
    path = m.get('path')
    if not path or not os.path.exists(path):
        return None
    return m


def set_mount(name: str, enabled: bool, path: str = '', cmd: str = '') -> dict:
    """写入/更新一个挂载项。返回完整配置。"""
    cfg = read_mounts()
    mounts = cfg.setdefault('mounts', {})
    item = {'enabled': bool(enabled), 'path': (path or '').strip()}
    if cmd:
        item['cmd'] = cmd.strip()
    mounts[name] = item
    write_mounts(cfg)
    return cfg


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
    """默认 OCR provider id: 配置 default > XSF_OCR_METHOD (匹配 id) > 首个。

    P2c (2026-10-17): 只在 vl_api 型中解析——llm_chat (翻译 LLM) 即使被
    写进 default (旧数据/手工编辑) 也跳过, 防止 OCR 引擎误拿翻译配置。
    """
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 3 else []
    vl = [p for p in providers if p.get('type') != 'llm_chat' and p.get('id')]
    ids = [p.get('id') for p in vl]
    if cfg.get('default') in ids:
        return cfg['default']
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
                       prompt: str = '',
                       ptype: str = 'vl_api') -> dict:
    """新增 (pid 为空) / 编辑 (pid 已存在) provider (v3, 统一 vl_api).

    endpoint:
      'paddle_http'  (本地/自建 PaddleOCR-VL HTTP, 需 base_url)
      'aistudio_job' (aistudio 云端, api_key=token)
      'openai_chat'  (OpenAI 兼容视觉端点, 需 base_url; Ollama/vLLM/LM Studio)
    ptype (P2c, 2026-10-17):
      'vl_api'   视觉 OCR provider (default 解析链走 cfg['default'])
      'llm_chat' 纯文本 LLM provider (翻译/autofill; endpoint 强制 openai_chat,
                 default 走独立键 cfg['default_llm'])
    api_key 传 None/空 且为编辑 → 保留旧值。
    prompt: openai_chat 专属自定义转录 prompt (可空=用内置默认; 其他 endpoint 忽略)。
    返回写入后的完整 provider dict。
    """
    ENDPOINTS = ('paddle_http', 'aistudio_job', 'openai_chat')
    name = (name or '').strip()
    url = (url or '').strip()
    ep = (endpoint or 'paddle_http').strip() or 'paddle_http'
    ptype = (ptype or 'vl_api').strip() or 'vl_api'
    if ptype not in ('vl_api', 'llm_chat'):
        raise ValueError(f'未知 provider 类型: {ptype} (可选: vl_api/llm_chat)')
    if ptype == 'llm_chat':
        ep = 'openai_chat'  # 纯文本 LLM 同协议, 强制
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
        target['type'] = ptype
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
            'type': ptype,
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
    if ptype == 'vl_api':
        if not cfg.get('default'):
            cfg['default'] = pid
    else:
        if not cfg.get('default_llm'):
            cfg['default_llm'] = pid
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)
    return result


def delete_ocr_provider(pid: str) -> bool:
    """删除 provider。若它是 default/default_llm 则清之。返回是否删除。"""
    cfg = _read_ocr_config()
    if cfg.get('version') != 3:
        return False
    providers = cfg.get('providers', [])
    remaining = [p for p in providers if p.get('id') != pid]
    if len(remaining) == len(providers):
        return False
    cfg['providers'] = remaining
    if cfg.get('default') == pid:
        cfg['default'] = next(
            (p['id'] for p in remaining if p.get('type') != 'llm_chat'),
            None) if any(p.get('type') != 'llm_chat' for p in remaining) else None
    if cfg.get('default_llm') == pid:
        llm_left = [p['id'] for p in remaining if p.get('type') == 'llm_chat']
        cfg['default_llm'] = llm_left[0] if llm_left else None
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)
    return True


def set_default_ocr_provider(pid: str, scope: str = 'ocr') -> None:
    """设置默认 provider。scope='ocr' (cfg['default']) / 'llm' (cfg['default_llm'])。"""
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 3 else []
    ids = [p.get('id') for p in providers]
    if pid not in ids:
        raise KeyError(f'provider 不存在: {pid}')
    cfg['default_llm' if scope == 'llm' else 'default'] = pid
    cfg['updated_at'] = datetime.now().isoformat(timespec='seconds')
    _write_ocr_config(cfg)


# ── LLM provider (P2c 翻译模型, 2026-10-17) ────────────

def get_llm_providers() -> list[dict]:
    """type=='llm_chat' 的 provider 列表 (深拷贝)。"""
    return [p for p in get_ocr_providers() if p.get('type') == 'llm_chat']


def get_default_llm_provider_id() -> str | None:
    """默认 LLM: default_llm > XSF_LLM_METHOD (匹配 id) > 首个 llm_chat。"""
    cfg = _read_ocr_config()
    providers = cfg.get('providers', []) if cfg.get('version') == 3 else []
    llms = [p for p in providers if p.get('type') == 'llm_chat']
    ids = [p.get('id') for p in llms]
    default = cfg.get('default_llm')
    if default in ids:
        return default
    env = os.environ.get('XSF_LLM_METHOD')
    if env in ids:
        return env
    return ids[0] if ids else None


def get_llm_provider_cfg(provider_id: str = None) -> dict:
    """解析 LLM provider 配置。未配置 raise RuntimeError。"""
    llms = get_llm_providers()
    if not llms:
        raise RuntimeError(
            '未配置翻译模型 (llm_chat provider)。请在前端「OCR 设置」添加 '
            '类型=llm_chat 的 provider (OpenAI 兼容: zhipuai/deepseek/ollama 均可)。'
        )
    want = provider_id or get_default_llm_provider_id()
    for p in llms:
        if p['id'] == want:
            return p
    avail = ', '.join(p['id'] for p in llms)
    raise RuntimeError(f'未知 LLM provider: {want}。已配置: {avail}')
