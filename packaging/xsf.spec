# -*- mode: python ; coding: utf-8 -*-
# 小書房 Windows 便携版打包 (PyInstaller onedir, 多入口共享 _internal)
#
# 构建 (Windows, 仓库根目录):
#   pip install -e ".[desktop,build,mcp]"
#   pyinstaller packaging/xsf.spec --noconfirm
# 产物: dist/xsf-portable/  (小書房.exe + xsf.exe + xsf-mcp.exe + _internal/)
# 组装 zip: pwsh packaging/make_portable.ps1

import importlib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).resolve().parent   # 仓库根 (SPECPATH = spec 所在目录 packaging/, 再上一级)
entries = root / 'packaging' / 'entries'

# jieba 词典 / opencc 字典是包内非 .py 数据, PyInstaller 静态分析收不到, 手动收集
extra_datas = []
extra_datas += collect_data_files('jieba')
extra_datas += collect_data_files('opencc')

pkg_datas = [
    (str(root / 'packaging' / 'xsf.ico'), 'packaging'),
    # api.py 以 __file__ 相对路径加载 web 资源 (frozen 时 = _internal/xsf/),
    # StaticFiles(check_dir=True) import 期检查目录, 缺失即崩; templates 同理 (页面渲染)
    (str(root / 'xsf' / 'static'), 'xsf/static'),
    (str(root / 'xsf' / 'templates'), 'xsf/templates'),
]


def make_analysis(script: str):
    return Analysis(
        [str(entries / script)],
        pathex=[str(root)],
        binaries=[],
        datas=extra_datas + pkg_datas,
        hiddenimports=['pystray._win32'],
        hookspath=[],
        runtime_hooks=[],
        excludes=['tkinter', 'matplotlib', 'numpy', 'pytest'],
        noarchive=False,
    )


a_gui = make_analysis('gui.py')
a_cli = make_analysis('cli.py')
a_mcp = make_analysis('mcp.py')

pyz_gui = PYZ(a_gui.pure)
pyz_cli = PYZ(a_cli.pure)
pyz_mcp = PYZ(a_mcp.pure)

# PyInstaller 6: EXE 只吃 PYZ + TOC (scripts), 不能传 Analysis 对象;
# onedir 共享 _internal 时须 exclude_binaries=True, binaries/datas 归 COLLECT.
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name='小書房',
    console=False,
    icon=str(root / 'packaging' / 'xsf.ico'),
    disable_windowed_traceback=False,
)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='xsf',
    console=True,
)
exe_mcp = EXE(
    pyz_mcp,
    a_mcp.scripts,
    [],
    exclude_binaries=True,
    name='xsf-mcp',
    console=True,
)

coll = COLLECT(
    exe_gui, exe_cli, exe_mcp,
    a_gui.binaries, a_gui.datas,
    a_cli.binaries, a_cli.datas,
    a_mcp.binaries, a_mcp.datas,
    strip=False,
    upx=False,
    name='xsf-portable',
)
