# -*- coding: utf-8 -*-
"""一键打包 DiskGuard.exe (onefile + windowed)。

为什么要显式 --add-binary:
  Anaconda 会把一批原生 DLL 放在 <conda 根>\\Library\\bin
  (ffi.dll / tcl86t.dll / tk86t.dll / sqlite3.dll / libssl-1_1-x64.dll ...),
  只靠 PATH 让 PyInstaller 去搜不可靠 —— 曾出现漏收 8 个 DLL 导致
  "ImportError: DLL load failed while importing _ctypes"。这里显式列全。

  conda DLL 目录按以下顺序解析:
    1. 环境变量 DISKGUARD_CONDA_LIB (若已设置且目录存在)
    2. 由当前解释器推导: <sys.prefix>\\Library\\bin
    3. 两者都不存在时, 只打印警告并跳过 --paths 与 --add-binary,
       即"尽力而为地打包", 绝不让打包脚本失败（非 conda 环境同样可用）。

用法: python build_exe.py            (自动先把旧 exe 挪成 DiskGuard.old.exe)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DLLS = ("ffi.dll", "LIBBZ2.dll", "libcrypto-1_1-x64.dll", "liblzma.dll",
        "libssl-1_1-x64.dll", "sqlite3.dll", "tcl86t.dll", "tk86t.dll")


def resolve_conda_lib():
    """解析 conda 原生 DLL 目录, 找不到时返回 None。"""
    env_dir = os.environ.get("DISKGUARD_CONDA_LIB")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    derived = os.path.join(sys.prefix, "Library", "bin")
    if os.path.isdir(derived):
        return derived
    return None


def main():
    exe = os.path.join(HERE, "dist", "DiskGuard.exe")
    if os.path.exists(exe):                      # 沙箱/权限原因删除可能失败, 用改名让路
        old = os.path.join(HERE, "dist", "DiskGuard.old.exe")
        try:
            if os.path.exists(old):
                os.remove(old)
            os.replace(exe, old)
            print("[build] 旧 exe -> DiskGuard.old.exe")
        except OSError as e:
            print("[build] 挪走旧 exe 失败:", e)

    conda_lib = resolve_conda_lib()

    args = [sys.executable, os.path.join(HERE, "build_with_conda.py"),
            "--onefile", "--windowed", "--clean", "--noconfirm",
            "--name", "DiskGuard",
            "--distpath", os.path.join(HERE, "dist"),
            "--workpath", os.path.join(HERE, "build_conda"),
            "--specpath", HERE]
    if conda_lib:
        args += ["--paths", conda_lib]
        for d in DLLS:
            p = os.path.join(conda_lib, d)
            if not os.path.exists(p):
                print("[build] 警告: 缺少", p)
                continue
            args += ["--add-binary", p + os.pathsep + "."]
    else:
        print("[build] 警告: 未找到 conda 原生 DLL 目录, 跳过 --paths 与 --add-binary "
              "(非 conda 环境属正常现象); 如需指定请设置 DISKGUARD_CONDA_LIB")

    # 应用图标: 缺失时只警告并跳过, 绝不让打包失败(图标非功能性依赖)
    icon = os.path.join(HERE, "assets", "DiskGuard.ico")
    if os.path.exists(icon):
        args += ["--icon", icon]
    else:
        print("[build] 警告: 未找到图标, 跳过 --icon:", icon)
    args.append(os.path.join(HERE, "disk_guard.py"))

    env = dict(os.environ)
    if conda_lib:
        env["PATH"] = conda_lib + os.pathsep + env.get("PATH", "")
    t0 = time.time()
    r = subprocess.run(args, cwd=HERE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log = os.path.join(HERE, "build_log.txt")
    open(log, "w", encoding="utf-8").write((r.stdout or "") + (r.stderr or ""))
    size = os.path.getsize(exe) / 1048576 if os.path.exists(exe) else 0
    print(f"[build] exit={r.returncode} 用时={time.time()-t0:.0f}s "
          f"exe={size:.2f} MB (见 build_log.txt)")

    # 打包后自测: 冻结态能起来才算打包成功（体积正常但运行即崩是最难发现的失败）
    if r.returncode == 0 and os.path.exists(exe):
        try:
            st = subprocess.run([exe, "--selftest"], cwd=tempfile.gettempdir(),
                                capture_output=True, text=True, timeout=120)
            print(f"[build] 冻结态自测 exit={st.returncode}")
            if st.returncode != 0:
                print("[build] 警告: 冻结态自测未通过, 该 exe 很可能运行即崩")
        except Exception as e:
            print("[build] 警告: 冻结态自测未能执行:", e)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
