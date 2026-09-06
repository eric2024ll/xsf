"""OCR 疑点启发式标注 (零依赖规则, 供 ingest/reocr/画框重OCR 三处写库点共用)。

规则刻意保守——疑点只是提示人工复核, 不参与任何自动决策。
人工在校对界面改过该行后, edit_line 会清掉 suspect。
"""
import json
import unicodedata


def _weird_ratio(text: str) -> float:
    """不可打印/控制字符/PUA 占比 (超过即可能是乱码)."""
    if not text:
        return 0.0
    weird = 0
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith('C') and ch not in ('\n', '\t'):
            weird += 1
        elif 0xE000 <= ord(ch) <= 0xF8FF:  # PUA
            weird += 1
    return weird / len(text)


def _bbox_wh(bbox) -> tuple:
    """paddle bbox: [x1,y1,x2,y2] → (w, h); 非法则 (0,0)."""
    try:
        arr = json.loads(bbox) if isinstance(bbox, str) else bbox
        if isinstance(arr, (list, tuple)) and len(arr) == 4:
            x1, y1, x2, y2 = (float(v) for v in arr)
            return max(x2 - x1, 0.0), max(y2 - y1, 0.0)
    except (TypeError, ValueError):
        pass
    return 0.0, 0.0


def detect_suspect(text: str, label: str = '', bbox=None,
                   page_w=None, page_h=None) -> str | None:
    """返回疑点标记 (逗号连接) 或 None。规则:

    single_char        单字块 (切分异常或题字, 人工确认)
    weird_chars        控制字符/PUA 占比 > 0.2 (疑似乱码)
    label_geom_mismatch label=text 但几何呈细长竖排形态
                       (w/h < 0.45 且 h >= 0.25*page_h, 实测标签不一致型)
    """
    flags = []
    t = (text or '').strip()
    if not t:
        return None
    if len(t) == 1:
        flags.append('single_char')
    if _weird_ratio(t) > 0.2:
        flags.append('weird_chars')
    if label == 'text' and page_h:
        w, h = _bbox_wh(bbox)
        if w and h and (w / h) < 0.45 and h >= 0.25 * float(page_h):
            flags.append('label_geom_mismatch')
    return ','.join(flags) or None
