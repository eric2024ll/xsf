import argparse
import sys
from pathlib import Path

from .config import get_data_dir, get_collections_dir
from .db import init_db, get_conn
from .ingest import ingest_pdf, ingest_scanned_pdf, remove_doc
from .search import search, get_block_lines, get_context


def cmd_init(args):
    init_db()
    print('架阁已初始化')
    print(f'  数据目录: {get_data_dir()}')
    print(f'  书架目录: {get_collections_dir()}')

    conn = get_conn()
    rows = conn.execute(
        '''SELECT collection, COUNT(*) as cnt
           FROM documents GROUP BY collection ORDER BY cnt DESC'''
    ).fetchall()
    conn.close()
    if rows:
        print('\n已有书架:')
        for r in rows:
            print(f'  {r["collection"]}: {r["cnt"]} 篇')


def cmd_add(args):
    pdf_path = Path(args.file)
    if not pdf_path.exists():
        print(f'错误: 文件不存在: {pdf_path}', file=sys.stderr)
        sys.exit(1)
    if not pdf_path.suffix.lower() == '.pdf':
        print(f'错误: 仅支持 PDF 文件', file=sys.stderr)
        sys.exit(1)

    try:
        ingest_func = ingest_scanned_pdf if args.ocr else ingest_pdf
        result = ingest_func(
            pdf_path,
            collection=args.collection,
            cite_key=args.cite_key,
            title=args.title,
            author=args.author,
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
        print(f'    书架: {r["collection"]} | '
              f'页 {r["page_num"]} | 段 {r["block_num"]}')

        lines = get_block_lines(r['doc_id'], r['page_num'], r['block_num'])
        text = ' '.join(lines)
        if len(text) > 200:
            text = text[:200] + '...'
        print(f'    {text}')
        print()


def cmd_context(args):
    """显示命中块的上下文"""
    lines = get_context(args.doc_id, args.page, args.block,
                        radius=args.radius)
    if not lines:
        print('未找到内容')
        return

    for line in lines:
        marker = '▶' if line['block_num'] == args.block else ' '
        print(f'  {marker} [{line["block_num"]}:{line["line_num"]}] '
              f'{line["text"]}')


def cmd_remove(args):
    remove_doc(args.doc_id)
    print(f'已删除 doc_id={args.doc_id}')


def cmd_stats(args):
    conn = get_conn()
    try:
        n_docs = conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        n_lines = conn.execute('SELECT COUNT(*) FROM lines').fetchone()[0]
        n_blocks = conn.execute(
            'SELECT COUNT(*) FROM blocks_fts'
        ).fetchone()[0]

        print('架阁统计')
        print(f'  文献: {n_docs}')
        print(f'  文本块: {n_blocks}')
        print(f'  文本行: {n_lines}')

        colls = conn.execute(
            '''SELECT collection, COUNT(*) as cnt,
                      SUM(page_count) as pages
               FROM documents GROUP BY collection ORDER BY cnt DESC'''
        ).fetchall()
        if colls:
            print('\n书架:')
            for c in colls:
                print(f'  {c["collection"]}: {c["cnt"]} 篇, '
                      f'{c["pages"] or 0} 页')
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog='jiage', description='架阁 — 文献池检索系统')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('init', help='初始化数据库')

    p_add = sub.add_parser('add', help='添加PDF')
    p_add.add_argument('file', help='PDF文件路径')
    p_add.add_argument('-c', '--collection', required=True, help='书架名')
    p_add.add_argument('--cite-key', help='关联 cite_key')
    p_add.add_argument('--title', help='标题')
    p_add.add_argument('--author', help='作者')
    p_add.add_argument('--ocr', action='store_true',
                       help='扫描件OCR（PaddleOCR-VL）')

    p_s = sub.add_parser('search', help='全文搜索')
    p_s.add_argument('query', help='搜索词')
    p_s.add_argument('-c', '--collection', help='限定书架')
    p_s.add_argument('-n', '--limit', type=int, default=20)

    p_ctx = sub.add_parser('context', help='查看命中块上下文')
    p_ctx.add_argument('doc_id', type=int)
    p_ctx.add_argument('page', type=int)
    p_ctx.add_argument('block', type=int)
    p_ctx.add_argument('-r', '--radius', type=int, default=1)

    p_rm = sub.add_parser('remove', help='删除文献')
    p_rm.add_argument('doc_id', type=int)

    sub.add_parser('stats', help='统计信息')

    args = parser.parse_args()

    dispatch = {
        'init': cmd_init, 'add': cmd_add, 'search': cmd_search,
        'context': cmd_context, 'remove': cmd_remove, 'stats': cmd_stats,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
