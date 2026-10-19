小書房 (xsf) — 说明
====================

个人史料文献检索与校对平台

把散落在各处的 PDF / Word 变成可检索、可定位、可校对的文本池: 丢进书架,
自动入库、自动 OCR、全文检索、图文对照校对; 并把文献中的概念、人物、关系
编译为实体图谱与知识地图。以 Web『小書房』为主入口, CLI / MCP / Windows
便携版随行。数据完全留在本机, 不依赖任何云服务。

本文件讲「装起来」; 各功能怎么用见同目录的《使用说明.txt》。


【核心功能】
- 多书架管理: 按研究主题分 collection, 可单架搜索也可跨架搜索
- 双通道解析: born-digital PDF 用 PyMuPDF 秒级提取立即可见; 扫描件走 OCR 队列回填
- 文件夹投放自动入库: PDF/MD 丢进 {书架}/uploads/, 后台自动扫描入库 + 排 OCR
- 全文检索: FTS5 + jieba 分词, 繁简归一; 结果按文档分组, 文档内分页 + load-more
- OCR 校对工作台: 图文对照、块粒度编辑、栏系统(横排/竖右起/竖左起)、页面级断点续跑、
  手工分栏重 OCR、疑点标记
- 智能校对: 双通道分歧点校对——第二引擎自动对齐比对, 或人工录入整篇文本(自动页映射),
  产出「实质异文/异体字对/页码」分类的分歧表并定位到原文行
- 卡片资产: 每文献的元数据、目录树、摘要, 人工调整 + AI 辅助生成
- 实体图谱: 书架级本体层——实体归一、提及锚点(跳原文页)、属性、共现关系; 可导出 JSON
- 知识地图 (Wiki): 由文献编译的概念页, 双链互引, 可一键重编
- 多语种文本层: 中文单行 / 非中文三行 / 原语四行(见《使用说明.txt》)
- 导出: .bib / .md / 归档 tar.gz; 归档可整体备份与迁移
- 数据与代码分离: 文献与 SQLite 库独立存放(默认 ~/xsf-data/), 代码仓只保留程序


【环境要求】
- Python 3.11+
- Git (源码安装)
- 运行依赖: PyMuPDF、jieba、requests、fastapi、uvicorn、jinja2、
  python-multipart、pypinyin、opencc-python-reimplemented
- 可选 extras: [mcp](MCP server)、[office](Word 导入)、[desktop](托盘 GUI)、
  [build](PyInstaller 打包)


【安装】
--------------------------------------------------------------
git clone <repo-url> xsf && cd xsf
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .                     # Web + CLI
pip install -e ".[mcp]"              # 需要 MCP 时
pip install -e ".[office]"           # 需要导入 Word 文档时
--------------------------------------------------------------
不想装环境可直接用【Windows 便携版】(见下)。


【数据目录】
--------------------------------------------------------------
默认 ~/xsf-data/。想改位置:
    export XSF_DATA=/path/to/data
也可写入 .env 或 shell profile 持久生效。结构:

~/xsf-data/
├── db/                     数据库(本地磁盘, 不放网络盘)
│   └── {书架}/xsf.db       每个 collection 独立 SQLite + FTS5
├── collections/            书架目录
│   └── {书架}/uploads/     源文件按书架分目录
├── ocr-config.json         OCR / LLM provider 配置 (权限 600)
├── mounts.json             外部权威挂载配置 (见下)
└── entity-additions...     实体图谱等运行数据

DB 内只存 filename(不含绝对路径); 源文件与 db 对称分书架存放, 跨书架同名不冲突。
--------------------------------------------------------------


【首次启动】
--------------------------------------------------------------
xsf init                                  # 建库与目录
uvicorn xsf.api:app --port 8090           # 起 Web 服务
# 浏览器打开 http://127.0.0.1:8090/
--------------------------------------------------------------
常驻部署见【部署】。服务无认证, 默认只监听本机回环 127.0.0.1。


【OCR / LLM provider 配置】
所有 provider 配置存 <XSF_DATA>/ocr-config.json(权限 600), 在 Web「OCR 设置」
页自助添加 / 切换默认 / 连通测试。统一 vl_api 架构, 按 endpoint profile 分三类:

  paddle_http   本地 GPU / 自建服务      structured  含坐标, 支持图文校对与画框重 OCR
  aistudio_job  AI Studio 云端            structured  内置三阶段协议, 只填 token
  openai_chat   OpenAI 兼容 chat API      plain       整页图直送纯文本, 可作默认引擎

另有一类 llm_chat(翻译/实体/知识地图/AI 填充等用的文本 LLM), 同样在此页配置。
- 上传/OCR 走全局默认 provider(列表点选切换); CLI 可用 --provider <id> 显式指定
- 网络错误/5xx 指数退避, 最多重试 3 次; 无 provider 时报友好错误并引导配置

本地 GPU 模型:
- PaddleOCR-VL: 自建 HTTP 服务(如 systemd 常驻 :8091), 以 paddle_http 接入
- 自训练管线: contrib/ 提供模板(本地模型缓存、GPU 独占、GPU→CPU 回退), PP_DEVICE 可覆盖


【外部权威挂载】
实体归一可对接外部权威库(如 CBDB 历史人物、CHGIS 历史地名)。挂载是**显式**的:
由用户决定是否挂载、挂在哪里; 未挂载即自动降级为纯 LLM 归一。
- 配置存 <XSF_DATA>/mounts.json, 在「实体图谱」页展开「外部权威挂载」填写:
    启用勾选 + 脚本路径(可为脚本或其所在目录) + 运行命令(默认 python3)
- 机制通用, 后续可接 CHGIS / Wikidata 等同类外部库


【部署 (systemd 常驻, Linux)】
--------------------------------------------------------------
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
--------------------------------------------------------------
sudo systemctl daemon-reload && sudo systemctl enable --now xsf
journalctl -u xsf -f                  # 实时日志

只监听回环即满足单机使用; 如需局域网访问改 --host 0.0.0.0, 并自行放行防火墙
(服务无认证, 仅限可信内网)。代码改动后需重启服务生效。


【Windows 便携版】
免安装 zip, 双击「小書房.exe」→ 起服务 + 开浏览器 + 托盘常驻。

快速开始:
1. 解压 zip 到任意目录(路径可含中文, 建议避免 OneDrive 同步目录)
2. 双击「小書房.exe」:
   - 首次启动自动在 %USERPROFILE%\xsf-data\ 建数据目录
   - 浏览器自动打开 http://127.0.0.1:8090/
   - 托盘图标菜单可「打开小書房」/「退出」
   - 只监听本机回环(127.0.0.1), 不对外网开放
3. 再次双击 = 单实例: 只再开一个浏览器页, 不重复起服务
4. 8090 端口被占时自动顺延(8091-8099), 实际端口见托盘提示
   注意: 同时运行两份便携版时第二份会占下一个端口, 浏览器停着旧地址看到的
   不是新实例 —— 书架列表对不上请跑 xsf doctor 对比各端口。

配置 (env.txt):
- 复制本目录 env.example.txt → 改名为 env.txt, 去掉要用的行前的 # 并修改, 重启生效
- 或托盘菜单「编辑配置文件」(自动生成 env.txt 并打开记事本)
- (Windows 不允许资源管理器改名点开头文件, 故用 env.txt; .env 也认)
- XSF_DATA=D:\my-xsf  → 数据目录(默认 %USERPROFILE%\xsf-data)
- XSF_HOST=0.0.0.0    → 允许局域网访问(需放行防火墙, 有安全风险)
- XSF_PORT=8090       → 起始端口

CLI: 本目录开 PowerShell 跑 .\xsf.exe init / .\xsf.exe stats; 双击
add-to-path.bat 可把本目录加入用户 PATH(重开终端生效), 之后随处敲 xsf。

OCR: 便携版不内置本地引擎, 在 Web「OCR 设置」配远程 provider
(paddle_http 内网 GPU / aistudio_job 云端 / openai_chat API)。

构建 (维护者):
  # GitHub Actions: Actions → windows-portable → Run workflow
  #   或 push tag: git tag vX.Y.Z && git push origin vX.Y.Z
  # 本地复现 (Windows + Python 3.12):
  pip install -e ".[desktop,build,office,mcp]"
  pyinstaller packaging/xsf.spec --noconfirm
  pwsh packaging/make_portable.ps1    # → dist/xsf-portable-win64-<ver>.zip
产物: 小書房.exe(托盘 GUI) + xsf.exe(CLI) + xsf-mcp.exe(MCP), 共享 _internal/。


【MCP 接入】
pip install -e ".[mcp]" 后可用 xsf-mcp, 把检索/上下文/元数据暴露为 MCP 工具,
供 AI agent 直接查询文献池。零 API 改动, 与 Web/CLI 共享同一数据层。
便携版的 xsf-mcp.exe 可作为 MCP server 接入(stdio)。


【升级注意事项】
- 数据与程序分离: 覆盖解压 / 重装不动数据——数据在 XSF_DATA(db/*.db + 上传的
  文献/图片); 程序目录里的 .env 与 ocr-config.json(含 api_key) 需自己留意。
- 升级前备份: 停服务 → 备份 $XSF_DATA/db/ 下全部 *.db + 上传/文献目录
  (或 Web「导出归档 tar.gz」)+ .env、ocr-config.json。
- Schema 迁移是单向的: 新版首次访问数据库会自动建新表/补列; 升级后请勿直接
  换回旧版, 需回退时先还原备份。
- 升级后验证: xsf stats 正常打开、Web 校对页出现「智能校对」按钮即为正常。
- 便携版: zip 文件名即版本号, 覆盖前先核对。


【环境变量】
XSF_DATA              ~/xsf-data           数据根目录
XSF_DB_DIR            XSF_DATA/db/         数据库目录(必须本地磁盘, 网络盘不支持 SQLite 锁)
XSF_COLLECTIONS_DIR   XSF_DATA/collections/ 源文件目录, 可指 NAS / 挂载路径
XSF_OCR_METHOD        配置 default         默认 OCR provider id
XSF_LLM_METHOD        配置 default         默认文本 LLM provider id(翻译/实体/知识地图)
XSF_SCAN_INTERVAL     120                  扫描轮询间隔秒, 0 关闭整个扫描
XSF_SCAN_STABLE_SEC   60                   文件稳定阈值秒(mtime 距今小于此值跳过)
XSF_SCAN_OCR          1                    PDF 自动 OCR, 0 仅标记不自动跑
XSF_HOST / XSF_PORT   127.0.0.1 / 8090     Web 绑定地址与端口(便携版端口占用自动顺延)
XSF_BASE_URL          http://localhost:8090  MCP 适配器连接的服务地址
XSF_EXTRACT_WORKERS   8                    实体抽取并发线程数
XSF_CBDB_WORKERS      8                    CBDB 查询并发数
XSF_ENV               —                    显式指定 .env 路径(便携版)


【License】 MIT

版本: 见 zip 文件名 | 主页: github.com/eric2024ll/xsf (私有)
