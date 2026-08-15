# 20260815 小書房 jiage → xsf 全面改名

## 背景

用户要求项目目录从 jiage 改为 xsf, 数据存放位置改为 xsf-data。摸底发现 `.env` 误粘密码产生脏数据目录 (`~/xiaoshufangMzyj2934!`), 且服务无认证暴露 0.0.0.0:8090。

## 改动

- `git mv jiage xsf` (历史保留), commit `b28636a`
- pyproject / config / api / cli / ocr.registry / collection_io / templates / .env.example / AGENTS.md / README.md 全面改名
- 环境变量 `JIAGE_*` → `XSF_*` (不兼容旧名); DB 文件 `jiage.db` → `xsf.db` (collection_io 导入兼容旧归档)
- 本机: `~/jiage` → `~/xsf`, venv 重建 (uv + 清华镜像); 数据迁移至 `~/xsf-data` (db 内 jiage.db 一并改名)
- `.env`: 误粘密码 `Mzyj2934!` 转正为 `XSF_AUTH_TOKEN`, 服务启用登录认证

## 验证 (2026-08-15)

- `xsf init` / `xsf stats`: 书架「测试」1 篇文献完整
- Web: 未登录 `/` 303→/login、API 401; 登录后书架 200、stats 数据齐全、OCR 两个 provider (本机GPU + aistudio) 随迁完好

## 遗留

- systemd: `xsf.service` 需 sudo 部署 (暂存 /tmp/opencode/xsf.service), 部署命令见 AGENTS.md §本机 GPU 机部署
- GitHub: 待用户网页 rename 为 xsf 后 `git push origin main`
- WSL `~/projects/jiage` 与阿里云 `~/jiage` 待各自迁移 (见 AGENTS.md §改名记录)
