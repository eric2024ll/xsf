"""临时用户管理 (管理员创建, 仅搜索/浏览权限).

存储: XSF_DATA/users.json
    {"users": [{
        "username": str, "salt": hex, "hash": hex,      # pbkdf2-hmac-sha256
        "created_at": ISO, "expires_at": ISO | null,    # null = 永久
        "enabled": bool, "note": str
    }]}

角色模型:
    admin  = XSF_AUTH_TOKEN (环境变量, 不落盘)
    guest  = users.json 中的账号 (内存 session, 重启需重登)
"""
import hashlib
import json
import os
import re
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

_lock = Lock()
_PBKDF2_ITER = 200_000
_USERNAME_RE = re.compile(r'^[\w\u4e00-\u9fff-]{1,32}$')
RESERVED_NAMES = {'admin', 'users', 'api'}


def _users_path() -> Path:
    from .config import get_data_dir
    return get_data_dir() / 'users.json'


def _now() -> datetime:
    return datetime.now()


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _load() -> dict:
    p = _users_path()
    if not p.exists():
        return {"users": []}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"users": []}


def _save(data: dict):
    p = _users_path()
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, p)


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), _PBKDF2_ITER
    ).hex()


def _public(u: dict) -> dict:
    return {
        "username": u["username"],
        "created_at": u.get("created_at"),
        "expires_at": u.get("expires_at"),
        "enabled": bool(u.get("enabled", True)),
        "note": u.get("note", ""),
        "expired": _is_expired(u),
    }


def _is_expired(u: dict) -> bool:
    exp = _parse_iso(u.get("expires_at"))
    return exp is not None and _now() >= exp


def has_users() -> bool:
    with _lock:
        return bool(_load()["users"])


def list_users() -> list:
    with _lock:
        return [_public(u) for u in _load()["users"]]


def gen_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def create_user(username: str, password: str = None, days=None, note: str = ""):
    """创建临时用户. 返回 (error, user_public, plain_password)."""
    username = (username or '').strip()
    if not username:
        return "用户名不能为空", None, None
    if not _USERNAME_RE.match(username):
        return "用户名仅限 1-32 位中英文/数字/下划线/连字符", None, None
    if username.lower() in RESERVED_NAMES:
        return "该用户名保留, 请换一个", None, None

    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return "有效期天数须为整数", None, None
        if days <= 0:
            return "有效期天数须为正整数 (留空 = 永久)", None, None

    plain = password if password else gen_password()
    if len(plain) < 4 or len(plain) > 64:
        return "密码长度须在 4-64 位之间", None, None

    salt_hex = secrets.token_hex(16)
    record = {
        "username": username,
        "salt": salt_hex,
        "hash": _hash_password(plain, salt_hex),
        "created_at": _now().isoformat(timespec='seconds'),
        "expires_at": (_now() + timedelta(days=days)).isoformat(timespec='seconds') if days else None,
        "enabled": True,
        "note": (note or '').strip()[:200],
    }
    with _lock:
        data = _load()
        if any(u["username"] == username for u in data["users"]):
            return "用户名已存在", None, None
        data["users"].append(record)
        _save(data)
    return None, _public(record), plain


def delete_user(username: str):
    """删除用户. 返回 (error, deleted_username)."""
    with _lock:
        data = _load()
        users = [u for u in data["users"] if u["username"] != username]
        if len(users) == len(data["users"]):
            return "用户不存在", None
        data["users"] = users
        _save(data)
    return None, username


def set_enabled(username: str, enabled: bool):
    """启用/停用. 返回 error 或 None."""
    with _lock:
        data = _load()
        for u in data["users"]:
            if u["username"] == username:
                u["enabled"] = bool(enabled)
                _save(data)
                return None
        return "用户不存在"


def verify_user(username: str, password: str):
    """校验登录. 返回 (status, user_public).

    status: 'ok' | 'no_user' | 'bad_password' | 'disabled' | 'expired'
    """
    with _lock:
        data = _load()
        rec = next((u for u in data["users"] if u["username"] == username), None)
    if rec is None:
        return 'no_user', None
    if not secrets.compare_digest(
        _hash_password(password or '', rec["salt"]), rec["hash"]
    ):
        return 'bad_password', None
    if not rec.get("enabled", True):
        return 'disabled', None
    if _is_expired(rec):
        return 'expired', None
    return 'ok', _public(rec)
