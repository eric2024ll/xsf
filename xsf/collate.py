"""智能校对核心: 双候选字符级对齐 + 分歧分拣 + 人工文本页映射。

设计依据: pqa design/client/19-smart-proofread.md
方法来源: arXiv 2607.08459 (ICDAR2026) 方法学迁移;
mem 仓 晚清乡土志 e002 实验 (5 页实测) 提供参数与防护条款。

零第三方依赖 (纯标准库)。

e002 教训 (已落码):
- NW 回溯必须 max 三分支, 漏分支产生全局错位假象 (96.9%→2.5%);
- 自检一致率 < MIN_AGREE 判错位假象, 整页拒绝;
- 表格页无竖排文本块 → 上游判 unsupported。
"""

import re
import unicodedata
from difflib import SequenceMatcher

# ── 参数 (e002 实测调优值) ──────────────────────────────
NW_MATCH = 2
NW_MISMATCH = -1
NW_GAP = -1.5
MIN_AGREE = 0.60          # 自检: 低于此一致率判对齐错位, 拒绝
CLUSTER_GAP = 3           # 分歧列距离 <= 3 聚为一段
ANCHOR_N = 8              # 页映射锚点 n-gram 长度
MIN_MAP_CONF = 0.5        # 页映射置信度阈值, 低于需人工确认

# ── 异体字归一表 (e002 种子, 只收人工确认过的对; 勿接 opencc 全量) ──
_VARIANT_MAP = {
    '黃': '黄',
    '糧': '粮',
    '荳': '豆',
    '跡': '迹',
    '戶': '户',
}

# 版心页码黑名单用字 (pageno 分拣)
_PAGENO_CHARS = set('〇一二三四五六七八九十百千兩0123456789')

# 剥除类: 标点/符号/空白 (Unicode P* S* Z* C*)
_STRIP_CATS = ('P', 'S', 'Z', 'C')


def normalize_char(ch: str) -> str | None:
    """单字符归一: 异体表 → None(剥除) → 原字。"""
    if ch in _VARIANT_MAP:
        return _VARIANT_MAP[ch]
    if unicodedata.category(ch).startswith(_STRIP_CATS):
        return None
    return ch


def normalize(text: str) -> list[str]:
    """文本 → 归一汉字序列 (剥标点空白, 异体归一, 保留位置无关的序列)。"""
    out = []
    for ch in (text or ''):
        c = normalize_char(ch)
        if c is not None:
            out.append(c)
    return out


def is_pageno(seg: str) -> bool:
    """段是否为版心页码 (纯数字/版心用字)。"""
    s = normalize(seg)
    return bool(s) and all(c in _PAGENO_CHARS for c in s)


# ── NW 全局对齐 ──────────────────────────────────────────

def align(a: list[str], b: list[str]) -> list[tuple]:
    """Needleman-Wunsch 全局对齐。

    返回列列表 [(ca, cb, i, j)]: ca/cb 为字符 (None=gap), i/j 为原下标。
    回溯用 max 三分支 — 这是 e002 修过的 bug, 勿改回 if/elif。
    """
    n, m = len(a), len(b)
    # 滚动数组需全量回溯, 直接开全表 (页级 ≤ 千字, 内存无虞)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * NW_GAP
    for j in range(1, m + 1):
        score[0][j] = j * NW_GAP
    for i in range(1, n + 1):
        row, prev = score[i], score[i - 1]
        ai = a[i - 1]
        for j in range(1, m + 1):
            s = NW_MATCH if ai == b[j - 1] else NW_MISMATCH
            row[j] = max(prev[j - 1] + s,      # 对角
                         prev[j] + NW_GAP,     # 上 (a 消耗, b gap)
                         row[j - 1] + NW_GAP)  # 左 (b 消耗, a gap)

    cols = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            s = NW_MATCH if a[i - 1] == b[j - 1] else NW_MISMATCH
            best, bt = score[i][j], 0
            if score[i - 1][j - 1] + s >= best:
                best, bt = score[i - 1][j - 1] + s, 1
            if score[i - 1][j] + NW_GAP >= best:
                best, bt = score[i - 1][j] + NW_GAP, 2
            if score[i][j - 1] + NW_GAP >= best:
                bt = 3
            if bt == 1:
                cols.append((a[i - 1], b[j - 1], i - 1, j - 1))
                i, j = i - 1, j - 1
            elif bt == 2:
                cols.append((a[i - 1], None, i - 1, None))
                i -= 1
            else:
                cols.append((None, b[j - 1], None, j - 1))
                j -= 1
        elif i > 0:
            cols.append((a[i - 1], None, i - 1, None))
            i -= 1
        else:
            cols.append((None, b[j - 1], None, j - 1))
            j -= 1
    cols.reverse()
    return cols


def agreement(cols: list[tuple]) -> float:
    """一致率 = 双方均有字符且相等的列 / max(len(a), len(b))。"""
    both = [c for c in cols if c[0] is not None and c[1] is not None]
    if not cols:
        return 1.0
    same = sum(1 for ca, cb, _, _ in both if ca == cb)
    return same / max(len(cols), 1)


# ── 分歧聚类与分拣 ────────────────────────────────────────

def collate(cols: list[tuple]) -> dict:
    """对齐列 → {"agree": float, "divergences": [...], "accepted": bool}。

    分歧段: 连续/近邻 (距离<=CLUSTER_GAP) 的非常规列聚为一段。
    分拣: pageno (版心页码, 过滤) / variant (异体字对) / substantive (实质异文)。
    """
    bad = [(k, ca, cb, i, j) for k, (ca, cb, i, j) in enumerate(cols)
           if ca != cb]   # 含 gap 列
    if not bad:
        return {"agree": agreement(cols), "divergences": [], "accepted": True}

    # 聚段
    segs, cur = [], [bad[0]]
    for item in bad[1:]:
        if item[0] - cur[-1][0] <= CLUSTER_GAP:
            cur.append(item)
        else:
            segs.append(cur)
            cur = [item]
    segs.append(cur)

    divergences = []
    for seg in segs:
        seg_a = ''.join(c[1] for c in seg if c[1] is not None)
        seg_b = ''.join(c[2] for c in seg if c[2] is not None)
        idx_a = [c[3] for c in seg if c[3] is not None]
        idx_b = [c[4] for c in seg if c[4] is not None]
        s_a, s_b = normalize(seg_a), normalize(seg_b)
        if ((not s_a or is_pageno(seg_a)) and (not s_b or is_pageno(seg_b))):
            kind = 'pageno'
        elif normalize(seg_a) == normalize(seg_b):
            kind = 'variant'
        else:
            kind = 'substantive'
        divergences.append({
            "kind": kind,
            "seg_a": seg_a,          # 候选A (存量 OCR) 文本
            "seg_b": seg_b,          # 候选B 文本
            "raw_a": seg_a, "raw_b": seg_b,   # 归一前原文 (当前输入已归一, 保留字段契约)
            "idx_a": idx_a, "idx_b": idx_b,   # 归一序列内下标, 供调用方映射回行
        })

    return {"agree": agreement(cols),
            "divergences": divergences,
            "accepted": agreement(cols) >= MIN_AGREE}


# ── 人工录入文本 → OCR 页映射 ─────────────────────────────

def map_pages(page_texts: dict, manual_text: str) -> dict:
    """人工全文与各 OCR 页文本的预对齐。

    page_texts: {page_num: str} (该页 vertical_text 块拼接文本)。
    返回 {"ok": bool, "reason": str|None, "pages": [
        {"page_num", "char_start", "char_end", "confidence", "low"}]}。

    算法: 8-gram 唯一锚点 → 锚点序列按页序单调取最长递增链 (LIS)
    → 相邻锚点间线性推出每页字符区间 → 置信度 = 锚点覆盖占比。
    """
    manual_norm = normalize(manual_text)
    manual_grams: dict[str, list[int]] = {}
    for k in range(len(manual_norm) - ANCHOR_N + 1):
        manual_grams.setdefault(''.join(manual_norm[k:k + ANCHOR_N]), []).append(k)
    manual_grams = {g: ps[0] for g, ps in manual_grams.items() if len(ps) == 1}
    if not manual_grams:
        return {"ok": False, "reason": "无法建立页映射: 人工文本过短", "pages": []}

    # 每个 OCR 页在人工全文中的锚点: [(page_num, man_pos, n_hit), ...] 按页序
    hits = []
    for pno in sorted(page_texts):
        pnorm = normalize(page_texts[pno])
        found = []
        for k in range(len(pnorm) - ANCHOR_N + 1):
            g = ''.join(pnorm[k:k + ANCHOR_N])
            if g in manual_grams:
                found.append(manual_grams[g])
        if found:
            # 页内取首个命中代表该页起点 (保守; 页内乱序由单调链兜底)
            hits.append((pno, min(found), len(found)))

    if len(hits) < 2:
        return {"ok": False, "reason": "无法建立页映射: 锚点命中不足", "pages": []}

    # 单调链 (LIS, 按锚点在人工全文中的位置递增)
    lis_len = [1] * len(hits)
    lis_prev = [-1] * len(hits)
    for i in range(len(hits)):
        for j in range(i):
            if hits[j][1] < hits[i][1] and lis_len[j] + 1 > lis_len[i]:
                lis_len[i] = lis_len[j] + 1
                lis_prev[i] = j
    end = max(range(len(hits)), key=lambda i: lis_len[i])
    chain = []
    while end != -1:
        chain.append(hits[end])
        end = lis_prev[end]
    chain.reverse()

    total_anchor_chars = sum(c[2] for c in chain) * ANCHOR_N
    pages = []
    for idx, (pno, mpos, n_hit) in enumerate(chain):
        if idx + 1 < len(chain):
            nxt_pos = chain[idx + 1][1]
        else:
            nxt_pos = len(manual_norm)
        conf = min(1.0, (n_hit * ANCHOR_N) / max(nxt_pos - mpos, 1))
        pages.append({
            "page_num": pno,
            "char_start": mpos,
            "char_end": nxt_pos,
            "confidence": round(conf, 3),
            "low": conf < MIN_MAP_CONF,
        })

    covered = len(chain)
    return {"ok": True, "reason": None, "pages": pages,
            "pages_total": len(page_texts), "pages_covered": covered}


def slice_page(manual_text: str, char_start: int, char_end: int) -> str:
    """按映射区间从人工**归一**文本切片 (供逐页 collate 的候选 B)。"""
    manual_norm = normalize(manual_text)
    return ''.join(manual_norm[char_start:char_end])


def longest_common_span(a_norm: list[str], b_norm: list[str]) -> tuple:
    """归一后两侧粗对齐占比 (map_pages 的快速预检辅助)。"""
    sm = SequenceMatcher(None, a_norm, b_norm, autojunk=False)
    m = sum(bl.size for bl in sm.get_matching_blocks())
    return m, max(len(a_norm), len(b_norm), 1)
