---
name: xsf
description: "小書房 (xsf) 个人文献池只读检索. 查询书架 (collections)、FTS5 全文+子串检索（繁简互容）、文献全文/上下文/关键词定位、元数据筛选、实体图谱（归一实体/提及锚点/共现邻居）、统计. 适用于晚清乡土志等 OCR 文献池的引文核对与史料定位. 触发词: 小书房、小書房、xsf、文献池、乡土志检索、全文检索、实体图谱、查实体、cite_key、引文核对、史料定位、查原文页码."
---

# 小書房 (xsf) 只读检索

个人文献池检索系统. 服务地址链 (自动顺延, 任一可用即命中):
① 本机 `http://127.0.0.1:8090` → ② 内网 GPU 机 `http://192.168.1.113:8090` (2026-09-30 云端已停用, 链止于此);
`XSF_URL` 环境变量可整体覆盖. 代码仓 `/home/eric/xsf`, 数据 `~/xsf-data`. 本 skill 只覆盖**只读**端点;
写入/管理 (上传、编辑、重 OCR、导入导出) 走 Web 界面或 `xsf` CLI; **LLM 生成** (卡片资产/实体图谱编译/知识地图) 走 `xsf gen cards|wiki|entities` CLI, 不在此.

> Windows 便携版: 服务同样监听 `127.0.0.1:8090` (地址链①天然命中).

检索引擎: **xsf API** (FTS5 关键词 + 页/块/行定位) — 走 `xsf_api.py`, 见下.

## 用法

```bash
python3 ~/.agents/skills/xsf/xsf_api.py <子命令> [参数]
```

- 服务无认证 (本地客户端); `XSF_URL` 环境变量可覆盖服务地址.
- 输出 JSON. 全局 flag: `--pretty` (缩进), `--mark` (保留 `<mark>` 高亮, 默认剥除).
  flag 放子命令前后均可.

## 命令对照

| 命令 | 说明 | 响应关键字段 |
|------|------|-------------|
| `collections` | 列出书架 | `collections: [名...]` |
| `search <q> -c 书架 [-n N] [--source-type 标签]` | FTS5 关键词检索 (繁简互容) | `results[]: {doc_id, page_num, block_num, line_id, title, cite_key, text}` |
| `context <doc_id> <page> <block> -c 书架 [-r R]` | 命中块上下文 (radius=前后块数) | `lines[]: {block_num, line_num, text, bbox, block_label}` |
| `content <doc_id> -c 书架 [--page N] [--limit N] [-q 高亮词]` | 文献全文, 页→块→行 | `doc: {title, author, bib_type, bib_data, cite_key...}, pages[], total_lines` |
| `hits <doc_id> <keyword> -c 书架` | 关键词在文内全部命中 | `hits[]: {page_num, block_num, line_id}` |
| `docs -c 书架 [-n N]` | 最近入库文献 | `docs[]: {id, cite_key, title, author, page_count, doc_type, is_primary...}` |
| `query -c 书架 [--tag/--author/--title/--year/--bib-type] [--sort 字段] [--order asc\|desc] [--page N] [-n N]` | 元数据筛选+分页 | `{total, page, pages, docs[]}`; author/title 模糊, tag/bib_type 精确, year=bib_data.date 前 4 位; sort: created_at/title/author/cite_key/page_count |
| `entities [q] -c 书架 [--category 类别] [--doc ID] [-n N]` | 实体图谱检索 (书架级归一实体) | `entities[]: {id, canonical, category, aliases[], ext_ids (如 {cbdb: PID}), props, mention_count}`; q 匹配规范名/别名 |
| `entity <id> -c 书架` | 实体详情 | `{id, canonical, category, aliases, ext_ids, props, mentions[]: {doc_id, page_num, block_num, surface, title}, additions[], neighbors[]: {id, canonical, category, co(共现次数)}}` |
| `stats [书架]` | 统计 (省略书架=全库) | `{documents, blocks, lines, ocr_docs, born_digital_docs, source_tags}`; 全库多 `collections[]` |

## 典型工作流

1. **检索→核对原文**: `search` 拿到 `doc_id/page_num/block_num` → `context` 拉上下文核对 OCR 质量 (bbox 可判断版面, `block_label: vertical_text` 表示竖排).
2. **检索→全文摘录**: `search` 或 `query` 定位文献 → `content --page N` 取整页文本; 大文献用 `--limit` 截断.
3. **文内定位**: 已知读过的文献 (`docs`/`query` 拿 doc_id) → `hits <doc_id> 关键词` 列出全部出现位置 → `context` 逐处核对.
4. **元数据筛选**: `query --tag primary --year 1908 --sort page_count` 找一手史料; `docs` 看最近入库.
5. **实体关联**: `entities 曾国藩 -c 书架` 查归一实体 (别名命中曾文正/曾涤生) → `entity <id>` 取提及锚点 (跨文献证据链, 每条 `doc_id+page_num+block_num` 可接 `context` 核对原文) 与共现邻居 (同页共现人物) → 邻居 id 可继续 `entity` 展开多跳.

## 注意事项

- OCR 文本可能有识别错误, 繁简体差异由检索层自动互转 (变体扩展), 但引文务必以 `context`/`content` 原文为准.
- 连接失败 = 脚本自动按服务链顺延 (本机→内网), 全挂时检查各机连通性或用 `XSF_URL` 手动指定.
- 检索词含引号 `"` 可能触发服务端 FTS 语法 500 (unterminated string), 避免在 q 里用引号; 不存在的书架报 500 no such table (先用 `collections` 核对书架名); 「测试」书架 db 为空文件 (历史遗留), 各命令均 500, 不可用.
- 本 skill 只读, 不含任何写操作.
