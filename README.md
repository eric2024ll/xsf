# 架阁 (jiage)

> 主题研究文献池检索系统

架阁取自中国古代藏书制度。宋代设「架阁库」，以架阁官专管官府文书档案的存置与检索；研究者做一项主题时，把相关书籍、论文、史料尽置架上，随时检取。本项目即取此意：把散落的 PDF 文献集中上架，通过全文检索快速定位，再回源头阅读、摘录。

---

## 功能

- **多书架管理**：按研究主题分 `collection`（如「民族研究」「历史理论」「周易」），可单架搜索也可跨架搜索。
- **born-digital PDF 解析**：用 PyMuPDF 提取文本，按页/块/行三级存储。
- **中文全文检索**：FTS5 + jieba 分词，支持短语匹配与上下文展示。
- **行级回溯**：搜索命中到「页-段」后，可查看上下数行原文，便于核对与摘录。
- **数据与代码分离**：文献与 SQLite 数据库独立存放，代码仓只保留程序。

---

## 快速开始

### 1. 安装

```bash
cd ~/projects/jiage
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. 指定数据目录（可选）

默认数据目录为 `~/jiage-data/`。想改位置：

```bash
export JIAGE_DATA=/path/to/data
```

或写入 `.env`、shell profile 持久生效。

### 3. 初始化

```bash
jiage init
```

输出示例：

```
架阁已初始化
  数据目录: /home/eric/jiage-data
  书架目录: /home/eric/jiage-data/collections
```

### 4. 添加文献

```bash
jiage add /path/to/云南茶业考.pdf -c 民族研究 \
  --cite-key zhang2010yunnan \
  --title "云南茶业考" \
  --author "张某"
```

`--cite-key` 用于与 histflow 写作层的 pandoc 引用 `[@cite_key, p.XX]` 对齐；可留空。

### 5. 搜索

```bash
jiage search 云南茶业
```

输出示例：

```
找到 6 条结果:

[1] 云南茶业考 [zhang2010yunnan]
    书架: 民族研究 | 页 3 | 段 2
    云南茶业始于唐，盛于明清……
```

### 6. 查看上下文

```bash
jiage context <doc_id> <page_num> <block_num> [-r 1]
```

例如命中结果显示「页 3 | 段 2」、doc_id 为 1：

```bash
jiage context 1 3 2
```

输出中 `▶` 标记命中段，方便回原文核对。

### 7. 统计

```bash
jiage stats
```

---

## 命令参考

| 命令 | 说明 | 示例 |
|------|------|------|
| `jiage init` | 初始化数据库与目录 | `jiage init` |
| `jiage add <pdf> -c <书架> [--cite-key ...] [--title ...] [--author ...]` | 导入 PDF | `jiage add book.pdf -c 民族研究 --cite-key wu1963xibei` |
| `jiage search <query> [-c <书架>] [-n <条数>]` | 全文搜索 | `jiage search 茶马古道 -c 民族研究` |
| `jiage context <doc_id> <page> <block> [-r <半径>]` | 查看上下文 | `jiage context 1 3 2 -r 2` |
| `jiage remove <doc_id>` | 删除文献 | `jiage remove 1` |
| `jiage stats` | 统计书架与文献 | `jiage stats` |

---

## 数据目录结构

```
~/jiage-data/                      # 由 JIAGE_DATA 指定，默认 ~/jiage-data
├── jiage.db                        # SQLite + FTS5 索引
└── collections/                    # 书架目录，按 collection 分文件夹
    ├── 民族研究/
    │   └── 云南茶业考.pdf
    ├── 历史理论/
    └── 周易/
```

代码目录（`~/projects/jiage/`）只放程序与配置，不存文献或数据库。

---

## 数据模型

| 层级 | 表/存储 | 作用 |
|------|---------|------|
| 元数据 | `documents` | 文献、书架、页数、cite_key、标题、作者 |
| 行级 | `lines` | 每页每块每行的原始文本，用于上下文展示与校对 |
| 检索 | `blocks_fts` | FTS5 虚拟表，jieba 分词后的块级文本，用于快速命中 |

为什么分「块」与「行」两层？
- **块**：FTS 搜索的基本单元，jieba 分词后建立索引，命中时可定位到页与段。
- **行**：保留原文的物理行，便于 `jiage context` 展示上下行，未来也支撑 OCR 校对管线。

---

## 架构与定位

架阁是 histflow 研究循环中 **L1 感知层** 的上游工具之一：

```
文献池 (jiage)          L1 感知层
   ↓ 检索、定位
阅读 / 摘录 / 摘要 / 综述  →  histflow 写作
   ↓ 发现缺口
回到 jiage 补充文献
```

它不负责笔记管理、文献综述或写作；只解决一个问题：**把散落在各处的 PDF 快速找出来、定位到页与段**。

---

## 开发

```bash
cd ~/projects/jiage
source .venv/bin/activate
python -m jiage.cli --help
```

依赖：

- `PyMuPDF`：PDF 文本提取
- `jieba`：中文分词
- `setuptools`：开发模式安装

---

## 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 建仓、SQLite schema、基础 CLI、born-digital PDF 解析、FTS 搜索 | ✅ 完成 |
| P1 | 扫描件 OCR 接入（PP-OCRv5 行检测 + LLM 文字增强） | 待做 |
| P2 | 增加书架/集合管理命令；搜索 snippet 高亮；重复文件检测 | 待做 |
| P3 | Web 界面（FastAPI） | 待做 |
| P4 | 与 histflow L1 条目格式联动，导出 `摘录/摘要/综述/线索` | 待做 |

---

## 致谢

命名灵感来自宋代「架阁库」制度。代码与数据分离的设计，来自 histflow 对研究流程的分层：感知层（L1）→ 结构层（L2）→ 成果层（L3）。

---

## License

MIT
