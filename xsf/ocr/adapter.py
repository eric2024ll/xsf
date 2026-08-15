"""OCR 适配器层。

DirectAdapter: 封装流水线 provider (自带坐标+标签)，开箱即用。
HybridAdapter: 裸 VL provider (纯 markdown)，需自拼检测器 (暂未实现)。

设计依据: histflow-plan/system/tools/14-ocr-pipeline.md §3
"""


class DirectAdapter:
    """封装流水线适配器。

    provider 输出已含 block_bbox + block_label + block_content，
    直接映射到 parsing_res_list 中间格式，无需组装。
    """

    def __init__(self, ocr_func, name):
        self._ocr_func = ocr_func
        self.name = name

    def ocr(self, pdf_path):
        """返回 list[{page_index, parsing_res_list, width, height}]。"""
        return self._ocr_func(pdf_path)


class HybridAdapter:
    """裸 VL 适配器 (需配合检测器)。

    对于纯 markdown 输出的 provider (Qwen-OCR / DeepSeek-OCR / 裸 vLLM)，
    需自拼 PP-DocLayout-V3 拿坐标。当前占位，待 P2+ 实现。
    """

    def __init__(self, ocr_func, detector_func, name):
        raise NotImplementedError(
            "HybridAdapter 暂未实现。裸 VL provider 需配合 PP-DocLayout-V3 "
            "检测器，见 14-ocr-pipeline.md §3.4"
        )
