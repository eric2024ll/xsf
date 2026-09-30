"""极简 .env 加载器 (无第三方依赖).

优先级: 进程环境变量 > 找到的第一个配置文件 (只填缺失键, 绝不覆盖已有环境变量).

查找顺序 ($XSF_ENV 显式指定优先), 每级 `.env` 先于 `env.txt`
(`.env` 为 Unix 习惯名; `env.txt` 供 Windows 便携版使用 —— 资源管理器
不允许把文件改名成点开头的名字, 用户无法手工创建 .env):
1. $XSF_ENV 指向的文件
2. 可执行文件同目录/.env | 同目录/env.txt     (Windows 便携版: zip 根目录)
3. 可执行文件上级目录/.env | 上级/env.txt     (Linux venv 布局: ~/xsf/.venv/bin/xsf → ~/xsf/.env)

Linux systemd 部署不受影响: EnvironmentFile 已注入环境变量, 配置文件只填缺失键.
"""

import os
import sys
from pathlib import Path

_NAMES = ('.env', 'env.txt')


def _candidate_paths() -> list[Path]:
    cands: list[Path] = []
    explicit = os.environ.get('XSF_ENV')
    if explicit:
        cands.append(Path(explicit))
    dirs = []
    try:
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir)
        dirs.append(exe_dir.parent)
    except (OSError, RuntimeError):
        pass
    # venv console_script 的 sys.executable 是 python 解释器而非入口脚本,
    # 上两级到不了仓库根; 用包位置兜底 (源码运行 = 仓库根, frozen = _internal)
    try:
        dirs.append(Path(__file__).resolve().parents[1])
    except (OSError, RuntimeError):
        pass
    for d in dirs:
        for name in _NAMES:
            cands.append(d / name)
    return cands


def find_env_file() -> Path | None:
    """返回第一个实际存在的候选 .env 路径 (无则 None). 供 doctor 标注配置来源."""
    for p in _candidate_paths():
        try:
            if p.is_file():
                return p
        except (OSError, RuntimeError):
            pass
    return None


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
