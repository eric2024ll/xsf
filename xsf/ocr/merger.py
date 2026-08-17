"""本地合并 OCR 通道: 并发调用 PP-OCRv6 + PP-StructureV3, 合并结果.

适用范围: 横排文档. 竖排文字 (v6 det 为横排行模型, 竖排列被横切) 不可用,
竖排文档请走 PaddleOCR-VL 通道 (见 2026-08-17 实测, 校闻_台湾生番标本).

策略 (行覆盖率归属, 2026-08-17 v2):
  - v6 行是细长小框, sv3 块是大框; 归属判据 = area(行 ∩ 块) / area(行) > 0.6
  - sv3 文本块收集其覆盖的所有 v6 行, 按 (y, x) 排序后用 \\n 拼接为 block_content
    (v6 识别准确率高于 sv3, sv3 提供 label + bbox)
  - sv3 块一行都没收到 → 保留 sv3 原文 (v6 漏检)
  - 非文本块 (表格/公式/图) → 原样保留, 落入其中的 v6 行丢弃 (避免文字散进表格)
  - 不属于任何 sv3 块的 v6 行 → 独立 text 块补入 (v6 多检)
"""
import logging

log = logging.getLogger("xsf.ocr.merge")

# StructureV3 中非纯文本的 block_label, 保留其原始 content 不覆盖
NON_TEXT_LABELS = {
    "table_html", "formula_latex", "figure", "figure_caption",
    "table_caption", "header_image", "footer_image", "seal",
}

# 行覆盖率阈值: v6 行与 sv3 块交面积 / 行面积 超过此值则归属该块
LINE_COVER_THRESH = 0.6


def _area(bbox):
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _inter_area(a, b):
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _line_cover(line_bbox, block_bbox):
    """v6 行被 sv3 块覆盖的比例: area(交) / area(行)。"""
    la = _area(line_bbox)
    if la <= 0:
        return 0.0
    return _inter_area(line_bbox, block_bbox) / la


def _bbox_iou(a, b):
    """计算两个 bbox [x0,y0,x1,y1] 的 IoU。"""
    inter = _inter_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / (_area(a) + _area(b) - inter)


def merge_pages(pages_v66: list[dict], pages_sv3: list[dict]) -> list[dict]:
    """合并两个通道的页面结果。

    两个 list 按 page_index 对齐。返回 pages 列表, 格式同 parsing_res_list 契约。
    """
    idx_v66 = {p["page_index"]: p for p in pages_v66}
    idx_sv3 = {p["page_index"]: p for p in pages_sv3}
    all_indices = sorted(set(idx_v66.keys()) | set(idx_sv3.keys()))

    merged = []
    for pi in all_indices:
        p_v66 = idx_v66.get(pi)
        p_sv3 = idx_sv3.get(pi)

        if p_v66 is None:
            merged.append(p_sv3)
            continue
        if p_sv3 is None:
            merged.append(p_v66)
            continue

        merged.append(_merge_single_page(p_v66, p_sv3))
    return merged


def _join_lines(line_contents: list[str]) -> str:
    """拼接同一 sv3 块内的多行 v66 文字。

    中文行直接相连会粘连歧义, 统一用换行连接 (保留行结构, 校对友好)。
    """
    return "\n".join(c for c in line_contents if c)


def _merge_single_page(p_v66: dict, p_sv3: dict) -> dict:
    """合并单页两个通道的结果 (行覆盖率归属算法)。"""
    blocks_v66 = p_v66.get("parsing_res_list", [])
    blocks_sv3 = p_sv3.get("parsing_res_list", [])
    w = p_sv3.get("width") or p_v66.get("width", 0)
    h = p_sv3.get("height") or p_v66.get("height", 0)

    # 预处理: sv3 块分为文本块 / 非文本块
    text_blocks = []      # (bbox, block)
    non_text_bboxes = []  # [bbox]
    for b in blocks_sv3:
        bbox = b.get("block_bbox", [0, 0, 0, 0])
        if b.get("block_label", "text") in NON_TEXT_LABELS:
            non_text_bboxes.append(bbox)
        else:
            text_blocks.append((bbox, b))

    # 归属: 每个 v6 行找覆盖率最高的 sv3 文本块
    owner = [None] * len(blocks_v66)          # 行 j -> 文本块索引
    dropped = [False] * len(blocks_v66)       # 落入非文本块 → 丢弃
    for j, b_v66 in enumerate(blocks_v66):
        lb = b_v66.get("block_bbox", [0, 0, 0, 0])
        best_cov = LINE_COVER_THRESH
        best_k = None
        for k, (sb, _) in enumerate(text_blocks):
            cov = _line_cover(lb, sb)
            if cov > best_cov:
                best_cov = cov
                best_k = k
        if best_k is not None:
            owner[j] = best_k
            continue
        # 不属于任何文本块: 落入非文本块 (表格/图) 超过阈值 → 丢弃
        for nb in non_text_bboxes:
            if _line_cover(lb, nb) > LINE_COVER_THRESH:
                dropped[j] = True
                break

    # 收集: 每个文本块按 (y, x) 序拼接其行
    merged_blocks = []
    for k, (sb, b_sv3) in enumerate(text_blocks):
        lines = [blocks_v66[j] for j in range(len(blocks_v66))
                 if owner[j] == k]
        if lines:
            lines.sort(key=lambda b: (
                b["block_bbox"][1], b["block_bbox"][0]))
            content = _join_lines(
                [str(b.get("block_content", "")) for b in lines])
        else:
            content = str(b_sv3.get("block_content", ""))
        merged_blocks.append({
            "block_label": b_sv3.get("block_label", "text"),
            "block_content": content,
            "block_bbox": sb,
            "block_order": b_sv3.get("block_order", len(merged_blocks) + 1),
        })

    # 非文本块原样保留
    for b in blocks_sv3:
        if b.get("block_label", "text") in NON_TEXT_LABELS:
            merged_blocks.append(dict(b))

    # 未归属且未丢弃的 v6 行 → 独立 text 补入
    for j, b_v66 in enumerate(blocks_v66):
        if owner[j] is None and not dropped[j]:
            merged_blocks.append({
                "block_label": "text",
                "block_content": str(b_v66.get("block_content", "")),
                "block_bbox": b_v66.get("block_bbox", [0, 0, 0, 0]),
                "block_order": len(merged_blocks) + 1,
            })

    merged_blocks.sort(key=lambda b: (
        b.get("block_bbox", [0, 0, 0, 0])[1],
        b.get("block_bbox", [0, 0, 0, 0])[0],
    ))

    return {
        "page_index": p_sv3.get("page_index", 0),
        "parsing_res_list": merged_blocks,
        "width": w,
        "height": h,
    }
