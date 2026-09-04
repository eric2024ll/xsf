# 小書房 (xsf)

> 主题研究文献池检索系统

---

## 功能

- **多书架管理**：按研究主题分 `collection`（如「民族研究」「历史理论」「周易」），可单架搜索也可跨架搜索。
- **born-digital PDF 解析**：用 PyMuPDF 提取文本，按页/块/行三级存储。
- **文件夹投放自动入库**：把 PDF/MD 丢进 `{书架}/uploads/`，后台线程自动扫描入库；无文本层的扫描件自动排 OCR 队列回填（见「数据目录结构」注记）。
- **中文全文检索**：FTS5 + jieba 分词，支持短语匹配与上下文展示。
- **行级回溯**：搜索命中到「页-段」后，可查看上下数行原文，便于核对与摘录。
- **数据与代码分离**：文献与 SQLite 数据库独立存放，代码仓只保留程序。

---

## 快速开始

### 1. 安装

```bash
cd ~/projects/xsf
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. 指定数据目录（可选）

默认数据目录为 `~/xsf-data/`。想改位置：

```bash
export XSF_DATA=/path/to/data
```

或写入 `.env`、shell profile 持久生效。

### 3. 初始化

```bash
xsf init
```

输出示例：

```
小書房已初始化
  数据目录: /home/eric/xsf-data
  书架目录: /home/eric/xsf-data/collections
```

### 4. 添加文献

```bash
xsf add /path/to/云南茶业考.pdf -c 民族研究 \
  --cite-key zhang2010yunnan \
  --title "云南茶业考" \
  --author "张某"
```

`--cite-key` 用于与 histflow 写作层的 pandoc 引用 `[@cite_key, p.XX]` 对齐；可留空。

### 5. 搜索

```bash
xsf search 云南茶业
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
xsf context <doc_id> <page_num> <block_num> [-r 1]
```

例如命中结果显示「页 3 | 段 2」、doc_id 为 1：

```bash
xsf context 1 3 2
```

输出中 `▶` 标记命中段，方便回原文核对。

### 7. 统计

```bash
xsf stats
```

---

## 命令参考

| 命令 | 说明 | 示例 |
|------|------|------|
| `xsf init` | 初始化数据库与目录 | `xsf init` |
| `xsf add <pdf> -c <书架> [--cite-key ...] [--title ...] [--author ...]` | 导入 PDF | `xsf add book.pdf -c 民族研究 --cite-key wu1963xibei` |
| `xsf search <query> [-c <书架>] [-n <条数>]` | 全文搜索 | `xsf search 茶马古道 -c 民族研究` |
| `xsf context <doc_id> <page> <block> [-r <半径>]` | 查看上下文 | `xsf context 1 3 2 -r 2` |
| `xsf remove <doc_id>` | 删除文献 | `xsf remove 1` |
| `xsf stats` | 统计书架与文献 | `xsf stats` |

---

## Collection 导入导出

独立 CLI 脚本 `scripts/collection_io.py`，在项目根目录运行。用于 collection 的整体备份、迁移、服务器 ↔ 本地同步。

### 导出

```bash
cd ~/projects/xsf && source .venv/bin/activate
python scripts/collection_io.py export <collection> [-o output.tar.gz]
```

自动查 DB `documents` 表，打包该 collection 的 **DB 快照 + 全部源文件 + manifest.json**。DB 用 SQLite backup API 导出，确保 WAL 一致性。缺失文件（DB 有记录但文件不存在）会列出来。

### 导入

```bash
python scripts/collection_io.py import <collection> <archive.tar.gz> \
  [--conflict skip|overwrite] [--db-only] [--force]
```

| 选项 | 说明 |
|------|------|
| `--db-only` | 只导入 DB，跳过源文件（OSS 手动迁移场景） |
| `--conflict overwrite` | 同名源文件覆盖（默认 skip） |
| `--force` | 覆盖已存在的 collection DB |

> 兼容：导入时同时接受 `xsf.db` 与旧名 `jiage.db`（2026-08 改名前的归档）。

### OSS 服务器场景

源文件在 OSS 挂载路径（`XSF_COLLECTIONS_DIR`）下。大文件导入 OSS 可能慢，推荐分两步：

```bash
# 1. 先只导 DB
python scripts/collection_io.py import 两岸三交 backup.tar.gz --db-only

# 2. 源文件手动 rsync 到 OSS 挂载路径 (按书架分目录)
rsync -av uploads/ /mnt/oss/sources/xsf/collections/<collection>/uploads/
```

### 打包格式

```
{collection}_YYYYMMDD.tar.gz
├── xsf.db                  # SQLite 快照
├── manifest.json           # collection 名、导出时间、文献数、filename 列表
└── uploads/                # 该 collection 的全部源文件
```

---

## 数据目录结构

```
~/xsf-data/                       # 由 XSF_DATA 指定，默认 ~/xsf-data
├── db/                             # 数据库（本地磁盘，不放 OSS）
│   ├── 民族研究/xsf.db             #   每个 collection 独立 SQLite + FTS5
│   └── 历史理论/xsf.db
└── collections/                    # 书架目录
    ├── 两岸三交/uploads/            #   源文件按书架分目录 (2026-09-04 起)
    │   ├── 云南茶业考.pdf
    │   └── 茶马古道.md
    └── _orphan/                    #   迁移时无 DB 引用的遗留文件
```

- `XSF_DB_DIR`：DB 目录（默认 `XSF_DATA/db/`），**必须本地磁盘**——OSS 不支持 SQLite 文件锁。
- `XSF_COLLECTIONS_DIR`：源文件目录（默认 `XSF_DATA/collections/`），服务器可指 OSS 挂载路径。
- DB 内只存 `filename`（不含绝对路径），迁移时无需修改 DB 内容。
- 源文件按书架分目录（`collections/{书架}/uploads/`，与 `db/{书架}/xsf.db` 对称），
  跨书架同名文件互不干扰。历史迁移工具: `scripts/migrate_uploads_per_collection.py`。

### 文件夹投放自动入库（2026-09-04 起）

把 PDF/MD 直接丢进 `{书架}/uploads/`（SMB/NFS/rsync 均可），后台线程每
`XSF_SCAN_INTERVAL` 秒（默认 120，`0` 关闭）轮询 diff，新文件自动入库：

- PDF 先走 born-digital 秒级解析（文献列表立即可见），MD 直接入库。
- 平均行数/页 < 2 的 PDF 视为扫描件，自动入 OCR 队列（worker 单线程串行，
  与手动「重新 OCR」共用同一任务机制，校对页可看进度），完成后可检索。
  `XSF_SCAN_OCR=0` 可关自动 OCR（仅标记待 OCR）。
- 文件写入未满 `XSF_SCAN_STABLE_SEC` 秒（默认 60）视为仍在传输，下轮再收。
- `collections/` 根目录与 `_orphan/` 不扫描——文件必须放进书架子目录才会被认领。
- 文献列表页左上角角标显示扫描时间/新增/OCR 队列状态。
- 投放入库的文献初始无 cite_key/元数据，可在 Web 详情页补录。

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `XSF_SCAN_INTERVAL` | `120` | 扫描轮询间隔秒，`0` 关闭整个扫描 |
| `XSF_SCAN_STABLE_SEC` | `60` | 文件稳定阈值秒（mtime 距今小于此值跳过） |
| `XSF_SCAN_OCR` | `1` | 扫描件自动 OCR，`0` 仅标记不自动跑 |

代码目录（`~/projects/xsf/`）只放程序与配置，不存文献或数据库。

---

## 数据模型

| 层级 | 表/存储 | 作用 |
|------|---------|------|
| 元数据 | `documents` | 文献、书架、页数、cite_key、标题、作者 |
| 行级 | `lines` | 每页每块每行的原始文本，用于上下文展示与校对 |
| 检索 | `blocks_fts` | FTS5 虚拟表，jieba 分词后的块级文本，用于快速命中 |

为什么分「块」与「行」两层？
- **块**：FTS 搜索的基本单元，jieba 分词后建立索引，命中时可定位到页与段。
- **行**：保留原文的物理行，便于 `xsf context` 展示上下行，未来也支撑 OCR 校对管线。

---

## 架构与定位

小書房是 histflow 研究循环中 **L1 感知层** 的上游工具之一：

```
文献池 (xsf)            L1 感知层
   ↓ 检索、定位
阅读 / 摘录 / 摘要 / 综述  →  histflow 写作
   ↓ 发现缺口
回到 xsf 补充文献
```

它不负责笔记管理、文献综述或写作；只解决一个问题：**把散落在各处的 PDF 快速找出来、定位到页与段**。

---

## 开发

```bash
cd ~/projects/xsf
source .venv/bin/activate
python -m xsf.cli --help
```

依赖：

- `PyMuPDF`：PDF 文本提取
- `jieba`：中文分词
- `requests`：generic_http OCR provider 客户端
- `setuptools`：开发模式安装

### OCR provider（两种类型，Web「OCR 设置」自助添加）

**1. `generic_http`（通用）**——本地 GPU / 自建服务 / 第三方 API，统一契约：

```
POST <url>                     # multipart/form-data
  file: PDF/图片
  model: 可选表单字段
  Authorization: Bearer <key>  # 可选
→ 200 {pages: [{page_index, parsing_res_list, width, height}]}
```

**2. `aistudio`（内置云端）**——内置三阶段协议（提交→轮询→取结果），只需填 token，无需 URL 与本地服务。

- 配置存 `<XSF_DATA>/ocr-config.json`（v2，权限 600），`parsing_res_list` 契约与 histflow-plan 一致
- 上传/OCR 始终走全局默认 provider（列表点选切换）；CLI 可 `--provider <id>` 显式指定
- 本地模型示例：`~/paddleocr-vl/server.py`（PaddleOCR-VL-1.6 GPU，:8091，systemd `paddleocr-vl.service`）

---

## 路线图

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 建仓、SQLite schema、基础 CLI、born-digital PDF 解析、FTS 搜索 | ✅ 完成 |
| P1 | 扫描件 OCR 接入（generic_http 多 provider，用户自助添加本地/远程服务，设计见 histflow-plan/system/tools/14-ocr-pipeline.md §3.3） | ✅ 完成 (2026-08-14) |
| P2 | 增加书架/集合管理命令；搜索 snippet 高亮；重复文件检测 | 待做 |
| P3 | Web 界面（FastAPI） | 待做 |
| P4 | 与 histflow L1 条目格式联动，导出 `摘录/摘要/综述/线索` | 待做 |

---

## License

MIT
