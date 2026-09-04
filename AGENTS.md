# AGENTS.md — xsf

> histflow-plan 设计的代码落地仓库. 本文件定义**本机 GPU 机开发 + 部署一体**的标准流程 (WSL / 阿里云为备用).
> 设计依据: histflow-plan `system/tools/14-ocr-pipeline.md`

## 项目定位

xsf 是史学研究工具链的**感知层上游**——把 PDF 变成可检索的文本池，以 Web『小書房』(FastAPI) 为主入口、CLI 为辅.

- **设计来源**: histflow-plan (`/mnt/d/workspace/mem/histflow-plan/`). 两库分工与关系方向的权威定义见其 `schema.md` §与外部系统/仓库的关系；本文件只聚焦 xsf 的开发与部署.
- **原则**: 设计决策在 histflow-plan; 代码与数据实验在 xsf; Bug/约束反馈回 histflow 修订设计
- **数据分离**: 代码在本仓库, 数据在 `~/xsf-data/` (`XSF_DATA` 环境变量可覆盖)

## 多机架构

| 角色 | 位置 | 用途 |
|------|------|------|
| **本机 GPU 机 (标准)** | `~/xsf/` | **开发 + 部署一体**: 写代码、git commit/push、systemd 常驻 Web (:8090) + 本地 OCR (:8091) |
| **GitHub** | `git@github.com:eric2024ll/xsf.git` (私有, SSH) | 版本控制中转 |
| **WSL 开发机 (备用)** | `~/projects/xsf/` | 备用开发环境, 改动经 GitHub 同步 |
| **阿里云服务器 (可选)** | `root@47.93.199.96:~/xsf/` | 实测、OCR 跑批 |

> 2026-08-15 由 `jiage` 全面改名 `xsf`. GitHub 旧 URL 自动 redirect;
> WSL `~/projects/jiage/` 与阿里云 `~/jiage/` 尚待各自迁移 (见 §改名记录).

## 本机标准流程 (开发 + 部署一体)

> **2026-08-21 用户裁定**: 本机 GPU 机 (`~/xsf/`) 为标准开发部署环境,
> 写代码、commit、push、重启服务全在本机完成; WSL 与阿里云降为辅助.

- **代码**: `~/xsf/` (git clone, venv 同目录, 标准 pip venv)
- **Web**: systemd `xsf.service` — `uvicorn xsf.api:app --host 0.0.0.0 --port 8090`, `EnvironmentFile=/home/eric/xsf/.env`
- **数据**: `~/xsf-data/` (`.env` 里 `XSF_DATA` 指定; db/collections/ocr-config.json 都在此)
- **本地 OCR**: systemd `paddleocr-vl.service` (:8091, `~/paddleocr-vl/server.py`), Web「OCR 设置」里以 generic_http provider 接入
- **认证**: `.env` 里 `XSF_AUTH_TOKEN` 设密码, 所有页面需登录

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
.venv/bin/pip install -e .        # 仅依赖变更 (pyproject.toml 改了) 才需要
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

# 阿里云服务器 (绑外网)
cd ~/xsf
source .venv/bin/activate
uvicorn xsf.api:app --host 0.0.0.0 --port 8090
# → 浏览器访问 http://47.93.199.96:8090
```

端点 (按功能分组，`{c}` = collection):

**页面 (HTML)**
- `/` 书架页 (全库统计 + 书架列表 + 最近文献; 记 `last_collection` cookie 供下次回归)
- `/login` `/logout` 登录 / 退出
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
- `POST /collections/{c}/add` 上传 (pdf/md/图片, 可 OCR)
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

## 阿里云服务器部署 (可选: 实测/OCR 跑批)

> **服务器**: `47.93.199.96` (阿里云轻量 2核4G)
> **Python**: 3.12.3 (`/usr/bin/python3`, **仅 python3 无 python**), pip 24.0

### 首次设置 (一次性)

```bash
# 1. SSH 登录 (如非 root 用户请替换)
ssh root@47.93.199.96

# 2. 配置 GitHub SSH key (如未配置)
ssh-keygen -t ed25519 -C "xsf-server"
cat ~/.ssh/id_ed25519.pub
# → 复制输出, 添加到 GitHub → Settings → SSH and GPG keys → New SSH key

# 3. clone 仓库
cd ~ && git clone git@github.com:eric2024ll/xsf.git

# 4. 创建 venv + 安装
cd ~/xsf
sudo apt install python3.12-venv -y    # Debian/Ubuntu 前置依赖 (ensurepip)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 5. 配置 OSS 数据存储 (collections 上 OSS, xsf.db 留本地)
#    uploads 按书架分目录, 程序会自动创建 <collections>/{书架}/uploads/, 这里只建根目录
mkdir -p /mnt/oss/sources/xsf/collections
echo 'export XSF_COLLECTIONS_DIR=/mnt/oss/sources/xsf/collections' >> ~/.bashrc
source ~/.bashrc

# 6. 验证安装
xsf init       # 初始化 ~/xsf-data/ 目录结构
xsf stats      # 应显示空库
```

### 日常更新 (本地 push 后)

```bash
# 1. 服务器拉取最新代码
ssh root@47.93.199.96
cd ~/xsf && git pull origin main

# 2. 依赖变更时重新安装 (pyproject.toml 改了才需要)
source .venv/bin/activate
pip install -e .

# 3. 重启 systemd 服务
systemctl restart xsf

# 4. 查看日志
journalctl -u xsf -f
```

### systemd 服务部署 (推荐)

> 用 systemd 管理 uvicorn 进程，实现开机自启 + 崩溃自动重启.

#### 1. 创建服务文件

```bash
cat > /etc/systemd/system/xsf.service << 'EOF'
[Unit]
Description=xsf Web Service
After=network.target ossfs2-sources.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/xsf
EnvironmentFile=/root/xsf/.env
ExecStart=/root/xsf/.venv/bin/uvicorn xsf.api:app --host 0.0.0.0 --port 8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

#### 2. 创建 .env 文件 (chmod 600)

```bash
cat > /root/xsf/.env << 'EOF'
XSF_AUTH_TOKEN=<用户自设密码>
XSF_COLLECTIONS_DIR=/mnt/oss/sources/xsf/collections
EOF
chmod 600 /root/xsf/.env
```

> **XSF_AUTH_TOKEN**: 设为你的登录密码. 留空或不设则无认证（开发模式）.
> 设了之后访问任何页面都需先登录 (`/login`).

> **⚠ DB 存储位置**: xsf.db 必须在**本地磁盘** (`XSF_DB_DIR`, 默认 `~/xsf-data/db/`),
> 不能放 OSS (ossfs 不支持 SQLite 文件锁, 会报 `disk I/O error`).

#### 3. 部署命令

```bash
# 首次
systemctl daemon-reload
systemctl enable xsf
systemctl start xsf
systemctl status xsf          # 确认 active (running)

# 查看日志
journalctl -u xsf -f          # 实时跟踪
journalctl -u xsf --since "1 hour ago"  # 最近1小时

# 更新代码后
cd /root/xsf && git pull origin main && .venv/bin/pip install -e .
systemctl restart xsf

# 停止/启动
systemctl stop xsf
systemctl start xsf
```

#### 4. 验证

```bash
# 无 token 时（开发模式）直接访问 / 返回 200
curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/

# 有 token 时访问 / 重定向到 /login (303)
curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/

# 登录获取 cookie
curl -s -X POST http://localhost:8090/login -d 'password=<你的密码>' -c /tmp/xsf_cookie -w '%{http_code}\n'

# 带 cookie 访问
curl -s -b /tmp/xsf_cookie http://localhost:8090/ -o /dev/null -w '%{http_code}\n'
```

### OCR 实测注意事项

- OCR provider 两种类型（Web「OCR 设置」添加）: `generic_http`（本地 GPU 服务 `~/paddleocr-vl/server.py` :8091 或任意兼容 API）/ `aistudio`（内置云端三阶段, 只填 token）
- 无 provider 时上传 OCR 会报友好错误（引导去 OCR 设置页）
- 网络错误/5xx 自动指数退避, 整体重试 MAX 3 次
- **样本来源**: `~/projects/两岸近代三交资料与研究/` 下的史料 PDF (需上传到服务器, 或用服务器上已有的 PDF)

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `XSF_AUTH_TOKEN` | 可选 | Web 登录密码. 未设则无认证（开发模式）; 设了则所有页面需登录 |
| `XSF_DATA` | 可选 | 数据根目录, 默认 `~/xsf-data/` |
| `XSF_DB_DIR` | 可选 | **数据库目录(本地磁盘!)**, 默认 `XSF_DATA/db/`. ossfs 不支持 SQLite 文件锁, 服务器上**不要**指向 OSS |
| `XSF_COLLECTIONS_DIR` | 可选 | 源文件+上传目录, 默认 `XSF_DATA/collections/`; 服务器指向 OSS `/mnt/oss/sources/xsf/collections/` |
| `XSF_OCR_METHOD` | 可选 | 默认 OCR provider id (匹配 ocr-config.json). 未设则取配置文件 default > 首个 provider |
| `XSF_SCAN_INTERVAL` | 可选 | 文件夹扫描轮询间隔秒, 默认 120, `0` 关闭. PDF/MD 丢进 `{书架}/uploads/` 自动入库 |
| `XSF_SCAN_STABLE_SEC` | 可选 | 文件稳定阈值秒 (默认 60), mtime 距今小于此值视为仍在写入, 下轮再收 |
| `XSF_SCAN_OCR` | 可选 | PDF 自动 OCR (默认 1, 不区分 born-digital, 2026-09-04 起), `0` 仅标记待OCR 不自动跑. Web 上传 OCR 复选框默认勾选 |

## 常用命令速查

```bash
# === 本机 GPU 机 (标准) ===
cd ~/xsf
git add -A && git commit -m "<type>: <描述>"          # git 流程
.venv/bin/pip install -e .                            # 依赖变更时
.venv/bin/xsf init                                   # 初始化 DB
.venv/bin/xsf add <pdf> -c <col>                     # born-digital PDF
.venv/bin/xsf add <pdf> -c <col> --ocr               # 扫描件 OCR (PaddleOCR-VL)
.venv/bin/xsf search "<query>"                       # FTS 搜索
.venv/bin/xsf context <doc_id> <page> <block>        # 查看上下文
.venv/bin/xsf remove <doc_id>                        # 删除文献
.venv/bin/xsf stats                                  # 统计
sudo systemctl restart xsf                           # 部署重启
journalctl -u xsf -f                                 # 实时日志

# === WSL 备用机 (uv venv) ===
cd ~/projects/xsf
uv pip install -e .                                  # 安装/更新依赖
# 其余命令同上 (先 .venv/bin/activate 或用全路径)

# === 阿里云服务器 (可选) ===
ssh root@47.93.199.96
cd ~/xsf && git pull origin main && .venv/bin/pip install -e .
systemctl restart xsf
```

## 改名记录 (2026-08-15)

`jiage` → `xsf` 全面改名 (仓库/包/CLI/环境变量/db 文件名/cookie/导出文件名):

| 项 | 旧 | 新 |
|----|----|----|
| 目录 | `~/jiage` (本机/服务器), `~/projects/jiage` (WSL) | `~/xsf`, `~/projects/xsf` |
| 包名 / CLI | `jiage` / `jiage` | `xsf` / `xsf` |
| 环境变量 | `JIAGE_DATA` 等 5 个 | `XSF_DATA` 等 5 个 (**不识别旧名**) |
| DB 文件 | `db/{collection}/jiage.db` | `db/{collection}/xsf.db` (导入兼容旧名) |
| 登录 cookie | `jiage_auth` | `xsf_auth` (改名后需重新登录) |
| GitHub | `eric2024ll/jiage` | `eric2024ll/xsf` (旧 URL redirect) |

**遗留 follow-up**:
- [ ] WSL 开发机: `mv ~/projects/jiage ~/projects/xsf` + 改 remote + 重建 venv
- [ ] 阿里云服务器: `~/jiage` 迁移 + systemd unit 更名 + OSS 路径迁移
- [ ] 旧数据目录 `~/jiage-data`、`~/xiaoshufang` 确认后清理

## Notes for the LLM

- 中文思考、中文回复; commit message 用英文
- **不自行 push**: 写完代码后 commit, 报告 hash, 提示用户手动 push
- **不自行跑 OCR 测试** (省 token): 给出验证命令让用户执行
- 代码改动遵循 histflow-plan 的设计文档 (`system/tools/14-ocr-pipeline.md` 是 OCR 直接依据)
- 发现的设计问题 (新 block_label / 坐标问题等) 反馈到 histflow-plan 设计文档
