# 20260813 待讨论：用 agent 访问小書房数据

## 议题

如何让 agent（如 opencode/其他 LLM agent）访问小書房的数据？

## 背景

小書房 (jiage) 当前只有 web UI + CLI 两套入口。agent 需要能够：
- 查询文献库（搜索、按条件过滤）
- 读取文献内容（页/块/行）
- 可能需要写入（标注、bib 元数据）

## 待讨论方向

1. **MCP server**：把现有 API 封装为 MCP 工具，agent 通过 MCP 协议访问
2. **REST API 直接调用**：agent 用 http 调现有 `/collections/{c}/search` 等端点
3. **直读 SQLite**：agent 用 SQL 查 jiage.db（绕过 API 层）
4. **wikisearch 集成**：把 jiage 数据纳入 wikisearch 的 FTS5 索引

## 约束

- 认证：当前可选 `JIAGE_AUTH_TOKEN`，agent 访问需处理
- 数据格式：页/块/行三级结构，agent 需理解
- 只读 vs 读写：先只读，还是一步到位支持写入

---

# 前置议题：部署到群晖 NAS + 阿里云外网访问

## 议题

将小書房部署在群晖 NAS 上，通过阿里云服务器实现外网访问。

## 待讨论方向

1. **群晖部署方式**：Docker Container（推荐，隔离性好）vs Python venv 直接跑 vs Synology 的 Python 环境
2. **数据持久化**：`~/jiage-data/` 映射到 NAS 共享文件夹，SQLite + 上传文件跨容器挂载
3. **内网服务**：NAS 本地 `uvicorn --host 0.0.0.0 --port 8000`，局域网直连
4. **外网穿透方案**：
   - 阿里云服务器做反向代理（frp / nginx reverse proxy / SSH 隧道）
   - 群晖自带 DDNS + 端口转发（需公网 IP 或路由器支持）
   - 阿里云 ECS 跑 frp server，NAS 跑 frp client（无需公网 IP）
5. **HTTPS**：阿里云域名 + Let's Encrypt 证书，在 ECS 上终止 TLS 再转发到 NAS
6. **认证加固**：外网暴露后 `JIAGE_AUTH_TOKEN` 必须开启，或加 Basic Auth / IP 白名单

## 约束

- 群晖 NAS：型号/架构未知（ARM vs x86 影响 Docker 镜像选择），待确认
- 阿里云 ECS：规格/带宽/是否有公网 IP，待确认
- 域名：是否已有备案域名（国内服务器需备案）
- 带宽：上传大 PDF / OCR 图片预览对带宽有要求
