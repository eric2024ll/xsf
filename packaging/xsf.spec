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

root = Path(SPECPATH).resolve()          # 仓库根 (spec 所在目录的上级)
entries = root / 'packaging' / 'entries'

# jieba 词典 / opencc 字典是包内非 .py 数据, PyInstaller 静态分析收不到, 手动收集
extra_datas = []
extra_datas += collect_data_files('jieba')
extra_datas += collect_data_files('opencc')

pkg_datas = [
    (str(root / 'packaging' / 'xsf.ico'), 'packaging'),
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

exe_gui = EXE(
    pyz_gui, a_gui, a_gui.binaries, a_gui.datas,
    name='小書房',
    console=False,
    icon=str(root / 'packaging' / 'xsf.ico'),
    disable_windowed_traceback=False,
)
exe_cli = EXE(
    pyz_cli, a_cli, a_cli.binaries, a_cli.datas,
    name='xsf',
    console=True,
)
exe_mcp = EXE(
    pyz_mcp, a_mcp, a_mcp.binaries, a_mcp.datas,
    name='xsf-mcp',
    console=True,
)

coll = COLLECT(
    exe_gui, exe_cli, exe_mcp,
    a_gui, a_cli, a_mcp,
    strip=False,
    upx=False,
    name='xsf-portable',
)
