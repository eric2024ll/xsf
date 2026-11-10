# ISSUE: anydoc 对 ClearScan 型 PDF 判 needs-OCR 拒转, 静默产出薄文献

- 日期: 2026-11-11
- 状态: FIXED (2026-11-11 根因已修, `4d9a440`; 修复实施与检测记录见文末)
- 来源: 互嵌式书架批量入库 (181 篇) 过程中发现
- 修复会话引用本文即可, 复现样本/兜底脚本路径见下

## 现象

`ingest_native_pdf` (文本层管线) 对 **ClearScan 型 PDF** (图像版式 +
隐形 OCR 文本层, 如 Abbyy ClearScan / Adobe ClearScan 产物) 入库后
行数寥寥: 互嵌式 doc132《社会行动的结构》927 页只入 **2 行**,
`doc_type='pdf_native'`, 无任何告警。

同类: 同批次 14 篇中文论文 (部分页失败型) 入库仅 1-9 行,
均已被 reocr/直提人工修复, 但根因仍在。

## 根因链

1. `_office_to_markdown()` (xsf/ingest.py:305) 只捕 `anydoc.ConvertError`;
   `anydoc.NeedsOcrError` 逃逸。
2. `_native_extract_pages()` (xsf/ingest.py:412) 的
   `except Exception: empty_pages.append(...)` 把它连同任何转换异常
   **静默吞成空页**, 不中断、不告警。
3. 调用方只在 `total_lines == 0` (整篇全空) 才报错回退 OCR 管线;
   「927 页只转出 2 行」这类大面积失败直接通过。

## 复现

```python
# 样本: /mnt/nas/xsf-collections/互嵌式/files/132/original.pdf (927页)
import pymupdf, anydoc
src = pymupdf.open('/mnt/nas/xsf-collections/互嵌式/files/132/original.pdf')
print(len(src[100].get_text()))        # ~723 字符 — pymupdf 文本层完好
one = pymupdf.open(); one.insert_pdf(src, from_page=100, to_page=100)
one.save('/tmp/p101.pdf')
anydoc.to_markdown('/tmp/p101.pdf')    # NeedsOcrError: needs OCR
```

## 修复建议

1. `_office_to_markdown` 显式捕 `NeedsOcrError`, 转成带类型的 RuntimeError。
2. `_native_extract_pages` 区分「真空页」与「转换失败页」;
   **空页率超阈值 (建议 >30%) 时告警或自动建议回退扫描件 OCR 管线**。
3. (可选) ClearScan 检测兜底: NeedsOcrError 页改用 pymupdf `get_text('blocks')`
   直提 (带 bbox, 可完整建 lines)。已验证的实现:
   `contrib/parsons_direct.py` (doc132 2 行 → 13301 行, 927 页仅 5 空页)。
   脚本本体已从 /tmp 固化入仓 (2026-11-11); 核心仅 13 行 (blocks 过滤
   b[6]==0 → detect_suspect/_tokenize/_apply_detected_lang 入库), 可直接抄。

## 相关数据修复记录 (已完成, 勿重复)

- doc132: parsons_direct.py 直提修复 (13301 行)
- 14 篇薄文献 (doc 27/32/34/46/48/56/60/62/72/79/81/91/121/125):
  reocr 云端逐页修复, 全部 ≥76 行, error 页清零

## 修复实施 (2026-11-11, `4d9a440`)

建议三条全落地:

1. `_office_to_markdown` 显式捕 `NeedsOcrError` (子类先于 ConvertError;
   import 路径 + CLI 路径文案识别) → 抛 `NativePdfNeedsOcr` 带类型异常。
   根因链第 1 条的实际机制修正: 子类其实被笼统捕住了, 问题是**类型被抹掉**,
   效果同逃逸。
2. `_native_extract_pages` 区分两类失败页; 空页率 >30% 时
   `logger.warning` + 结果字段 `warning`, 上传页 note 直接可见。
3. 兜底落地: NeedsOcr 页先试 `_fallback_blocks_extract` (PyMuPDF
   blocks 直提, 带 bbox + suspect, parsons_direct 13 行生产化),
   提不出才算空页。返回 4 元组, `needs_ocr_fallback` 字段计入结果。

检测 (隔离库, 本 issue 复现样本 doc132 p101-103):
ClearScan 0→39 行; 数字原生不受扰; 纯图仍回退 OCR 管线;
混合 40% 空页告警触发; 上传 API note 可见; FTS 可达。

## 追加实例 (2026-11-11)

- danfeifei2019xinshidai: 普通 CNKI 5 页小 PDF (203KB, 文本层完好,
  pymupdf 每页 ~1700 字符) 也被 anydoc 判 needs-OCR → add 回退 OCR 路径
  → 本地 8091 被拒/云端 500 两次失败。已 pymupdf 直提手动入库。
  **影响面不止 ClearScan 大部头, 普通 CNKI PDF 同样中招**, 建议优先修。
