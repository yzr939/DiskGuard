# -*- mode: python ; coding: utf-8 -*-
"""DiskGuard 诊断版打包规格: console=True, 便于抓取 stderr; 不嵌图标(诊断用不上)。

原生 DLL 的收集规则与 build_exe.py **共用同一份来源**(DLLS + resolve_conda_lib),
避免两处各维护一份、日后只改一处造成漂移 —— 曾因此产出运行即崩的 exe。

构建(conda 环境必须经 build_with_conda.py 驱动, 否则 PyInstaller 会被
site-packages 残留的 obsolete backport 卡住):
    python build_with_conda.py --distpath dist_diag DiskGuardDiag.spec
"""
import os
import sys

_SPEC_DIR = globals().get("SPECPATH") or os.path.dirname(os.path.abspath(globals()["SPEC"]))
sys.path.insert(0, _SPEC_DIR)

from build_exe import DLLS, resolve_conda_lib  # noqa: E402

_conda_lib = resolve_conda_lib()
_pathex = []
_binaries = []

if _conda_lib:
    _pathex = [_conda_lib]
    for _name in DLLS:
        _path = os.path.join(_conda_lib, _name)
        if os.path.exists(_path):
            _binaries.append((_path, "."))
        else:
            print("[spec] 警告: 缺少原生 DLL " + _path)
else:
    print("[spec] 警告: 未找到 conda 原生 DLL 目录, 跳过显式捆绑"
          "(非 conda 环境属正常现象, PyInstaller 会自行收集); "
          "如需指定请设置 DISKGUARD_CONDA_LIB")

a = Analysis(
    ['disk_guard.py'],
    pathex=_pathex,
    binaries=_binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DiskGuardDiag',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
