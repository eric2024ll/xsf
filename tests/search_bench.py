#!/usr/bin/env python3
"""检索防回归 bench: 固定题集快照 / diff / 边界用例。

用法:
  python tests/search_bench.py snapshot --base http://127.0.0.1:8099 \
      --out tests/baselines/<名字>.json
  python tests/search_bench.py diff tests/baselines/a.json tests/baselines/b.json
  python tests/search_bench.py boundary --base http://127.0.0.1:8099

题集设计 (2026-11-05, 与检索体系设计批 A 同步建):
  覆盖 4 书架 × 繁/简/2 字短词/3+ 字词/实体名/译文词/繁简对照。
  快照记 total + top10 (doc/page/block/文本前 30 字) + 分组命中计数。
  任何一步改动后 diff: 命中集不变 (或变化可解释) 才算过。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

# ── 固定题集 (勿随意改动; 扩题只能追加, 保持历史可比) ──
CASES = [
    # 繁体 2 字 (LIKE 路径) / 繁简对照 / 3+ 字 (FTS 短语)
    {'shelf': '晚清乡土志', 'q': '學堂'},
    {'shelf': '晚清乡土志', 'q': '学堂'},          # 简体对照 → 变体扩展
    {'shelf': '晚清乡土志', 'q': '上谷郡'},
    {'shelf': '晚清乡土志', 'q': '居庸'},
    {'shelf': '晚清乡土志', 'q': '故城'},
    {'shelf': '晚清乡土志', 'q': '簡筆'},
    # 考据: 正文 + 译文 (trans 列)
    {'shelf': '考据', 'q': '乾嘉考据学'},
    {'shelf': '考据', 'q': '布洛赫'},              # 期望经 trans 命中
    {'shelf': '考据', 'q': '历史导论'},
    # 两岸三交
    {'shelf': '两岸三交', 'q': '臺灣生番'},
    {'shelf': '两岸三交', 'q': '中央研究院'},
    # ai与历史研究综合: 中文人名 / 拉丁实体名
    {'shelf': 'ai与历史研究综合', 'q': '史学集刊'},
    {'shelf': 'ai与历史研究综合', 'q': '包伟民'},
    {'shelf': 'ai与历史研究综合', 'q': 'Marc Bloch'},
    {'shelf': 'ai与历史研究综合', 'q': 'Berlin'},
]

# ── 过滤器用例 (批 A2 起追加): 只增不改, diff 双方都有才比较 ──
FILTER_CASES = [
    {'shelf': '晚清乡土志', 'q': '學堂',
     'params': {'exclude': 'header,page_number,footer,number'}},
    {'shelf': '晚清乡土志', 'q': '學堂',
     'params': {'label': 'text,vertical_text'}},
    {'shelf': '晚清乡土志', 'q': '上谷郡',
     'params': {'source_type': 'primary'}},
    {'shelf': '考据', 'q': '乾嘉考据学',
     'params': {'label': 'text'}},
    {'shelf': 'ai与历史研究综合', 'q': '包伟民',
     'params': {'year_from': 2020, 'year_to': 2026}},
    # A3: 批注繁简变体 — clip_text 是简体「乾嘉考据学研究」, 繁体查询须命中
    {'shelf': '考据', 'q': '考據',
     'params': {}},
    # B1: OCR 混淆扩展 — 戊戌 应覆盖语料中的错字「戊戍」(526+1)
    {'shelf': '晚清乡土志', 'q': '戊戌',
     'params': {}},
    # B2b: wiki 命中组繁简变体 — 繁体查询命中简体概念页 (3 页)
    {'shelf': '考据', 'q': '史學方法論',
     'params': {}},
    # B2: 实体 names 拆表繁简变体 — 大語言模型(繁) → 大语言模型
    {'shelf': 'ai与历史研究综合', 'q': '大語言模型',
     'params': {}},
]

# ── 边界用例: 断言期望状态码 (期望栏按批 A 之后的目标填写) ──
BOUNDARY = [
    # (shelf, q, 批A后期望状态)
    ('晚清乡土志', 'a"b', 200),            # FTS 语法字符 → 净化后 200
    ('晚清乡土志', '', 200),               # 空查询 → 空结果不崩
    ('晚清乡土志', '(', 200),              # 括号 → 净化后 200
    ('晚清乡土志', 'a OR b', 200),         # FTS 操作符混入 → 净化后 200
    ('晚清乡土志', r'%_\^:', 200),         # LIKE/FTS 元字符
    ('不存在书架xyz', '测试', 400),        # 坏书架 → 400 (原 500)
    ('测试', '测试', 400),                 # 空壳库 → 400 (原 500)
]

TIMEOUT = 60


def _get(base, path, timeout=TIMEOUT):
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode('utf-8'))
        except Exception:
            body = {}
        return e.code, body


def run_case(base, shelf, q, extra=None):
    """跑单题: /search 取 total+top10, /search-grouped 取分组计数。"""
    coll = urllib.parse.quote(shelf)
    qq = urllib.parse.quote(q)
    t0 = time.time()
    qs = f'q={qq}&limit=10'
    if extra:
        qs += '&' + urllib.parse.urlencode(extra)
    code, d = _get(base, f'/collections/{coll}/search?{qs}')
    dt = time.time() - t0
    rec = {'shelf': shelf, 'q': q, 'params': extra or {},
           'status': code, 'seconds': round(dt, 2)}
    if code == 200:
        rec['total'] = d.get('total', 0)
        rec['top'] = [
            {'doc': r.get('doc_id'), 'page': r.get('page_num'),
             'block': r.get('block_num'),
             'text': (r.get('text') or '')[:30]}
            for r in (d.get('results') or [])[:10]
        ]
        _, g = _get(base, f'/collections/{coll}/search-grouped?q={qq}'
                    + ('&' + urllib.parse.urlencode(extra) if extra else '')
                    + '&limit=5')
        if isinstance(g, dict):
            rec['groups'] = {
                'docs': len(g.get('docs') or g.get('results') or []),
                'cards': len(g.get('cards') or []),
                'wiki': len(g.get('wiki') or []),
                'entities': len(g.get('entities') or []),
                'annotations': len(g.get('annotations') or []),
            }
    else:
        rec['error'] = str(d.get('error') or d)[:120]
    return rec


def cmd_snapshot(args):
    out = {'created': time.strftime('%Y-%m-%d %H:%M:%S'),
           'base': args.base, 'cases': [], 'filters': []}
    for c in CASES:
        rec = run_case(args.base, c['shelf'], c['q'])
        out['cases'].append(rec)
        print(f"[{rec['status']}] {c['shelf']} · {c['q']} → "
              f"total={rec.get('total')} {rec.get('seconds')}s")
    for c in FILTER_CASES:
        rec = run_case(args.base, c['shelf'], c['q'], c['params'])
        out['filters'].append(rec)
        print(f"[{rec['status']}] 滤 {c['shelf']} · {c['q']} {c['params']} → "
              f"total={rec.get('total')} {rec.get('seconds')}s")
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('快照已写:', args.out)
    return 0


def cmd_diff(args):
    a = json.load(open(args.a, encoding='utf-8'))
    b = json.load(open(args.b, encoding='utf-8'))

    def cmp(section):
        am = {(c['shelf'], c['q'], json.dumps(c.get('params') or {}, sort_keys=True)): c
              for c in (a.get(section) or [])}
        bm = {(c['shelf'], c['q'], json.dumps(c.get('params') or {}, sort_keys=True)): c
              for c in (b.get(section) or [])}
        changed = 0
        for key in sorted(set(am) | set(bm)):
            ca, cb = am.get(key), bm.get(key)
            if ca is None or cb is None:
                if section == 'cases':   # filters 段可后建, 只提示不判变化
                    print(f'  仅一侧存在: {key[0]} · {key[1]}')
                    changed += 1
                continue
            fa = (ca.get('status'), ca.get('total'),
                  json.dumps(ca.get('top'), ensure_ascii=False, sort_keys=True))
            fb = (cb.get('status'), cb.get('total'),
                  json.dumps(cb.get('top'), ensure_ascii=False, sort_keys=True))
            if fa != fb:
                print(f'  变化[{section}]: {key[0]} · {key[1]} {key[2]}: '
                      f'status {ca.get("status")}→{cb.get("status")}, '
                      f'total {ca.get("total")}→{cb.get("total")}')
                ta = {(t['doc'], t['page'], t['block']) for t in ca.get('top') or []}
                tb = {(t['doc'], t['page'], t['block']) for t in cb.get('top') or []}
                if ta - tb: print(f'    top10 减少: {sorted(ta - tb)[:3]}')
                if tb - ta: print(f'    top10 新增: {sorted(tb - ta)[:3]}')
                changed += 1
        return changed

    changed = cmp('cases') + cmp('filters')
    print('结论:', '有变化, 逐条核对可解释性' if changed else '快照一致 ✅')
    return 1 if changed else 0


def cmd_boundary(args):
    ok = True
    for shelf, q, expect in BOUNDARY:
        coll = urllib.parse.quote(shelf)
        code, d = _get(args.base, f'/collections/{coll}/search?'
                       f'q={urllib.parse.quote(q)}&limit=5')
        mark = '✅' if code == expect else '❌'
        if code != expect:
            ok = False
        err = str(d.get('error') or '')[:60] if isinstance(d, dict) else ''
        print(f'{mark} [{code}] {shelf} · {q!r} (期望 {expect}) {err}')
    print('边界用例:', '全部通过 ✅' if ok else '有未达标 ❌')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('--base', default='http://127.0.0.1:8099')
    s.add_argument('--out', required=True)
    d = sub.add_parser('diff')
    d.add_argument('a'); d.add_argument('b')
    b = sub.add_parser('boundary')
    b.add_argument('--base', default='http://127.0.0.1:8099')
    args = ap.parse_args()
    sys.exit({'snapshot': cmd_snapshot, 'diff': cmd_diff,
              'boundary': cmd_boundary}[args.cmd](args))


if __name__ == '__main__':
    main()
