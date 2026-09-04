#!/usr/bin/env python3
"""一次性迁移: collections/uploads/ 全局平铺 → collections/{书架}/uploads/ 按书架分目录.

背景: 2026-09-04 前所有书架的源文件混在 collections/uploads/ 一层,
跨书架同名文件会互相覆盖。迁移后每书架独立目录, DB 不变 (只存裸 filename)。

规则:
  - 各书架 DB documents.filename 清单 = 应有文件 → 移入该书架 uploads/
  - 无任何书架引用的文件 → 移入 collections/_orphan/ (保留不删)
  - 同名文件被多个书架引用 → 第一个 rename, 其余 copy (当前数据无此情况)
  - 目标已存在同名文件 (曾跑过迁移) → 跳过并报告 (幂等)
  - 全部移完后 uploads/ 为空则删除该目录

用法:
  python scripts/migrate_uploads_per_collection.py           # dry-run 预览
  python scripts/migrate_uploads_per_collection.py --apply   # 实际执行

需在 XSF_DATA / XSF_COLLECTIONS_DIR 环境与生产一致时运行 (本机读 .env 或
systemd EnvironmentFile; 手动运行前先 source 或 export 同名变量)。
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xsf.config import get_collections_dir, get_db_path, list_collections  # noqa: E402

LEGACY_DIR = get_collections_dir() / "uploads"
ORPHAN_DIR = get_collections_dir() / "_orphan"


def collection_filenames(collection: str) -> set[str]:
    db = get_db_path(collection)
    conn = sqlite3.connect(str(db))
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT DISTINCT filename FROM documents WHERE filename IS NOT NULL"
            )
        }
    finally:
        conn.close()


def move(src: Path, dst: Path) -> None:
    """同卷 rename, 跨设备回退 shutil.move."""
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="实际执行 (默认 dry-run)")
    args = parser.parse_args()
    acting = args.apply

    if not LEGACY_DIR.exists():
        print(f"无平铺 uploads/ 目录 ({LEGACY_DIR}), 无需迁移。")
        return

    legacy_files = sorted(f for f in LEGACY_DIR.iterdir() if f.is_file())
    print(f"书架: {list_collections()}")
    print(f"平铺 uploads/ 现有文件: {len(legacy_files)} 个")
    print(f"模式: {'APPLY (实际执行)' if acting else 'DRY-RUN (预览, 加 --apply 执行)'}")
    print()

    claimed: dict[str, list[str]] = {}  # filename -> [collection, ...]
    for coll in list_collections():
        for fn in collection_filenames(coll):
            claimed.setdefault(fn, []).append(coll)

    # 1) 按书架归属移动
    moved = already = missing = 0
    problems = []
    for fn in sorted(claimed):
        src = LEGACY_DIR / fn
        owners = claimed[fn]
        dst0 = get_collections_dir() / owners[0] / "uploads" / fn
        if src.exists():
            if dst0.exists():
                already += 1
                problems.append(f"  跳过(目标已存在): {fn} → {owners[0]}")
            else:
                if acting:
                    dst0.parent.mkdir(parents=True, exist_ok=True)
                    move(src, dst0)
                moved += 1
        elif dst0.exists():
            already += 1  # 上次迁移已完成
        else:
            missing += 1
            problems.append(f"  缺失: {fn} (DB 引用但任何位置都找不到)")
        # 同名被多书架引用: 首个已移, 其余从首个目标复制
        for other in owners[1:]:
            dst_n = get_collections_dir() / other / "uploads" / fn
            copy_src = dst0 if dst0.exists() else (src if src.exists() else None)
            if copy_src and not dst_n.exists():
                if acting:
                    dst_n.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(copy_src, dst_n)
                problems.append(f"  多书架同名, 复制到: {other}/uploads/{fn}")

    # 2) 无书架引用的文件 → _orphan/ (按 claimed 清单判定, dry-run/apply 一致)
    orphan_names = sorted(f.name for f in legacy_files if f.name not in claimed)
    orphans = []
    for name in orphan_names:
        f = LEGACY_DIR / name
        dst = ORPHAN_DIR / name
        if not f.exists():
            continue  # apply 模式下已被本次运行移走
        if dst.exists():
            problems.append(f"  孤儿跳过(_orphan/ 已有同名): {name}")
            continue
        orphans.append(name)
        if acting:
            ORPHAN_DIR.mkdir(parents=True, exist_ok=True)
            move(f, dst)

    # 3) 清空则删平铺目录
    if acting and LEGACY_DIR.exists() and not any(LEGACY_DIR.iterdir()):
        LEGACY_DIR.rmdir()
        print("已删除空目录 uploads/")

    print(f"归属移动: {moved} 个 (已迁移过 {already}, DB 引用但缺失 {missing})")
    print(f"孤儿 → _orphan/: {len(orphans)} 个")
    for name in orphans:
        print(f"  {name}")
    if problems:
        print("备注:")
        for p in problems:
            print(p)
    if not acting:
        print("\n(dry-run 未做任何改动)")
    return_code = 1 if missing else 0
    sys.exit(return_code)


if __name__ == "__main__":
    main()
