"""本地合并 OCR 通道: 并发调用 PP-OCRv66 + PP-StructureV3, 合并结果.

策略:
  - 非文本块 (table_html / formula_latex / figure / ...) → 取 StructureV3 的 block_content, 不覆盖
  - 文本块 (text / text_header / doc_title / paragraph_title / ...) → 取 StructureV3 的 block_label + block_bbox,
    block_content 取 v66 的识别文字 (v66 识别准确率更高)
  - 重叠区: bbox 重合度 > 0.3 → 按 StructureV3 标签 + v66 文字
  - 非重叠区 v66 多识别的行 → 补入, label 标 "text"
  - 非重叠区 StructureV3 多识别但 v66 遗漏 → 保留 StructureV3 的 block_content
"""
import logging

log = logging.getLogger("xsf.ocr.merge")

# StructureV3 中非纯文本的 block_label, 保留其原始 content 不覆盖
NON_TEXT_LABELS = {
    "table_html", "formula_latex", "figure", "figure_caption",
    "table_caption", "header_image", "footer_image", "seal",
}


def _bbox_iou(a, b):
    """计算两个 bbox [x0,y0,x1,y1] 的 IoU。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / (area_a + area_b - inter)


def _center_in_bbox(center_x, center_y, bbox):
    """判断点 (center_x, center_y) 是否在 bbox [x0,y0,x1,y1] 内。"""
    return bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]


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


def _merge_single_page(p_v66: dict, p_sv3: dict) -> dict:
    """合并单页两个通道的结果。"""
    blocks_v66 = p_v66.get("parsing_res_list", [])
    blocks_sv3 = p_sv3.get("parsing_res_list", [])
    w = p_sv3.get("width") or p_v66.get("width", 0)
    h = p_sv3.get("height") or p_v66.get("height", 0)

    used_v66 = set()
    merged_blocks = []

    for b_sv3 in blocks_sv3:
        label = b_sv3.get("block_label", "text")
        bbox_sv3 = b_sv3.get("block_bbox", [0, 0, 0, 0])
        cx = (bbox_sv3[0] + bbox_sv3[2]) / 2
        cy = (bbox_sv3[1] + bbox_sv3[3]) / 2

        if label in NON_TEXT_LABELS:
            merged_blocks.append(b_sv3)
            continue

        best = None
        best_iou = 0.3
        for j, b_v66 in enumerate(blocks_v66):
            if j in used_v66:
                continue
            iou = _bbox_iou(bbox_sv3, b_v66.get("block_bbox", [0, 0, 0, 0]))
            if iou > best_iou or _center_in_bbox(cx, cy, b_v66.get("block_bbox", [0, 0, 0, 0])):
                best = j
                best_iou = iou

        if best is not None:
            used_v66.add(best)
            merged_blocks.append({
                "block_label": label,
                "block_content": blocks_v66[best].get("block_content", ""),
                "block_bbox": bbox_sv3,
                "block_order": b_sv3.get("block_order", len(merged_blocks) + 1),
            })
        else:
            merged_blocks.append(b_sv3)

    for j, b_v66 in enumerate(blocks_v66):
        if j in used_v66:
            continue
        merged_blocks.append({
            "block_label": "text",
            "block_content": b_v66.get("block_content", ""),
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