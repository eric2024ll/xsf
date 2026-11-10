#!/usr/bin/env bash
# 重新生成 packaging/constraints.txt (便携版构建依赖锁定)。
#
# 用法 (仓库根): bash packaging/gen-constraints.sh
# 前提: .venv 已装 editable xsf; 需要联网访问 PyPI。
#
# 说明: 在 Linux 上解析完整依赖闭包并锁定版本; Windows 构建时用同一份
# 约束 (pip install -c packaging/constraints.txt ...)。跨平台同名包版本
# 一致, Windows 独有包 (pywin32 等) 不在列、由 pip 现场解析。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
REPORT="${REPORT:-/tmp/xsf-resolve.json}"

"$PY" -m pip install --dry-run --quiet --report "$REPORT" \
    -e ".[desktop,build,office,mcp]"

"$PY" - "$REPORT" <<'EOF'
import datetime
import json
import pathlib
import sys

rep = json.load(open(sys.argv[1], encoding="utf-8"))
rows = sorted(
    ((i["metadata"]["name"], i["metadata"]["version"])
     for i in rep["install"]
     if i["metadata"]["name"].lower() != "xsf"),
    key=lambda x: x[0].lower(),
)

out = [
    "# 便携版构建依赖锁定 (CI 可复现) — 生成于 %s" % datetime.date.today(),
    "# 重新生成: bash packaging/gen-constraints.sh",
    "# 范围: Linux 解析闭包 (与 Windows 同名包版本一致); Windows 独有包",
    "# (pywin32 等) 不在列, pip 现场解析。",
    "# 背景: v0.3.5 便携包因无锁解析 + pyinstaller-hooks-contrib 2026.10",
    "# 漂移, 体积 +43MB (mem 小书房 2026-11-11-xsf-portable-size)。",
    "",
]
out += ["%s==%s" % (n, v) for n, v in rows]
pathlib.Path("packaging/constraints.txt").write_text(
    "\n".join(out) + "\n", encoding="utf-8")
print("已写入 packaging/constraints.txt: %d 包" % len(rows))
EOF