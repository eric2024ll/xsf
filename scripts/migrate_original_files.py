#!/usr/bin/env python3
"""存量原件对账迁移 (2026-10-28 原件统一模型).

对每书架逐文档:
  1. linked_pdf 旧格式 (uploads/ 文件名, 含 .snap-N.pdf) → 文件移入
     files/<id>/original.<ext>, linked_pdf 更新为 files/<id>/ 原件相对路径
  2. linked_pdf 为空但 uploads/<filename> 存在 → 同上移入
  3. 仍无原件 → original_virtual=1 (bib-only 虚拟文档)
迁移只 move 不复制 (uploads 不留双份); 跑之前建议备份 db 与 collections。

用法:
    .venv/bin/python scripts/migrate_original_files.py [-c 书架名] [--dry-run]

设计: mem 小书房/完善路线图 (2026-10-28 用户裁定, 废除 linked_pdf 旧逻辑,
统一「左原件右解析」)。
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xsf.config import get_collections_dir, get_db_dir, list_collections  # noqa: E402


def migrate_collection(name: str, dry_run: bool) -> dict:
    croot = get_collections_dir() / name
    dbp = get_db_dir() / name / 'xsf.db'
    if not dbp.exists():
        return {'skip': f'{name}: 无 DB'}
    updir = croot / 'uploads'
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
    if 'original_virtual' not in cols:
        conn.execute('ALTER TABLE documents ADD COLUMN original_virtual INTEGER DEFAULT 0')
        conn.commit()
    rows = conn.execute(
        'SELECT id, filename, linked_pdf, original_virtual FROM documents'
    ).fetchall()
    stats = {'moved': 0, 'virtual': 0, 'kept': 0, 'warn': []}
    for r in rows:
        did = r['id']
        lp = (r['linked_pdf'] or '').strip()
        fdir = croot / 'files' / str(did)
        # 0) 已是新格式 → 核对文件在即计 kept
        if lp.startswith('files/'):
            if (croot / lp).is_file():
                stats['kept'] += 1
            else:
                stats['warn'].append(f'#{did} linked_pdf={lp} 文件缺失')
            continue
        # 1) 旧格式: uploads/<linked_pdf>
        src = None
        if lp:
            p = updir / lp
            if p.is_file():
                src = p
            else:
                stats['warn'].append(f'#{did} linked_pdf={lp} 文件缺失(旧格式)')
        # 2) 无 linked_pdf → uploads/<filename> 对账
        if src is None:
            fn = r['filename'] or ''
            p = updir / fn
            if fn and p.is_file():
                src = p
        if src is not None:
            ext = src.suffix.lower() or '.pdf'
            dst_dir = croot / 'files' / str(did)
            rel = f'files/{did}/original{ext}'
            dst = croot / rel
            if dry_run:
                stats['moved'] += 1
                continue
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            conn.execute(
                'UPDATE documents SET linked_pdf=?, original_virtual=0 WHERE id=?',
                (rel, did))
            stats['moved'] += 1
            continue
        # 3) 无原件 → virtual
        if not r['original_virtual']:
            if not dry_run:
                conn.execute(
                    'UPDATE documents SET original_virtual=1 WHERE id=?', (did,))
            stats['virtual'] += 1
    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description='存量原件对账迁移')
    ap.add_argument('-c', '--collection', help='只跑指定书架')
    ap.add_argument('--dry-run', action='store_true', help='只报告不落盘')
    args = ap.parse_args()
    names = [args.collection] if args.collection else list_collections()
    total = {'moved': 0, 'virtual': 0, 'kept': 0, 'warn': 0}
    for n in names:
        s = migrate_collection(n, args.dry_run)
        if 'skip' in s:
            print(f'== {n}: {s["skip"]}')
            continue
        print(f'== {n}: 移入 {s["moved"]}, 转虚拟 {s["virtual"]}, '
              f'已是新格式 {s["kept"]}, 警告 {len(s["warn"])}')
        for w in s['warn'][:10]:
            print(f'   ! {w}')
        total['moved'] += s['moved']
        total['virtual'] += s['virtual']
        total['kept'] += s['kept']
        total['warn'] += len(s['warn'])
    act = '将移入' if args.dry_run else '已移入'
    print(f'\n合计: {act} {total["moved"]}, 虚拟 {total["virtual"]}, '
          f'kept {total["kept"]}, 警告 {total["warn"]}')


if __name__ == '__main__':
    main()
