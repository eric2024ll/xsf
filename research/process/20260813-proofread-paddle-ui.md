# 20260813 校对页仿 PaddleOCR 展示界面改造

## 问题

校对页（proofread）视觉平淡，用户希望做成 PaddleOCR aistudio task 展示界面的风格
（彩色类型检测框 + 清爽卡片风），功能不变，可借鉴 Paddle 增加 1-2 个实用功能。

Paddle task 页是登录后 JS 应用，无法直接抓取；按对 PaddleOCR 展示界面的了解对齐。

## 改动

仅改 `jiage/templates/proofread.html`（api.py / db.py / ocr 不动——blocks 已携带 block_label）。

### A. 视觉重塑（CSS）

- **彩色类型框**：bbox-overlay 按 `block_label` 着色（标题=红/正文=蓝/图=绿/表=紫/公式=橙…），
  用 CSS `color-mix(in srgb, var(--tc) N%, transparent)` + 内联 `--tc` 变量实现；
  hover/active 加粗边框 + 加深同色调，不再统一变红。
- **类型小标签**：每个框左上角注入 `<span class="bbox-type-tag">`（类型中文名 + 对应色底）。
- **文本块卡片化**：block-group 左侧 3px 类型色条；徽章 badge 用类型色底+字。
- **类型图例条**：图片工具栏下自动生成本页出现的类型色块（可点击）。

### B. 借鉴 Paddle 的功能

- **F1 类型筛选**：点图例色块 → 隐藏/显示该类型的框 + 文本块（`_hiddenTypes` Set）。
- **F2 一键复制全文**：右栏顶部按钮，收集当前页所有行文本写剪贴板。
  带 `execCommand` 降级（服务器 http://47.93.199.96 非安全上下文，`navigator.clipboard` 不可用）。
- **F3 导出本页**：客户端拼 Blob 下载 `<标题>_第N页.txt`。
- **F5 导出全篇**：复用已有 `POST /docs/export-md`（{ids:[docId]} → zip），fetch+blob 下载。

### C. 保留不动

逐行编辑/保存、页内搜索高亮、框选重OCR、移动端图/文 tab、书目元数据、页码导航、
来源标签、md 关联 PDF 分支——全部原样。

## 决策

- **不加置信度**：PaddleOCR-VL 是整段 VL 模型，不像传统检测+识别有逐行 rec_score；
  加置信度需改 OCR 流水线 + 加 DB 列，成本高收益低，本次不做。
- **色板用 `color-mix` + `--tc` 变量**：DRY，6 类规则即可覆盖所有类型 hover 态，
  避免每种类型写一套 hover。`color-mix` 现代浏览器（2023+）全支持。
- **类型着色放 JS 不放后端**：色板/中文标签集中在 JS 一处维护，模板只加 `data-type` 属性，
  api.py 零改动。
- **复制带降级**：服务器走 http，clipboard API 失效，用 textarea+execCommand 兜底。
- **旋转（F4）本次不做**：旋转图片需同步重算 bbox 坐标（否则框与文字错位），工作量大，留后续。

## 验证

- TestClient 渲染 doc 3（台蕃采风录，96 行带 bbox+label）→ 200，data-type/图例/动作栏/toast 全部在场。
- `node --check` 校验提取的页面 JS → OK。
- `POST /docs/export-md {ids:[3]}` → 200 application/zip 9384 bytes（F5 链路通）。
- 实际数据标签为 `doc_title`/`vertical_text`（PaddleOCR-VL 实际标签，非标准 PP-Structure 名），
  已补全色板覆盖。

## 反思

- PaddleOCR-VL 的实际 block_label 与文档示例不同（doc_title/vertical_text/plain_text 等），
  初版色板按标准 PP-Structure 名写，渲染后才发现需补——以后接新 provider 先 dump 实际 label 值再配色板。
