# 20260812 前端重构 + BibTeX 标准化 + 繁简搜索修复

## 问题

1. 前端 4 页各自为政：index.html (1751 行) 塞了搜索+统计+上传+文献列表+bib 编辑+预览模态框；无共享导航、无共享 CSS/JS；docs_list 暖色调与其他页不一致；proofread 无登出无 tab
2. BibTeX 字段用 biblatex 命名 (date/journaltitle/location 等)，与用户粘贴的标准 BibTeX (year/journal/address) 不匹配，导致 preset 字段全空、数据落入「额外字段」
3. 搜索繁简匹配不全：DB 中存在混合形式如「台灣」(简体台+繁体灣)，旧的 `_expand_token` 只生成全简/全繁，漏掉混合形式
4. 书架 (collection) 管理散落在导航栏全局选择器，逻辑让人困惑

## 改动

### 前端重构 (7 页架构)

删除 index.html，拆分为：

| 页面 | 路由 | 模板 |
|------|------|------|
| 书架 (首页) | `GET /` | bookshelf.html |
| 搜索 | `GET /search?c=&q=` | search.html |
| 文献列表 | `GET /collections/{c}/docs/list` | docs_list.html (改) |
| 资料上传 | `GET /collections/{c}/upload` | upload.html (Tab: 上传/导入数据包) |
| 预览 | `GET /collections/{c}/doc/{id}/preview` | preview.html (纯文本，无 bib) |
| 校对 | `GET /collections/{c}/doc/{id}/proofread` | proofread.html (改) |
| 登录 | `GET /login` | login.html (不变) |

共享基础设施：
- `static/app.css` — 统一蓝色调 (--primary:#4361ee)
- `static/nav.js` — 导航栏 (coll 全局选择器后来删除)
- `static/bib.js` — 共享 bib 编辑器 IIFE (消除 index/proofread 重复)
- `static/preview-modal.js` — 共享预览模态框
- `templates/_nav.html` — Jinja include partial (品牌+4 tab+登出)
- `templates/_preview_modal.html` — 模态框 HTML partial

### 书架管理 (bookshelf.html)

- 每行操作：进入 · 改名 · 删除
- 表格底部：+ 新建书架
- API: `POST /api/collections/{c}` (init_db), `PATCH` (rename), `DELETE` (rmtree)
- 导航栏的全局 coll select + ☰ 按钮已删除

### BibTeX 字段标准化 (bib_utils.py)

- `BIB_TYPE_FIELDS`: date→year, journaltitle→journal, location→address, bookauthor→editor, repository→institution, urldate 移出 preset
- `BIB_FIELD_LABELS`: 对应更新中文标签
- `generate_cite_key`: 优先查 year 字段，date 作为回退

### 搜索繁简修复 (search.py)

- `_char_variants(ch)`: 每个字 → {原字, s2t, t2s}
- `_all_variants(token)`: 字符级笛卡尔积
- 新增 `get_highlight_terms()`: 返回所有变体供高亮

### Bug 修复 (过程中)

- bib 粘贴: onpaste → renderBibPanel DOM 重建导致文字一闪即逝 → 改为纯手动「识别」按钮
- bib 粘贴后 stale DOM: _applyBibResult/onBibPaste 持有旧 DOM 引用 → renderBibPanel 后重新查询元素
- cite_key 服务端不生成: generate_cite_key 查 date 但用户用 year → 加 year 回退
- 导出数据包 500: api.py 未导入 get_db_path → 补 import
- docs_list 加载失败: 删 migrateModal 遗留 fillMigrateTarget → 删残留函数
- 搜索无响应: uvicorn 服务挂了 → 重启
- 预览模态框 onclick 双引号截断: JSON.stringify 嵌入 HTML 属性 → attrEsc() 转义
- context API 路径 404: preview-modal.js 多写了 /doc/ → 修正
- 新建书架失败: newColl 直接跳 docs/list 但 DB 不存在 → 加 POST /api/collections/{c} 先 init_db

## 决策

1. **预览页无 bib** — bib 编辑只放校对页，预览纯文本浏览
2. **proofread header 一行** — 导航下方：标题 | cite_key 只读 | 来源标签 | 页面跳转 | 页内搜索
3. **取消全局 coll 选择器** — 书架入口统一在首页，导航栏不再有 coll select
4. **BibTeX 标准** — 参照 bibtex.eu/zh-cn/fields/ 使用标准 BibTeX 字段名
5. **预览模态框** — 共享 partial，搜索命中显示前后 3 段，其他显示前 10 block

## 反思

**有效**：
- 7 页拆分后每个页面职责清晰，共享 CSS/JS 消除大量重复
- 预览模态框共享 partial 一处修改全页面生效
- 书架管理集中在首页，逻辑不再困惑

**不足**：
- 过程中多次出现 stale DOM 引用 bug (renderBibPanel 替换 innerHTML 后旧引用失效)，说明前端缺乏组件化保护
- get_conn 会在不存在的 collection 上创建空 DB 文件 (无表)，是一个潜在隐患
- 改动量大 (16 文件，+1889/-2158)，单 commit 粒度偏粗

**下一步**：
- 阶段 7: 文献列表批量导入 BibTeX (选中条目 → 粘贴 .bib → title 模糊匹配 → 批量应用)
- 考虑给 get_conn 加表存在性检查，避免创建空 DB
