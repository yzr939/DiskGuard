# -*- coding: utf-8 -*-
"""打包驱动: 屏蔽 obsolete pathlib backport 的检测, 让 PyInstaller 可在 Anaconda 环境运行。

用法: <你的 python> build_with_conda.py <PyInstaller 参数...>

为什么需要它: 部分 Anaconda site-packages 残留 obsolete backport (enum34 / typing /
pathlib), 纯 Python 3.11 环境下无害, 但会让 PyInstaller 的依赖检测报错。这里在导入
PyInstaller 之前屏蔽对这几个包的检测, 从而让 conda 布局的 Tcl/Tk 被正确打包。
"""
import sys

import importlib.metadata as md

_orig_distribution = md.distribution


def _patched_distribution(name, *args, **kwargs):
    # Anaconda site-packages 残留的 obsolete backport, 纯 Python 3.11 环境下无害,
    # 屏蔽检测使 PyInstaller 正常运行 (conda 布局的 Tcl/Tk 才能被正确打包)
    if name in ("enum34", "typing", "pathlib"):
        raise md.PackageNotFoundError(name)
    return _orig_distribution(name, *args, **kwargs)


md.distribution = _patched_distribution

import PyInstaller.__main__  # noqa: E402

if __name__ == "__main__":
    PyInstaller.__main__.run()
