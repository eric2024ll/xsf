#!/usr/bin/env python3
"""小書房 — Collection 导入导出 CLI

用法:
  python scripts/collection_io.py export <collection> [-o output.tar.gz]
  python scripts/collection_io.py import <collection> <archive.tar.gz> \
      [--conflict skip|overwrite] [--db-only] [--force]

数据结构:
  db/{collection}/xsf.db           — per-collection SQLite（本地磁盘）
  collections/{collection}/uploads/{filename} — 源文件（按书架分目录，服务器指 OSS）

打包格式 (tar.gz):
  xsf.db
  manifest.json                     — collection 名、导出时间、文献数、filename 列表
  uploads/{filename}                — 该 collection 的全部源文件

兼容: 导入时同时接受 xsf.db 与旧名 jiage.db（2026-08 改名前的归档）。
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

# 确保 xsf 包可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.env import load_env  # noqa: E402
load_env()  # XSF_COLLECTIONS_DIR 等来自 ~/xsf/.env (修复: 此前缺载,
#            无环境变量时 collections 落回本地默认, 导出静默空包)
from xsf.config import (  # noqa: E402
    get_db_path, get_collections_dir, resolve_original_path,
)


# ── export ──────────────────────────────────────────────

def cmd_export(collection: str, output: str | None):
    db_path = get_db_path(collection)
    if not db_path.exists():
        print(f"错误: collection「{collection}」的数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    # 查 DB 获取文献数 + 原件路径 (单布局 2026-10-31: files/<id>/)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        rows = conn.execute(
            "SELECT id, filename, linked_pdf FROM documents"
        ).fetchall()
    finally:
        conn.close()

    # 原件解析 + 归档名 (单布局 2026-10-31): 原件一律 files/<id>/;
    # 扩展名一致按 filename 归档, 错配 (md 挂 pdf 原件等) 按
    # {id}-{原件名}; 无原件 → NULL+virtual
    def _ext_lower(name):
        n = name or ""
        return n.rsplit(".", 1)[-1].lower() if "." in n else ""

    items = []   # (row, 归档名 | None, 原件路径)
    for r in rows:
        p = resolve_original_path(collection, r["linked_pdf"])
        if p and p.is_file():
            arc = (r["filename"] if _ext_lower(r["filename"]) == _ext_lower(p.name)
                   else f"{r['id']}-{p.name}")
            items.append((r, arc, p))
        else:
            items.append((r, None, None))

    # 用 SQLite backup API 导出一致性快照
    manifest = {
        "collection": collection,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "doc_count": doc_count,
        "filenames": [arc for _r, arc, _p in items if arc],
    }

    out = output or f"{collection}_{datetime.now():%Y%m%d}.tar.gz"
    out_path = Path(out).resolve()
    found, missing = 0, []

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)

        # DB 快照（backup API 确保 WAL 一致性）
        snapshot = tdp / "xsf.db"
        src_conn = sqlite3.connect(str(db_path))
        dst_conn = sqlite3.connect(str(snapshot))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # 快照 DB 归一化: linked_pdf = 归档内实际文件名 (裸名, 包自洽)
        norm = sqlite3.connect(str(snapshot))
        try:
            for r, arc, _p in items:
                if arc:
                    norm.execute(
                        "UPDATE documents SET linked_pdf = ? WHERE id = ?",
                        (arc, r["id"]))
                else:
                    norm.execute(
                        "UPDATE documents SET linked_pdf = NULL, "
                        "original_virtual = 1 WHERE id = ?",
                        (r["id"],))
            norm.commit()
        finally:
            norm.close()

        # manifest
        (tdp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 源文件: 原件归档为 uploads/ 平铺 (石渠兼容, 格式不变)
        files_tmp = tdp / "uploads"
        files_tmp.mkdir()
        for r, arc, p in items:
            if arc and p:
                shutil.copy2(p, files_tmp / arc)
                found += 1
            elif not arc:
                missing.append(r["filename"])

        # 打包
        with tarfile.open(str(out_path), "w:gz") as tar:
            tar.add(str(snapshot), arcname="xsf.db")
            tar.add(str(tdp / "manifest.json"), arcname="manifest.json")
            if files_tmp.iterdir():
                tar.add(str(files_tmp), arcname="uploads")

    print(f"导出完成: {out_path}")
    print(f"  collection: {collection}")
    print(f"  文献数: {doc_count}")
    print(f"  源文件: {found} 个已打包")
    if missing:
        print(f"  ⚠ 缺失文件 {len(missing)} 个（DB 有记录但文件不存在）:")
        for fn in missing:
            print(f"      {fn}")


# ── import ──────────────────────────────────────────────

def cmd_import(collection: str, archive: str, conflict: str,
               db_only: bool, force: bool):
    archive_path = Path(archive).resolve()
    if not archive_path.exists():
        print(f"错误: 归档文件不存在: {archive_path}", file=sys.stderr)
        sys.exit(1)

    db_path = get_db_path(collection)
    if db_path.exists() and not force:
        print(
            f"错误: collection「{collection}」已存在: {db_path}\n"
            f"  用 --force 覆盖",
            file=sys.stderr,
        )
        sys.exit(1)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with tarfile.open(str(archive_path), "r:gz") as tar:
            tar.extractall(str(tdp))

        # 读 manifest
        manifest_path = tdp / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            src_collection = manifest.get("collection", "?")
            if src_collection != collection:
                print(
                    f"⚠ 归档 collection 名「{src_collection}」与目标「{collection}」不一致"
                )
        else:
            manifest = {}

        # 导入 DB（兼容旧归档中的 jiage.db）
        snapshot = tdp / "xsf.db"
        if not snapshot.exists():
            snapshot = tdp / "jiage.db"
        if not snapshot.exists():
            print("错误: 归档中缺少 xsf.db", file=sys.stderr)
            sys.exit(1)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        shutil.copy2(snapshot, db_path)
        print(f"DB 导入: {db_path}")

        if db_only:
            print("（--db-only，跳过源文件）")
            return

        # 导入源文件 (单布局 2026-10-31): 归档 uploads/ 平铺文件按
        # 裸名 linked_pdf / filename 配对恢复库文档 (整库回放, id 保留),
        # 落盘 files/<id>/original.<ext> 并回写; 配不上的留在 uploads/
        # 投放暂存, folder_scan 下一周期认领为新文献。
        croot = get_collections_dir()
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        src_files_dir = tdp / "uploads"
        attached = unmatched = skipped_f = overwritten = 0
        try:
            files = sorted(src_files_dir.iterdir()) \
                if src_files_dir.exists() else []
            for f in files:
                row = conn.execute(
                    "SELECT id FROM documents "
                    "WHERE linked_pdf = ? OR filename = ? "
                    "ORDER BY (linked_pdf = ?) DESC LIMIT 1",
                    (f.name, f.name, f.name)).fetchone()
                if not row:
                    unmatched += 1
                    staging = croot / collection / "uploads"
                    staging.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, staging / f.name)
                    continue
                did = row["id"]
                dst = (croot / collection / "files" / str(did)
                       / f"original{f.suffix.lower()}")
                if dst.exists():
                    if conflict != "overwrite":
                        skipped_f += 1
                        continue
                    overwritten += 1
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                conn.execute(
                    "UPDATE documents SET linked_pdf = ?, original_virtual = 0 "
                    "WHERE id = ?",
                    (f"files/{did}/{dst.name}", did))
                attached += 1
            conn.commit()
        finally:
            conn.close()

        if not src_files_dir.exists():
            print("（归档中无 uploads 目录，跳过源文件）")
            return
        print(f"源文件: 归位 {attached}, 覆盖 {overwritten}, "
              f"跳过 {skipped_f}, 未配对入暂存 {unmatched}")
        if skipped_f:
            print("  （已有原件跳过，用 --conflict overwrite 覆盖）")


# ── main ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="小書房 Collection 导入导出工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="导出 collection 为 tar.gz")
    p_export.add_argument("collection", help="collection 名称")
    p_export.add_argument("-o", "--output", help="输出文件路径（默认 {collection}_YYYYMMDD.tar.gz）")

    p_import = sub.add_parser("import", help="从 tar.gz 导入 collection")
    p_import.add_argument("collection", help="目标 collection 名称")
    p_import.add_argument("archive", help="归档文件路径")
    p_import.add_argument("--conflict", choices=["skip", "overwrite"],
                          default="skip", help="同名源文件处理（默认 skip）")
    p_import.add_argument("--db-only", action="store_true",
                          help="只导入 DB，跳过源文件（OSS 手动迁移场景）")
    p_import.add_argument("--force", action="store_true",
                          help="覆盖已存在的 collection DB")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args.collection, args.output)
    elif args.command == "import":
        cmd_import(args.collection, args.archive, args.conflict,
                   args.db_only, args.force)


if __name__ == "__main__":
    main()
