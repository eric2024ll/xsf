"""xsf.entity — 实体图层编译管线 (P8, 2026-10-18).

书架级编译层 (ontology RAG 底座): 从文献块抽取提及 → 归一 → 落
entities / entity_mentions。事实源是文献 (资产层); 本模块产物**可再生**,
不入修订史、不进语料包。

归一四级 (宁分不误合——同名误合比拆分危险):
  ① 精确   canonical / aliases 命中
  ② CBDB   人名, 命中 PID → ext_ids 确定性挂靠 + 属性继承
  ③ LLM    书架内上下文归一 (批量, 低置信即放弃)
  ④ NIL    新建实体

设计: 小书房/design/完善路线图 P8。
"""
import json
import os
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import get_mount
from .db import get_conn
from .llm import chat, _extract_json_array


# 并发度 (编译提速: 抽取批 / CBDB 查询), 可用环境变量覆盖
_EXTRACT_WORKERS = int(os.environ.get('XSF_EXTRACT_WORKERS', '8'))
_CBDB_WORKERS = int(os.environ.get('XSF_CBDB_WORKERS', '6'))
_CBDB_CACHE = {}


# ── CBDB resolver: **显式挂载** (用户决定是否挂载并告知位置) ──
# 未挂载 → 该能力降级, 实体归一退化为纯 LLM。不自动探测路径。
def _cbdb_py():
    """返回挂载的 cbdb 入口脚本绝对路径; 未挂载/无效 → None。"""
    m = get_mount('cbdb')
    if not m:
        return None
    p = (m.get('path') or '').strip()
    if not p:
        return None
    if os.path.isdir(p):
        p = os.path.join(p, 'cbdb.py')
    return p if os.path.isfile(p) else None


def _cbdb_cmd():
    m = get_mount('cbdb') or {}
    return (m.get('cmd') or 'python3').strip() or 'python3'


def mount_status():
    """给 UI 用: 各外部挂载的可用状态。"""
    from .config import read_mounts
    out = {}
    for name, m in (read_mounts().get('mounts') or {}).items():
        if not isinstance(m, dict):
            continue
        p = (m.get('path') or '').strip()
        exists = bool(p) and os.path.exists(p)
        out[name] = {'enabled': bool(m.get('enabled')),
                     'path': p,
                     'cmd': m.get('cmd') or 'python3',
                     'path_exists': exists,
                     'available': bool(m.get('enabled')) and exists}
    return out


def cbdb_find(name, timeout=8):
    """CBDB 查人物 → {pid,name,dynasty,birth,death} 或 None。

    未挂载 / 未找到 / 多义 (>1 条同名) 一律返回 None (宁分不误合)。
    进程内缓存 (含未命中), 避免同名词重复起子进程。
    """
    if name in _CBDB_CACHE:
        return _CBDB_CACHE[name]
    py = _cbdb_py()
    if not py:
        _CBDB_CACHE[name] = None
        return None
    try:
        r = subprocess.run([_cbdb_cmd(), py, 'find', name],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        _CBDB_CACHE[name] = None
        return None
    out = r.stdout or ''
    m = re.search(r'找到\s*(\d+)\s*条', out)
    if not m or int(m.group(1)) != 1:
        _CBDB_CACHE[name] = None
        return None
    mm = re.search(r'PID\s+(\d+)\s+(\S+)\s+(\S+)\s*\(([^)]*)\)', out)
    if not mm:
        _CBDB_CACHE[name] = None
        return None
    pid, cn, dy, yrs = mm.group(1), mm.group(2), mm.group(3), mm.group(4)
    by = dth = None
    ym = re.match(r'(\d+|\?)-(\d+|\?)', yrs.strip())
    if ym:
        by = int(ym.group(1)) if ym.group(1).isdigit() else None
        dth = int(ym.group(2)) if ym.group(2).isdigit() else None
    hit = {'pid': int(pid), 'name': cn,
           'dynasty': None if dy == '?' else dy,
           'birth': by, 'death': dth}
    _CBDB_CACHE[name] = hit
    return hit


def _cbdb_lookup_many(names):
    """并发 CBDB 查询 → {name: hit|None}; 去重后投池。"""
    uniq = list(dict.fromkeys(names))
    if not uniq:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=_CBDB_WORKERS) as ex:
        for n, hit in zip(uniq, ex.map(cbdb_find, uniq)):
            out[n] = hit
    return out


def _cbdb_props(hit):
    props = {}
    if hit.get('dynasty'):
        props['朝代'] = hit['dynasty']
    if hit.get('birth'):
        props['生'] = hit['birth']
    if hit.get('death'):
        props['卒'] = hit['death']
    if hit.get('birth') or hit.get('death'):
        props['生卒'] = f"{hit.get('birth') or '?'}-{hit.get('death') or '?'}"
    return props


# ── 抽取 ──
ENTITY_SYSTEM = (
    "你是史料实体抽取助手。给定文献的若干文本块, 每块有编号。"
    "抽取每块中的人名、地名、机构、物产、职官等具名实体。\n"
    "仅输出 JSON 数组, 每元素形如 {\"i\": 块编号, \"surface\": \"实体原文写法\", "
    "\"category\": \"人名/地名/机构/物产/职官 之一\"}。\n"
    "surface 必须是原文出现的写法 (可含异写/字号)。不要解释, 不要臆造。"
)

_PAPER_TYPES = {'@article', '@inproceedings', '@phdthesis', '@mastersthesis',
                '@techreport', '@unpublished', '@manuscript'}
_BOOK_TYPES = {'@book', '@incollection', '@inbook', '@proceedings',
               '@manual', '@booklet'}


def _split_terms(s):
    return [t.strip() for t in re.split(r'[，,；;、/|\s]+', s or '') if t.strip()]


def _doc_kind(bib_type):
    """文献类型 → 'paper' | 'book' | 'other'。"""
    try:
        from .bib_utils import normalize_bib_type
        t = normalize_bib_type(bib_type)
    except Exception:
        t = (bib_type or '').strip().lower()
    if t in _PAPER_TYPES:
        return 'paper'
    if t in _BOOK_TYPES:
        return 'book'
    return 'other'


SEED_CARD_KIND = 'entity_source'


def _dedupe_seeds(seeds):
    seen, out = set(), []
    for s in seeds:
        n = re.sub(r'\s+', '', s['surface'] or '')
        if n and n not in seen:
            seen.add(n)
            out.append(s)
    return out


def load_seed_card(conn, doc_id):
    """读取实体依据卡 (kind='entity_source') → {'keywords':[...], 'index':[{term,page_num}]}。
    无卡或空卡返回 None (→ 回退文献自带关键词/索引的自动识别)。"""
    try:
        row = conn.execute(
            "SELECT content FROM doc_cards WHERE doc_id = ? AND kind = ?",
            (doc_id, SEED_CARD_KIND)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        c = json.loads(row['content'] or '{}') or {}
    except (ValueError, TypeError):
        return None
    kw = [str(x).strip() for x in (c.get('keywords') or []) if str(x).strip()]
    idx = []
    for it in (c.get('index') or []):
        if isinstance(it, dict):
            t, pn = (it.get('term') or '').strip(), it.get('page_num')
        elif isinstance(it, str):
            t, pn = it.strip(), None
        else:
            continue
        if not t:
            continue
        try:
            pn = int(pn) if pn not in (None, '') else None
        except (TypeError, ValueError):
            pn = None
        idx.append({'term': t, 'page_num': pn})
    if not kw and not idx:
        return None
    return {'keywords': kw, 'index': idx}


def _authoritative_seeds(conn, doc_id, kind):
    """文献自带的权威实体来源 → [{surface,category,page_num,block_num,source}]。
    优先「实体依据卡」(关键词/索引, 人工或模型整理); 无卡则自动识别:
    论文: bib 关键词 + 正文关键词段; 图书: 索引段条目。"""
    card = load_seed_card(conn, doc_id)
    if card:
        seeds = [{'surface': t, 'category': None, 'page_num': None,
                  'block_num': None, 'source': 'card:keywords'}
                 for t in card['keywords']]
        seeds += [{'surface': it['term'], 'category': None,
                   'page_num': it['page_num'], 'block_num': None,
                   'source': 'card:index'} for it in card['index']]
        return _dedupe_seeds(seeds)
    seeds = []
    row = conn.execute("SELECT bib_data FROM documents WHERE id = ?",
                       (doc_id,)).fetchone()
    try:
        bd = json.loads((row['bib_data'] if row else None) or '{}')
    except (ValueError, TypeError):
        bd = {}
    for k in ('keywords', 'keyword', '关键词', '关键字'):
        if bd.get(k):
            for t in _split_terms(str(bd[k])):
                seeds.append({'surface': t, 'category': None, 'page_num': None,
                              'block_num': None, 'source': 'bib:keywords'})
    blocks = conn.execute(
        "SELECT page_num, block_num, text FROM blocks_fts "
        "WHERE doc_id = ? ORDER BY page_num, block_num", (doc_id,)).fetchall()
    texts = [(b['page_num'], b['block_num'], b['text'] or '') for b in blocks]
    if kind == 'paper':
        for pg, bk, tx in texts:
            m = re.search(r'(?:^|\n)\s*(?:关键词|关键字|關鍵詞|key\s*words?)'
                          r'\s*[:：]?\s*(.+)', tx, re.I)
            if m:
                for t in _split_terms(m.group(1)):
                    seeds.append({'surface': t, 'category': None, 'page_num': pg,
                                  'block_num': bk, 'source': 'text:keywords'})
                break
    elif kind == 'book':
        started = False
        for pg, bk, tx in texts:
            if not started:
                if re.match(r'^\s*(?:索引|index)\s*$', tx.strip(), re.I):
                    started = True
                continue
            for line in tx.split('\n'):
                line = line.strip()
                if not line or re.match(r'^[\d\s,，、.·…\-–—]+$', line):
                    continue
                term = re.split(r'[.·…]{2,}|\s{2,}', line)[0].strip(' .·…')
                term = re.sub(r'\s*[\d,，、\-–—].*$', '', term).strip()
                if 1 < len(term) <= 24:
                    seeds.append({'surface': term, 'category': None, 'page_num': pg,
                                  'block_num': bk, 'source': 'text:index'})
    return _dedupe_seeds(seeds)


def _extract_json_obj(content):
    """从模型输出里取 JSON 对象 (去 ``` 包裹, 截取首个 { 到末个 })。"""
    if not content:
        return {}
    t = content.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    i, j = t.find('{'), t.rfind('}')
    if i >= 0 and j > i:
        try:
            v = json.loads(t[i:j + 1])
            return v if isinstance(v, dict) else {}
        except ValueError:
            pass
    return {}


SEED_SYSTEM = (
    "你是文献编目助手。给定文献文本, 提取可作为实体图谱建构依据的条目, "
    "只输出严格 JSON (无解释): "
    '{"keywords":["词1","词2"],"index":[{"term":"条目","page_num":数字或null}]}。'
    "keywords=文献的关键词/主题词(人名、地名、机构、概念); "
    "index=文献自带的索引条目(term 为条目名, page_num 为原书页码, 无则 null)。"
    "严禁编造; 文本中若确无关键词与索引, 返回 {\"keywords\":[],\"index\":[]}。")


def extract_seeds(conn, doc_id, provider_id=None, source=None):
    """模型提取实体依据 (关键词/索引) → {'keywords':[...], 'index':[...], 'model':...}。
    source 给定 (粘贴/导入的文本) 时以其为准解析; 否则从文献块中识别,
    输入优先含「索引/关键词」的块, 再补前若干块。"""
    if source and source.strip():
        text = source.strip()[:12000]
    else:
        blocks = conn.execute(
            "SELECT page_num, block_num, text FROM blocks_fts "
            "WHERE doc_id = ? ORDER BY page_num, block_num", (doc_id,)).fetchall()
        picked, seen = [], set()
        for b in blocks:
            if not (b['text'] or '').strip():
                continue
            if re.search(r'索引|關鍵詞|关键词|关键字|index|key\s*words?',
                         b['text'], re.I):
                k = (b['page_num'], b['block_num'])
                if k not in seen:
                    seen.add(k); picked.append(b)
        for b in blocks:
            if len(picked) >= 40:
                break
            k = (b['page_num'], b['block_num'])
            if (b['text'] or '').strip() and k not in seen:
                seen.add(k); picked.append(b)
        if not picked:
            return {'keywords': [], 'index': [], 'model': None}
        text = "\n".join(f"[p{b['page_num']}b{b['block_num']}] {b['text']}"
                         for b in picked[:40])
    content, model = chat(
        [{"role": "system", "content": SEED_SYSTEM},
         {"role": "user", "content": "文献文本:\n" + text}],
        provider_id=provider_id)
    obj = _extract_json_obj(content or '')
    kw = [str(x).strip() for x in (obj.get('keywords') or []) if str(x).strip()]
    idx = []
    for it in (obj.get('index') or []):
        if not isinstance(it, dict):
            continue
        t = (it.get('term') or '').strip()
        if not t:
            continue
        pn = it.get('page_num')
        try:
            pn = int(pn) if pn not in (None, '') else None
        except (TypeError, ValueError):
            pn = None
        idx.append({'term': t, 'page_num': pn})
    return {'keywords': kw[:200], 'index': idx[:500], 'model': model}


def _batches(blocks, max_blocks=8, max_chars=1400):
    cur, n = [], 0
    for b in blocks:
        t = b['text'] or ''
        if cur and (len(cur) >= max_blocks or n + len(t) > max_chars):
            yield cur
            cur, n = [], 0
        cur.append(b)
        n += len(t)
    if cur:
        yield cur


def extract_mentions(conn, doc_id, provider_id=None):
    """按文献抽取提及 → [{'surface','category','page_num','block_num'}]。
    来源优先级: 论文关键词 / 图书索引 (权威种子, 强制入图) > 正文抽取。"""
    drow = conn.execute("SELECT bib_type FROM documents WHERE id = ?",
                        (doc_id,)).fetchone()
    kind = _doc_kind(drow['bib_type'] if drow else None)
    seeds = _authoritative_seeds(conn, doc_id, kind)

    blocks = conn.execute(
        "SELECT page_num, block_num, text FROM blocks_fts "
        "WHERE doc_id = ? ORDER BY page_num, block_num", (doc_id,)).fetchall()
    # 低价值块跳过 (页码/页眉等过短块), 省调用
    blocks = [b for b in blocks if len((b['text'] or '').strip()) >= 6]
    system = ENTITY_SYSTEM
    if kind == 'paper':
        system += "\n本文献是期刊论文, 正文关键词段落中的词语优先作为实体。"
    elif kind == 'book':
        system += "\n本文献是图书, 若含索引段, 索引条目优先作为实体。"
    if seeds:
        label = '关键词' if kind == 'paper' else ('索引' if kind == 'book' else '权威来源')
        system += ("\n文献自带%s (必须全部纳入抽取结果): %s"
                   % (label, '、'.join(s['surface'] for s in seeds)))
    out = []

    def _one(batch):
        idx = []
        lines = []
        for i, b in enumerate(batch):
            idx.append((i, b['page_num'], b['block_num']))
            lines.append(f"[{i}] {b['text']}")
        user = "文本块:\n" + "\n".join(lines)
        try:
            content, _ = chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}], provider_id=provider_id)
        except Exception:
            return []
        res = []
        for item in _extract_json_array(content or ''):
            if not isinstance(item, dict):
                continue
            surface = (item.get('surface') or '').strip()
            if not surface:
                continue
            pn = bn = None
            try:
                j = int(item.get('i'))
                for k, pg, bk in idx:
                    if k == j:
                        pn, bn = pg, bk
                        break
            except (TypeError, ValueError):
                pass
            res.append({'surface': surface,
                        'category': (item.get('category') or '').strip() or None,
                        'page_num': pn, 'block_num': bn})
        return res

    batches = list(_batches(blocks))
    if batches:
        with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as ex:
            for res in ex.map(_one, batches):
                out.extend(res)
    have = {re.sub(r'\s+', '', m['surface'] or '') for m in out}
    for s in seeds:
        n = re.sub(r'\s+', '', s['surface'] or '')
        if n and n not in have:
            out.append({'surface': s['surface'], 'category': s['category'],
                        'page_num': s['page_num'], 'block_num': s['block_num']})
            have.add(n)
    return out


# ── 归一 ──
def _norm(s):
    return re.sub(r'\s+', '', s or '')


def load_index(conn):
    """书架归一词典: {'canon':{}, 'alias':{}, 'cbdb':{}} → entity_id。"""
    idx = {'canon': {}, 'alias': {}, 'cbdb': {}}
    for r in conn.execute("SELECT id, canonical, aliases, ext_ids FROM entities"):
        idx['canon'][_norm(r['canonical'])] = r['id']
        try:
            for a in json.loads(r['aliases'] or '[]'):
                idx['alias'].setdefault(_norm(a), r['id'])
        except (ValueError, TypeError):
            pass
        try:
            e = json.loads(r['ext_ids'] or '{}')
            if e.get('cbdb'):
                idx['cbdb'][int(e['cbdb'])] = r['id']
        except (ValueError, TypeError):
            pass
    return idx


def _insert_entity(conn, canonical, category, aliases=None, ext_ids=None,
                   props=None):
    conn.execute(
        "INSERT OR IGNORE INTO entities(canonical, category, aliases, "
        "ext_ids, props, origin, compiled_at) VALUES(?,?,?,?,?,?,datetime('now'))",
        (canonical, category, json.dumps(aliases or [], ensure_ascii=False),
         json.dumps(ext_ids or {}, ensure_ascii=False),
         json.dumps(props or {}, ensure_ascii=False), 'ai'))
    row = conn.execute("SELECT id FROM entities WHERE canonical = ?",
                       (canonical,)).fetchone()
    return row['id'] if row else None


def _add_alias(conn, entity_id, alias):
    row = conn.execute("SELECT aliases FROM entities WHERE id = ?",
                       (entity_id,)).fetchone()
    if not row:
        return
    try:
        aliases = json.loads(row['aliases'] or '[]')
    except (ValueError, TypeError):
        aliases = []
    if alias and alias not in aliases:
        aliases.append(alias)
        conn.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                     (json.dumps(aliases, ensure_ascii=False), entity_id))


def _set_ext_ids(conn, entity_id, ext, props=None):
    row = conn.execute("SELECT ext_ids, props FROM entities WHERE id = ?",
                       (entity_id,)).fetchone()
    if not row:
        return
    try:
        cur = json.loads(row['ext_ids'] or '{}')
    except (ValueError, TypeError):
        cur = {}
    cur.update(ext)
    updates, params = ["ext_ids = ?"], [json.dumps(cur, ensure_ascii=False)]
    if props:
        try:
            curp = json.loads(row['props'] or '{}')
        except (ValueError, TypeError):
            curp = {}
        for k, v in props.items():
            curp.setdefault(k, v)
        updates.append("props = ?")
        params.append(json.dumps(curp, ensure_ascii=False))
    params.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(updates)} WHERE id = ?", params)


def _llm_disambiguate(surfaces, canon_list, provider_id=None):
    """批量判候选名称是否等于某已有实体。返回 {surface: canonical|''}。"""
    if not surfaces or not canon_list:
        return {}
    sys_p = ("你是史料实体归一助手。给定「候选名称」与「已有实体」，判断每个"
             "候选是否与某个已有实体指同一事物 (考虑别名、字号、异写)。"
             "仅输出 JSON 数组 [{\"surface\":\"候选\",\"match\":\"已有实体名或空\"}]，"
             "没有匹配填空字符串。不要臆造。")
    user = ("候选名称:\n" + "\n".join(f"- {s}" for s in surfaces) +
            "\n\n已有实体:\n" + "\n".join(f"- {c}" for c in canon_list))
    try:
        content, _ = chat(
            [{"role": "system", "content": sys_p},
             {"role": "user", "content": user}], provider_id=provider_id)
    except Exception:
        return {}
    out = {}
    for item in _extract_json_array(content):
        if isinstance(item, dict) and item.get('surface'):
            out[_norm(item['surface'])] = (item.get('match') or '').strip()
    return out


def resolve_surfaces(conn, surfaces_cat, provider_id=None):
    """归一: [{'surface','category'}] → {surface: entity_id}。"""
    idx = load_index(conn)
    # 预扫描: 收集需要 CBDB 的人名 (不在索引中), 并发查询一次
    need, seen = [], set()
    for sc in surfaces_cat:
        k = _norm(sc['surface'])
        if k in idx['canon'] or k in idx['alias']:
            continue
        cat = sc.get('category')
        if cat and '人' in cat and k not in seen:
            seen.add(k)
            need.append(sc['surface'])
    cbdb_hits = _cbdb_lookup_many(need)
    resolved, pending = {}, []
    for sc in surfaces_cat:
        surface, cat = sc['surface'], sc.get('category')
        k = _norm(surface)
        if k in idx['canon']:
            resolved[surface] = idx['canon'][k]
            continue
        if k in idx['alias']:
            resolved[surface] = idx['alias'][k]
            continue
        hit = cbdb_hits.get(surface) if (cat and '人' in cat) else None
        if hit:
            if hit['pid'] in idx['cbdb']:
                resolved[surface] = idx['cbdb'][hit['pid']]
                continue
            hk = _norm(hit['name'])
            if hk in idx['canon']:
                eid = idx['canon'][hk]
                _set_ext_ids(conn, eid, {'cbdb': hit['pid']}, _cbdb_props(hit))
                idx['cbdb'][hit['pid']] = eid
                resolved[surface] = eid
                continue
            aliases = [surface] if k != hk else []
            eid = _insert_entity(conn, hit['name'], cat, aliases,
                                 {'cbdb': hit['pid']}, _cbdb_props(hit))
            idx['canon'][hk] = eid
            idx['cbdb'][hit['pid']] = eid
            resolved[surface] = eid
            continue
        pending.append(sc)

    if pending:
        canon_list = [r['canonical'] for r in
                      conn.execute("SELECT canonical FROM entities")]
        surf2match = _llm_disambiguate([p['surface'] for p in pending],
                                       canon_list, provider_id)
        for sc in pending:
            surface, cat = sc['surface'], sc.get('category')
            k = _norm(surface)
            match = surf2match.get(k, '')
            if match and _norm(match) in idx['canon']:
                eid = idx['canon'][_norm(match)]
                _add_alias(conn, eid, surface)
                idx['alias'][k] = eid
                resolved[surface] = eid
            elif k in idx['canon']:
                resolved[surface] = idx['canon'][k]
            else:
                eid = _insert_entity(conn, surface, cat)
                idx['canon'][k] = eid
                resolved[surface] = eid
    return resolved


def ingest_doc(conn, doc_id, provider_id=None):
    """单文献: 抽取 → 归一 → 替换该 doc 提及 (局部重建)。返回提及数。"""
    mentions = extract_mentions(conn, doc_id, provider_id)
    surfaces_cat, seen = [], set()
    for m in mentions:
        k = _norm(m['surface'])
        if k and k not in seen:
            seen.add(k)
            surfaces_cat.append({'surface': m['surface'],
                                 'category': m.get('category')})
    resolved = resolve_surfaces(conn, surfaces_cat, provider_id)
    conn.execute("DELETE FROM entity_mentions WHERE doc_id = ?", (doc_id,))
    n = 0
    for m in mentions:
        eid = resolved.get(m['surface'])
        if not eid:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO entity_mentions"
            "(entity_id, doc_id, page_num, block_num, surface) VALUES(?,?,?,?,?)",
            (eid, doc_id, m.get('page_num'), m.get('block_num'), m['surface']))
        n += 1
    conn.commit()
    return n


def generate_graph(collection, doc_ids=None, provider_id=None, progress=None):
    """全量/增量编译。doc_ids=None 即全书架。返回写入提及数。"""
    conn = get_conn(collection)
    try:
        if doc_ids is None:
            doc_ids = [r['id'] for r in
                       conn.execute("SELECT id FROM documents ORDER BY id")]
        total = 0
        for i, did in enumerate(doc_ids):
            total += ingest_doc(conn, did, provider_id)
            if progress:
                progress(i + 1, len(doc_ids))
        base = conn.execute("SELECT COUNT(*) FROM revision_log").fetchone()[0]
        conn.execute(
            "INSERT INTO entity_status(collection, status, base_edits, generated_at) "
            "VALUES(?, 'ready', ?, datetime('now')) "
            "ON CONFLICT(collection) DO UPDATE SET status='ready', "
            "base_edits=excluded.base_edits, generated_at=excluded.generated_at",
            (collection, base))
        conn.commit()
        return total
    finally:
        conn.close()


def detect_suspicious(conn):
    """编译错误巡查 → [{'type','entity_id','canonical','detail','other_id','other_canonical'}]。

    三类 (无需 LLM):
    - repeat    规范名含相邻重复片段 (如「水浒水浒传」)
    - alias_dup 甲的别名等于乙的规范名 (归一遗漏/误分)
    - contain   一个规范名包含另一个 (长度差 ≤6, 疑拆分/粘连)
    """
    rows = conn.execute(
        "SELECT id, canonical, category, aliases FROM entities").fetchall()
    ents = []
    for r in rows:
        try:
            aliases = json.loads(r['aliases'] or '[]')
        except (ValueError, TypeError):
            aliases = []
        if not isinstance(aliases, list):
            aliases = []
        ents.append({'id': r['id'], 'canonical': r['canonical'] or '',
                     'category': r['category'], 'aliases': [
                         str(a).strip() for a in aliases if str(a).strip()]})
    out, seen = [], set()

    def add(t, e, detail, other=None):
        key = (t, e['id'], other['id'] if other else None)
        if key in seen:
            return
        seen.add(key)
        out.append({
            'type': t, 'entity_id': e['id'], 'canonical': e['canonical'],
            'detail': detail,
            'other_id': other['id'] if other else None,
            'other_canonical': other['canonical'] if other else None,
        })

    # 1 重复片段
    for e in ents:
        c = _norm(e['canonical'])
        m = re.search(r'(.{2,})\1', c)
        if m:
            add('repeat', e, f"疑似重复片段「{m.group(1)}」")

    # 2 别名撞他人规范名
    canon_map = {}
    for e in ents:
        canon_map.setdefault(_norm(e['canonical']), e)
    for e in ents:
        for a in e['aliases']:
            tgt = canon_map.get(_norm(a))
            if tgt and tgt['id'] != e['id']:
                add('alias_dup', e, f"别名「{a}」即另一实体规范名", tgt)

    # 3 规范名包含 (仅中短名, 避免长标题噪声)
    for e in ents:
        ce = _norm(e['canonical'])
        if len(ce) < 2 or len(ce) > 24:
            continue
        for o in ents:
            if o['id'] == e['id']:
                continue
            co = _norm(o['canonical'])
            if ce == co or len(co) < len(ce) + 1:
                continue
            if ce in co and len(co) - len(ce) <= 6:
                add('contain', e, f"规范名被「{o['canonical']}」包含", o)
    return out


def migrate_legacy(collection, provider_id=None):
    """一次性: 旧 per-doc 实体卡 items → 上卷为提及 (归一到书架实体)。"""
    conn = get_conn(collection)
    try:
        rows = []
        try:
            rows = conn.execute(
                "SELECT doc_id, content FROM doc_cards WHERE kind = 'entity'"
            ).fetchall()
        except Exception:
            return 0
        # 收集所有 (surface, category) 与 (doc, page, surface)
        surfaces, pending = [], []
        for r in rows:
            try:
                items = (json.loads(r['content'] or '{}') or {}).get('items') or []
            except (ValueError, TypeError):
                items = []
            for it in items:
                lbl = (it.get('label') or '').strip()
                if not lbl:
                    continue
                cat = it.get('category')
                surfaces.append({'surface': lbl, 'category': cat})
                pending.append({'doc_id': r['doc_id'], 'surface': lbl,
                                'page_num': it.get('page_num')})
        resolved = resolve_surfaces(conn, surfaces, provider_id)
        n = 0
        for p in pending:
            eid = resolved.get(p['surface'])
            if not eid:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entity_mentions"
                "(entity_id, doc_id, page_num, block_num, surface) "
                "VALUES(?,?,?,?,?)",
                (eid, p['doc_id'], p['page_num'], None, p['surface']))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def graph_status(collection):
    conn = get_conn(collection)
    try:
        st = conn.execute("SELECT status, base_edits, generated_at FROM "
                          "entity_status WHERE collection = ?",
                          (collection,)).fetchone()
        links = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        mentions = conn.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()[0]
        if not st:
            return {'status': 'none', 'entities': links, 'mentions': mentions,
                    'stale': mentions > 0}
        cur = conn.execute("SELECT COUNT(*) FROM revision_log").fetchone()[0]
        return {'status': st['status'], 'entities': links, 'mentions': mentions,
                'base_edits': st['base_edits'], 'generated_at': st['generated_at'],
                'stale': cur > (st['base_edits'] or 0)}
    finally:
        conn.close()
