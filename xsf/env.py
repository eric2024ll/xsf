"""极简 .env 加载器 (无第三方依赖).

优先级: 进程环境变量 > 找到的第一个 .env (只填缺失键, 绝不覆盖已有环境变量).

查找顺序 ($XSF_ENV 显式指定优先):
1. $XSF_ENV 指向的文件
2. 可执行文件同目录/.env      (Windows 便携版: zip 根目录)
3. 可执行文件上级目录/.env    (Linux venv 布局: ~/xsf/.venv/bin/xsf → ~/xsf/.env)

Linux systemd 部署不受影响: EnvironmentFile 已注入环境变量, .env 只填缺失键.
"""

import os
import sys
from pathlib import Path


def _candidate_paths() -> list[Path]:
    cands: list[Path] = []
    explicit = os.environ.get('XSF_ENV')
    if explicit:
        cands.append(Path(explicit))
    try:
        exe_dir = Path(sys.executable).resolve().parent
        cands.append(exe_dir / '.env')
        cands.append(exe_dir.parent / '.env')
    except (OSError, RuntimeError):
        pass
    return cands


def load_env() -> dict:
    """读第一个存在的 .env, 填充缺失的环境变量. 返回本次填充的键值."""
    for p in _candidate_paths():
        try:
            if not p.is_file():
                continue
            filled = {}
            for raw in p.read_text('utf-8').splitlines():
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[len('export '):].strip()
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                if key and val and key not in os.environ:
                    os.environ[key] = val
                    filled[key] = val
            if filled:
                pass  # 找到文件但可能全被环境变量覆盖 — 仍视为命中, 停止搜索
            return filled
        except (OSError, UnicodeDecodeError):
            continue
    return {}
