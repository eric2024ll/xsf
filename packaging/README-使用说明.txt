小書房 (xsf) Windows 便携版 — 使用说明
========================================

【快速开始】
1. 解压 zip 到任意目录 (路径可含中文, 建议避免 OneDrive 同步目录)
2. 双击「小書房.exe」:
   - 首次启动自动在 %USERPROFILE%\xsf-data\ 建数据目录
   - 浏览器自动打开 http://127.0.0.1:8090/
   - 系统托盘出现小書房图标: 菜单可「打开小書房」/「退出」
   - 服务只监听本机回环 (127.0.0.1), 不对外网开放
3. 再次双击「小書房.exe」= 单实例: 只会再开一个浏览器页, 不会重复起服务
4. 8090 端口被占时自动顺延 (8091-8099), 实际端口见托盘提示

【配置 (.env)】
- 想设登录密码 / 改数据目录: 复制 .env.example 为 .env, 放在本目录, 按注释修改
- XSF_AUTH_TOKEN=密码   → 所有页面需登录
- XSF_DATA=D:\my-xsf    → 数据目录 (默认 %USERPROFILE%\xsf-data)
- XSF_HOST=0.0.0.0      → 允许局域网访问 (需自行放行 Windows 防火墙, 有安全风险)
- XSF_PORT=8090         → 起始端口

【CLI (命令行)】
- 直接用: 在本目录开 PowerShell, 跑 .\xsf.exe init / .\xsf.exe stats
- 想在任何位置敲 xsf: 双击 add-to-path.bat 把本目录加入用户 PATH (重开终端生效)
- 常用命令:
    xsf init                          初始化/查看数据目录
    xsf add a.pdf -c 书架名           导入 born-digital PDF
    xsf add a.pdf -c 书架名 --ocr     扫描件 OCR 入库
    xsf search 关键词                 全文检索
    xsf context <doc_id> <页> <段>    查命中块上下文
    xsf stats                         统计

【OCR 配置】
便携版不内置本地 OCR, 走远程 provider (Web「OCR 设置」页添加):
- generic_http: 填内网 GPU 服务地址 (如 http://192.168.1.113:8091)
- aistudio: 内置云端三阶段, 只填 token

【AI skill / MCP 接入】
- skill (HTTP API): 服务地址链首选项本机 127.0.0.1:8090, 本地服务天然命中。
  将 skill 目录 (xsf_api.py 所在) 复制到 %USERPROFILE%\.agents\skills\xsf\,
  并设环境变量 XSF_ENV 指向本目录的 .env (读 XSF_AUTH_TOKEN)。
- MCP: 本目录的 xsf-mcp.exe 可作为 MCP server 接入 (stdio)。

【数据迁移 (Linux ↔ Windows)】
- Web 界面: 文献列表 → 导出归档 (tar.gz) → Windows 端导入归档
- 或 Linux 服务器用 scripts/collection_io.py export 后拷贝导入

【常见问题】
- 杀毒软件误报: PyInstaller 打包的 exe 偶被误报, 可加排除项或提交误报申诉
- 端口全部被占: 设 XSF_PORT 换起始端口
- 中文乱码: PowerShell 执行 chcp 65001 后再跑 xsf.exe
- 升级: 覆盖解压即可, 数据在 %USERPROFILE%\xsf-data\ 不受影响。
  但注意:
  1) 升级前建议停服备份 %USERPROFILE%\xsf-data\db\ 下全部 .db
     (以及本目录的 .env / ocr-config.json);
  2) 新版首次访问会自动做数据库迁移 (建新表/补列), 此过程单向——
     升级后请勿直接换回旧版 exe, 需回退时先还原备份;
  3) 升级后验证: 双击能起服务 + 校对页出现「智能校对」按钮。

版本: 见 zip 文件名 | 主页: github.com/eric2024ll/xsf (私有)
