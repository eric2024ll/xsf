# AGENTS.md — xsf

> histflow-plan / pqa 设计的代码落地仓库. 本文件定义**本机 GPU 机开发 + 部署一体**的标准流程 (WSL 为备用).
> 设计依据: pqa `design/client/14-ocr-pipeline.md`（2026-09-19 自 histflow-plan 移交，mem vault 内路径 `/mnt/d/workspace/mem/pqa/`）

## 项目定位

xsf 是史学研究工具链的**感知层上游**——把 PDF 变成可检索的文本池，以 Web『小書房』(FastAPI) 为主入口、CLI 为辅.

- **设计来源**: histflow-plan（体系/方法论）+ pqa（L0 语料层设计，`/mnt/d/workspace/mem/pqa/design/`，2026-09-19 起小书房设计文档统一管理于此）+ litsearch（L1-L2 检索执行体策划，`/mnt/d/workspace/mem/litsearch/`，2026-09-20 起；对 xsf API 的消费契约见其 `design/02-分层契约.md`）. 各库分工与关系方向的权威定义见各自 `schema.md`；本文件只聚焦 xsf 的开发与部署.
- **原则**: 设计决策在 mem vault 记录（体系设计在 histflow-plan，L0 语料层设计在 pqa，L1-L2 检索执行体策划在 litsearch）; 代码与数据实验在 xsf; Bug/约束反馈回相应设计库修订（检索能力问题按层归口: 语料层→pqa `design/20-检索问题库.md` X 组, 策略层→litsearch `design/20-检索问题库-L组.md`）
- **数据分离**: 代码在本仓库, 数据在 `~/xsf-data/` (`XSF_DATA` 环境变量可覆盖)

## 多机架构

| 角色 | 位置 | 用途 |
|------|------|------|
| **本机 GPU 机 (标准)** | `~/xsf/` | **开发 + 部署一体**: 写代码、git commit/push、systemd 常驻 Web (:8090) + 本地 OCR (:8091) |
| **GitHub** | `git@github.com:eric2024ll/xsf.git` (私有, SSH) | 版本控制中转 |
| **WSL 开发机 (备用)** | `~/projects/xsf/` | 备用开发环境, 改动经 GitHub 同步 |

> 2026-08-15 由 `jiage` 全面改名 `xsf`. GitHub 旧 URL 自动 redirect;
> WSL `~/projects/jiage/` 尚待迁移 (见 §改名记录).
> 2026-09-30 阿里云服务器已退租停用, 云部署叙事移除 (OCR aistudio 云端 provider 不受影响).

## 本机标准流程 (开发 + 部署一体)

> **2026-08-21 用户裁定**: 本机 GPU 机 (`~/xsf/`) 为标准开发部署环境,
> 写代码、commit、push、重启服务全在本机完成; WSL 降为辅助.

- **代码**: `~/xsf/` (git clone, venv 同目录, 标准 pip venv)
- **Web**: systemd `xsf.service` — `uvicorn xsf.api:app --host 0.0.0.0 --port 8090`, `EnvironmentFile=/home/eric/xsf/.env`
- **数据**: `~/xsf-data/` (`.env` 里 `XSF_DATA` 指定; db/collections/ocr-config.json 都在此)
- **本地 OCR**: systemd `paddleocr-vl.service` (:8091, `~/paddleocr-vl/server.py`), Web「OCR 设置」里以 generic_http provider 接入

### git 流程 (标准)

```bash
cd ~/xsf
git add -A
git commit -m "<type>: <描述>"    # type = feat / fix / docs / refactor
git push origin main              # agent 不自行 push, 报 hash 由用户手动 push
```

### 改代码后的部署

```bash
cd ~/xsf
.venv/bin/python -m pip install -e .   # 仅依赖变更 (pyproject.toml 改了) 才需要; 本机 venv 无 pip 二进制, 首次需 ensurepip 引导
sudo systemctl restart xsf
journalctl -u xsf -f              # 实时日志
```

### CLI 测试

```bash
cd ~/xsf
.venv/bin/xsf init                                 # 首次初始化 DB
.venv/bin/xsf add <pdf> -c <collection> --ocr      # OCR 入库 (默认 provider)
.venv/bin/xsf add <pdf> -c <collection> --ocr --provider p1   # 指定 provider
.venv/bin/xsf search "<query>"                     # FTS 搜索
.venv/bin/xsf stats                                # 统计
```

## 备用: WSL 开发机 (`~/projects/xsf/`)

### 环境
- venv: `.venv/` (**uv 管理, 无 pip**, 用 `uv pip install`), 与本机的 pip venv 不同
- Python 3.11+
- 依赖: PyMuPDF + jieba + requests
- git 流程同本机标准流程; 改动经 GitHub 同步到本机 (`git pull`)

### 本地测试

```bash
cd ~/projects/xsf
# OCR provider 在 Web「OCR 设置」页添加 (generic_http: 本地 ~/paddleocr-vl/server.py :8091 或任意远程 API)
.venv/bin/xsf init                                 # 首次初始化 DB
.venv/bin/xsf add <pdf> -c <collection> --ocr      # OCR 入库 (默认 provider)
.venv/bin/xsf add <pdf> -c <collection> --ocr --provider p1   # 指定 provider
.venv/bin/xsf search "<query>"                     # FTS 搜索
.venv/bin/xsf stats                                # 统计
```

### Web 界面 (FastAPI)

```bash
# 本机开发模式 (auto-reload, 停 systemd 后用)
cd ~/xsf
.venv/bin/uvicorn xsf.api:app --reload --port 8090
```

端点 (按功能分组，`{c}` = collection):

**页面 (HTML)**
- `/` 书架页 (全库统计 + 书架列表 + 最近文献; 记 `last_collection` cookie 供下次回归)
- `/search` 跨书架搜索页
- `/collections/{c}/docs/list` 文献列表页
- `/collections/{c}/upload` 资料上传页
- `/collections/{c}/doc/{id}/preview` 纯文本预览
- `/collections/{c}/doc/{id}/proofread` 图文对照 OCR 校对页

**书架 / 配置 API**
- `GET /api/collections` 书架列表
- `POST` / `DELETE` / `PATCH /api/collections/{c}` 创建 / 删除 / 重命名书架
- `GET /api/ocr-config` provider 列表 (key 打码) + default
- `POST /api/ocr-config/provider` 新增/编辑 provider　`DELETE /api/ocr-config/provider/{id}` 删除
- `POST /api/ocr-config/default` 设默认　`POST /api/ocr-config/test` 连通测试 (空白页 PDF)
- `GET /api/stats` 全库统计　`GET /collections/{c}/stats` 单书架统计

**文献操作 API**
- `GET /collections/{c}/docs` 文献列表(简)　`/docs/query` 分页筛选
- `POST /collections/{c}/add` 上传 (pdf/md/word/图片, 可 OCR; word=doc/docx/docm 经 anydoc 转 Markdown)
- `DELETE` / `PATCH /collections/{c}/doc/{id}` 删除 / 改元数据
- `POST /collections/{c}/doc/{id}/link-pdf` 关联 PDF
- `GET /collections/{c}/doc/{id}/content` 文献内容(页/块/行)
- `GET /collections/{c}/doc/{id}/page/{p}/image` PDF 页 PNG
- `POST /collections/{c}/doc/{id}/line/{lid}/edit` 保存行编辑
- `POST /collections/{c}/doc/{id}/page/{p}/reocr` 手工分栏重 OCR
- `GET /collections/{c}/doc/{id}/hits` 文档内命中
- `GET /collections/{c}/search` 书架内搜索　`GET /collections/{c}/context/{doc_id}/{page}/{block}` 命中上下文

**批量导入 / 导出**
- `POST .../docs/export-bib` `/export-md` `/export-archive` 导出　`.../docs/import-archive` 导入
- `POST .../docs/match-bib` 自动书目匹配　`.../docs/batch-patch` 批量改元数据

## Windows 便携版打包

> 设计依据: pqa `design/client/18-windows-portable.md`. CLI + skill + MCP 全保留, 零 API 改动.

- **构建**: GitHub Actions `windows-portable` workflow (workflow_dispatch 手动触发, 或 push tag `v*` 自动构建并附 release)
- **本地复现** (Windows + Python 3.12): `pip install -e ".[desktop,build,office,mcp]"` → `pyinstaller packaging/xsf.spec --noconfirm` → `pwsh packaging/make_portable.ps1`
- **产物**: `dist/xsf-portable-win64-<ver>.zip` — `小書房.exe` (托盘 GUI, windowed) + `xsf.exe` (CLI) + `xsf-mcp.exe` 共享 `_internal/`, 附 `env.example.txt` / `README-使用说明.txt` / `add-to-path.bat`
- **配置文件加载**: `xsf/env.py` — 环境变量 > `$XSF_ENV` 显式 > exe 旁 `.env`/`env.txt` > exe 上级 `.env`/`env.txt` > 包根 `.env`/`env.txt` (同级 `.env` 优先; `env.txt` 为 Windows 便携版通道——资源管理器无法手工建点开头文件名; 托盘菜单「编辑配置文件」可程序生成; Linux systemd 部署不受影响, 只填缺失键)
- **托盘启动器**: `xsf/desktop.py` — 绑 127.0.0.1 (XSF_HOST 可改), 端口 8090 起顺延 (XSF_PORT), 单实例检测, pystray 托盘
- **OCR**: 便携版不内置本地引擎, 走远程 provider (generic_http 内网 GPU / aistudio 云端)
- **Word 上传**: 0.2.1 起内置 anydoc (Rust 核心随包, hiddenimports 收集), 便携版即传即转; CI 冒烟: `packaging/make_smoke_docx.py` → frozen exe add → search 断言
- **数据迁移**: Web 导出/导入归档, 或 `scripts/collection_io.py`


## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `XSF_DATA` | 可选 | 数据根目录, 默认 `~/xsf-data/` |
| `XSF_DB_DIR` | 可选 | **数据库目录(本地磁盘!)**, 默认 `XSF_DATA/db/`. 不要指向网络文件系统 (NFS/ossfs 不支持 SQLite 文件锁) |
| `XSF_COLLECTIONS_DIR` | 可选 | 源文件+上传目录, 默认 `XSF_DATA/collections/`; 本机 NAS 部署指向 `/mnt/nas/xsf-collections` |
| `XSF_OCR_METHOD` | 可选 | 默认 OCR provider id (匹配 ocr-config.json). 未设则取配置文件 default > 首个 provider |
| `XSF_SCAN_INTERVAL` | 可选 | 文件夹扫描轮询间隔秒, 默认 120, `0` 关闭. PDF/MD 丢进 `{书架}/uploads/` 自动入库 |
| `XSF_SCAN_STABLE_SEC` | 可选 | 文件稳定阈值秒 (默认 60), mtime 距今小于此值视为仍在写入, 下轮再收 |
| `XSF_SCAN_OCR` | 可选 | PDF 自动 OCR (默认 1, 不区分 born-digital, 2026-09-04 起), `0` 仅标记待OCR 不自动跑. Web 上传 OCR 复选框默认勾选 |

## 常用命令速查

```bash
# === 本机 GPU 机 (标准) ===
cd ~/xsf
git add -A && git commit -m "<type>: <描述>"          # git 流程
.venv/bin/python -m pip install -e .                   # 依赖变更时
.venv/bin/xsf init                                   # 初始化 DB
.venv/bin/xsf add <pdf> -c <col>                     # born-digital PDF
.venv/bin/xsf add <pdf> -c <col> --ocr               # 扫描件 OCR (PaddleOCR-VL)
.venv/bin/xsf search "<query>"                       # FTS 搜索
.venv/bin/xsf context <doc_id> <page> <block>        # 查看上下文
.venv/bin/xsf remove <doc_id>                        # 删除文献
.venv/bin/xsf stats                                  # 统计
.venv/bin/xsf doctor                                 # 环境自检: 数据目录/书架一致性/孤儿/多实例
sudo systemctl restart xsf                           # 部署重启
journalctl -u xsf -f                                 # 实时日志

# === WSL 备用机 (uv venv) ===
cd ~/projects/xsf
uv pip install -e .                                  # 安装/更新依赖
# 其余命令同上 (先 .venv/bin/activate 或用全路径)
```

## 改名记录 (2026-08-15)

`jiage` → `xsf` 全面改名 (仓库/包/CLI/环境变量/db 文件名/cookie/导出文件名):

| 项 | 旧 | 新 |
|----|----|----|
| 目录 | `~/jiage` (本机/服务器), `~/projects/jiage` (WSL) | `~/xsf`, `~/projects/xsf` |
| 包名 / CLI | `jiage` / `jiage` | `xsf` / `xsf` |
| 环境变量 | `JIAGE_DATA` 等 5 个 | `XSF_DATA` 等 5 个 (**不识别旧名**) |
| DB 文件 | `db/{collection}/jiage.db` | `db/{collection}/xsf.db` (导入兼容旧名) |
| GitHub | `eric2024ll/jiage` | `eric2024ll/xsf` (旧 URL redirect) |

**遗留 follow-up**:
- [ ] WSL 开发机: `mv ~/projects/jiage ~/projects/xsf` + 改 remote + 重建 venv
- [ ] 旧数据目录 `~/jiage-data`、`~/xiaoshufang` 确认后清理

## Notes for the LLM

- 中文思考、中文回复; commit message 用英文
- **不自行 push**: 写完代码后 commit, 报告 hash, 提示用户手动 push
- **不自行跑 OCR 测试** (省 token): 给出验证命令让用户执行
- 代码改动遵循 pqa 的设计文档 (`design/client/14-ocr-pipeline.md` 是 OCR 直接依据)
- 发现的设计问题 (新 block_label / 坐标问题等) 反馈到 histflow-plan 设计文档
