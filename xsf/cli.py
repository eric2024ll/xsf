import argparse
import json
import sys
from pathlib import Path

from .config import (
    get_data_dir, get_collections_dir, list_collections,
    validate_collection_name,
)
from .db import init_db, get_conn
from .ingest import (
    ingest_pdf, ingest_scanned_pdf, ingest_office, _OFFICE_EXTS, remove_doc,
)
from .search import search, get_block_lines, get_context


def cmd_init(args):
    colls = list_collections()
    if colls:
        for coll in colls:
            init_db(coll)
        print(f'已初始化 {len(colls)} 个书架的 DB')
    else:
        print('暂无书架，首次上传时自动创建 DB')
    print(f'  数据目录: {get_data_dir()}')
    print(f'  书架目录: {get_collections_dir()}')


def cmd_add(args):
    err = validate_collection_name(args.collection)
    if err:
        print(f'错误: 书架名不合法: {err}', file=sys.stderr)
        sys.exit(1)
    pdf_path = Path(args.file)
    if not pdf_path.exists():
        print(f'错误: 文件不存在: {pdf_path}', file=sys.stderr)
        sys.exit(1)
    suffix = pdf_path.suffix.lower()
    if suffix != '.pdf' and suffix not in _OFFICE_EXTS:
        print(f'错误: 仅支持 PDF 或 Word (.doc/.docx/.docm) 文件',
              file=sys.stderr)
        sys.exit(1)
    is_office = suffix in _OFFICE_EXTS

    init_db(args.collection)

    tags = []
    if args.primary:
        tags.append('primary')
    if args.secondary:
        tags.append('secondary')
    if args.reference:
        tags.append('reference')
    if not tags:
        tags = ['primary']
    source_tags = json.dumps(tags)

    if getattr(args, 'provider', None):
        if not args.ocr:
            print('错误: --provider 仅在 --ocr 模式下有效', file=sys.stderr)
            sys.exit(1)

    if is_office and args.ocr:
        print('错误: Word 文档不支持 --ocr（anydoc 直接提取文本）',
              file=sys.stderr)
        sys.exit(1)

    try:
        if is_office:
            result = ingest_office(
                pdf_path,
                collection=args.collection,
                cite_key=args.cite_key,
                title=args.title,
                author=args.author,
                source_tags=source_tags,
            )
        elif args.ocr:
            result = ingest_scanned_pdf(
                pdf_path,
                collection=args.collection,
                cite_key=args.cite_key,
                title=args.title,
                author=args.author,
                source_tags=source_tags,
                provider_id=args.provider,
            )
        else:
            result = ingest_pdf(
                pdf_path,
                collection=args.collection,
                cite_key=args.cite_key,
                title=args.title,
                author=args.author,
                source_tags=source_tags,
            )
    except Exception as e:
        if 'UNIQUE constraint' in str(e):
            print(f'跳过: {pdf_path.name} 已在书架 [{args.collection}] 中',
                  file=sys.stderr)
            sys.exit(1)
        raise

    title = result.get('title') or pdf_path.name
    print(f'已导入: {title}')
    print(f'  书架: {args.collection}')
    print(f'  页/块/行: {result["pages"]} / {result["blocks"]} / {result["lines"]}')


def cmd_search(args):
    results = search(args.query, collection=args.collection,
                     limit=args.limit)
    if not results:
        print('未找到匹配结果')
        return

    print(f'找到 {len(results)} 条结果:\n')
    for i, r in enumerate(results, 1):
        title = r.get('title') or r.get('filename', '?')
        cite = f' [{r["cite_key"]}]' if r.get('cite_key') else ''
        print(f'[{i}] {title}{cite}')
        print(f'    书架: {args.collection} | '
              f'页 {r["page_num"]} | 段 {r["block_num"]}')

        lines = get_block_lines(r['doc_id'], r['page_num'], r['block_num'],
                                args.collection)
        text = ' '.join(lines)
        if len(text) > 200:
            text = text[:200] + '...'
        print(f'    {text}')
        print()


def cmd_context(args):
    """显示命中块的上下文"""
    lines = get_context(args.doc_id, args.page, args.block,
                        radius=args.radius, collection=args.collection)
    if not lines:
        print('未找到内容')
        return

    for line in lines:
        marker = '▶' if line['block_num'] == args.block else ' '
        print(f'  {marker} [{line["block_num"]}:{line["line_num"]}] '
              f'{line["text"]}')


def cmd_remove(args):
    remove_doc(args.doc_id, args.collection)
    print(f'已删除 doc_id={args.doc_id} (书架: {args.collection})')


def cmd_stats(args):
    colls = list_collections()
    if not colls:
        print('暂无书架')
        return

    total_docs = 0
    total_lines = 0
    total_blocks = 0

    print('小書房统计')
    for coll in colls:
        conn = get_conn(coll)
        try:
            n_docs = conn.execute(
                'SELECT COUNT(*) FROM documents').fetchone()[0]
            n_lines = conn.execute(
                'SELECT COUNT(*) FROM lines').fetchone()[0]
            n_blocks = conn.execute(
                'SELECT COUNT(*) FROM blocks_fts').fetchone()[0]
            pages = conn.execute(
                'SELECT COALESCE(SUM(page_count), 0) FROM documents'
            ).fetchone()[0]
        finally:
            conn.close()
        total_docs += n_docs
        total_lines += n_lines
        total_blocks += n_blocks
        print(f'  书架 [{coll}]: {n_docs} 篇, {pages} 页, '
              f'{n_blocks} 块, {n_lines} 行')

    print(f'\n合计: {total_docs} 文献, {total_blocks} 文本块, '
          f'{total_lines} 文本行')


def cmd_doctor(args):
    """环境自检: 数据目录来源 / 书架与 DB 一致性 / 孤儿目录 / 多实例对比.

    便携版多入口 (小書房.exe GUI / xsf.exe CLI) 与双实例 (端口顺延) 场景下,
    书架「消失」多因数据目录分裂或孤儿目录; 本命令 30 秒定案.
    """
    import os
    import socket
    import urllib.request

    from .config import get_db_dir, find_orphan_collections
    from .env import find_env_file

    print('== 数据目录 ==')
    env_file = find_env_file()
    env_vals = {}
    if env_file:
        try:
            for raw in env_file.read_text('utf-8').splitlines():
                line = raw.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    env_vals[k.strip()] = v.strip()
        except (OSError, UnicodeDecodeError):
            env_vals = {}

    def src(key: str) -> str:
        if key not in os.environ:
            return '默认'
        if key in env_vals:
            if os.environ[key] == env_vals[key]:
                return f'.env: {env_file}'
            return '环境变量 (覆盖 .env)'
        return '环境变量'

    print(f'  XSF_DATA         = {get_data_dir()}  [{src("XSF_DATA")}]')
    print(f'  XSF_DB_DIR       = {get_db_dir()}  [{src("XSF_DB_DIR")}]')
    try:
        coll_dir = get_collections_dir()
    except OSError as e:
        coll_dir = f'(不可达: {e})'
    print(f'  XSF_COLLECTIONS  = {coll_dir}  [{src("XSF_COLLECTIONS_DIR")}]')
    print(f'  .env: {env_file or "未找到"}')

    print('\n== 书架 (db/) ==')
    db_dir = get_db_dir()
    known = list_collections()
    if not known:
        print('  (无)')
    for c in known:
        try:
            size = (db_dir / c / 'xsf.db').stat().st_size
        except OSError:
            size = -1
        print(f'  {c}  {size} 字节')
    try:
        db_orphans, dir_orphans = find_orphan_collections()
    except OSError as e:
        db_orphans, dir_orphans = [], []
        print(f'  ! 孤儿目录探测失败 (目录不可达): {e}')
    for c in db_orphans:
        print(f'  ! 孤儿 DB 目录 (无有效 xsf.db, 列表不显示): {c}')
    for c in dir_orphans:
        print(f'  ! 投放孤儿 (collections/ 有目录无 DB, 文件不会被自动入库): {c}')

    print('\n== 服务实例 (127.0.0.1 本机端口) ==')
    try:
        base = int(os.environ.get('XSF_PORT', '8090'))
    except ValueError:
        base = 8090
    local = set(known)
    found = False
    for port in range(base, base + 10):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                pass
        except OSError:
            continue
        found = True
        try:
            url = f'http://127.0.0.1:{port}/api/collections'
            with urllib.request.urlopen(url, timeout=2) as r:
                remote = set(json.loads(r.read()).get('collections', []))
        except Exception as e:
            print(f'  :{port} 在监听, 但 /api/collections 不可达 ({e})')
            continue
        if remote == local:
            print(f'  :{port} 书架与本机 db/ 一致 ({len(remote)} 个)')
        else:
            print(f'  ! :{port} 与本机 db/ 不一致 (该实例用了别的数据目录)')
            for c in sorted(remote - local):
                print(f'      仅该实例有: {c}')
            for c in sorted(local - remote):
                print(f'      仅本机 db/ 有: {c}')
    if not found:
        print(f'  ({base}~{base + 9} 无监听, 服务未启动)')

    if db_orphans or dir_orphans:
        print('\n提示: 孤儿目录可在 Web 书架页用同名新建书架修复 (自动补建 DB);'
              ' 投放孤儿修复后 folder_scan 下一周期即自动认领 uploads/ 文件')


def main():
    # 便携版/桌面版: 从 exe 旁 .env 补缺失环境变量 (已有环境变量优先)
    from .env import load_env
    load_env()
    # 输出重定向 (管道/文件) 时切 UTF-8, 防非 UTF-8 默认编码下中文报错;
    # 真控制台不动 (Windows 下 PEP 528 原生支持中文)
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(
        prog='xsf', description='小書房 — 文献池检索系统')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('init', help='初始化数据库')

    p_add = sub.add_parser('add', help='添加 PDF / Word 文档')
    p_add.add_argument('file', help='PDF文件路径')
    p_add.add_argument('-c', '--collection', required=True, help='书架名')
    p_add.add_argument('--cite-key', help='关联 cite_key')
    p_add.add_argument('--title', help='标题')
    p_add.add_argument('--author', help='作者')
    p_add.add_argument('--ocr', action='store_true',
                       help='扫描件OCR（vl_api provider）')
    p_add.add_argument('--provider',
                       help='OCR provider id（默认用 OCR 设置里的 default）')
    p_add.add_argument('--primary', dest='primary', action='store_true',
                       default=True, help='原始史料（默认）')
    p_add.add_argument('--no-primary', dest='primary', action='store_false',
                       help='取消原始史料标记')
    p_add.add_argument('--secondary', action='store_true', default=False,
                       help='标记为研究文献')
    p_add.add_argument('--reference', action='store_true', default=False,
                       help='标记为工具书')

    p_s = sub.add_parser('search', help='全文搜索')
    p_s.add_argument('query', help='搜索词')
    p_s.add_argument('-c', '--collection', required=True, help='限定书架')
    p_s.add_argument('-n', '--limit', type=int, default=20)

    p_ctx = sub.add_parser('context', help='查看命中块上下文')
    p_ctx.add_argument('doc_id', type=int)
    p_ctx.add_argument('page', type=int)
    p_ctx.add_argument('block', type=int)
    p_ctx.add_argument('-c', '--collection', required=True, help='书架名')
    p_ctx.add_argument('-r', '--radius', type=int, default=1)

    p_rm = sub.add_parser('remove', help='删除文献')
    p_rm.add_argument('doc_id', type=int)
    p_rm.add_argument('-c', '--collection', required=True, help='书架名')

    sub.add_parser('stats', help='统计信息')
    sub.add_parser('doctor', help='环境自检: 数据目录/书架一致性/孤儿目录/多实例')

    args = parser.parse_args()

    dispatch = {
        'init': cmd_init, 'add': cmd_add, 'search': cmd_search,
        'context': cmd_context, 'remove': cmd_remove, 'stats': cmd_stats,
        'doctor': cmd_doctor,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
