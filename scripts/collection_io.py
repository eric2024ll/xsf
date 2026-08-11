#!/usr/bin/env python3
"""架閣 — Collection 导入导出 CLI

用法:
  python scripts/collection_io.py export <collection> [-o output.tar.gz]
  python scripts/collection_io.py import <collection> <archive.tar.gz> \
      [--conflict skip|overwrite] [--db-only] [--force]

数据结构:
  db/{collection}/jiage.db          — per-collection SQLite（本地磁盘）
  collections/uploads/{filename}    — 源文件（全局共享，服务器指 OSS）

打包格式 (tar.gz):
  jiage.db
  manifest.json                     — collection 名、导出时间、文献数、filename 列表
  uploads/{filename}                — 该 collection 的全部源文件
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

# 确保 jiage 包可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jiage.config import get_db_path, get_collections_dir  # noqa: E402


# ── export ──────────────────────────────────────────────

def cmd_export(collection: str, output: str | None):
    db_path = get_db_path(collection)
    if not db_path.exists():
        print(f"错误: collection「{collection}」的数据库不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    upload_dir = get_collections_dir() / "uploads"

    # 查 DB 获取文献数 + 源文件列表
    conn = sqlite3.connect(str(db_path))
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        rows = conn.execute(
            "SELECT DISTINCT filename FROM documents WHERE filename IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    filenames = [r[0] for r in rows]

    # 用 SQLite backup API 导出一致性快照
    manifest = {
        "collection": collection,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "doc_count": doc_count,
        "filenames": filenames,
    }

    out = output or f"{collection}_{datetime.now():%Y%m%d}.tar.gz"
    out_path = Path(out).resolve()
    found, missing = 0, []

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)

        # DB 快照（backup API 确保 WAL 一致性）
        snapshot = tdp / "jiage.db"
        src_conn = sqlite3.connect(str(db_path))
        dst_conn = sqlite3.connect(str(snapshot))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # manifest
        (tdp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 源文件
        files_tmp = tdp / "uploads"
        files_tmp.mkdir()
        for fn in filenames:
            src = upload_dir / fn
            if src.exists():
                shutil.copy2(src, files_tmp / fn)
                found += 1
            else:
                missing.append(fn)

        # 打包
        with tarfile.open(str(out_path), "w:gz") as tar:
            tar.add(str(snapshot), arcname="jiage.db")
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

        # 导入 DB
        snapshot = tdp / "jiage.db"
        if not snapshot.exists():
            print("错误: 归档中缺少 jiage.db", file=sys.stderr)
            sys.exit(1)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        shutil.copy2(snapshot, db_path)
        print(f"DB 导入: {db_path}")

        if db_only:
            print("（--db-only，跳过源文件）")
            return

        # 导入源文件
        upload_dir = get_collections_dir() / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        src_files_dir = tdp / "uploads"
        if not src_files_dir.exists():
            print("（归档中无 uploads 目录，跳过源文件）")
            return

        copied = skipped = overwritten = 0
        for f in sorted(src_files_dir.iterdir()):
            dst = upload_dir / f.name
            if dst.exists():
                if conflict == "overwrite":
                    shutil.copy2(f, dst)
                    overwritten += 1
                else:
                    skipped += 1
            else:
                shutil.copy2(f, dst)
                copied += 1

        print(f"源文件: 新增 {copied}, 覆盖 {overwritten}, 跳过 {skipped}")
        if skipped:
            print("  （同名文件已跳过，用 --conflict overwrite 覆盖）")


# ── main ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="架閣 Collection 导入导出工具",
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
