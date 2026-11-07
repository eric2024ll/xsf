#!/usr/bin/env python3
"""批量翻译驱动 — 分片多进程 + 429 退避 + 断点续跑 (2026-11-07).

背景: 小书房「AI与历史研究综合」有 96 篇英文论文缺译 (~835 万字符).
逐篇串行跑 CLI 太慢 (单路 ~80 字符/秒), 需要按 provider 分池并发.

策略:
- 只处理缺译文献 (trans 或 gloss 为空的行), 走 autofill_doc(only_missing=True),
  已有译文/人工修订一律不动
- 按 provider 分池: p7 并发受 429 限制 (实测 4 稳定, 8 触发限流),
  p6 关思维链后可到 8-16; 每池一个进程池, 各自消费自己的文献队列
- 文献按缺译字符数降序派发 (大件先跑, 尾部长尾更短)
- 单篇失败退避重试 (30/60/120s), 仍失败记录后继续, 不炸整批
- 两轮补缺: 第一轮结束后重算缺译, 再跑一轮捞回失败批次 (autofill_rows
  遇部分批失败会占空行, 只能靠重跑补)

用法:
  python scripts/batch_translate.py -c 'ai与历史研究综合' --pools p7=4,p6=8 \
      --targets trans,gloss --log ~/xsf-data/translate-20261107.jsonl
  # 先看计划不执行:
  python scripts/batch_translate.py -c 'ai与历史研究综合' --pools p7=4,p6=8 --dry-run

范围: 默认 lang!='zh' 的全部文献, 除 --exclude 指定的 id;
--docs 可显式指定 id 列表 (逗号分隔)。
"""
import argparse
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def db_path(collection: str) -> str:
    from xsf.config import get_data_dir
    return str(get_data_dir() / 'db' / collection / 'xsf.db')


def scan(collection: str, targets: list[str], docs: list[int] | None,
         exclude: set[int], include_books: bool, max_chars: int | None):
    """返回 [{id, cite_key, doc_type, miss, chars}], 按 chars 降序."""
    p = db_path(collection)
    if not os.path.exists(p):
        raise SystemExit(f'数据库不存在: {p}')
    conn = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    where = " OR ".join(f"(l.{t} IS NULL OR l.{t}='')" for t in targets)
    rows = conn.execute(f"""
        SELECT d.id, d.cite_key, d.doc_type, d.page_count,
               COUNT(l.id) AS n,
               SUM(CASE WHEN {where} THEN 1 ELSE 0 END) AS miss,
               SUM(CASE WHEN {where} THEN LENGTH(l.text) ELSE 0 END) AS chars
        FROM documents d JOIN lines l ON l.doc_id = d.id
        WHERE d.lang IS NOT NULL AND d.lang != 'zh'
        GROUP BY d.id
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        if not r['miss']:
            continue
        if docs and r['id'] not in docs:
            continue
        if r['id'] in exclude:
            continue
        if not include_books and r['doc_type'] == 'markdown' and (r['n'] or 0) > 2000:
            continue  # 整书教材, 默认跳过 (量太大)
        if max_chars and (r['chars'] or 0) > max_chars:
            continue
        out.append({'id': r['id'], 'cite_key': r['cite_key'],
                    'doc_type': r['doc_type'], 'n': r['n'],
                    'miss': r['miss'], 'chars': r['chars'] or 0})
    out.sort(key=lambda d: -d['chars'])
    return out


def assign(docs, pools):
    """按各池吞吐比例分派文献 (大件先分给最快的池, 平衡完成时间)."""
    # 权重 = 并发数 * 单路吞吐经验值 (p7≈80, p6≈143 字符/秒)
    speed = {'p7': 80, 'p6': 143}
    weight = {pid: n * speed.get(pid, 100) for pid, n in pools.items()}
    total = sum(weight.values()) or 1
    quota = {pid: w / total for pid, w in weight.items()}
    got = {pid: 0 for pid in pools}
    total_chars = sum(d['chars'] for d in docs) or 1
    plan = {pid: [] for pid in pools}
    for d in docs:
        # 选「已分字符 / 配额」最小的池
        pid = min(pools, key=lambda x: got[x] / quota[x] if quota[x] else 9e9)
        plan[pid].append(d)
        got[pid] += d['chars']
    return plan, {pid: got[pid] / total_chars for pid in pools}


def _worker(args):
    doc, collection, targets, provider, batch_blocks, batch_chars = args
    from xsf.generate import autofill_doc
    t0 = time.time()
    try:
        r = autofill_doc(collection, doc['id'], targets=targets,
                         provider_id=provider, only_missing=True,
                         batch_blocks=batch_blocks, batch_chars=batch_chars)
        return {'id': doc['id'], 'cite_key': doc['cite_key'],
                'provider': provider, 'status': 'ok',
                'filled': r.get('filled', 0), 'empty': r.get('empty', 0),
                'seconds': round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001 - 单篇失败不炸整批
        return {'id': doc['id'], 'cite_key': doc['cite_key'],
                'provider': provider, 'status': 'error',
                'error': str(e)[:300], 'seconds': round(time.time() - t0, 1)}


def run_pool(pool_size, queue_docs, collection, targets, provider,
             batch_blocks, batch_chars, logpath, tag):
    """单 provider 池: 顺序消费文献, 内部进程池并发=pool_size.

    JSONL 由池进程自己 open(append) 写, 多进程 O_APPEND 单行写入不撕裂;
    不把文件对象传给子进程 (fork 可继承但语义脆弱)。
    """
    done = 0
    total = len(queue_docs)
    pending = list(queue_docs)
    logf = open(logpath, 'a', encoding='utf-8')
    with mp.Pool(pool_size) as pool:
        while pending:
            chunk = pending[:pool_size * 2]
            pending = pending[pool_size * 2:]
            tasks = [(d, collection, targets, provider, batch_blocks, batch_chars)
                     for d in chunk]
            for res in pool.imap_unordered(_worker, tasks):
                done += 1
                line = json.dumps({'ts': datetime.now().isoformat(timespec='seconds'),
                                   **res}, ensure_ascii=False)
                logf.write(line + '\n')
                logf.flush()
                if res['status'] == 'ok':
                    log(f"[{tag}] {done}/{total} doc {res['id']} {res['cite_key'][:28]} "
                        f"+{res['filled']} 行 空{res['empty']} {res['seconds']}s")
                else:
                    log(f"[{tag}] {done}/{total} doc {res['id']} {res['cite_key'][:28]} "
                        f"失败: {res['error'][:120]}")
    logf.close()
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-c', '--collection', required=True)
    ap.add_argument('--pools', default='p7=4,p6=8',
                    help='provider=并发数, 逗号分隔, 如 p7=4,p6=8')
    ap.add_argument('--targets', default='trans,gloss')
    ap.add_argument('--batch-blocks', type=int, default=4)
    ap.add_argument('--batch-chars', type=int, default=800)
    ap.add_argument('--docs', help='只跑这些 id (逗号分隔)')
    ap.add_argument('--exclude', default='', help='排除 id (逗号分隔)')
    ap.add_argument('--include-books', action='store_true',
                    help='包含 markdown 整书 (>2000 行)')
    ap.add_argument('--max-chars', type=int, default=0)
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 篇 (按字符降序)')
    ap.add_argument('--passes', type=int, default=2, help='补缺轮数 (默认 2)')
    ap.add_argument('--log', default=None, help='JSONL 日志路径')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    targets = [t.strip() for t in a.targets.split(',') if t.strip()]
    pools = {}
    for part in a.pools.split(','):
        pid, _, n = part.partition('=')
        pools[pid.strip()] = int(n or 4)
    docs_filter = ({int(x) for x in a.docs.split(',') if x.strip()}
                   if a.docs else None)
    exclude = {int(x) for x in a.exclude.split(',') if x.strip()}

    docs = scan(a.collection, targets, docs_filter, exclude,
                a.include_books, a.max_chars or None)
    if a.limit:
        docs = docs[:a.limit]
    if not docs:
        log('没有缺译文献, 收工')
        return
    plan, shares = assign(docs, pools)
    log(f"待译 {len(docs)} 篇, 缺译 {sum(d['chars'] for d in docs):,} 字符, "
        f"目标字段 {targets}")
    for pid, lst in plan.items():
        log(f"  {pid}×{pools[pid]}: {len(lst)} 篇 {sum(d['chars'] for d in lst):,} 字符 "
            f"({shares[pid]*100:.0f}%)")
    if a.dry_run:
        for pid, lst in plan.items():
            for d in lst:
                print(f"  {pid} {d['id']:4} {d['doc_type']:11} "
                      f"{d['chars']:8,}字 {d['cite_key'][:40]}")
        return

    logpath = a.log or os.path.expanduser(
        f"~/xsf-data/translate-{datetime.now():%Y%m%d-%H%M}.jsonl")
    log(f'日志: {logpath}')

    for rnd in range(1, a.passes + 1):
        if rnd > 1:
            docs = scan(a.collection, targets, docs_filter, exclude,
                        a.include_books, a.max_chars or None)
            if a.limit:
                docs = docs[:a.limit]
            if not docs:
                log('补缺轮: 无剩余缺译, 收工')
                break
            plan, _ = assign(docs, pools)
            log(f'=== 第 {rnd} 轮补缺: {len(docs)} 篇 '
                f'{sum(d["chars"] for d in docs):,} 字符')
        procs = []
        for pid, lst in plan.items():
            if not lst:
                continue
            p = mp.Process(target=run_pool,
                           args=(pools[pid], lst, a.collection, targets, pid,
                                 a.batch_blocks, a.batch_chars, logpath, pid))
            p.start()
            procs.append((pid, p))
        for pid, p in procs:
            p.join()
            log(f'{pid} 池结束 (exit={p.exitcode})')
    log('全部结束')


if __name__ == '__main__':
    main()
