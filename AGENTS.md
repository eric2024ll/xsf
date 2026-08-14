# AGENTS.md — jiage

> histflow-plan 设计的代码落地仓库. 本文件定义**本地开发 + 服务器部署实测**的标准流程.
> 设计依据: histflow-plan `system/tools/14-ocr-pipeline.md`

## 项目定位

jiage 是史学研究工具链的**感知层上游**——把 PDF 变成可检索的文本池，以 Web『小書房』(FastAPI) 为主入口、CLI 为辅.

- **设计来源**: histflow-plan (`/mnt/d/workspace/mem/histflow-plan/`). 两库分工与关系方向的权威定义见其 `schema.md` §与外部系统/仓库的关系；本文件只聚焦 jiage 的开发与部署.
- **原则**: 设计决策在 histflow-plan; 代码与数据实验在 jiage; Bug/约束反馈回 histflow 修订设计
- **数据分离**: 代码在本仓库, 数据在 `~/jiage-data/` (`JIAGE_DATA` 环境变量可覆盖)

## 两地架构

| 角色 | 位置 | 用途 |
|------|------|------|
| **开发机 (WSL)** | `~/projects/jiage/` | 写代码、git commit/push |
| **GitHub** | `git@github.com:eric2024ll/jiage.git` (私有, SSH) | 版本控制中转 |
| **服务器** | `root@47.93.199.96:~/jiage/` | 实测、OCR 跑批 |

## 本地开发规范 (WSL)

### 环境
- venv: `.venv/` (**uv 管理, 无 pip**, 用 `uv pip install`)
- Python 3.11+
- 依赖: PyMuPDF + jieba + requests

### git 流程

```bash
cd ~/projects/jiage
git add -A
git commit -m "<type>: <描述>"    # type = feat / fix / docs / refactor
git push origin main
```

> commit message 描述中英皆可 (参考历史), type 用英文: feat / fix / docs / refactor

### 本地测试

```bash
cd ~/projects/jiage
# OCR provider 在 Web「OCR 设置」页添加 (generic_http: 本地 ~/paddleocr-vl/server.py :8091 或任意远程 API)
.venv/bin/jiage init                                 # 首次初始化 DB
.venv/bin/jiage add <pdf> -c <collection> --ocr      # OCR 入库 (默认 provider)
.venv/bin/jiage add <pdf> -c <collection> --ocr --provider p1   # 指定 provider
.venv/bin/jiage search "<query>"                     # FTS 搜索
.venv/bin/jiage stats                                # 统计
```

### Web 界面 (FastAPI)

```bash
# 本地 (开发模式, auto-reload)
cd ~/projects/jiage
.venv/bin/uvicorn jiage.api:app --reload --port 8090

# 服务器 (绑外网)
cd ~/jiage
source .venv/bin/activate
uvicorn jiage.api:app --host 0.0.0.0 --port 8090
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

## 服务器部署与实测流程 (标准)

> **服务器**: `47.93.199.96` (阿里云轻量 2核4G)
> **Python**: 3.12.3 (`/usr/bin/python3`, **仅 python3 无 python**), pip 24.0

### 首次设置 (一次性)

```bash
# 1. SSH 登录 (如非 root 用户请替换)
ssh root@47.93.199.96

# 2. 配置 GitHub SSH key (如未配置)
ssh-keygen -t ed25519 -C "jiage-server"
cat ~/.ssh/id_ed25519.pub
# → 复制输出, 添加到 GitHub → Settings → SSH and GPG keys → New SSH key

# 3. clone 仓库
cd ~ && git clone git@github.com:eric2024ll/jiage.git

# 4. 创建 venv + 安装
cd ~/jiage
sudo apt install python3.12-venv -y    # Debian/Ubuntu 前置依赖 (ensurepip)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 5. 配置 OSS 数据存储 (collections 上 OSS, jiage.db 留本地)
mkdir -p /mnt/oss/sources/jiage/collections/uploads
echo 'export JIAGE_COLLECTIONS_DIR=/mnt/oss/sources/jiage/collections' >> ~/.bashrc
source ~/.bashrc

# 6. 验证安装
jiage init      # 初始化 ~/jiage-data/jiage.db
jiage stats     # 应显示空库
```

### 日常更新 (本地 push 后)

```bash
# 1. 服务器拉取最新代码
ssh root@47.93.199.96
cd ~/jiage && git pull origin main

# 2. 依赖变更时重新安装 (pyproject.toml 改了才需要)
source .venv/bin/activate
pip install -e .

# 3. 重启 systemd 服务
systemctl restart jiage

# 4. 查看日志
journalctl -u jiage -f
```

### systemd 服务部署 (推荐)

> 用 systemd 管理 uvicorn 进程，实现开机自启 + 崩溃自动重启.

#### 1. 创建服务文件

```bash
cat > /etc/systemd/system/jiage.service << 'EOF'
[Unit]
Description=jiage Web Service
After=network.target ossfs2-sources.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/jiage
EnvironmentFile=/root/jiage/.env
ExecStart=/root/jiage/.venv/bin/uvicorn jiage.api:app --host 0.0.0.0 --port 8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

#### 2. 创建 .env 文件 (chmod 600)

```bash
cat > /root/jiage/.env << 'EOF'
JIAGE_AUTH_TOKEN=<用户自设密码>
JIAGE_COLLECTIONS_DIR=/mnt/oss/sources/jiage/collections
EOF
chmod 600 /root/jiage/.env
```

> **JIAGE_AUTH_TOKEN**: 设为你的登录密码. 留空或不设则无认证（开发模式）.
> 设了之后访问任何页面都需先登录 (`/login`).

> **⚠ DB 存储位置**: jiage.db 必须在**本地磁盘** (`JIAGE_DB_DIR`, 默认 `~/jiage-data/db/`),
> 不能放 OSS (ossfs 不支持 SQLite 文件锁, 会报 `disk I/O error`).
> 如果旧版 collection 重构时 DB 被放到了 OSS 上, 需迁移到本地:
> ```bash
> mkdir -p ~/jiage-data/db
> for d in /mnt/oss/sources/jiage/collections/*/; do
>   coll=$(basename "$d")
>   if [ -f "$d/jiage.db" ]; then
>     mkdir -p ~/jiage-data/db/"$coll"
>     mv "$d/jiage.db" ~/jiage-data/db/"$coll"/
>   fi
> done
> ```

#### 3. 部署命令

```bash
# 首次
systemctl daemon-reload
systemctl enable jiage
systemctl start jiage
systemctl status jiage          # 确认 active (running)

# 查看日志
journalctl -u jiage -f          # 实时跟踪
journalctl -u jiage --since "1 hour ago"  # 最近1小时

# 更新代码后
cd /root/jiage && git pull origin main && .venv/bin/pip install -e .
systemctl restart jiage

# 停止/启动
systemctl stop jiage
systemctl start jiage
```

#### 4. 验证

```bash
# 无 token 时（开发模式）直接访问 / 返回 200
curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/

# 有 token 时访问 / 重定向到 /login (303)
curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/

# 登录获取 cookie
curl -s -X POST http://localhost:8090/login -d 'password=<你的密码>' -c /tmp/jiage_cookie -w '%{http_code}\n'

# 带 cookie 访问
curl -s -b /tmp/jiage_cookie http://localhost:8090/ -o /dev/null -w '%{http_code}\n'
```

### OCR 实测注意事项

- OCR provider 两种类型（Web「OCR 设置」添加）: `generic_http`（本地 GPU 服务 `~/paddleocr-vl/server.py` :8091 或任意兼容 API）/ `aistudio`（内置云端三阶段, 只填 token）
- 无 provider 时上传 OCR 会报友好错误（引导去 OCR 设置页）
- 网络错误/5xx 自动指数退避, 整体重试 MAX 3 次
- **样本来源**: `~/projects/两岸近代三交资料与研究/` 下的史料 PDF (需上传到服务器, 或用服务器上已有的 PDF)

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `JIAGE_AUTH_TOKEN` | 可选 | Web 登录密码. 未设则无认证（开发模式）; 设了则所有页面需登录 |
| `JIAGE_DATA` | 可选 | 数据根目录, 默认 `~/jiage-data/` |
| `JIAGE_DB_DIR` | 可选 | **数据库目录(本地磁盘!)**, 默认 `JIAGE_DATA/db/`. ossfs 不支持 SQLite 文件锁, 服务器上**不要**指向 OSS |
| `JIAGE_COLLECTIONS_DIR` | 可选 | 源文件+上传目录, 默认 `JIAGE_DATA/collections/`; 服务器指向 OSS `/mnt/oss/sources/jiage/collections/` |
| `JIAGE_OCR_METHOD` | 可选 | 默认 OCR provider id (匹配 ocr-config.json). 未设则取配置文件 default > 首个 provider |

## 常用命令速查

```bash
# === 本地 (WSL, uv venv) ===
cd ~/projects/jiage
uv pip install -e .                                  # 安装/更新依赖
.venv/bin/jiage init                                 # 初始化 DB
.venv/bin/jiage add <pdf> -c <col>                   # born-digital PDF
.venv/bin/jiage add <pdf> -c <col> --ocr             # 扫描件 OCR (PaddleOCR-VL)
.venv/bin/jiage search "<query>"                     # FTS 搜索
.venv/bin/jiage context <doc_id> <page> <block>      # 查看上下文
.venv/bin/jiage remove <doc_id>                      # 删除文献
.venv/bin/jiage stats                                # 统计

# === 服务器 (标准 venv) ===
ssh root@47.93.199.96
cd ~/jiage && git pull origin main                   # 更新代码
source .venv/bin/activate
jiage <command>                                      # 同上
```

## Notes for the LLM

- 中文思考、中文回复; commit message 用英文
- **不自行 push**: 写完代码后 commit, 报告 hash, 提示用户手动 push
- **不自行跑 OCR 测试** (省 token): 给出验证命令让用户执行
- 代码改动遵循 histflow-plan 的设计文档 (`system/tools/14-ocr-pipeline.md` 是 OCR 直接依据)
- 发现的设计问题 (新 block_label / 坐标问题等) 反馈到 histflow-plan 设计文档
