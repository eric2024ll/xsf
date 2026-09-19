# 小書房 (xsf)

> 人文社科学者本地文献管理和检索平台

把散落在各处的 PDF 变成**可检索、可定位、可校对**的文本池：丢进书架，自动入库、自动 OCR、全文检索、图文对照校对。以 Web『小書房』为主入口，CLI / MCP / Windows 便携版随行。数据完全留在本机，不依赖任何云服务。

---

## 功能

- **多书架管理**：按研究主题分 collection，可单架搜索也可跨架搜索，Web 上创建/重命名/删除。
- **双通道解析**：born-digital PDF 用 PyMuPDF 秒级提取立即可见；扫描件走 OCR 队列回填。PDF 上传一律排 OCR 队列（`XSF_SCAN_OCR=0` 可关），确保检索层始终有全文。
- **文件夹投放自动入库**：把 PDF/MD 丢进 `{书架}/uploads/`（SMB/NFS/rsync 均可），后台线程自动扫描入库 + 排 OCR 队列，见下文专节。
- **中文全文检索**：FTS5 + jieba 分词，短语匹配；搜索结果**按文档分组**，文档内分页 + load-more，跨文档通读不割裂。
- **OCR 校对工作台**：图文对照页，块粒度编辑；**栏系统**定义页面阅读区域（横排/竖右起/竖左起）后按栏重组、手工分栏重 OCR；页级断点续跑（done/error 状态）；疑点标记 `suspect`；失败页码明示；文献列表带 OCR 页状态徽标。
- **智能校对**：OCR 候选云双通道分歧点校对——第二引擎自动对齐比对，或粘贴/上传整篇人工录入文本（自动页映射），逐点产出「实质异文 / 异体字对 / 页码」分类的分歧表，定位到原文行；分歧点自动写入疑点标记。
- **批量元数据**：批量 设为/添加/移除 来源标签（原始史料/研究文献/工具书 + 自定义）；自动书目匹配（match-bib）；批量改元数据（batch-patch）；导出 .bib / .md / 归档。
- **行级回溯**：命中定位到「页-段」后，可查看上下数行原文，便于核对与摘录。
- **认证**：`XSF_AUTH_TOKEN` 设密码后所有页面需登录。
- **数据与代码分离**：文献与 SQLite 库独立存放（默认 `~/xsf-data/`），代码仓只保留程序。

---

## 快速开始

### 1. 安装

```bash
git clone <repo-url> xsf && cd xsf
python -m venv .venv
source .venv/bin/activate
pip install -e .            # Web + CLI
pip install -e ".[mcp]"     # 需要 MCP 时
```

要求 Python 3.10+。不想装环境可直接用 [Windows 便携版](#windows-便携版)。

### 2. 指定数据目录（可选）

默认 `~/xsf-data/`。想改位置：

```bash
export XSF_DATA=/path/to/data
```

或写入 `.env`、shell profile 持久生效。

### 3. 初始化 + 添加文献

```bash
xsf init

# born-digital PDF
xsf add /path/to/book.pdf -c 书架名 \
  --cite-key zhang2010yunnan \
  --title "云南茶业考" \
  --author "张某"

# 扫描件 (走默认 OCR provider)
xsf add /path/to/scan.pdf -c 书架名 --ocr
xsf add /path/to/scan.pdf -c 书架名 --ocr --provider p1   # 指定 provider
```

`--cite-key` 用于书目引用对齐（如 pandoc 的 `[@cite_key, p.XX]`）；可留空，Web 详情页可补录。

### 4. 搜索与回溯

```bash
xsf search 云南茶业
xsf context <doc_id> <page_num> <block_num> [-r 2]   # ▶ 标记命中段
xsf stats
```

日常使用以 **Web 界面**为主（见下节），CLI 适合脚本化与抽查。

---

## 命令参考（CLI）

| 命令 | 说明 | 示例 |
|------|------|------|
| `xsf init` | 初始化数据库与目录 | `xsf init` |
| `xsf add <pdf> -c <书架> [--cite-key ...] [--title ...] [--author ...] [--ocr] [--provider <id>]` | 导入 PDF | `xsf add book.pdf -c 书架名 --cite-key wu1963xibei` |
| `xsf search <query> [-c <书架>] [-n <条数>]` | 全文搜索 | `xsf search 茶马古道 -c 书架名` |
| `xsf context <doc_id> <page> <block> [-r <半径>]` | 查看上下文 | `xsf context 1 3 2 -r 2` |
| `xsf remove <doc_id>` | 删除文献 | `xsf remove 1` |
| `xsf stats` | 统计书架与文献 | `xsf stats` |

---

## Web 界面（主入口）

`uvicorn xsf.api:app --port 8090` 起服务（常驻部署建议 systemd，见「部署」）。

- **书架页** `/`：全库统计 + 书架列表 + 最近文献
- **搜索页** `/search`：跨书架分组搜索
- **文献列表** `/collections/{c}/docs/list`：筛选、OCR 页状态徽标、扫描角标、批量标签
- **上传页** `/collections/{c}/upload`：单传/批量，OCR 复选（默认勾选）
- **预览** `/collections/{c}/doc/{id}/preview`：纯文本页/块/行
- **校对页** `/collections/{c}/doc/{id}/proofread`：图文对照、行编辑、分栏重 OCR、智能校对
- **OCR 设置**：provider 自助添加/切换默认/连通测试

---

## OCR 体系

### 统一 vl_api 架构

所有 provider 统一为 `type: 'vl_api'`，按 **endpoint profile** 区分三种接入方式，能力二分 **structured / plain**：

| endpoint profile | 接入 | 能力 | 说明 |
|------------------|------|------|------|
| `paddle_http` | 本地 GPU / 自建服务 | structured | `parsing_res_list` 坐标契约，支持图文校对、画框重 OCR |
| `aistudio_job` | AI Studio 云端 | structured | 内置三阶段协议（提交→轮询→取结果），只填 token |
| `openai_chat` | OpenAI 兼容 chat API（qwen 等 VL 模型） | plain | 整页图直送、纯文本入库，可作默认引擎；对已有画框可裁切图片重 OCR 直送 |

- 配置存 `<XSF_DATA>/ocr-config.json`（权限 600），Web「OCR 设置」自助添加
- 上传/OCR 始终走全局默认 provider（列表点选切换）；CLI 可 `--provider <id>` 显式指定
- 网络错误/5xx 指数退避，整体最多重试 3 次；无 provider 时报友好错误并引导配置
- 引擎切换有**防错位**保护（structured/plain 各自的入库路径不同）

### 本地 GPU 模型

- **PaddleOCR-VL**：自建 HTTP 服务（如 systemd 常驻 :8091），以 `paddle_http` profile 接入
- **自训练管线**：`contrib/` 提供 pipeline 模板，支持本地模型缓存、GPU 独占（默认 `gpu:0`）、设备双层回退 GPU→CPU（无 CUDA 直落 + 初始化异常兜底），`PP_DEVICE` 可覆盖，`/health` 暴露实际设备

---

## 文件夹投放自动入库

把 PDF/MD 直接丢进 `{书架}/uploads/`，后台线程每 `XSF_SCAN_INTERVAL` 秒轮询 diff，新文件自动入库：

- PDF 先走秒级解析（文献列表立即可见），**再一律入 OCR 队列**（worker 单线程串行，与手动「重新 OCR」共用同一任务机制，校对页可看进度）。`XSF_SCAN_OCR=0` 可关自动 OCR（仅标记待 OCR）。幂等：已 OCR（doc_type='ocr'）不再重跑。
- 文件写入未满 `XSF_SCAN_STABLE_SEC` 秒视为仍在传输，下轮再收。
- **查重**：filename 已在本架 DB 的重复投放自动跳过（角标「重复N」可见，只报最近 1 小时投放的）；新入库文件若与其它书架同名，角标 tooltip 提醒（跨书架共存是设计行为，仅提醒不阻断）。
- `collections/` 根目录与 `_orphan/` 不扫描——文件必须放进书架子目录才会被认领。
- 投放入库的文献初始无 cite_key/元数据，可在 Web 详情页补录。

---

## 数据目录与环境变量

```
~/xsf-data/                       # 由 XSF_DATA 指定，默认 ~/xsf-data
├── db/                             # 数据库（本地磁盘，不放网络盘）
│   ├── 书架A/xsf.db                #   每个 collection 独立 SQLite + FTS5
│   └── 书架B/xsf.db
├── collections/                    # 书架目录
│   ├── 书架A/uploads/              #   源文件按书架分目录
│   │   ├── 云南茶业考.pdf
│   │   └── 茶马古道.md
│   └── _orphan/                    #   迁移时无 DB 引用的遗留文件
└── ocr-config.json                 # OCR provider 配置 (权限 600)
```

- DB 内只存 `filename`（不含绝对路径），迁移时无需修改 DB 内容。
- 源文件按书架分目录（`collections/{书架}/uploads/`，与 `db/{书架}/xsf.db` 对称），跨书架同名文件互不干扰。历史迁移工具：`scripts/migrate_uploads_per_collection.py`。
- 代码目录只放程序与配置，不存文献或数据库。

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `XSF_AUTH_TOKEN` | 空 | Web 登录密码；未设则无认证（开发模式） |
| `XSF_DATA` | `~/xsf-data` | 数据根目录 |
| `XSF_DB_DIR` | `XSF_DATA/db/` | 数据库目录，**必须本地磁盘**——网络盘不支持 SQLite 文件锁 |
| `XSF_COLLECTIONS_DIR` | `XSF_DATA/collections/` | 源文件目录，可指 NAS / OSS 挂载路径 |
| `XSF_OCR_METHOD` | 配置文件 default | 默认 OCR provider id |
| `XSF_SCAN_INTERVAL` | `120` | 扫描轮询间隔秒，`0` 关闭整个扫描 |
| `XSF_SCAN_STABLE_SEC` | `60` | 文件稳定阈值秒（mtime 距今小于此值跳过） |
| `XSF_SCAN_OCR` | `1` | PDF 自动 OCR，`0` 仅标记不自动跑 |
| `XSF_HOST` / `XSF_PORT` | `127.0.0.1` / `8090` | Web 绑定地址与端口（便携版端口占用自动顺延 8091-8099） |
| `XSF_ENV` | — | 显式指定 `.env` 路径（便携版；加载链：环境变量 > exe 旁 .env > exe 上级 .env） |

---

## 数据模型

| 表/存储 | 作用 |
|---------|------|
| `documents` | 元数据：cite_key、标题、作者、页数、doc_type、来源标签、书目字段（bib_type/bib_data）、linked_pdf |
| `lines` | 文本层：每页每块每行原文 + bbox 坐标 + block_label + 页面宽高 + suspect 疑点标记——检索定位与校对编辑的基本数据 |
| `blocks_fts` | FTS5 虚拟表，jieba 分词后的块级文本，快速命中页与段 |
| `ocr_page_state` | 页级断点：每页 done/error 状态与错误信息，重跑跳过已完成页 |
| `page_regions` | 栏定义：页面阅读区域 bbox（150dpi 坐标空间）+ 方向（h 横排 / v_rtl 竖右起 / v_ltr 竖左起）+ 阅读顺序 |

「块」是检索单元（FTS 索引、命中定位）；「行」保留物理原文（上下文展示、校对编辑）。栏系统在行/块之上定义**阅读顺序**，竖排古籍按栏重组后检索与校对才不错序。

---

## Collection 导入导出

两种方式：Web 批量接口（`export-bib` / `export-md` / `export-archive` / `import-archive`）与独立 CLI 脚本 `scripts/collection_io.py`。后者用于整体备份、迁移、服务器 ↔ 本地同步。

### 导出

```bash
python scripts/collection_io.py export <collection> [-o output.tar.gz]
```

自动打包该 collection 的 **DB 快照 + 全部源文件 + manifest.json**。DB 用 SQLite backup API 导出，确保 WAL 一致性。缺失文件会列出。

### 导入

```bash
python scripts/collection_io.py import <collection> <archive.tar.gz> \
  [--conflict skip|overwrite] [--db-only] [--force]
```

| 选项 | 说明 |
|------|------|
| `--db-only` | 只导入 DB，跳过源文件（源文件手动同步场景） |
| `--conflict overwrite` | 同名源文件覆盖（默认 skip） |
| `--force` | 覆盖已存在的 collection DB |

---

## 升级注意事项

- **数据与程序分离**：覆盖解压 / 重装不动数据——数据在 `XSF_DATA`（`db/*.db` + 上传的 PDF/图片文件），程序目录里只有 `.env` 和 `ocr-config.json`（含 api_key）需要自己留意。
- **升级前备份**：停服务 → 备份 `$XSF_DATA/db/` 下全部 `*.db` + 上传/文献目录（或 Web 端「导出归档 tar.gz」）+ `.env`、`ocr-config.json`。
- **Schema 迁移是单向的**：新版首次访问数据库会自动建新表/补列（v0.1.1 起含智能校对三表 `ocr_candidates` / `manual_transcripts` / `transcript_pages`）。**升级后请勿直接换回旧版 exe**——需回退时先还原备份。
- **升级后验证**：`xsf stats` 能正常打开、Web 校对页出现「智能校对」按钮即为正常。
- 便携版用户：zip 文件名即版本号，覆盖前先核对。

## Windows 便携版

免安装 zip（双击 `小書房.exe` → 起服务 + 开浏览器 + 托盘常驻），详见 `packaging/README-使用说明.txt`（随 zip 分发）。

```bash
# 构建 (GitHub Actions, 推荐): Actions → windows-portable → Run workflow
#   或 push tag: git tag v0.1.1 && git push origin v0.1.1  (自动附到 release)

# 本地复现 (Windows + Python 3.12):
pip install -e ".[desktop,build,mcp]"
pyinstaller packaging/xsf.spec --noconfirm
pwsh packaging/make_portable.ps1     # → dist/xsf-portable-win64-<ver>.zip
```

- 产物：`小書房.exe`（托盘 GUI）+ `xsf.exe`（CLI）+ `xsf-mcp.exe`（MCP），共享 `_internal/`
- 配置：zip 根 `.env`（复制 `.env.example`），环境变量优先；数据默认 `%USERPROFILE%\xsf-data`
- 单实例：8090 已有服务时只开浏览器
- OCR：不内置本地引擎，Web「OCR 设置」配远程 provider（paddle_http 内网 GPU / aistudio_job 云端 / openai_chat API）

---

## MCP 接入

`xsf-mcp`（`pip install -e ".[mcp]"` 后可用）把检索/上下文/元数据暴露为 MCP 工具，供 AI agent 直接查询文献池。零 API 改动，与 Web/CLI 共享同一数据层。

---

## 部署

### systemd 常驻（Linux）

```ini
# /etc/systemd/system/xsf.service
[Unit]
Description=xsf 文献池
After=network.target

[Service]
WorkingDirectory=/opt/xsf
Environment=XSF_DATA=/opt/xsf-data
EnvironmentFile=/opt/xsf/.env
ExecStart=/opt/xsf/.venv/bin/uvicorn xsf.api:app --host 127.0.0.1 --port 8090
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now xsf
journalctl -u xsf -f        # 实时日志
```

服务只监听本机回环即满足单机使用；如需局域网访问改 `--host 0.0.0.0` 并自行注意安全（设 `XSF_AUTH_TOKEN` + 防火墙）。

### 依赖

运行：PyMuPDF（PDF 提取）、jieba（分词）、requests（OCR client）、fastapi + uvicorn + jinja2 + python-multipart（Web）、pypinyin（排序）、opencc-python-reimplemented（繁简归一）。
可选：`[mcp]`（MCP server）、`[desktop]`（托盘 GUI）、`[build]`（PyInstaller 打包）。

### 改动后验证

```bash
xsf stats                                  # DB 可读
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8090/   # Web 存活
# OCR 冒烟: Web 上传一页扫描件 → 校对页看断点与徽标
```

---

## License

MIT
