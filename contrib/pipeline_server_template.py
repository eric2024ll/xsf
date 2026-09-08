"""自训练 PaddleOCR 管线接入 xsf 的参考服务模板.

场景: 小语种 (西夏文/契丹文/八思巴文/蒙文/满文…) 无 VL 大模型可用,
自行标注数据微调 PaddleOCR det/rec 模型, 部署为 PP-OCR 管线, 包装成
本模板的 HTTP 契约后, 即可作为 xsf 的 paddle_http provider 接入 —
上传 OCR / 整本重 OCR / 画框几何过滤全部自动可用 (行框有坐标).

上线步骤:
  1. pip install fastapi uvicorn pymupdf paddleocr paddlepaddle-gpu
  2. 修改 CONFIG (模型路径 / 渲染 dpi / 端口)
  3. 填空 _run_pipeline() — 调用你微调的模型, 返回 [(box, text, score), …]
  4. uvicorn pipeline_server_template:app --host 0.0.0.0 --port 8095
  5. xsf 网页 → OCR 设置 → 添加 → 类型「结构化 HTTP」→ base_url
     http://<host>:8095  (vl_api 自动补 /ocr 路径)

HTTP 契约 (与 xsf/ocr/vl_api.py 的 paddle_http 端点一致):
  POST /ocr  multipart 字段名任意, 收 PDF 文件
  → {"pages": [{"page_index": 0, "width": W, "height": H,
       "parsing_res_list": [
         {"block_label": "text", "block_content": "行文本",
          "block_bbox": [x0, y0, x1, y1], "block_order": 0}]}]}
  坐标约定: width/height 与 block_bbox 同一像素空间 (本模板 = 渲染像素);
  xsf 端按页面宽度比例换算回 150dpi 存储坐标, 无需关心 xsf 内部坐标系。

管线输出语义 (与 VL 模型的差异, 入库层已兼容):
  - det+rec 输出为文本行级, 一行一个 parsing_res_list 元素,
    block_label 恒 'text' — xsf 块粒度下即「一块一行」, 检索/校对不受影响
  - 无版面分析, 阅读顺序由服务端排好填 block_order;
    本模板 _sort_lines 实现竖排右起列序 / 横排行序几何排序
"""

import os
import tempfile

import pymupdf
from fastapi import FastAPI, UploadFile

CONFIG = {
    "det_model_name": "PP-OCRv6_medium_det",  # 官方模型名 (本地缓存即用)
    "rec_model_name": "PP-OCRv6_medium_rec",  # 换自训练时置空并填 *_model_dir
    "det_model_dir": "",       # 微调检测模型目录, 空 = 用官方模型名
    "rec_model_dir": "",       # 微调识别模型目录 (3.x 字典放模型目录内)
    "render_dpi": 300,         # 页面渲染精度, 与 xsf 重 OCR 默认一致
    "device": "cpu",           # 实测 gpu:0 与 VL 同卡 OOM (14+1.7GB 推理峰值>16GB, ResourceExhaustedError)
    "lang": "ch",              # 识别语言 (小语种换对应 lang 或自训练模型)
    "host": "0.0.0.0",
    "port": 8095,
}

app = FastAPI(title="xsf pipeline OCR adapter")


def _load_engine():
    """加载管线引擎。官方模型名直用本地缓存; 微调模型填 *_model_dir."""
    from paddleocr import PaddleOCR
    kwargs = {}
    if CONFIG["det_model_dir"]:
        kwargs["det_model_dir"] = CONFIG["det_model_dir"]
    elif CONFIG["det_model_name"]:
        kwargs["text_detection_model_name"] = CONFIG["det_model_name"]
    if CONFIG["rec_model_dir"]:
        kwargs["rec_model_dir"] = CONFIG["rec_model_dir"]
    elif CONFIG["rec_model_name"]:
        kwargs["text_recognition_model_name"] = CONFIG["rec_model_name"]
    return PaddleOCR(
        lang=CONFIG["lang"],
        device=CONFIG["device"],
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        **kwargs,
    )


_ENGINE = None


def _run_pipeline(image_path: str):
    """一行级 OCR: [(box(4 像素坐标), text, score), …]。填空点.

    通用预训练写法 (已可直接冒烟); 换自训练模型只需改 CONFIG:
      result = _ENGINE.predict(image_path)
      for res in result:
          for t, b, s in zip(res["rec_texts"], res["rec_boxes"],
                             res["rec_scores"]):
              out.append(([float(v) for v in b], t, float(s)))
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _load_engine()
    out = []
    for res in _ENGINE.predict(image_path):
        texts = res["rec_texts"]
        boxes = res["rec_boxes"]
        scores = res["rec_scores"]
        for t, b, s in zip(texts, boxes, scores):
            out.append(([float(v) for v in b], t, float(s)))
    return out


def _sort_lines(lines, page_w: float, page_h: float):
    """几何阅读排序: 竖排页右起列序, 横排页行序.

    竖排判定: 细长行框 (w/h < 0.45 且 h >= 0.25*page_h) 占比过半 → 竖排。
    竖排 key = (-cx, cy): 列从右到左, 列内自上而下 (一列一行的 det 输出)。
    横排 key = (行带, cx): 按行高中点聚合成行带, 带内从左到右。
    局限: 混排页 (横竖同页) 按页级判定统一排序, 复杂版面请在 xsf
    校对页用画框重 OCR / 栏系统手工分栏修正。
    """
    if not lines:
        return lines
    vertical_votes = sum(
        1 for box, _, _ in lines
        if (box[2] - box[0]) / max(1e-6, box[3] - box[1]) < 0.45
        and (box[3] - box[1]) >= 0.12 * page_h)
    vertical = vertical_votes > len(lines) / 2
    if vertical:
        return sorted(
            lines,
            key=lambda it: (-(it[0][0] + it[0][2]) / 2,
                            (it[0][1] + it[0][3]) / 2))
    heights = sorted(max(1e-6, it[0][3] - it[0][1]) for it in lines)
    med_h = heights[len(heights) // 2]
    return sorted(
        lines,
        key=lambda it: (round(((it[0][1] + it[0][3]) / 2) / (med_h / 2)),
                        it[0][0]))


def pages_from_pdf(pdf_path: str, run_line_ocr=_run_pipeline):
    """PDF → xsf 契约 pages。渲染段与排序段独立, 便于无管线环境单测."""
    pages = []
    src = pymupdf.open(pdf_path)
    try:
        for i, page in enumerate(src):
            pix = page.get_pixmap(dpi=CONFIG["render_dpi"])
            img_path = pdf_path + f".p{i}.png"
            pix.save(img_path)
            try:
                lines = run_line_ocr(img_path)
            finally:
                os.unlink(img_path)
            lines = _sort_lines(lines, pix.width, pix.height)
            pages.append({
                "page_index": i,
                "width": pix.width,
                "height": pix.height,
                "parsing_res_list": [
                    {"block_label": "text",
                     "block_content": text,
                     "block_bbox": [box[0], box[1], box[2], box[3]],
                     "block_order": order}
                    for order, (box, text, _score) in enumerate(lines)],
            })
    finally:
        src.close()
    return pages


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ocr")
async def ocr(file: UploadFile):
    suffix = os.path.splitext(file.filename or "")[1] or ".pdf"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, await file.read())
        os.close(fd)
        return {"pages": pages_from_pdf(tmp)}
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])
