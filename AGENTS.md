# AGENTS.md — jiage

> histflow-plan 设计的代码落地仓库. 本文件定义**本地开发 + 服务器部署实测**的标准流程.
> 设计依据: histflow-plan `system/tools/14-ocr-pipeline.md`

## 项目定位

jiage 是史学研究工具链的**感知层上游**——把 PDF 变成可检索的文本池.

- **设计来源**: histflow-plan (`/mnt/d/workspace/mem/histflow-plan/`)
- **原则**: 设计决策在 histflow-plan; 代码与数据实验在 jiage; 实现中的 Bug/约束反馈回 histflow 修订设计
- **主入口**: CLI (`jiage` 命令), 未来 P3 加 Web (FastAPI)
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

> commit message 用英文, 参考历史: `feat: P1 OCR integration with PaddleOCR-VL`

### 本地测试

```bash
cd ~/projects/jiage
export PADDLE_OCR_TOKEN="<token>"                   # 从 aistudio 获取
.venv/bin/jiage init                                 # 首次初始化 DB
.venv/bin/jiage add <pdf> -c <collection> --ocr      # OCR 入库
.venv/bin/jiage search "<query>"                     # FTS 搜索
.venv/bin/jiage stats                                # 统计
```

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

# 5. 配置 OCR token (持久化到 ~/.bashrc)
echo 'export PADDLE_OCR_TOKEN="<从 aistudio 获取的 token>"' >> ~/.bashrc
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

# 3. 实测
jiage add <pdf路径> -c <collection> --ocr
jiage search "<查询词>"
jiage stats
```

### OCR 实测注意事项

- 单文件 **< 100 页** (PaddleOCR-VL 限制)
- 需 `PADDLE_OCR_TOKEN` 环境变量 (aistudio bearer token)
- 重试 {429, 500, 502, 503, 504} 自动指数退避, 整体重试 MAX 3 次
- **样本来源**: `~/projects/两岸近代三交资料与研究/` 下的史料 PDF (需上传到服务器, 或用服务器上已有的 PDF)

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `PADDLE_OCR_TOKEN` | OCR 时必填 | aistudio bearer token |
| `JIAGE_DATA` | 可选 | 数据目录, 默认 `~/jiage-data/` |
| `JIAGE_OCR_METHOD` | 可选 | OCR provider, 默认 `paddle_api` |

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
