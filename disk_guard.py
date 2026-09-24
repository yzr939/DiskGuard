# -*- coding: utf-8 -*-
"""DiskGuard - Windows 磁盘空间监控小工具

功能:
- 扫描指定磁盘/文件夹下一级子目录的大小, 与比较基准对比, 增长超阈值标红预警
  (基准 = 当天第一条记录; 当天此前无记录时用上一条记录)
- 数据保存在 SQLite 数据库 (diskguard.db), 重启后可直接浏览历史记录(免扫描下钻)
- 每个文件夹保留最近 HIST_KEEP 次扫描的 [时间, 大小] 历史, 单击行可弹出大小变化曲线
- 列表中用字符缩略图显示近期大小走势, 并按变大(红)/变小(绿)着色
- 无权限访问的目录自动捕获并标记; 不做"系统目录"预判
- 支持星标重点文件夹、删除文件夹到回收站
- 筛选: 全部 / 仅预警 / 仅星标 / 仅增长 / 仅减少
- 比较时间锚点按"天"锚定(只影响比较基准, 当前大小仍为最新)
- 定时自动扫描、双击下钻、返回上一层、小文件夹合并为最小单元

运行:   python disk_guard.py
自测:   python disk_guard.py --selftest
"""
import ctypes
import datetime
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import stat
import string
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import ttk, messagebox
from tkinter.font import Font


# ---------------- 路径与常量 ----------------
def app_dir():
    """程序所在目录。

    打包成 onefile exe 后 __file__ 指向解包临时目录(_MEIxxxx), 数据写在那里会被清掉,
    所以冻结状态下取 exe 自身所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
LEGACY_BASELINE = os.path.join(APP_DIR, "baseline.json")


def icon_path():
    """程序图标文件路径(与 exe 同目录下的 assets/DiskGuard.ico)。

    为什么用 APP_DIR: 冻结成 onefile exe 后 __file__ 指向解包临时目录, 那里没有 assets,
    故统一走 app_dir() 解析出的"程序所在目录"。
    """
    return os.path.join(APP_DIR, "assets", "DiskGuard.ico")

# ---- 配色(浅色现代风) ----
# 品牌主色(现代蓝)
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_LIGHT = "#dbeafe"    # 表格选中行背景
# 表面与文字
SURFACE = "#ffffff"
SURFACE_ALT = "#f8fafc"     # 斑马纹/次级背景
BORDER = "#e2e8f0"
TEXT = "#1e293b"
TEXT_MUTED = "#64748b"
# 语义色(中国习惯: 红=涨/预警, 绿=跌)
RED_BG = "#fee2e2"      # 预警行背景
RED_FG = "#b91c1c"
GROW_BG = "#fef2f2"     # 较上次变大: 浅红
GROW_FG = "#dc2626"
SHRINK_BG = "#ecfdf5"   # 较上次变小: 浅绿
SHRINK_FG = "#059669"
FLAT_BG = "#ffffff"
FLAT_FG = "#1e293b"
STAR_BG = "#fef9c3"     # 星标行背景
STAR_FG = "#a16207"
OK_BG = "#ffffff"
STRIPED_BG = "#f8fafc"

MERGED_PREFIX = "📦 合并的小文件夹"
# 旧版合并行前缀: 导入旧 baseline.json 时仍需识别(不能只认新前缀, 否则旧合并行会被当成真实目录导入)
LEGACY_MERGED_PREFIX = "📦 小文件夹(合并显示)"
DENIED_STATUS = "⛔ 无权限"
PARTIAL_HINT = "⚠ 部分无权限"

# 筛选档位 —— 全模块唯一来源。
# 为什么集中定义: 校验点(至少 4 处)与控件候选值若各自写字面量, 新增档位时极易漏改某处,
# 导致"控件里能选、逻辑上被判非法"这类难查的不一致。这里只维护一份。
FILTERS = ("全部", "仅预警", "仅星标", "仅增长", "仅减少")

# 页面与卡片配色(现代浅色风)
PAGE_BG = "#f3f5f9"          # 页面底色(浅灰), 与白色卡片形成层次
CARD_BORDER = "#e7eaf0"      # 卡片 1px 细边框(近似圆角卡片的描边)

BATCH = 500             # 数据库批量写入条数
HIST_KEEP = 24          # 每个文件夹保留的历史大小点数(次)
SPARK_CHARS = "▁▂▃▄▅▆▇█"
SPARK_WIDTH = 8         # 列表缩略图显示最近几个点


# ---------------- 临时文件清理 ----------------
MEI_MARKER = ".diskguard_mei"     # 在本程序解包目录里留的标记(下次启动可确认是自己的)


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _payload_signature():
    """onefile 运行时本程序解包目录的文件名集合; 非冻结运行返回 None。"""
    mei = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    if not mei or not os.path.isdir(mei):
        return None
    try:
        return set(os.listdir(mei))
    except OSError:
        return None


def _looks_like_our_bundle(names):
    """按内容判断是否为本程序的 onefile 解包目录(兼容旧版本构建留下的)。

    特征: 同时含 python{maj}{min}.dll + _tkinter.pyd + _tcl_data + _tk_data。
    只用于识别残留, 真正删除前还会用 rename 试探是否被进程占用(占用则跳过)。
    """
    if not names or len(names) > 400:
        return False
    py = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    return (py in names and "_tkinter.pyd" in names
            and "_tcl_data" in names and "_tk_data" in names)


def _rmtree_force(path, timeout=3.0):
    """尽力删除目录: 清只读属性 + 重试。

    刚退出的进程对映射进内存的 DLL 会有短暂 "delete pending" 状态, 杀软也可能临时占用,
    所以不能只试一次。
    """
    deadline = time.time() + timeout
    while True:
        def on_err(func, p, _exc):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except OSError:
                pass
        shutil.rmtree(path, onerror=on_err)
        if not os.path.exists(path):
            return True
        if time.time() > deadline:
            return False
        time.sleep(0.4)


def _mark_own_payload():
    """在当前解包目录写标记文件, 便于下次启动识别本程序遗留。"""
    mei = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    if not mei or not os.path.isdir(mei):
        return
    try:
        with open(os.path.join(mei, MEI_MARKER), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def cleanup_temp_extract_dirs(tmp_dir=None, retry_seconds=3.0):
    """清理本程序历次运行残留在 %TEMP% 的 onefile 解包目录 (_MEIxxxxx)。

    onefile 每次启动会把运行时解包到 %TEMP%\\_MEIxxxxx (约 24MB), 正常退出会自行删除,
    但被强杀/崩溃时会留下。启动时在后台顺手收拾, 三重保险保证不误伤:
      - 只处理 (a)带本程序标记文件, 或 (b)文件名集合与本程序当前解包内容一致,
        或 (c)内容特征符合本程序(旧版本构建)的目录;
      - 先用 rename 试探占用: 目录里的文件被进程占用时改名会失败 → 跳过正在运行的实例;
      - 删除被短暂占用(delete-pending / 杀软扫描)时留 .dg-clean 目录并在 retry_seconds
        内重试, 仍未删掉的留到下次启动继续。
    返回 (清理个数, 释放字节数)。
    """
    if not getattr(sys, "frozen", False):      # 仅 onefile 打包运行才会产生解包目录
        return 0, 0
    sig = _payload_signature()
    cur = os.path.abspath(getattr(sys, "_MEIPASS", ""))
    tmp = tmp_dir or tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0, 0
    removed = freed = 0
    for name in names:
        path = os.path.join(tmp, name)
        if name.endswith(".dg-clean"):            # 上次删除中断的半成品, 继续清
            probe = path
        elif name.startswith("_MEI"):
            if not os.path.isdir(path) or os.path.abspath(path) == cur:
                continue
            try:
                inner = set(os.listdir(path))
            except OSError:
                continue
            ours = (MEI_MARKER in inner
                    or (sig and len(sig) >= 20 and inner - {MEI_MARKER} == sig)
                    or _looks_like_our_bundle(inner))
            if not ours:
                continue
            probe = path + ".dg-clean"
            try:
                os.rename(path, probe)            # 仍被占用 → 抛错, 跳过
            except OSError:
                continue
        else:
            continue
        size = _dir_size(probe)
        if _rmtree_force(probe, timeout=max(1.0, retry_seconds)):
            removed += 1
            freed += size
    return removed, freed


def start_temp_cleanup(ui_queue=None, retry_seconds=10.0, extra_passes=(30, 120, 300)):
    """后台清理残留解包目录。

    启动先清一遍, 之后按 extra_passes 的秒数再补几遍: 被强杀的实例退出后, 它映射过的
    DLL 在 Windows 上要过一段时间才真正释放(delete pending), 第一遍删不掉的稍后能删掉。
    结果以 ("cleanup", 个数, 字节数) 投递到 ui_queue, 由界面显示。
    """
    if not getattr(sys, "frozen", False):
        return

    def work():
        done_n = done_freed = 0
        for delay in (0,) + tuple(extra_passes):
            if delay:
                time.sleep(delay)
            try:
                n, freed = cleanup_temp_extract_dirs(retry_seconds=retry_seconds)
            except Exception:
                continue
            if n:
                done_n += n
                done_freed += freed
                if ui_queue is not None:
                    try:
                        ui_queue.put(("cleanup", done_n, done_freed))
                    except Exception:
                        pass
    threading.Thread(target=work, daemon=True, name="temp-cleanup").start()


def norm(p):
    """路径规范化为数据库主键形式。"""
    return os.path.normcase(os.path.normpath(p))


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 目录遍历核心 ----------------
def is_reparse_point(path):
    """判断是否为联接点/符号链接, 避免循环遍历。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs:
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return os.path.islink(path)


class _Frame:
    """后序遍历的一层目录帧(显式栈, 避免递归深度限制, 内存只占目录深度)。"""
    __slots__ = ("path", "kids", "i", "fb", "sub", "part", "denied", "err", "sig")

    def __init__(self, path):
        self.path = path
        self.kids = []
        self.i = 0
        self.fb = 0          # 直接文件字节数
        self.sub = 0         # 已完成子目录合计
        self.part = False    # 子孙中存在无权限/异常项
        self.denied = False  # 自身无法读取
        self.err = None
        self.sig = None


def _open_frame(path):
    """读取一层目录: 收集子目录、累加文件大小, 捕获无权限异常。

    DirEntry.stat(follow_symlinks=False) 在 Windows 上复用目录枚举时缓存的数据,
    不额外发起系统调用, 因此这里用它同时判断类型、大小与重解析点。
    """
    fr = _Frame(path)
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    fr.part = True
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue                      # 联接点/符号链接: 跳过, 防死循环
                if stat.S_ISDIR(st.st_mode):
                    fr.kids.append(e.path)
                else:
                    fr.fb += st.st_size
    except FileNotFoundError:
        pass                                      # 扫描期间被删除
    except OSError as e:
        fr.denied = True                          # 无权限 / 设备不可访问
        fr.err = e
    return fr


def walk_dir(root, stop_event, emit, root_sig=None):
    """后序遍历 root 子树, 每个目录算完时回调 emit(path, size, denied, partial, sig)。

    返回 root 的 (size, denied, partial)。
    denied: 自身无法读取; partial: 子孙中存在无权限项(大小可能不完整)。
    """
    frames = [_open_frame(root)]
    frames[0].sig = root_sig
    while frames:
        fr = frames[-1]
        if fr.i < len(fr.kids):
            child = fr.kids[fr.i]
            fr.i += 1
            frames.append(_open_frame(child))
            continue
        frames.pop()
        size = fr.fb + fr.sub
        if frames:
            parent = frames[-1]
            parent.sub += size
            if fr.denied or fr.part:
                parent.part = True
        emit(fr.path, size, fr.denied, fr.part, fr.sig)
        if not frames:
            return size, fr.denied, fr.part
    return 0, False, False


def quick_sig(path):
    """目录浅层签名: 自身 mtime + 直接子项(名称/mtime/类型/大小)。

    注意: 只能检测直接子项变化; 更深层的修改若未引起中间目录 mtime 变化则会漏检。
    """
    try:
        items = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    items.append([e.name.lower(), round(st.st_mtime, 3),
                                  st.st_size if stat.S_ISREG(st.st_mode) else -1,
                                  stat.S_ISDIR(st.st_mode)])
                except OSError:
                    items.append([e.name.lower(), -1, -1, False])
        items.sort()
        root_st = os.stat(path)
        return [round(root_st.st_mtime, 3), len(items)] + items
    except OSError:
        return None


def sig_hash(sig):
    """签名哈希(存数据库, 短且可精确比较)。"""
    if sig is None:
        return None
    raw = json.dumps(sig, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def merge_allowed(merge, flt):
    """是否允许"小文件夹合并"。

    **只有"全部"档才合并**: 合并行自身既不是预警也不带星标/增长语义, 一旦合并, 成员行
    会从视图里被移除 —— 任何筛选档下(含新增的"仅增长"/"仅减少")被筛出的成员行都会被
    "📦 合并的小文件夹"吞掉而永远看不见。故只要筛选非"全部"就禁用合并。
    抽成唯一判据, 避免散在多处、新增档位时漏改某一处。
    """
    return bool(merge) and flt == "全部"


def merge_small(records, min_mb):
    """小于 min_mb(MB) 的文件夹合并为一行"最小单元"。

    返回 (参与显示的记录, 合并行或 None, 合并成员列表)。
    无权限/部分无权限的项不参与合并(大小不可信)。
    """
    if min_mb <= 0:
        return list(records), None, []
    lim = min_mb * 1024 * 1024
    small, big = [], []
    for r in records:
        if (r["size"] is not None and r["size"] < lim
                and not r.get("denied") and not r.get("partial")):
            small.append(r)
        else:
            big.append(r)
    if not small:
        return big, None, []
    prevs = [r["prev"] for r in small if r.get("prev")]
    merged = {"name": f"{MERGED_PREFIX}({len(small)}个)", "path": None, "parent": None,
              "size": sum(r["size"] for r in small),
              "prev": sum(prevs) if prevs else None,
              "prev_kind": None, "prev_ts": None,
              "ts": now_str(), "sig": None,
              "denied": False, "partial": False, "starred": False,
              "cached": False, "merged": True}
    return big, merged, small


# ---------------- 大小趋势 ----------------
def hist_pairs(raw):
    """数据库 hist 字段(JSON 文本) -> [(时间戳|None, 字节数), ...], 由旧到新。

    兼容两种条目: 旧版纯数字 (时间未知 -> None), 新版 [时间, 大小] 对。
    """
    if not raw:
        return []
    try:
        vals = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for v in vals if isinstance(vals, list) else []:
        if isinstance(v, (int, float)):
            out.append((None, v))
        elif (isinstance(v, (list, tuple)) and len(v) == 2
                and isinstance(v[1], (int, float))):
            out.append((v[0] if isinstance(v[0], str) else None, v[1]))
    return out


def hist_sizes(pairs):
    """[(ts, size), ...] -> [size, ...] (画缩略图/曲线用)。"""
    return [s for _t, s in pairs]


def parse_hist(raw):
    """兼容旧调用: hist 字段 -> [字节数, ...]。"""
    return hist_sizes(hist_pairs(raw))


def baseline_of(pairs):
    """计算增长的比较基准, 返回 (基准大小, 基准类型, 基准记录时间) 或 (None, None, None)。

    规则: 取最新一条记录的日期作为"当天",
      - 当天此前已有记录 -> 基准 = 当天第一条记录的大小
      - 当天此前没有记录 -> 基准 = 上一条记录(前一天最后一条)的大小
      - 只有一条/没有历史 -> 无基准
    旧格式条目(无时间戳)不参与"当天"判定, 只能作为"上一条"。
    """
    if len(pairs) < 2:
        return None, None, None
    cur_ts = pairs[-1][0]
    today = cur_ts[:10] if cur_ts else None
    earlier = pairs[:-1]
    if today:
        for ts, size in earlier:
            if ts and ts[:10] == today:
                return size, "当天第一条", ts
    ts, size = earlier[-1]
    return size, "上一条", ts


def baseline_asof(pairs, cutoff=None):
    """按"日期"计算比较基准(锚点只精确到天, 不再到时分秒)。

    cutoff=None -> 与 baseline_of(pairs) 完全一致(逐字等价, 不许有行为差异)。
    cutoff 为真 -> 先只保留"带时间戳且其日期 <= cutoff 日期"的点, 再对这批点套用
      baseline_of 的"当天第一条/上一条"规则。

    按天的语义(本次刻意的行为变更): 只要日期不晚于锚定日, 该日**更晚时刻**的点也会被纳入
    —— 例如锚点取某天, 该天稍晚的记录同样算作"锚定日及更早"的历史点。内部统一取
    cutoff[:10] 归一化, 这样旧版本保存的、带时分秒的锚点设置也能继续照常工作。
    无时间戳的旧格式点在有 cutoff 时被排除(无法判定先后)。
    """
    if not cutoff:
        return baseline_of(pairs)
    cut = str(cutoff)[:10]                       # 只按日期比较, 兼容旧式带时分秒的输入
    usable = [(t, s) for t, s in pairs if t and t[:10] <= cut]
    return baseline_of(usable)


def sparkline(values, width=SPARK_WIDTH):
    """把一串大小值画成字符缩略图(按窗口内相对大小取高度)。

    values: 由旧到新。不足 width 时有几个画几个; 全等时统一中间高度。
    """
    vals = [v for v in (values or []) if v is not None]
    if not vals:
        return ""
    vals = vals[-width:]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(vals)
    span = float(hi - lo)
    return "".join(
        SPARK_CHARS[max(0, min(7, int((v - lo) / span * 7 + 0.5)))] for v in vals)


def sum_series(series_list):
    """把多条序列按"从最新往回"对齐求和(用于合并行)。"""
    seqs = [list(s) for s in series_list if s]
    if not seqs:
        return []
    n = max(len(s) for s in seqs)
    out = []
    for i in range(1, n + 1):
        total, hit = 0, False
        for s in seqs:
            if len(s) >= i:
                total += s[-i]
                hit = True
        out.append(total if hit else 0)
    out.reverse()
    return out


def nice_range(vals):
    """给定数值序列, 返回 (y_min, y_max) —— 留白 8%, 全等时上下各留 5%。"""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        pad = max(1.0, hi * 0.05)
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def trend_of(series):
    """(方向, 最近变化量): 方向 1=变大 -1=变小 0=持平/无数据。"""
    vals = [v for v in (series or []) if v is not None]
    if len(vals) < 2:
        return 0, 0
    d = vals[-1] - vals[-2]
    return (1 if d > 0 else (-1 if d < 0 else 0)), d


# ---------------- SQLite 存储 ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
  path       TEXT PRIMARY KEY,   -- normcase 绝对路径
  real       TEXT NOT NULL,      -- 原始大小写路径(用于显示/操作)
  name       TEXT NOT NULL,
  parent     TEXT,               -- normcase 父路径
  size       INTEGER,            -- 最新扫描大小(字节), NULL=未扫描
  prev_size  INTEGER,            -- 上一次扫描大小(用于计算增长)
  scanned_at TEXT,               -- 该大小的时间戳
  sig        TEXT,               -- 浅层签名哈希(仅被扫描目标的一级子目录)
  denied     INTEGER NOT NULL DEFAULT 0,
  partial    INTEGER NOT NULL DEFAULT 0,
  starred    INTEGER NOT NULL DEFAULT 0,
  seq        INTEGER NOT NULL DEFAULT 0,
  hist       TEXT                -- 历史大小 JSON 数组(字节, 由旧到新, 最多 HIST_KEEP 个)
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent);
CREATE TABLE IF NOT EXISTS scans (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  path        TEXT,              -- normcase 扫描目标
  started_at  TEXT,
  finished_at TEXT,
  total       INTEGER,
  dirs        INTEGER,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_path ON scans(path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 写入时顺手把当前 [时间, 大小] 对追加进 hist(行内 JSON 数组), 超过 HIST_KEEP 丢最旧的一个。
# 参数: path, real, name, parent, size, scanned_at, sig, denied, partial, seq,
#       ts+size(VALUES hist 对), ts+size(冲突分支两组)
_UPSERT = f"""
INSERT INTO folders(path, real, name, parent, size, prev_size, scanned_at,
                    sig, denied, partial, seq, hist)
VALUES(?,?,?,?,?,NULL,?,?,?,?,?, json_array(json_array(?, ?)))
ON CONFLICT(path) DO UPDATE SET
  real=excluded.real, name=excluded.name, parent=excluded.parent,
  prev_size=folders.size, size=excluded.size, scanned_at=excluded.scanned_at,
  sig=excluded.sig, denied=excluded.denied, partial=excluded.partial,
  seq=excluded.seq,
  hist=CASE WHEN COALESCE(json_array_length(folders.hist), 0) >= {HIST_KEEP}
            THEN json_remove(json_insert(COALESCE(folders.hist,'[]'), '$[#]', json_array(?, ?)), '$[0]')
            ELSE json_insert(COALESCE(folders.hist,'[]'), '$[#]', json_array(?, ?)) END
"""

# 命中缓存(未重新遍历)的目录: 大小未变, 但为保持"第 n 次"横向可比, 同样追加一个 [时间, 大小] 点。
_TOUCH_SUBTREE = f"""
UPDATE folders SET seq=?,
  hist=CASE WHEN size IS NULL THEN hist
            WHEN COALESCE(json_array_length(hist), 0) >= {HIST_KEEP}
              THEN json_remove(json_insert(COALESCE(hist,'[]'), '$[#]', json_array(?, size)), '$[0]')
            ELSE json_insert(COALESCE(hist,'[]'), '$[#]', json_array(?, size)) END
WHERE path=? OR path LIKE ? ESCAPE '\\'
"""


def path_esc(p):
    """转义 LIKE 元字符, 用于前缀匹配整棵子树。"""
    return p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def upsert_row(path, size, ts, sig, denied, partial, seq):
    """构造 folders 表 upsert 参数(16 元组), ts/size 重复出现供 hist 追加 [时间, 大小] 对。"""
    val = 0 if size is None else size
    np_ = os.path.normpath(path)
    return (norm(path), np_, os.path.basename(np_), norm(os.path.dirname(np_)),
            val, ts, sig, int(bool(denied)), int(bool(partial)), seq,
            ts, val, ts, val, ts, val)



def resolve_db_path():
    """优先把数据库放在 exe/脚本同目录, 不可写时退回用户目录。"""
    cand = os.path.join(APP_DIR, "diskguard.db")
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(cand, "ab"):
            pass
        return cand
    except OSError:
        base = os.path.join(os.path.expanduser("~"), ".diskguard")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "diskguard.db")


class Store:
    """SQLite 存取层(线程安全: 所有语句走同一把锁, 扫描线程批量写库)。"""

    def __init__(self, path=None):
        self.path = path or resolve_db_path()
        self._lock = threading.RLock()
        self.write_time = 0.0
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=OFF")
            self._conn.execute("PRAGMA cache_size=-16000")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """老库升级: 补 hist 列, 并把已有的"上次/当前"两个值作为起始历史。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(folders)")}
        if "hist" in cols:
            return
        self._conn.execute("ALTER TABLE folders ADD COLUMN hist TEXT")
        self._conn.execute(
            "UPDATE folders SET hist="
            "CASE WHEN prev_size IS NOT NULL THEN json_array(prev_size, size) "
            "     ELSE json_array(size) END "
            "WHERE hist IS NULL AND size IS NOT NULL")

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---- 扫描批次 ----
    def begin_scan(self, target):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO scans(path, started_at) VALUES(?,?)",
                (norm(target), now_str()))
            self._conn.commit()
            return cur.lastrowid

    def end_scan(self, scan_id, total, dirs, note="done"):
        with self._lock:
            self._conn.execute(
                "UPDATE scans SET finished_at=?, total=?, dirs=?, note=? WHERE id=?",
                (now_str(), total, dirs, note, scan_id))
            self._conn.commit()

    def last_scan(self, target):
        """返回 (完成时间, 总大小, 文件夹数, 备注) 或 None。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT finished_at, total, dirs, note FROM scans "
                "WHERE path=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (norm(target),)).fetchone()
        return r

    def recent_scan_days(self, limit=20):
        """最近的扫描批次完成"日期"(去重、倒序), 供比较时间锚点下拉选择。

        比较时间锚点按天计算, 故这里只返回去重后的日期列表(substr(finished_at,1,10)),
        不再返回到秒的时间串 —— 否则同一天有多次扫描时下拉里会出现多个实质等价的项。
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT substr(finished_at, 1, 10) AS d FROM scans "
                "WHERE finished_at IS NOT NULL AND finished_at <> '' "
                "ORDER BY d DESC LIMIT ?", (limit,))
            return [r[0] for r in cur.fetchall() if r[0]]

    # ---- 读写 ----
    def upsert_many(self, rows):
        t0 = time.perf_counter()
        with self._lock:
            try:
                self._conn.executemany(_UPSERT, rows)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
        self.write_time += time.perf_counter() - t0

    def purge_stale(self, parents, seq):
        """删除这些目录下本次未出现的旧子项记录(文件夹已被删除/改名)。"""
        t0 = time.perf_counter()
        with self._lock:
            try:
                self._conn.executemany(
                    "DELETE FROM folders WHERE parent=? AND seq<>?", [(p, seq) for p in parents])
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
        self.write_time += time.perf_counter() - t0

    def touch(self, path, seq, ts=None):
        """命中缓存(未重新遍历)的目录: 保留原大小与时间, 只更新批次号并追加历史点。"""
        p = norm(path)
        t = ts or now_str()
        with self._lock:
            self._conn.execute(_TOUCH_SUBTREE, (seq, t, t, p, path_esc(p) + "%"))
            self._conn.commit()

    @staticmethod
    def _rec(row, cutoff=None):
        name, real, size, prev_raw, ts, sig, denied, partial, starred, hist = row
        pairs = hist_pairs(hist)
        if cutoff:
            # 比较时间锚点: 只按锚点之前(含)的历史算基准, 绝不回落到 prev_size
            # (prev_size 是"上一次扫描"的值, 与锚点时点无关, 回落会给出错误的、过新的基准)
            prev, prev_kind, prev_ts = baseline_asof(pairs, cutoff)
        else:
            prev, prev_kind, prev_ts = baseline_of(pairs)
            if prev is None:
                prev, prev_kind, prev_ts = prev_raw, ("上一条" if prev_raw is not None else None), None
        return {"name": name, "path": real, "size": size, "prev": prev,
                "prev_kind": prev_kind, "prev_ts": prev_ts,
                "ts": ts or None, "sig": sig, "denied": bool(denied),
                "partial": bool(partial), "starred": bool(starred),
                "hist": hist_sizes(pairs),
                "hist_ts": [t for t, _s in pairs],
                "cached": False, "merged": False}

    def children(self, parent, cutoff=None):
        """读取某个目录的一级子目录记录(按大小降序)。

        cutoff 为真时按"比较时间锚点"计算比较基准(仅锚定比较侧, 当前大小不变)。
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT name, real, size, prev_size, scanned_at, sig, denied, partial, "
                "starred, hist FROM folders WHERE parent=? ORDER BY size DESC",
                (norm(parent),))
            return [self._rec(r, cutoff) for r in cur.fetchall()]

    def starred_children(self, parent):
        with self._lock:
            cur = self._conn.execute(
                "SELECT real FROM folders WHERE parent=? AND starred=1", (norm(parent),))
            return {r[0] for r in cur.fetchall()}

    def is_starred(self, path):
        with self._lock:
            r = self._conn.execute(
                "SELECT starred FROM folders WHERE path=?", (norm(path),)).fetchone()
        return bool(r[0]) if r else False

    def set_star(self, path, starred, name=None, parent=None):
        p = norm(path)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO folders(path, real, name, parent, seq) VALUES(?,?,?,?,0)",
                (p, path, name or os.path.basename(path), norm(parent or os.path.dirname(path))))
            self._conn.execute("UPDATE folders SET starred=? WHERE path=?",
                               (1 if starred else 0, p))
            self._conn.commit()

    def remove_subtree(self, path):
        """删除记录(含所有子孙), 用于文件夹被删除后清理。"""
        p = norm(path)
        with self._lock:
            self._conn.execute(
                "DELETE FROM folders WHERE path=? OR path LIKE ? ESCAPE '\\'",
                (p, path_esc(p) + "%"))
            self._conn.commit()

    def info(self):
        """(行数, 文件大小MB, 数据库路径)"""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            mb = os.path.getsize(self.path) / 1048576
        except OSError:
            mb = 0.0
        return n, mb, self.path

    # ---- 界面设置持久化(复用 meta 表, 键名统一加 "ui." 前缀) ----
    def get_settings(self):
        """读取界面设置: 只取 meta 表中键以 "ui." 开头的行, 返回去掉前缀的 dict。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'ui.%'").fetchall()
        return {k[len("ui."):]: v for k, v in rows}

    def set_settings(self, values):
        """写入界面设置: UPSERT 进 meta 表(键加回 "ui." 前缀)。values: dict[str, str]。"""
        rows = [("ui." + str(k), "" if v is None else str(v)) for k, v in values.items()]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", rows)
            self._conn.commit()

    def delete_setting(self, key):
        """删除某条界面设置(按需使用)。"""
        with self._lock:
            self._conn.execute("DELETE FROM meta WHERE key=?", ("ui." + str(key),))
            self._conn.commit()

    def migrate_curve_click_default_v11(self):
        """一次性迁移: 把"单击行弹出曲线"的默认值由开改为关, 且对老库同样生效。

        背景: 旧版本默认开启(curve_click=True), 老库里已存着 ui.curve_click = "1"。仅改
        代码里的默认值对老用户无效(设置存在即被读回), 故用带版本号的标记键做一次性迁移。

        约束(必须全部满足):
          - 只执行一次: 以 ui.curve_click_default_v11 是否存在为准, 存在即直接返回, 绝不重复写;
          - 之后永不覆盖用户选择: 用户之后手动改的 ui.curve_click 不会被再次改写;
          - 不动其它键: 只读写这两个键;
          - 空库/新库不报错: 首次调用即完成迁移, 之后幂等。
        返回 True 表示本次真的执行了迁移, False 表示此前已迁移(幂等)。
        """
        marker = "ui.curve_click_default_v11"
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone() is not None:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('ui.curve_click', '0')")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", (marker,))
            self._conn.commit()
            return True

    def import_legacy_if_empty(self, json_path):
        """首次运行把旧的 baseline.json 导入数据库(保留为起始基线)。"""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        if n or not os.path.exists(json_path):
            return 0
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        rows = []
        for target, blk in data.items():
            ts = blk.get("timestamp")
            for fname, v in (blk.get("entries") or {}).items():
                if fname in (MERGED_PREFIX, LEGACY_MERGED_PREFIX):
                    continue              # 合并行是伪条目, 不导入(新旧前缀都要排除)
                size = v.get("size") if isinstance(v, dict) else v
                path = os.path.join(target, fname)
                rows.append(upsert_row(path, size, ts, None, False, False, 0))
        if rows:
            self.upsert_many(rows)
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_imported', ?)",
                    (json_path,))
                self._conn.commit()
        return len(rows)


# ---------------- 扫描调度 ----------------
def scan_children(target, stop_event, store, scan_id, progress_cb=None,
                  max_workers=None, skip_unchanged=False):
    """并行扫描 target 下一级子目录, 结果写入数据库。

    返回记录列表(按大小降序), 每条:
    {name, path, size, prev, ts, sig, denied, partial, starred, cached, merged}
    """
    if max_workers is None:
        max_workers = min(16, (os.cpu_count() or 4) * 2)

    # 先取"上一次"的记录(必须在本轮写入之前读, 用于计算增长)
    prev_map = {r["name"]: r for r in store.children(target)}
    try:
        entries = [e for e in os.scandir(target) if e.is_dir(follow_symlinks=False)]
    except PermissionError as e:
        raise RuntimeError(f"无权限访问 {target}: {e}")
    except OSError as e:
        raise RuntimeError(f"无法访问 {target}: {e}")

    results = []
    done = [0]
    lock = threading.Lock()
    total = len(entries)
    ts_full = now_str()

    def worker(entry):
        if stop_event.is_set():
            raise KeyboardInterrupt
        name, path = entry.name, entry.path
        buf = []

        def flush():
            if buf:
                store.upsert_many(list(buf))
                store.purge_stale([r[0] for r in buf], scan_id)
                buf.clear()

        def emit(p, size, denied, partial, sig):
            buf.append(upsert_row(p, size, ts_full, sig, denied, partial, scan_id))
            if len(buf) >= BATCH:
                flush()

        prev = prev_map.get(name)
        if is_reparse_point(path):
            size, denied, partial, cached, sig_h = 0, False, False, False, None
            emit(path, 0, False, False, None)
        else:
            sig_h = sig_hash(quick_sig(path))
            if (skip_unchanged and sig_h and prev
                    and prev.get("sig") == sig_h and prev.get("size") is not None):
                # 浅层签名一致 → 沿用旧值与旧子树记录, 不再遍历
                size, denied, partial = prev["size"], prev["denied"], prev["partial"]
                cached = True
                store.touch(path, scan_id, ts_full)
            else:
                size, denied, partial = walk_dir(path, stop_event, emit, root_sig=sig_h)
                cached = False
        flush()

        rec = {"name": name, "path": path, "size": size, "ts": ts_full,
               "sig": sig_h, "denied": denied, "partial": partial,
               "prev": (prev["size"] if prev else None),
               "prev_kind": (prev.get("prev_kind") if prev else None),
               "prev_ts": (prev.get("prev_ts") if prev else None),
               "prev_raw_ts": (prev.get("ts") if prev else None),
               "starred": bool(prev and prev["starred"]),
               "cached": cached, "merged": False}
        with lock:
            done[0] += 1
            n = done[0]
        if progress_cb:
            mark = "缓存命中" if cached else "完成"
            # 回调多带 (完成数, 总数), 供界面更新进度条; 纯文本调用方不受影响
            progress_cb(f"[{max_workers} 线程并行] {n}/{total} {mark}: {name}", n, total)
        return rec

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, e): e.name for e in entries}
        try:
            for fut in as_completed(futures):
                results.append(fut.result())
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    # 目标下已被删除的一级子目录记录清理
    store.purge_stale([norm(target)], scan_id)
    results.sort(key=lambda r: r["size"], reverse=True)
    # 同步更新"目标目录自身"的记录, 使上层视图看到最新大小
    parent_of_target = os.path.dirname(os.path.normpath(target))
    if parent_of_target and norm(parent_of_target) != norm(target):
        total = sum(r["size"] or 0 for r in results)
        partial = any(r["denied"] or r["partial"] for r in results)
        store.upsert_many([upsert_row(
            os.path.normpath(target), total, ts_full, sig_hash(quick_sig(target)),
            False, partial, scan_id)])
    return results


def format_size(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def list_fixed_drives():
    """枚举本机可用固定磁盘 (C:, D:, ...)。"""
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            root = f"{letter}:\\"
            if os.path.exists(root):
                drives.append(root)
    return drives


def pick_ui_family():
    """选一个含块状字符(U+2581-2588)的字体族。

    ttk 在 Windows 上默认用 Segoe UI, 而 Segoe UI 没有 ▁▂▃ 这类字符,
    走势缩略图会显示成方框; Microsoft YaHei UI 含全部所需字形。
    """
    try:
        from tkinter.font import families
        avail = set(families())
    except Exception:                                   # pragma: no cover
        return None
    for fam in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun"):
        if fam in avail:
            return fam
    return None


# ---------------- 删除到回收站 ----------------
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040        # 移入回收站(可还原)
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400


def delete_to_recycle_bin(path):
    """把文件夹/文件移入回收站。返回 (ok, 说明)。"""
    path = os.path.abspath(path)
    npth = os.path.normpath(path)
    if not os.path.exists(npth):
        return False, "路径不存在"
    if len(npth) <= 3 and npth.endswith(":"):
        return False, "不允许删除磁盘根目录"
    if not os.path.isdir(npth):
        return False, "目标不是文件夹"
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = npth + "\0"    # ctypes 会自动补一个终止符 → 双 \0 结尾
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:                                  # pragma: no cover
        return False, f"调用系统接口失败: {e}"
    if rc != 0:
        return False, f"删除失败(系统错误码 {rc})"
    if op.fAnyOperationsAborted:
        return False, "操作被取消"
    if os.path.exists(npth):
        return False, "删除未生效(可能被占用)"
    return True, "已移入回收站"


def confirm_delete(parent, path, size_text):
    """删除确认对话框(默认按钮是取消)。"""
    dlg = tk.Toplevel(parent)
    dlg.title("确认删除")
    dlg.transient(parent)
    dlg.resizable(False, False)
    frm = ttk.Frame(dlg, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="⚠️ 此操作非常危险，可能导致不可逆的数据丢失！",
              foreground=RED_FG, font=Font(weight="bold", size=10)).pack(anchor="w")
    ttk.Label(frm, text="即将把以下文件夹移入回收站（可从回收站还原）：",
              wraplength=520).pack(anchor="w", pady=(10, 2))
    ttk.Label(frm, text=path, foreground=ACCENT_DARK,
              wraplength=520).pack(anchor="w")
    ttk.Label(frm, text=f"当前大小: {size_text}").pack(anchor="w", pady=(6, 0))
    ttk.Label(frm, text="请确认路径无误后再继续。程序不会直接永久删除文件。",
              foreground=TEXT_MUTED, wraplength=520).pack(anchor="w", pady=(10, 0))
    box = {"ok": False}

    def _ok():
        box["ok"] = True
        dlg.destroy()

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(16, 0))
    ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
    ttk.Button(btns, text="移入回收站", style="Danger.TButton",
               command=_ok).pack(side="right", padx=8)
    dlg.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg.winfo_height()) // 3
    dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    dlg.grab_set()
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    parent.wait_window(dlg)
    return box["ok"]


# ---------------- 大小变化曲线窗口 ----------------
class CurveWindow:
    """单击列表行时弹出的曲线小窗(单例复用: 再点别的行只更新内容)。

    横坐标 = 第几次扫描记录, 纵坐标 = 文件夹大小。
    """

    def __init__(self, master, family=None):
        self.master = master
        self.family = family
        self.f_small = Font(family=family, size=8) if family else Font(size=8)
        self.f_mid = Font(family=family, size=9) if family else Font(size=9)
        self.f_big = Font(family=family, size=10, weight="bold") if family \
            else Font(size=10, weight="bold")
        self.series = []
        self.times = []
        self.label = ""
        self._placed = False
        self._build_window()

    def _build_window(self):
        """创建(必要时重建)曲线窗 Toplevel。

        用户点标题栏 [X] 时只隐藏不销毁(否则之后 deiconify 会报
        TclError: bad window path name ".!toplevel")；万一窗口已被销毁(如外部脚本),
        show() 会检测到并在此重建。
        """
        self.win = tk.Toplevel(self.master)
        self.win.title("文件夹大小变化曲线")
        self.win.geometry("660x420")
        self.win.minsize(420, 260)
        self.win.transient(self.master)
        self.win.withdraw()                      # 首屏不弹出, 单击行时才显示
        self.canvas = tk.Canvas(self.win, bg="#ffffff", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.title_var = tk.StringVar(value="—")
        self.info_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.title_var, anchor="w",
                  font=self.f_big).pack(fill="x", padx=10, pady=(6, 0))
        ttk.Label(self.win, textvariable=self.info_var, anchor="w",
                  foreground=TEXT_MUTED).pack(fill="x", padx=10, pady=(2, 8))
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.win.bind("<Escape>", lambda e: self.win.withdraw())
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)   # [X] 只隐藏

    def _alive(self):
        """Toplevel 是否仍存在(被 destroy 后再操作会抛 TclError)。"""
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def show(self, label, series, approx=False, times=None, prev=None, prev_kind=None,
             prev_ts=None):
        if not self._alive():                    # 窗口被销毁过 → 重建, 避免 bad window path name
            self._placed = False
            self._build_window()
        self.label = label
        self.series = list(series or [])
        self.approx = approx
        ts = list(times or [])
        # 时间点与数据点一一对应时才用于展示
        self.times = ts if len(ts) == len(self.series) and self.series else []
        self.prev = prev
        self.prev_kind = prev_kind
        self.prev_ts = prev_ts
        self.win.title(f"大小变化曲线 - {label}")
        self.title_var.set(label)
        self._place_near_master()
        self.win.deiconify()
        # 部分系统上 transient 窗口 deiconify 后会藏在主窗后面, 强制提到最前
        try:
            self.win.lift()
            self.win.attributes("-topmost", True)
            self.win.after(300, self._drop_topmost)
            self.win.focus_set()
        except tk.TclError:
            pass
        self._draw()

    def _drop_topmost(self):
        if self._alive():
            try:
                self.win.attributes("-topmost", False)
            except tk.TclError:
                pass

    def _place_near_master(self):
        """把窗口放到主窗口中部偏上(仅首次), 避免默认出现在屏幕 (0,0) 或屏幕外。"""
        if getattr(self, "_placed", False):
            return
        try:
            m = self.master
            m.update_idletasks()
            w, h = 660, 420
            x = m.winfo_rootx() + max(0, (m.winfo_width() - w) // 2)
            y = m.winfo_rooty() + max(0, (m.winfo_height() - h) // 4)
            self.win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
            self._placed = True
        except tk.TclError:
            pass

    def _draw(self):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), 80)
        series = self.series
        if not series:
            c.create_text(w // 2, h // 2, text="暂无历史记录\n(扫描两次以上即可看到变化曲线)",
                          fill=TEXT_MUTED, justify="center", font=self.f_mid)
            self.info_var.set("提示: 每次扫描都会为每个文件夹追加一个历史点")
            return

        lo, hi = nice_range(series)
        span = (hi - lo) or 1.0
        ml, mr, mt, mb = 76, 24, 18, 36
        pw, ph = max(w - ml - mr, 20), max(h - mt - mb, 20)
        n = len(series)
        px = lambda i: ml + (pw / 2 if n == 1 else pw * i / (n - 1))
        py = lambda v: mt + ph - (v - lo) / span * ph

        # 横向网格 + 纵轴刻度
        for k in range(5):
            y = mt + ph * k / 4
            c.create_line(ml, y, ml + pw, y, fill="#eef2f7")
            c.create_text(ml - 8, y, anchor="e", fill=TEXT_MUTED, font=self.f_small,
                          text=format_size(lo + span * (4 - k) / 4))

        # 折线颜色: 整体变大=红, 变小=绿, 持平=灰
        first, last = series[0], series[-1]
        color = GROW_FG if last > first else (SHRINK_FG if last < first else TEXT_MUTED)
        pts = []
        for i, v in enumerate(series):
            pts += [px(i), py(v)]
        if n > 1:
            c.create_line(*pts, fill=color, width=2, smooth=False)
        for i, v in enumerate(series):
            x, y = px(i), py(v)
            c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline="#ffffff")
            if i == n - 1 or n <= 12:
                c.create_text(x, y - 12, text=format_size(v), fill=TEXT,
                              font=self.f_small)

        # 横轴: 次数
        step = max(1, (n + 11) // 12)
        for i in range(0, n, step):
            c.create_text(px(i), mt + ph + 14, text=str(i + 1), fill=TEXT_MUTED,
                          font=self.f_small)
        c.create_line(ml, mt + ph, ml + pw, mt + ph, fill="#cbd5e1")
        c.create_line(ml, mt, ml, mt + ph, fill="#cbd5e1")
        c.create_text(ml - 46, mt + ph / 2, text="大小", fill=TEXT_MUTED,
                      font=self.f_small, angle=90)
        c.create_text(ml + pw / 2, mt + ph + 28, text="次数 (第几次扫描记录)",
                      fill=TEXT_MUTED, font=self.f_small)

        direction = "↑ 变大" if last > first else ("↓ 变小" if last < first else "— 持平")
        back = len(series[-SPARK_WIDTH:])
        info = (f"当前 {format_size(last)} | 区间 {format_size(first)} → {format_size(last)}"
                f"，变动 {format_size(last - first)} ({direction})")
        if self.prev is not None:
            info += f" | 比较基准 {format_size(self.prev)}"
            if self.prev_kind:
                info += f" ({self.prev_kind}"
                info += f" @ {self.prev_ts})" if self.prev_ts else ")"
        if self.times:
            info += f" | 记录时间 {self.times[0]} → {self.times[-1]}"
        info += f" | 共 {n} 次记录, 缩略图取最近 {back} 次"
        info += "  ※合并行数据为成员求和, 仅供趋势参考" if self.approx else ""
        self.info_var.set(info)


# ---------------- 界面设置恢复辅助 ----------------
def setting_bool(saved, key, default):
    """把持久化的布尔设置("1"/"0")解析为 bool; 缺失或非法时用默认值。"""
    raw = saved.get(key)
    if raw is None:
        return default
    if raw in ("1", "true", "True"):
        return True
    if raw in ("0", "false", "False"):
        return False
    return default


def setting_text(saved, key, default):
    """取数值型设置文本(可被 float 解析); 缺失或非法时用默认值。"""
    raw = saved.get(key)
    if raw is None:
        return default
    try:
        float(raw)
    except (TypeError, ValueError):
        return default
    return str(raw)


# ---------------- 轻量 tooltip ----------------
class Tooltip:
    """极简 tooltip: 无边框 Toplevel + Label, 悬停显示 / 离开或点击隐藏。

    为什么要自己写: 界面改版把冗长提示收敛成短标签, 详细说明挪到 tooltip; 而 Tk 本身没有
    tooltip, 引入第三方库又违反"零运行时依赖", 所以用几十行自绘一个足够用的版本。
    随控件销毁: 绑定 <Destroy> 一并收起, 避免残留的浮层挡住界面。
    """

    _BG = "#1e293b"
    _FG = "#ffffff"

    def __init__(self, widget, text, family=None, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._tip = None
        self._font = (family, 9) if family else ("TkDefaultFont", 9)
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except tk.TclError:
            self._after = None

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        self._after = None
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tip.geometry(f"+{x}+{y}")
        tk.Label(tip, text=self.text, justify="left", bg=self._BG, fg=self._FG,
                 padx=8, pady=5, font=self._font).pack()
        self._tip = tip

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# ---------------- GUI ----------------
class DiskGuardApp:
    def __init__(self, root):
        self.root = root
        root.title("DiskGuard - 磁盘空间监控")
        root.geometry("1180x760")
        # 高度下限 700: 低于它上方卡片(合计约 640px)会把树表挤到不可见(实测 640 时树表被压成 0 高)
        root.minsize(1060, 700)

        self.stop_event = threading.Event()
        self.scan_thread = None
        self.ui_queue = queue.Queue()
        self.scanned_target = None
        self.view_mode = "browse"
        self.auto_job = None
        self._rows = []
        self._records = []
        self._iid_rows = {}
        self._started = False
        self.cleanup_note = None     # 启动清理临时解包残留的结果提示
        self.restore_note = None     # 恢复上次设置的提示(供 db_info 显示)
        self.history = []            # [{"target","records","mode"}]
        self._asof = None            # 比较时间锚点(None = 最新, 不锚定)
        self._status_base = ""       # 状态栏基础文本(锚点/筛选提示追加在其后)
        self._settings_job = None    # "变更即存"的防抖句柄
        self._loading_settings = True  # 首次构建界面期间不触发保存
        self.store = Store()
        self.store.import_legacy_if_empty(LEGACY_BASELINE)
        # 一次性迁移: 把"单击行弹出曲线"默认改为关(对老库里已存 ui.curve_click="1" 同样生效)。
        # 必须放在读取 _saved 之前, 否则界面会把迁移前的旧值读回来。迁移幂等, 不会覆盖用户选择。
        try:
            self.store.migrate_curve_click_default_v11()
        except sqlite3.Error:
            pass
        self._saved = self.store.get_settings()   # 读回上次保存的界面设置(可能为空)

        self._build_ui()
        self._init_settings()        # 挂"变更即存"监听 + 恢复定时自动扫描
        self.root.after(100, self._poll_queue)
        self.root.after(250, self._initial_browse)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 界面构建 ----
    def _card(self, parent, title=None):
        """创建一个"卡片"容器, 返回 (卡片外框, 内容区)。

        为什么用 tk.Frame + highlightthickness 描边: Tk 没有圆角/阴影, 现代浅色风只能靠
        "浅灰页面底 + 白色卡片 + 1px 细边框"来分层; highlightbackground 恰好能画出这条
        1px 描边(未聚焦时), 无需引入任何第三方库。
        """
        card = tk.Frame(parent, bg=SURFACE, highlightbackground=CARD_BORDER,
                        highlightthickness=1, bd=0)
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=10)
        if title:
            ttk.Label(body, text=title, style="CardTitle.TLabel").pack(
                anchor="w", pady=(0, 8))
        return card, body

    def _tip(self, widget, text):
        """给控件挂一个轻量 tooltip(把被精简掉的说明挪到这里)。"""
        return Tooltip(widget, text, family=self.ui_family)

    def _build_filter_pills(self, parent):
        """构建筛选"分段 pill"组(5 个互斥胶囊按钮)。

        为什么用经典 tk.Radiobutton(indicatoron=False): 共享同一个自定义变量即可实现互斥,
        且外观可完全自绘(ttk.Radiobutton 在 clam 下不易做成实心胶囊)。Tk 无圆角, 用实心
        填充 + 无边框近似; 选中项主色实心白字, 未选项浅灰底深字, 一眼可辨。
        变量名与 5 个取值保持不变(与 ui.filter 持久化约定一致)。
        """
        flt_init = self._saved.get("filter", "全部")
        if flt_init not in FILTERS:
            flt_init = "全部"
        self.filter_var = tk.StringVar(value=flt_init)
        self._pills = {}
        for name in FILTERS:
            rb = tk.Radiobutton(parent, text=name, value=name,
                                variable=self.filter_var, indicatoron=False,
                                bd=0, relief="flat", highlightthickness=0,
                                padx=12, pady=4, cursor="hand2",
                                font=self.f_pill, command=self._on_filter_change)
            rb.pack(side="left", padx=(0, 4))
            self._pills[name] = rb
        self._style_pills()

    def _style_pills(self):
        """按当前选中项给 pill 重新着色(选中=主色实心, 未选=浅灰底)。"""
        cur = self.filter_var.get()
        for name, rb in self._pills.items():
            if name == cur:
                rb.configure(bg=ACCENT, fg="#ffffff", activebackground=ACCENT_DARK,
                             activeforeground="#ffffff", selectcolor=ACCENT)
            else:
                rb.configure(bg=SURFACE_ALT, fg=TEXT, activebackground="#eef2f7",
                             activeforeground=TEXT, selectcolor=SURFACE_ALT)

    def _on_filter_change(self):
        """筛选档位变化: 重绘 pill 外观并刷新当前视图(不重扫/不重读库)。"""
        self._style_pills()
        self._set_status()          # 即使当前无记录也要更新"锚点/筛选"指示
        self._refresh_view()

    def _build_ui(self):
        # 树控件字体要显式指定: 默认的 Segoe UI 缺少 ▁▂▃ 与 ★ 字形
        self.ui_family = pick_ui_family()
        fam = self.ui_family or "Microsoft YaHei UI"
        self.f_bold = Font(family=fam, size=9, weight="bold")
        self.f_title = Font(family=fam, size=14, weight="bold")
        self.f_pill = Font(family=fam, size=9)
        self.root.configure(background=PAGE_BG)

        style = ttk.Style()
        # ---- 浅色现代风样式: 浅灰页面 + 白卡片 + 现代蓝主色 + 柔和语义色 ----
        style.configure("Page.TFrame", background=PAGE_BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("CardTitle.TLabel", background=SURFACE, foreground=TEXT,
                        font=(fam, 10, "bold"))
        style.configure("Header.TLabel", background=PAGE_BG, foreground=TEXT)
        style.configure("Sub.TLabel", background=PAGE_BG, foreground=TEXT_MUTED)
        style.configure("TFrame", background=SURFACE)
        style.configure("TLabel", background=SURFACE, foreground=TEXT)
        style.configure("Muted.TLabel", background=SURFACE, foreground=TEXT_MUTED)
        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", SURFACE)])
        style.configure("TRadiobutton", background=SURFACE, foreground=TEXT)
        # 输入类控件统一扁平: 细边框 + 白底 + 统一内边距
        style.configure("TEntry", fieldbackground="#ffffff", foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=(6, 3))
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                        foreground=TEXT, bordercolor=BORDER, arrowcolor=TEXT_MUTED,
                        padding=(6, 3))
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#ffffff")],
                  bordercolor=[("focus", ACCENT), ("hover", BORDER)],
                  arrowcolor=[("active", ACCENT)])
        style.configure("TSpinbox", fieldbackground="#ffffff", foreground=TEXT,
                        bordercolor=BORDER, arrowcolor=TEXT_MUTED, arrowsize=12,
                        padding=(4, 3))
        # 普通按钮: 扁平浅灰底, 悬停加深
        style.configure("TButton", borderwidth=1, relief="flat", padding=(12, 5),
                        background=SURFACE_ALT, foreground=TEXT, bordercolor=BORDER,
                        focusthickness=0)
        style.map("TButton",
                  background=[("active", "#eef2f7"), ("pressed", "#e2e8f0")],
                  bordercolor=[("active", BORDER)])
        # 主按钮(开始扫描): 实心蓝底白字
        style.configure("Accent.TButton", borderwidth=0, padding=(14, 6),
                        background=ACCENT, foreground="#ffffff")
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK)])
        # 危险按钮(删除到回收站): 红字, 悬停浅红底
        style.configure("Danger.TButton", foreground=GROW_FG)
        style.map("Danger.TButton", background=[("active", GROW_BG)])
        # 状态栏
        style.configure("Status.TLabel", background="#f1f5f9", foreground=TEXT,
                        padding=(8, 4))
        # 进度条: 现代蓝
        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=BORDER, bordercolor=BORDER)
        style.configure("Vertical.TScrollbar", background=SURFACE_ALT,
                        troughcolor=PAGE_BG, bordercolor=SURFACE_ALT,
                        arrowcolor=TEXT_MUTED, relief="flat")
        # 表格: 白底, 行高 28, 表头浅灰底不加重边框
        if self.ui_family:
            style.configure("Treeview", font=(self.ui_family, 9))
            style.configure("Treeview.Heading", font=(self.ui_family, 9))
        style.configure("Treeview", rowheight=28, fieldbackground=SURFACE,
                        background=SURFACE, bordercolor=BORDER)
        style.configure("Treeview.Heading", background="#f1f5f9", foreground=TEXT,
                        relief="flat", padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", "#e8edf4")])
        # 选中行: 淡蓝底深色字(不用默认深蓝底白字)
        style.map("Treeview",
                  background=[("selected", ACCENT_LIGHT)],
                  foreground=[("selected", TEXT)])

        # ---- 顶部标题条: 产品名 + 一行灰色数据库状态 ----
        header = tk.Frame(self.root, bg=PAGE_BG)
        header.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(header, text="DiskGuard", bg=PAGE_BG, fg=TEXT,
                 font=self.f_title).pack(anchor="w")
        self.db_info_var = tk.StringVar()
        self.db_info_label = tk.Label(header, textvariable=self.db_info_var,
                                      bg=PAGE_BG, fg=TEXT_MUTED, font=(fam, 9),
                                      anchor="w", justify="left", wraplength=1100)
        self.db_info_label.pack(fill="x")
        # 副标题与状态栏按窗口宽度换行, 窄窗口下不裁切(见 _on_root_configure)
        header.bind("<Configure>",
                    lambda e: self.db_info_label.configure(
                        wraplength=max(200, e.width - 8)))
        self._tip(self.db_info_label,
                  "大小数据与历史记录保存在 SQLite 数据库, 双击行读库下钻(不重新扫描), "
                  "单击行弹出大小曲线; 增长/增长率与行色以\"比较基准\"为准。\n"
                  "比较基准 = 当天第一条记录; 当天此前无记录则用上一条。\n"
                  "红 = 变大, 绿 = 变小; 无权限目录标注无权限 / 部分无权限。")

        # 状态栏必须先于卡片 pack(side="bottom"): pack 按调用顺序分配空间, 若放到最后,
        # 上方卡片把窗口高度用尽时状态栏会被挤成 0 高(真实踩到)。
        # 分两行: 上行=基础提示(随窗口宽度换行, 不裁切), 下行=锚点/筛选指示(始终可见,
        # 这是用户最需要一眼看到的状态, 绝不能被基础文本挤掉)。
        self.status_var = tk.StringVar(
            value="就绪 | F5 重扫 · 退格 返回 · Ctrl+S 星标 · Del 删除")
        self._status_base = self.status_var.get()
        self.status_suffix_var = tk.StringVar(value=self._view_suffix())
        status_frame = tk.Frame(self.root, bg="#f1f5f9")
        status_frame.pack(fill="x", side="bottom")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg="#f1f5f9", fg=TEXT, anchor="w",
                                     font=self.f_bold, justify="left",
                                     wraplength=1100)
        self.status_label.pack(fill="x", padx=8, pady=(4, 0))
        status_frame.bind("<Configure>",
                          lambda e: self.status_label.configure(
                              wraplength=max(200, e.width - 20)))
        tk.Label(status_frame, textvariable=self.status_suffix_var, bg="#f1f5f9",
                 fg=TEXT_MUTED, anchor="w", justify="left",
                 font=(fam, 9)).pack(fill="x", padx=8, pady=(0, 4))

        # ---- 卡片「扫描目标」 ----
        scan_card, sc = self._card(self.root, "扫描目标")
        scan_card.pack(fill="x", padx=14, pady=(0, 8))
        row0 = ttk.Frame(sc)
        row0.pack(fill="x")
        ttk.Label(row0, text="驱动器").pack(side="left")
        self.drive_var = tk.StringVar()
        drives = list_fixed_drives() or ["C:\\"]
        self.drive_cb = ttk.Combobox(row0, textvariable=self.drive_var,
                                     values=drives, width=8, state="readonly")
        drive_init, path_init = self._restore_target(drives)
        self.drive_cb.set(drive_init)
        self.drive_cb.pack(side="left", padx=6)
        ttk.Label(row0, text="扫描路径").pack(side="left", padx=(12, 6))
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(row0, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_var.set(path_init)
        self.drive_cb.bind("<<ComboboxSelected>>", self._on_drive_change)
        self.scan_btn = ttk.Button(row0, text="开始扫描", style="Accent.TButton",
                                   command=self.start_scan)
        self.scan_btn.pack(side="left", padx=(12, 6))
        self.stop_btn = ttk.Button(row0, text="停止", command=self.stop_scan,
                                   state="disabled")
        self.stop_btn.pack(side="left")

        # ---- 卡片「视图与预警」(视图/导航/阈值/定时合并为一张卡片, 给树表让出高度) ----
        view_card, vc = self._card(self.root, "视图与预警")
        self.view_card = view_card
        view_card.pack(fill="x", padx=14, pady=(0, 8))
        r1 = ttk.Frame(vc)
        r1.pack(fill="x")
        ttk.Button(r1, text="⬅ 返回上一层", command=self.go_up).pack(side="left")
        ttk.Button(r1, text="⭐ 星标", command=self.toggle_star).pack(side="left", padx=6)
        # 危险按钮用纯文字: "🗑" 在红色前景下会渲染成一坨红色实心块, 很脏
        ttk.Button(r1, text="删除到回收站", style="Danger.TButton",
                   command=self.delete_selected).pack(side="left")
        pillbox = ttk.Frame(r1)
        pillbox.pack(side="right")
        ttk.Label(pillbox, text="筛选").pack(side="left", padx=(0, 6))
        self._build_filter_pills(pillbox)

        r2 = ttk.Frame(vc)
        r2.pack(fill="x", pady=(6, 0))
        merge_lbl = ttk.Label(r2, text="小于")
        merge_lbl.pack(side="left")
        self.min_mb_var = tk.StringVar(value=setting_text(self._saved, "min_mb", "0"))
        min_box = ttk.Spinbox(r2, from_=0, to=102400, textvariable=self.min_mb_var,
                              width=6, command=self._refresh_view)
        min_box.pack(side="left", padx=6)
        merge_lbl2 = ttk.Label(r2, text="MB 合并")
        merge_lbl2.pack(side="left")
        self.skip_var = tk.BooleanVar(value=setting_bool(self._saved, "skip", False))
        skip_cb = ttk.Checkbutton(r2, text="跳过无变化", variable=self.skip_var)
        skip_cb.pack(side="left", padx=16)
        self.drill_var = tk.BooleanVar(value=setting_bool(self._saved, "drill", True))
        drill_cb = ttk.Checkbutton(r2, text="双击行下钻", variable=self.drill_var)
        drill_cb.pack(side="left")
        for w in (merge_lbl, min_box, merge_lbl2):
            self._tip(w, "小于该值的文件夹合并成一行\"📦 合并的小文件夹\"显示; 0 = 不合并")
        self._tip(skip_cb, "加速: 仅比对浅层签名, 不重新遍历子目录(可能漏检深层变化)")
        self._tip(drill_cb, "双击一行进入该文件夹的子目录视图(读数据库记录, 不重新扫描)")

        r3 = ttk.Frame(vc)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="增长 ≥").pack(side="left")
        self.mb_var = tk.StringVar(value=setting_text(self._saved, "mb", "500"))
        mb_box = ttk.Spinbox(r3, from_=1, to=102400, textvariable=self.mb_var, width=6)
        mb_box.pack(side="left", padx=6)
        ttk.Label(r3, text="MB 或").pack(side="left")
        self.pct_var = tk.StringVar(value=setting_text(self._saved, "pct", "10"))
        pct_box = ttk.Spinbox(r3, from_=1, to=1000, textvariable=self.pct_var, width=5)
        pct_box.pack(side="left", padx=6)
        grow_lbl = ttk.Label(r3, text="%")
        grow_lbl.pack(side="left")
        for w in (mb_box, pct_box, grow_lbl):
            self._tip(w, "与\"比较基准\"比较, 达到任一条件即预警(默认基准 = 当天第一条记录)")
        self.auto_var = tk.BooleanVar(value=setting_bool(self._saved, "auto", False))
        auto_cb = ttk.Checkbutton(r3, text="定时扫描 每", variable=self.auto_var,
                                  command=self._toggle_auto)
        auto_cb.pack(side="left", padx=(16, 0))
        self.interval_var = tk.StringVar(value=setting_text(self._saved, "interval", "30"))
        interval_box = ttk.Spinbox(r3, from_=1, to=1440, textvariable=self.interval_var,
                                   width=5, command=self._toggle_auto)
        interval_box.pack(side="left", padx=6)
        ttk.Label(r3, text="分钟").pack(side="left")
        self.curve_click_var = tk.BooleanVar(
            value=setting_bool(self._saved, "curve_click", False))
        curve_cb = ttk.Checkbutton(r3, text="单击行看曲线", variable=self.curve_click_var)
        curve_cb.pack(side="left", padx=16)
        for w in (auto_cb, interval_box):
            self._tip(w, "勾选后按间隔自动重扫; 阈值或间隔修改后下次扫描生效")
        self._tip(curve_cb, "横轴 = 扫描批次, 纵轴 = 文件夹大小; 红 = 变大, 绿 = 变小")

        r4 = ttk.Frame(vc)
        r4.pack(fill="x", pady=(6, 0))
        asof_saved = self._saved.get("asof", "")
        asof_init = self._parse_asof(asof_saved) if asof_saved else None
        self._asof = asof_init
        self.asof_var = tk.StringVar(value=(asof_init or "最新"))
        asof_lbl = ttk.Label(r4, text="比较时间锚点(按天)")
        asof_lbl.pack(side="left")
        self.asof_cb = ttk.Combobox(r4, textvariable=self.asof_var, width=12,
                                    state="normal",
                                    values=["最新"] + self.store.recent_scan_days())
        self.asof_cb.pack(side="left", padx=6)
        ttk.Button(r4, text="应用", command=self._apply_asof).pack(side="left")
        ttk.Button(r4, text="回到最新", command=self._clear_asof).pack(side="left",
                                                                      padx=6)
        for w in (asof_lbl, self.asof_cb):
            self._tip(w, "仅锚定比较基准(按天): 基准只取自锚定日(含)当天及更早的历史点; "
                         "当前大小仍为数据库最新值, 不受锚点影响")

        # ---- 树表卡片(占据剩余空间) ----
        tree_card, tc = self._card(self.root)
        tree_card.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        cols = ("star", "name", "size", "prev", "prevtime", "delta", "pct", "trend",
                "status", "scantime")
        self.tree = ttk.Treeview(tc, columns=cols, show="headings",
                                 selectmode="browse")
        # 列 key(cols)保持不变, 仅缩短表头显示文案
        headers = {"star": "★", "name": "文件夹", "size": "大小", "prev": "基准",
                   "prevtime": "基准日期", "delta": "变化", "pct": "变化率",
                   "trend": "走势", "status": "状态", "scantime": "扫描时间"}
        # 列宽按"最宽内容"实测(Font.measure)后加内边距, 保证不截断; 合计 + 树内边距须 < 最小
        # 窗口宽度(1060), 故 name 列取 168(其余列已够用, 让名字承担伸缩)。
        f9 = Font(family=fam, size=9)
        pad = 20
        widths = {
            "star": 30,
            "name": 168,
            "size": max(84, f9.measure("1023.9 MB") + pad),
            "prev": max(84, f9.measure("1023.9 MB") + pad),
            "prevtime": max(92, f9.measure("2026-09-23") + pad),
            "delta": max(106, f9.measure("▲ +999.9 GB") + pad),
            "pct": max(86, f9.measure("+99999.9%") + pad),
            "trend": max(80, f9.measure(SPARK_CHARS) + pad),
            "status": max(152, f9.measure("⚠ 部分无权限 新建基线") + pad),
            "scantime": max(88, f9.measure("09-23 10:12") + pad),
        }
        for c in cols:
            self.tree.heading(c, text=headers[c], command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=widths[c],
                             anchor="center" if c in ("star", "trend")
                             else ("w" if c == "name" else "e"))
        vsb = ttk.Scrollbar(tc, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # 行着色: 预警(深红) > 变大(红) > 变小(绿) > 星标(黄) > 斑马纹
        self.tree.tag_configure("alert", background=RED_BG, foreground=RED_FG)
        self.tree.tag_configure("grow", background=GROW_BG, foreground=GROW_FG)
        self.tree.tag_configure("shrink", background=SHRINK_BG, foreground=SHRINK_FG)
        self.tree.tag_configure("star", background=STAR_BG, foreground=STAR_FG)
        self.tree.tag_configure("ok", background=OK_BG, foreground=FLAT_FG)
        self.tree.tag_configure("stripe", background=STRIPED_BG, foreground=FLAT_FG)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<ButtonRelease-1>", self._on_single_click)
        self.tree.bind("<Button-3>", self._on_right_click)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.curve_win = CurveWindow(self.root, family=self.ui_family)

        # 扫描进度条(初始隐藏, 开始扫描时出现, 结束后自动收起)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(self.root, variable=self.progress_var,
                                        maximum=100, mode="determinate")
        self._progress_shown = False

        self._refresh_db_info()

        # 快捷键: F5 重扫 / 退格返回 / Ctrl+S 星标 / Del 删除
        self.root.bind_all("<F5>", lambda e: self.start_scan())
        self.root.bind_all("<BackSpace>",
                           lambda e: self._nav_if_not_editing(self.go_up))
        self.root.bind_all("<Control-s>",
                           lambda e: self._nav_if_not_editing(self.toggle_star))
        self.root.bind_all("<Delete>",
                           lambda e: self._nav_if_not_editing(self.delete_selected))

    def _nav_if_not_editing(self, action):
        """焦点在输入框/数字框时不劫持按键(避免把删字变成删文件夹)。"""
        w = self.root.focus_get()
        if isinstance(w, (ttk.Entry, ttk.Spinbox, tk.Entry, tk.Spinbox)):
            return
        action()

    # ---- 扫描进度条 ----
    def _show_progress(self):
        """进度条挂到「扫描目标」卡片下方、「视图」卡片上方(只挂一次)。"""
        if not self._progress_shown:
            self.progress.pack(fill="x", padx=14, pady=(0, 8), before=self.view_card)
            self._progress_shown = True

    def _hide_progress(self):
        # 扫描线程还在跑就不收(防止上一次扫描的延迟收起误伤本次扫描)
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if self._progress_shown:
            self.progress.pack_forget()
            self._progress_shown = False

    # ---- 视图渲染 ----
    def _initial_browse(self):
        if self._started:          # 用户已经点过扫描, 不要用浏览视图覆盖结果
            return
        self._browse(self.path_var.get().strip(), push_history=False)

    def _on_drive_change(self, _event=None):
        self.path_var.set(self.drive_cb.get())
        self.history.clear()
        self._browse(self.drive_cb.get(), push_history=False)

    def _snapshot(self):
        return {"target": self.scanned_target, "records": list(self._records),
                "mode": self.view_mode}

    def _show_records(self, path, records, mode, note=None, merge=True):
        """渲染一组记录(扫描结果 / 数据库记录 / 合并成员)。返回预警数。"""
        self.scanned_target = path
        self.view_mode = mode
        self._records = list(records)
        try:
            mb_th = max(0.0, float(self.mb_var.get()))
            pct_th = max(0.0, float(self.pct_var.get()))
            min_mb = max(0.0, float(self.min_mb_var.get()))
        except ValueError:
            mb_th, pct_th, min_mb = 500.0, 10.0, 0.0

        flt = self.filter_var.get()
        if flt not in FILTERS:
            flt = "全部"
        # 只有"全部"时才合并小文件夹(判据见 merge_allowed): 否则被筛出的成员行(预警/星标/
        # 增长/减少)会被合并进"📦 合并的小文件夹"那一行而看不见(合并行自身不参与这些判定)
        if merge_allowed(merge, flt):
            display, merged, members = merge_small(records, min_mb)
        else:
            display, merged, members = list(records), None, []

        alert_cnt = self._build_rows(display, merged, members, mb_th, pct_th, flt)
        self._redraw_rows()
        self._set_status(note)
        self._refresh_db_info()
        return alert_cnt

    def _build_rows(self, display, merged, members, mb_th, pct_th, flt):
        items = list(display)
        if merged is not None:
            items.append(merged)
        rows = []
        for rec in items:
            size, prev = rec["size"], rec.get("prev")
            denied, partial = rec.get("denied"), rec.get("partial")
            alert = False
            delta = pct = None
            if rec.get("merged"):
                status = "— 合并单位(双击展开)"
                if size is not None and prev:
                    delta = size - prev
                    pct = delta / prev * 100.0
            elif size is None:
                status = "未扫描"
            elif denied:
                status = DENIED_STATUS
            else:
                if prev:
                    delta = size - prev
                    pct = delta / prev * 100.0
                    alert = self._is_alert(prev, size, mb_th, pct_th)
                    base = ("⚠ 预警" if alert else
                            ("↑ 增长" if delta > 0 else ("↓ 减少" if delta < 0 else "— 持平")))
                else:
                    base = "新建基线"
                status = (PARTIAL_HINT + " " + base) if partial else base
            if rec.get("cached"):
                status = "✓ 无变化(缓存)"
                alert = False
            # 筛选(必须在 alert 与 cached 强制置位之后判定)
            if flt == "仅星标" and not rec.get("starred"):
                continue
            if flt == "仅预警" and not alert:
                continue
            # "仅增长"/"仅减少": 以 delta(= 大小 - 基准)符号判定。
            # delta 为 None 的行(未扫描 / 新建基线等)两个筛选都排除;
            # 缓存命中行虽可能带有非零 delta, 但其"变化"语义会误导(状态显示无变化),
            # 故一并排除 —— 只保留真正发生变化的行。
            if flt == "仅增长" and not (delta is not None and delta > 0
                                       and not rec.get("cached")):
                continue
            if flt == "仅减少" and not (delta is not None and delta < 0
                                       and not rec.get("cached")):
                continue
            # 历史序列: 合并行用成员的序列求和(近似, 无时间戳)
            if rec.get("merged"):
                series = sum_series([m.get("hist") for m in members])
                series_ts = []
                prev_kind = "成员求和(近似)"
            else:
                series = list(rec.get("hist") or [])
                series_ts = list(rec.get("hist_ts") or [])
                if not series and size is not None:
                    series = ([prev, size] if prev else [size])
                prev_kind = rec.get("prev_kind")
            tdir, tdelta = trend_of(series)
            rows.append({"name": rec["name"], "path": rec["path"], "size": size,
                         "prev": prev, "prev_disp": prev if prev is not None else None,
                         "prev_kind": prev_kind,
                         "prev_ts": rec.get("prev_ts"),
                         "delta": delta, "pct": pct, "status": status,
                         "ts": rec.get("ts") or "—", "starred": bool(rec.get("starred")),
                         "denied": bool(denied), "merged": bool(rec.get("merged")),
                         "alert": alert, "cached": bool(rec.get("cached")),
                         "series": series, "series_ts": series_ts,
                         "spark": sparkline(series),
                         "trend_dir": tdir, "trend_delta": tdelta})
        self._rows = rows
        return sum(1 for r in rows if r["alert"])

    def _redraw_rows(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_rows = {}
        for i, r in enumerate(self._rows):
            if r["alert"]:
                tag = ("alert",)
            elif (r["delta"] or 0) > 0:
                tag = ("grow",)                       # 较上次变大 → 红
            elif (r["delta"] or 0) < 0:
                tag = ("shrink",)                     # 较上次变小 → 绿
            elif r["starred"]:
                tag = ("star",)
            elif i % 2:
                tag = ("stripe",)
            else:
                tag = ("ok",)
            delta = r["delta"]
            iid = f"r{i}"
            self._iid_rows[iid] = r
            # 锚点已按天: 基准日期只显示日期(YYYY-MM-DD); 扫描时间显示"月-日 时:分"
            # (完整时间戳仍存于记录/数据库, 列宽因此可容纳, 不出现残缺的 "10:")
            pv = r["prev_ts"]
            pv_disp = pv[:10] if pv else "—"
            ts = r["ts"]
            ts_disp = ts[5:16] if (ts and len(ts) >= 16) else ts
            self.tree.insert("", "end", iid=iid, values=(
                "⭐" if r["starred"] else "",
                r["name"],
                format_size(r["size"]),
                format_size(r["prev_disp"]) if r["prev_disp"] is not None else "—",
                pv_disp,
                ("▲ +" + format_size(delta)) if (delta or 0) > 0
                else (("▼ " + format_size(delta)) if delta is not None else "—"),
                f"{r['pct']:+.1f}%" if r["pct"] is not None else "—",
                r["spark"] or "—",
                r["status"], ts_disp), tags=tag)

    def _on_single_click(self, event):
        """单击行 → 弹出/更新大小变化曲线小窗。"""
        if not self.curve_click_var.get():
            return
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return                                    # 点的是表头/分隔条, 忽略
        row = self._iid_rows.get(self.tree.identify_row(event.y))
        if row:
            self.show_curve(row)

    def show_curve(self, row):
        try:
            label = row["name"] + (f"  —  {row['path']}" if row["path"] else "")
            self.curve_win.show(label, row["series"], approx=bool(row["merged"]),
                                times=row.get("series_ts"),
                                prev=row.get("prev"), prev_kind=row.get("prev_kind"),
                                prev_ts=row.get("prev_ts"))
        except Exception as e:               # 打包后回调异常是静默的, 这里必须可见
            messagebox.showerror("大小变化曲线", f"打开曲线窗口失败: {e!r}")
            return
        if not row["series"]:
            self._set_status(f"{row['name']}: 暂无历史记录 (至少扫描两次后才有曲线)")

    def _sort_by(self, col):
        if not self._rows:
            return
        key_map = {
            "size": lambda r: (r["size"] if r["size"] is not None else -1),
            "delta": lambda r: (r["delta"] or 0),
            "pct": lambda r: (r["pct"] if r["pct"] is not None else -1),
            "name": lambda r: r["name"],
            "prev": lambda r: (r["prev"] if r["prev"] is not None else -1),
            "status": lambda r: r["status"],
            "scantime": lambda r: (r["ts"] or ""),
            "prevtime": lambda r: (r["prev_ts"] or ""),
            "star": lambda r: r["starred"],
            "trend": lambda r: (r["trend_delta"] or 0),
        }
        self._rows.sort(key=key_map.get(col, key_map["size"]), reverse=(col != "name"))
        self._redraw_rows()

    @staticmethod
    def _is_alert(prev, size, mb_th, pct_th):
        if not prev:
            return False
        delta = size - prev
        if delta <= 0:
            return False
        return delta >= mb_th * 1024 * 1024 or delta / prev * 100.0 >= pct_th

    def _refresh_view(self):
        """阈值/筛选变化后重绘当前视图。"""
        if not self._records:
            return
        self._show_records(self.scanned_target, self._records, self.view_mode)

    # ---- 比较时间锚点 ----
    @staticmethod
    def _parse_asof(text):
        """把用户输入的比较时间按"天"解析成 "YYYY-MM-DD"; 无法识别返回 None。

        锚点只精确到天, 故返回值形态由旧的 "YYYY-MM-DD HH:MM:SS" 变为 "YYYY-MM-DD":
        语义上"锚定到该日含当天", 不再补 23:59:59。为兼容旧设置(可能带时分秒), 一律取
        前 10 个字符再校验; 规范化后返回(如 "2026-9-3" -> "2026-09-03")。
        允许首尾空格; 严格解析, 失败返回 None。
        """
        if text is None:
            return None
        s = str(text).strip()
        if not s:
            return None
        # 旧设置可能带时分秒(如 "2026-09-23 08:00:00"), 取前 10 字符校验; 不足 10 字符的
        # 短输入(如未补零的 "2026-9-3")则整体校验, 解析成功后统一规范化为 "YYYY-MM-DD"。
        cand = s[:10] if len(s) >= 10 else s
        try:
            dt = datetime.datetime.strptime(cand, "%Y-%m-%d")
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d")

    def _view_suffix(self):
        """锚点/筛选指示文本(独立成行, 始终可见; 让用户一眼知道所处视图)。

        锚点按天, 默认档也显式写出("锚点: 最新"), 避免用户误以为在看锚定视图。
        """
        flt_var = getattr(self, "filter_var", None)   # 建界面早期此控件可能尚未创建
        flt = flt_var.get() if flt_var is not None else "全部"
        if flt not in FILTERS:
            flt = "全部"
        asof = getattr(self, "_asof", None) or "最新"
        return f"锚点: {asof}  |  筛选: {flt}"

    def _set_status(self, text=None):
        """设置状态栏: 上行基础提示(随宽度换行) + 下行锚点/筛选指示(始终可见)。"""
        if text is not None:
            self._status_base = text
        self.status_var.set(self._status_base or "")
        self.status_suffix_var.set(self._view_suffix())

    def _apply_asof(self):
        """应用比较时间锚点(按天; 仅锚定比较侧, 当前大小仍为最新)。"""
        text = self.asof_var.get().strip()
        if text == "" or text == "最新":
            self._clear_asof()
            return
        parsed = self._parse_asof(text)
        if not parsed:
            self._set_status(
                f"日期无法识别: {text} (示例 2026-09-23)")
            return
        self._asof = parsed
        self.asof_var.set(parsed)            # 回填规范化后的日期
        self._save_settings()
        self.history.clear()                 # 历史栈里是按旧锚点算的, 必须清掉
        self._reload_with_asof()

    def _clear_asof(self):
        """清除比较时间锚点, 回到"最新"视图。"""
        self._asof = None
        if self.asof_var.get() != "最新":
            self.asof_var.set("最新")
        self._save_settings()
        self.history.clear()
        self._reload_with_asof()

    def _reload_with_asof(self):
        """按当前锚点重新读库并渲染, 一律回到"该目录的一级子目录视图"。"""
        target = self.scanned_target or self.path_var.get().strip()
        if not target:
            return
        target = os.path.normpath(target)
        if not os.path.isdir(target):
            self._set_status(f"路径不存在: {target}")
            return
        was_merged = (self.view_mode == "merged")
        recs = self.store.children(target, self._asof)
        if was_merged:
            note = (f"{len(recs)} 个子目录 | 已从最小单元明细返回一级视图"
                    f"(切换比较时间锚点)")
        else:
            note = (f"{target} — {len(recs)} 个子目录 | 已按比较时间锚点重新读库"
                    f"(未重新扫描)")
        self.path_var.set(target)
        self._show_records(target, recs, mode="browse", note=note, merge=True)

    def _refresh_asof_values(self):
        """刷新比较时间锚点下拉候选(扫描批次日期变化后调用)。"""
        try:
            days = self.store.recent_scan_days()
        except sqlite3.Error:
            days = []
        self.asof_cb["values"] = ["最新"] + days

    # ---- 界面设置持久化 ----
    def _restore_target(self, drives):
        """根据保存的设置算出启动时的 (盘符, 扫描路径); 做有效性校验。"""
        default_drive = "C:\\" if "C:\\" in drives else drives[0]
        p = self._saved.get("path")
        if p and os.path.isdir(p):
            p = os.path.normpath(p)
        else:
            p = default_drive
        d = self._saved.get("drive")
        if not d or d not in drives:
            d = None
        pd = os.path.splitdrive(p)[0]           # 同步盘符到路径所在盘(若在候选里)
        if pd:
            pd_root = pd + "\\"
            if pd_root in drives:
                d = pd_root
        if d is None:
            d = default_drive
        return d, p

    def _collect_settings(self):
        """把当前界面设置收集成 dict(值统一为字符串)。"""
        return {
            "drive": self.drive_var.get(),
            "path": self.path_var.get(),
            "mb": self.mb_var.get(),
            "pct": self.pct_var.get(),
            "min_mb": self.min_mb_var.get(),
            "skip": "1" if self.skip_var.get() else "0",
            "drill": "1" if self.drill_var.get() else "0",
            "curve_click": "1" if self.curve_click_var.get() else "0",
            "auto": "1" if self.auto_var.get() else "0",
            "interval": self.interval_var.get(),
            "filter": self.filter_var.get(),
            "asof": self._asof or "",
        }

    def _save_settings(self):
        """把当前界面设置写入数据库(变更即存 / 关闭兜底)。"""
        if self._settings_job:
            try:
                self.root.after_cancel(self._settings_job)
            except Exception:
                pass
            self._settings_job = None
        try:
            self.store.set_settings(self._collect_settings())
        except sqlite3.Error:
            pass

    def _on_setting_change(self, *_args):
        """任意设置变化 → 800ms 防抖后写库(避免 Spinbox 逐字输入频繁落库)。"""
        if getattr(self, "_loading_settings", False):
            return
        if self._settings_job:
            try:
                self.root.after_cancel(self._settings_job)
            except Exception:
                pass
        self._settings_job = self.root.after(800, self._save_settings)

    def _init_settings(self):
        """构建完界面后: 挂"变更即存"监听, 并按需武装定时自动扫描。"""
        for var in (self.drive_var, self.path_var, self.mb_var, self.pct_var,
                    self.min_mb_var, self.skip_var, self.drill_var,
                    self.curve_click_var, self.auto_var, self.interval_var,
                    self.filter_var, self.asof_var):
            var.trace_add("write", self._on_setting_change)
        self._loading_settings = False
        auto_on = self.auto_var.get()
        if auto_on:
            self._toggle_auto()              # 真的把定时器武装起来
        if self._saved:
            try:
                minutes = max(1, int(self.interval_var.get()))
            except ValueError:
                minutes = 30
            self.restore_note = (
                "已恢复上次设置"
                + (f"(含定时自动扫描 {minutes} 分钟)" if auto_on else ""))
            self._set_status(self.restore_note)
            self._refresh_db_info()

    # ---- 浏览(读数据库, 不扫描) ----
    def _browse(self, path, push_history=True, note=None):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            self._set_status(f"路径不存在: {path}")
            return
        recs = self.store.children(path, self._asof)
        if not recs:
            recs = self._live_listing(path)
            if recs is None:
                return
        if push_history and self.scanned_target and self._rows:
            self.history.append(self._snapshot())
        if note is None:
            last = self.store.last_scan(path)
            denied = sum(1 for r in recs if r["denied"])
            if recs and recs[0]["size"] is not None:
                when = last[0] if last else "—"
                note = (f"{path} — {len(recs)} 个子目录 | 数据来自数据库记录"
                        f"(最近扫描 {when}){f', 其中 {denied} 个无权限' if denied else ''}"
                        f" | 未重新扫描, 点\"开始扫描\"刷新")
            else:
                note = (f"{path} — {len(recs)} 个子目录 | 无扫描记录, 大小未计算"
                        f" (点\"开始扫描\"统计)")
        self.path_var.set(path)
        self._show_records(path, recs, mode="browse", note=note, merge=True)

    def _live_listing(self, path):
        """数据库无记录时实时列目录(不算大小)。"""
        starred = self.store.starred_children(path)
        try:
            recs = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISDIR(st.st_mode):
                        continue
                    if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    recs.append({"name": e.name, "path": e.path, "size": None, "prev": None,
                                 "prev_kind": None, "prev_ts": None,
                                 "ts": None, "sig": None, "denied": False, "partial": False,
                                 "starred": e.path in starred, "cached": False, "merged": False})
        except OSError as e:
            messagebox.showerror("错误", f"无法访问: {e}")
            return None
        recs.sort(key=lambda r: r["name"].lower())
        return recs

    # ---- 扫描控制 ----
    def start_scan(self, reset_history=True):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        target = self.path_var.get().strip()
        if not target:
            messagebox.showwarning("提示", "请填写扫描路径")
            return
        if not os.path.isdir(target):
            messagebox.showerror("错误", f"路径不存在: {target}")
            return
        try:
            float(self.mb_var.get()); float(self.pct_var.get()); float(self.min_mb_var.get())
        except ValueError:
            messagebox.showerror("错误", "阈值必须为数字")
            return
        if reset_history:
            self.history.clear()

        self._started = True
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.tree.delete(*self.tree.get_children())
        self._set_status(f"正在扫描 {target} ...")
        self.progress_var.set(0.0)          # 进度条归零并显示
        self._show_progress()
        self.store.write_time = 0.0

        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(target, self.skip_var.get()),
            daemon=True)
        self.scan_thread.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, target, skip):
        scan_id = None
        t0 = time.perf_counter()
        try:
            scan_id = self.store.begin_scan(target)

            def progress(msg, n=0, total=0):
                # scan_children 会多带 (完成数, 总数), 一起投进队列给进度条用
                self.ui_queue.put(("progress", msg, n, total))

            results = scan_children(target, self.stop_event, self.store, scan_id,
                                    progress, skip_unchanged=skip)
            total = sum(r["size"] or 0 for r in results)
            self.store.end_scan(scan_id, total, len(results))
            self.ui_queue.put(("done", target, results, time.perf_counter() - t0))
        except KeyboardInterrupt:
            if scan_id:
                self.store.end_scan(scan_id, None, None, "stopped")
            self.ui_queue.put(("stopped",))
        except RuntimeError as e:
            self.ui_queue.put(("error", str(e)))
        except Exception as e:                       # 兜底, 避免线程静默死亡
            self.ui_queue.put(("error", f"扫描异常: {e}"))

    def _poll_queue(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]
                if kind == "cleanup":
                    self.cleanup_note = (f"已清理上次运行残留的临时解包文件 {msg[1]} 个"
                                         f" (释放 {format_size(msg[2])})")
                    self._refresh_db_info()
                elif kind == "progress":
                    self._set_status(msg[1])
                    if len(msg) >= 4 and msg[3]:       # 有总数才更新进度条
                        self.progress_var.set(min(100.0, msg[2] / msg[3] * 100.0))
                elif kind == "done":
                    self._on_scan_done(*msg[1:])
                elif kind == "stopped":
                    self._finish_scan("已停止 (未完成的记录已部分写入数据库)")
                elif kind == "error":
                    self._finish_scan(f"错误: {msg[1]}")
                    messagebox.showerror("错误", msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _finish_scan(self, text):
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._set_status(text)
        self._refresh_db_info()
        self._refresh_asof_values()          # 新扫完一个批次, 候选时间变了
        self.progress_var.set(100.0)        # 置满后短暂停留再收起
        self.root.after(600, self._hide_progress)

    def _refresh_db_info(self):
        """更新顶部副标题: 压成一行简洁信息(详细说明见该 Label 的 tooltip)。"""
        try:
            n, mb, path = self.store.info()
        except sqlite3.Error:
            return
        flt = self.filter_var.get()
        if flt not in FILTERS:
            flt = "全部"
        parts = [f"数据库 {path} ({mb:.1f} MB, {n} 条记录)"]
        if self._asof:
            parts.append(f"锚点 {self._asof}")
        if flt != "全部":
            parts.append(f"筛选 {flt}")
        if self.cleanup_note:
            parts.append(self.cleanup_note)
        if self.restore_note:
            parts.append(self.restore_note)
        self.db_info_var.set("  |  ".join(parts))

    def _on_scan_done(self, target, results, elapsed):
        denied = sum(1 for r in results if r["denied"])
        cached_names = {r["name"] for r in results if r["cached"]}
        # 从数据库重新读取(权威数据: prev_size 与 hist 已由写库逻辑更新)
        recs = self.store.children(target, self._asof)
        for r in recs:
            if r["name"] in cached_names:
                r["cached"] = True
        note = (f"{time.strftime('%H:%M:%S')} 扫描完成: {len(results)} 个文件夹"
                f"{f', {denied} 个无权限' if denied else ''}, 用时 {elapsed:.1f} 秒"
                f"(其中写数据库 {self.store.write_time:.1f} 秒) — {target}")
        alert_cnt = self._show_records(target, recs, mode="scan", note=note, merge=True)
        self._finish_scan(note + f" | 预警 {alert_cnt} 个")
        self._toggle_auto(reschedule=True)

    # ---- 导航 ----
    def go_up(self):
        """返回上一层: 一律优先弹历史栈(合并单元/数据库视图都能正确回退)。"""
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if self.history:
            entry = self.history.pop()
            note = f"已返回 {entry['target']} (恢复视图, 未重新扫描)"
            if entry["target"]:
                self.path_var.set(entry["target"])     # 路径框与视图保持一致
            self._show_records(entry["target"], entry["records"], entry["mode"],
                               note=note, merge=(entry["mode"] != "merged"))
            return
        cur = os.path.normpath(self.path_var.get().strip())
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            self._set_status(f"已在根目录: {cur}")
            return
        self._browse(parent, push_history=False)

    def _on_double_click(self, event):
        if not self.drill_var.get():
            return
        item = self.tree.identify_row(event.y)
        row = self._iid_rows.get(item)
        if not row:
            return
        if self.scanned_target and self._rows:
            self.history.append(self._snapshot())

        if row["merged"]:
            # 展开合并的最小单元: 取当前视图里的小文件夹成员
            min_mb = max(0.0, float(self.min_mb_var.get() or 0))
            _big, _m, members = merge_small(self._records, min_mb)
            if not members:
                self.history.pop()
                return
            self._show_records(self.scanned_target, members, mode="merged", merge=False,
                               note=f"已展开最小单元明细 ({len(members)} 个, 未重新扫描; "
                                    f"再双击成员可继续下钻)")
            return

        path = row["path"]
        if not path or not os.path.isdir(path):
            self.history.pop()
            self._set_status(f"目录不存在或不可访问: {path}")
            return
        # 下钻: 直接读数据库里该目录的记录, 不重新扫描
        kids = self.store.children(path, self._asof)
        self.path_var.set(path)
        if kids:
            self._show_records(path, kids, mode="drill",
                               note=f"已显示 {path} 的子目录明细 "
                                    f"(来自数据库记录, 未重新扫描)")
        else:
            self._show_records(path, [], mode="drill",
                               note=f"{path} 下没有子目录记录 (该目录只有文件, "
                                    f"或尚未扫描过; 数据来自数据库, 未重新扫描)")
        return

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        row = self._iid_rows.get(item)
        if item:
            self.tree.selection_set(item)
        menu = self.menu
        menu.delete(0, "end")
        if row:
            menu.add_command(label=("取消星标" if row["starred"] else "⭐ 星标标记"),
                             command=self.toggle_star)
            menu.add_command(label="📈 大小变化曲线",
                             command=lambda: self.show_curve(row))
            if row["path"] and not row["merged"]:
                menu.add_command(label="📂 打开所在文件夹", command=self.open_in_explorer)
                menu.add_command(label="🔍 扫描此文件夹", command=self.scan_selected)
                menu.add_separator()
                menu.add_command(label="🗑 删除到回收站", command=self.delete_selected)
        menu.add_separator()
        menu.add_command(label="⬅ 返回上一层", command=self.go_up)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            self._set_status("请先选中一行")
            return None
        return self._iid_rows.get(sel[0])

    # ---- 星标 ----
    def toggle_star(self):
        row = self._selected_row()
        if not row:
            return
        if row["merged"]:
            self._set_status("合并行不支持星标, 请展开后对具体文件夹星标")
            return
        path = row["path"]
        new_val = not row["starred"]
        self.store.set_star(path, new_val)
        row["starred"] = new_val
        for rec in self._records:
            if rec.get("path") == path:
                rec["starred"] = new_val
                break
        self._redraw_rows()
        self._set_status(f"{'已星标' if new_val else '已取消星标'}: {path}")

    # ---- 打开/删除 ----
    def open_in_explorer(self):
        row = self._selected_row()
        if not row or not row["path"]:
            return
        path = row["path"]
        if not os.path.isdir(path):
            self._set_status(f"目录不存在: {path}")
            return
        try:
            os.startfile(path)
            self._set_status(f"已打开: {path}")
        except OSError as e:
            self._set_status(f"打开失败: {e}")

    def scan_selected(self):
        row = self._selected_row()
        if not row or not row["path"]:
            return
        if not os.path.isdir(row["path"]):
            self._set_status(f"目录不存在: {row['path']}")
            return
        self.path_var.set(row["path"])
        self.start_scan()

    def delete_selected(self):
        row = self._selected_row()
        if not row:
            return
        if row["merged"]:
            self._set_status("合并行不能直接删除, 请展开后逐个删除")
            return
        path = row["path"]
        if not path:
            return
        if not os.path.isdir(path):
            self._set_status(f"目录不存在: {path}")
            return
        p = os.path.normpath(path)
        if len(p) <= 3 and p.endswith(":"):
            self._set_status("不允许删除磁盘根目录")
            return
        if not confirm_delete(self.root, p, format_size(row["size"])):
            self._set_status("已取消删除")
            return
        ok, msg = delete_to_recycle_bin(p)
        if not ok:
            messagebox.showerror("删除失败", f"{msg}\n\n{path}")
            self._set_status(f"删除失败: {msg}")
            return
        self.store.remove_subtree(p)
        self._records = [r for r in self._records if norm(r["path"] or "") != norm(p)]
        self._rows = [r for r in self._rows if norm(r["path"] or "") != norm(p)]
        self._redraw_rows()
        self._set_status(
            f"{msg}: {p} | 上级文件夹大小已过期, 点\"开始扫描\"可重新统计")

    # ---- 定时扫描 ----
    def _toggle_auto(self, reschedule=False):
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None
        if self.auto_var.get():
            try:
                minutes = max(1, int(self.interval_var.get()))
            except ValueError:
                minutes = 30
            self.auto_job = self.root.after(minutes * 60 * 1000, self._auto_fire)
            if reschedule:
                self._set_status(f"下次自动扫描: {minutes} 分钟后")
        elif not reschedule:
            self._set_status("自动扫描已关闭")

    def _auto_fire(self):
        self.auto_job = None
        if not (self.scan_thread and self.scan_thread.is_alive()):
            self.start_scan()

    def _on_close(self):
        self.stop_event.set()
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
            self._settings_job = None
        try:
            self._save_settings()            # 兜底: 关闭前保存一次全部设置
        except Exception:
            pass
        self.store.close()
        try:                       # 退出时再扫一遍: 启动时被"delete pending"挡住的这次能删掉
            cleanup_temp_extract_dirs(retry_seconds=1.0)
        except Exception:
            pass
        self.root.destroy()


# ---------------- 自测 ----------------
def _selftest_body():
    import tempfile

    work = tempfile.mkdtemp(prefix="diskguard_test_")
    base = os.path.join(work, "root"); os.makedirs(base)
    a = os.path.join(base, "alpha"); os.makedirs(a)
    os.makedirs(os.path.join(a, "nested"))        # 空子目录: 验证子层级同样入库
    b = os.path.join(base, "beta"); os.makedirs(b)
    with open(os.path.join(a, "big.bin"), "wb") as f:
        f.write(b"\0" * (3 * 1024 * 1024))
    with open(os.path.join(b, "small.txt"), "wb") as f:
        f.write(b"hello")

    store = Store(os.path.join(work, "test.db"))
    stop = threading.Event()

    # --- 首次扫描 + 写库 ---
    sid = store.begin_scan(base)
    res = scan_children(base, stop, store, sid)
    store.end_scan(sid, sum(r["size"] for r in res), len(res))
    names = {r["name"]: r["size"] for r in res}
    assert names["alpha"] == 3 * 1024 * 1024, names
    assert names["beta"] == 5, names
    assert [r["name"] for r in res][0] == "alpha"          # 大小降序
    assert all(r["prev"] is None for r in res)             # 首扫无上次值
    assert all(not r["denied"] and not r["partial"] for r in res)

    # --- 数据库读回一致 ---
    kids = {r["name"]: r for r in store.children(base)}
    assert kids["alpha"]["size"] == 3 * 1024 * 1024
    assert kids["alpha"]["path"] == a                       # 保留原始大小写
    assert store.last_scan(base) is not None
    inner = store.children(a)                               # 子层级也已入库
    assert [r["name"] for r in inner] == ["nested"], inner
    assert inner[0]["size"] == 0 and inner[0]["path"] == os.path.join(a, "nested")

    # --- 第二次扫描: prev_size 来自上次 ---
    sid = store.begin_scan(base)
    res2 = scan_children(base, stop, store, sid)
    store.end_scan(sid, 0, len(res2))
    by = {r["name"]: r for r in res2}
    assert by["alpha"]["prev"] == 3 * 1024 * 1024, by["alpha"]
    assert by["alpha"]["size"] - by["alpha"]["prev"] == 0

    # --- 大小变化后重扫 ---
    with open(os.path.join(a, "new.bin"), "wb") as f:
        f.write(b"x" * 2048)
    sid = store.begin_scan(base)
    res3 = scan_children(base, stop, store, sid)
    by = {r["name"]: r for r in res3}
    assert by["alpha"]["size"] == 3 * 1024 * 1024 + 2048
    assert by["alpha"]["prev"] == 3 * 1024 * 1024

    # --- 跳过无变化(浅层签名命中) ---
    prev = {r["name"]: r for r in store.children(base)}
    sid = store.begin_scan(base)
    res4 = scan_children(base, stop, store, sid, skip_unchanged=True)
    assert all(r["cached"] for r in res4), [(r["name"], r["cached"]) for r in res4]
    assert store.children(base)[0]["size"] is not None      # 缓存命中仍保留记录

    # --- 历史大小序列: 每次扫描追加一个点 ---
    cur = {r["name"]: r for r in store.children(base)}
    s1 = 3 * 1024 * 1024
    assert cur["alpha"]["hist"] == [s1, s1, s1 + 2048, s1 + 2048], cur["alpha"]["hist"]
    assert cur["beta"]["hist"] == [5, 5, 5, 5], cur["beta"]["hist"]
    inner = {r["name"]: r for r in store.children(a)}
    assert inner["nested"]["hist"] == [0, 0, 0, 0], inner["nested"]["hist"]  # 缓存时子树也追加
    assert len(cur["alpha"]["hist"]) == len(cur["beta"]["hist"])            # "第n次"横向可比
    # hist 已改为 [时间, 大小] 对存储
    raw_hist = store._conn.execute(
        "SELECT hist FROM folders WHERE path=?", (norm(a),)).fetchone()[0]
    assert raw_hist.startswith('[["'), raw_hist[:80]

    # --- 增长比较基准: 同一天多次扫描时, 基准 = 当天第一条 ---
    assert cur["alpha"]["prev"] == s1 and cur["alpha"]["prev_kind"] == "当天第一条"
    assert cur["beta"]["prev"] == 5 and cur["beta"]["prev_kind"] == "当天第一条"
    # 时间列: 完整 "YYYY-MM-DD HH:MM:SS"; 比较时间 = 基准那一次记录的时间
    assert len(cur["alpha"]["ts"]) == 19 and cur["alpha"]["ts"][4] == "-", cur["alpha"]["ts"]
    assert len(cur["alpha"]["prev_ts"]) == 19 and cur["alpha"]["prev_ts"][4] == "-", \
        cur["alpha"]["prev_ts"]
    assert cur["alpha"]["prev_ts"] == cur["alpha"]["hist_ts"][0], \
        (cur["alpha"]["prev_ts"], cur["alpha"]["hist_ts"][0])   # 基准 = 当天第一条

    # --- 历史长度受 HIST_KEEP 限制 ---
    cap = os.path.join(work, "cap"); os.makedirs(cap)
    kid = os.path.join(cap, "kid"); os.makedirs(kid)
    sizes = []
    for i in range(HIST_KEEP + 5):
        with open(os.path.join(kid, "f.bin"), "wb") as f:
            f.write(b"\0" * (1000 * (i + 1)))
        sid = store.begin_scan(cap)
        scan_children(cap, stop, store, sid)
        sizes.append(1000 * (i + 1))
    kid_rec = {r["name"]: r for r in store.children(cap)}["kid"]
    assert len(kid_rec["hist"]) == HIST_KEEP, len(kid_rec["hist"])
    assert kid_rec["hist"] == sizes[-HIST_KEEP:], (kid_rec["hist"], sizes[-3:])

    # --- 跨天扫描: 当天第一条 / 上一条 基准规则 ---
    # 次日时刻基于"基准日"(受控时钟)相对计算, 不写死年月日 → 与真实今天无关
    mock_day = _SELFTEST_BASE_DAY + datetime.timedelta(days=1)
    mock_ts1 = f"{mock_day.isoformat()} 08:00:00"
    mock_ts2 = f"{mock_day.isoformat()} 09:00:00"
    real_now = globals()["now_str"]
    try:
        # 第二天 08:00 首次扫描 → 当天此前无记录 → 基准 = 上一条(昨天最后一条)
        globals()["now_str"] = lambda: mock_ts1
        sid = store.begin_scan(cap)
        scan_children(cap, stop, store, sid)
        kid2 = {r["name"]: r for r in store.children(cap)}["kid"]
        assert kid2["prev"] == sizes[-1], (kid2["prev"], sizes[-1])
        assert kid2["prev_kind"] == "上一条", kid2["prev_kind"]
        # 基准是"上一条"(前一天那批记录), 比较时间为该条记录的时间
        assert kid2["prev_ts"] and kid2["prev_ts"] < mock_ts1, kid2["prev_ts"]
        # 第二天 09:00 第二次扫描 → 基准 = 当天第一条(08:00 那次)
        globals()["now_str"] = lambda: mock_ts2
        sid = store.begin_scan(cap)
        scan_children(cap, stop, store, sid)
        kid3 = {r["name"]: r for r in store.children(cap)}["kid"]
        assert kid3["prev"] == sizes[-1] and kid3["prev_kind"] == "当天第一条", \
            (kid3["prev"], kid3["prev_kind"])
        assert kid3["prev_ts"] == mock_ts1, kid3["prev_ts"]
    finally:
        globals()["now_str"] = real_now

    # --- 趋势辅助函数 ---
    assert parse_hist("[1,2,3]") == [1, 2, 3]
    assert parse_hist(None) == [] and parse_hist("oops") == []
    # 新格式 [时间, 大小] 对 + 旧格式纯数字混存
    assert hist_pairs('[1,["2026-09-21 10:00:00",5],["2026-09-21 11:00:00",7]]') == \
        [(None, 1), ("2026-09-21 10:00:00", 5), ("2026-09-21 11:00:00", 7)]
    assert hist_pairs("[]") == [] and hist_pairs(None) == [] and hist_pairs("{}") == []
    # 基准规则: 当天第一条 / 上一条 / 无历史 (返回值: 大小, 类型, 基准记录时间)
    assert baseline_of([]) == (None, None, None)
    assert baseline_of([(None, 10)]) == (None, None, None)
    assert baseline_of([(None, 10), (None, 20)]) == (10, "上一条", None)
    assert baseline_of([("2026-09-20 18:00:00", 10), ("2026-09-21 09:00:00", 30)]) \
        == (10, "上一条", "2026-09-20 18:00:00")                      # 跨天 → 上一条
    assert baseline_of([("2026-09-21 09:00:00", 30), ("2026-09-21 11:00:00", 40)]) \
        == (30, "当天第一条", "2026-09-21 09:00:00")                   # 同天 → 当天第一条
    assert baseline_of([(None, 10), ("2026-09-21 09:00:00", 30), ("2026-09-21 11:00:00", 40)]) \
        == (30, "当天第一条", "2026-09-21 09:00:00")                   # 旧格式点不参与"当天"
    assert sparkline([]) == ""
    assert sparkline(list(range(8))) == SPARK_CHARS                 # 单调升 → 逐级加高
    assert sparkline([7, 7, 7]) == SPARK_CHARS[4] * 3               # 全等 → 中间高度
    assert sparkline(list(range(20)), 8) == SPARK_CHARS             # 只取最近 8 次
    assert len(sparkline([5])) == 1
    assert sum_series([[1, 2, 3], [10, 20]]) == [1, 12, 23]         # 从最新往回对齐求和
    assert sum_series([]) == [] and sum_series([[], None]) == []
    assert trend_of([5, 9]) == (1, 4) and trend_of([9, 5]) == (-1, -4)
    assert trend_of([5]) == (0, 0) and trend_of([]) == (0, 0)
    lo, hi = nice_range([100, 100])
    assert lo < 100 < hi                                            # 全等时上下留白
    lo, hi = nice_range([1, 3])
    assert lo < 1 and hi > 3

    # --- 浅层签名/哈希 ---
    h1 = sig_hash(quick_sig(b))
    assert h1 == sig_hash(quick_sig(b))
    with open(os.path.join(b, "add.txt"), "wb") as f:
        f.write(b"y")
    assert sig_hash(quick_sig(b)) != h1

    # --- 无权限捕获 ---
    deny = os.path.join(base, "denieddir"); os.makedirs(deny)
    sub_denied = os.path.join(a, "sub_denied"); os.makedirs(sub_denied)
    orig_scandir = os.scandir
    denied_prefix = (os.path.normcase(deny), os.path.normcase(sub_denied))

    def fake_scandir(p, *args, **kwargs):
        if os.path.normcase(str(p)).startswith(denied_prefix):
            raise PermissionError(13, "Access is denied")
        return orig_scandir(p, *args, **kwargs)

    os.scandir = fake_scandir
    try:
        sid = store.begin_scan(base)
        res5 = scan_children(base, stop, store, sid)
        rec5 = {r["name"]: r for r in res5}
        assert rec5["denieddir"]["denied"] and rec5["denieddir"]["size"] == 0, rec5["denieddir"]
        # 子孙被拒 → 上级标 partial(大小不完整)
        assert rec5["alpha"]["partial"] and not rec5["alpha"]["denied"], rec5["alpha"]
        kid_denied = {r["name"]: r for r in store.children(a)}
        assert kid_denied["sub_denied"]["denied"], kid_denied
    finally:
        os.scandir = orig_scandir

    # --- 合并最小单元 ---
    fake = [{"name": "big1", "path": "/x/big1", "size": 10 * 1024 * 1024, "prev": 9 * 1024 * 1024,
             "denied": False, "partial": False, "starred": False, "merged": False, "ts": "2026-09-21 10:00:00"},
            {"name": "tiny1", "path": "/x/tiny1", "size": 1024, "prev": 512, "denied": False,
             "partial": False, "starred": False, "merged": False, "ts": "2026-09-21 10:00:01"},
            {"name": "tiny2", "path": "/x/tiny2", "size": 2048, "prev": None, "denied": False,
             "partial": False, "starred": False, "merged": False, "ts": "2026-09-21 10:00:02"},
            {"name": "deny1", "path": "/x/deny1", "size": 0, "prev": None, "denied": True,
             "partial": False, "starred": False, "merged": False, "ts": "2026-09-21 10:00:03"}]
    big, merged, members = merge_small(fake, 1)
    assert [r["name"] for r in big] == ["big1", "deny1"]        # 无权限项不参与合并
    assert merged is not None and merged["size"] == 3072 and "2个" in merged["name"]
    assert merged["prev"] == 512 and merged["path"] is None
    assert [m["name"] for m in members] == ["tiny1", "tiny2"]
    b2, m2, mem2 = merge_small(fake, 0)
    assert len(b2) == 4 and m2 is None and mem2 == []

    # --- 星标 ---
    assert not store.is_starred(a)
    store.set_star(a, True)
    assert store.is_starred(a)
    assert a in store.starred_children(base)
    sid = store.begin_scan(base)
    res7 = scan_children(base, stop, store, sid)
    assert {r["name"]: r for r in res7}["alpha"]["starred"]     # 重扫保留星标
    store.set_star(a, False)
    assert not store.is_starred(a)

    # --- 删除记录清理(只删数据库记录, 不动磁盘) ---
    store.remove_subtree(a)
    assert os.path.exists(a)
    assert a not in [r["path"] for r in store.children(base)]
    assert not store.children(a)

    # --- 回收站删除(仅对临时目录) ---
    victim = os.path.join(work, "victim"); os.makedirs(victim)
    with open(os.path.join(victim, "f.txt"), "wb") as f:
        f.write(b"x")
    ok, msg = delete_to_recycle_bin(victim)
    assert ok, msg
    assert not os.path.exists(victim)
    ok2, _ = delete_to_recycle_bin("C:\\")
    assert not ok2                                              # 盘根拒绝

    # --- 杂项 ---
    assert format_size(3 * 1024 * 1024) == "3.0 MB"
    assert format_size(None) == "—"
    assert DiskGuardApp._is_alert(1024 ** 2, 1024 ** 2 + 600 * 1024, 500, 10)
    assert DiskGuardApp._is_alert(1024 ** 2, 1024 ** 2 + 200 * 1024, 500, 10)
    assert not DiskGuardApp._is_alert(1024 ** 2, 1024 ** 2 + 10 * 1024, 500, 10)
    assert not DiskGuardApp._is_alert(0, 100, 500, 10)
    assert len(list_fixed_drives()) >= 1

    # --- 临时解包残留清理(只动自己、不动别人、不动正在用的) ---
    fake_home = tempfile.mkdtemp(prefix="dg_tmpclean_")
    try:
        sig = {f"lib{i}.dll" for i in range(25)}       # 假装是本程序的解包内容
        cur = os.path.join(fake_home, "_MEIcur")
        os.makedirs(cur)
        for f in sig:
            open(os.path.join(cur, f), "wb").write(b"x" * 64)
        for tag in ("old1", "old2"):                   # 两个历史残留
            d = os.path.join(fake_home, "_MEI" + tag)
            os.makedirs(d)
            for f in sig:
                open(os.path.join(d, f), "wb").write(b"x" * 64)
        other = os.path.join(fake_home, "_MEIother")   # 别的软件的目录
        os.makedirs(other)
        for f in ("x.dll", "y.dll"):
            open(os.path.join(other, f), "wb").write(b"x" * 64)
        half = os.path.join(fake_home, "_MEIdead.dg-clean")   # 上次中断的半成品
        os.makedirs(half)
        open(os.path.join(half, "z.dll"), "wb").write(b"x" * 64)
        marked = os.path.join(fake_home, "_MEImarked")        # 带本程序标记(新版留下的)
        os.makedirs(marked)
        open(os.path.join(marked, MEI_MARKER), "w").close()
        open(os.path.join(marked, "any.dll"), "wb").write(b"x" * 64)
        pydll = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
        legacy = os.path.join(fake_home, "_MEIlegacy")        # 旧版本构建残留(靠内容特征识别)
        os.makedirs(legacy)
        for f in (pydll, "_tkinter.pyd"):
            open(os.path.join(legacy, f), "wb").write(b"x" * 64)
        os.makedirs(os.path.join(legacy, "_tcl_data"))
        os.makedirs(os.path.join(legacy, "_tk_data"))

        # 空目录里没有可清的东西 → 不管冻结与否都应是 (0, 0)
        empty = os.path.join(fake_home, "empty")
        os.makedirs(empty)
        assert cleanup_temp_extract_dirs(tmp_dir=empty) == (0, 0)
        if not getattr(sys, "frozen", False):          # 脚本模式: 不参与清理
            assert _payload_signature() is None
            assert cleanup_temp_extract_dirs() == (0, 0)
        saved = (getattr(sys, "frozen", False), getattr(sys, "_MEIPASS", None))
        sys.frozen, sys._MEIPASS = True, cur
        try:
            got, freed = cleanup_temp_extract_dirs(tmp_dir=fake_home)
        finally:
            if saved[1] is None:
                del sys._MEIPASS
            else:
                sys._MEIPASS = saved[1]
            sys.frozen = saved[0]
        assert got == 5 and freed > 0, (got, freed)    # 同签名×2 + 半成品 + 标记 + 旧版特征
        assert os.path.isdir(cur)                       # 当前实例的解包目录不动
        assert os.path.isdir(other)                     # 别的软件不动
        assert not os.path.exists(os.path.join(fake_home, "_MEIold1"))
        assert not os.path.exists(half)
        assert not os.path.exists(marked) and not os.path.exists(legacy)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)

    # --- 比较时间锚点: baseline_asof 与 baseline_of 的等价/锚定语义 ---
    pairs_ts = [("2026-09-20 08:00:00", 10), ("2026-09-21 09:00:00", 30),
                ("2026-09-21 11:00:00", 40), ("2026-09-22 10:00:00", 55)]
    # cutoff=None → 与 baseline_of 逐字等价
    assert baseline_asof(pairs_ts, None) == baseline_of(pairs_ts)
    assert baseline_asof([], None) == baseline_of([]) == (None, None, None)
    # cutoff 晚于全部历史点 → 与不传 cutoff 相同
    assert baseline_asof(pairs_ts, "2026-12-31 23:59:59") == baseline_of(pairs_ts)
    # cutoff 落在中间(按天锚定 09-21): 该日及更早的点都纳入 → 当天第一条 = 09:00
    assert baseline_asof(pairs_ts, "2026-09-21 11:00:00") == (30, "当天第一条",
                                                            "2026-09-21 09:00:00")
    # 本次刻意行为变更: 锚点按"天"算, 锚定 09-21 时该日**更晚时刻**(11:00)的点也被纳入 ——
    # 即便锚点时刻(09:30)早于它, 结果仍等于锚定该日任意时刻, 不再精确到时分秒
    assert baseline_asof(pairs_ts, "2026-09-21 09:30:00") == (30, "当天第一条",
                                                            "2026-09-21 09:00:00")
    assert baseline_asof(pairs_ts, "2026-09-21 09:30:00") == \
        baseline_asof(pairs_ts, "2026-09-21 00:00:00") == \
        baseline_asof(pairs_ts, "2026-09-21 23:59:59")
    # 锚定日之后一天(09-22)的点必须被排除
    assert baseline_asof(pairs_ts, "2026-09-21") == \
        baseline_asof(pairs_ts, "2026-09-21 23:59:59") == \
        (30, "当天第一条", "2026-09-21 09:00:00")
    # cutoff 早于第一个历史点 → 无基准
    assert baseline_asof(pairs_ts, "2026-01-01 00:00:00") == (None, None, None)
    # 含无时间戳的旧格式点: 启用 cutoff 时被排除
    pairs_mix = [(None, 5), ("2026-09-21 09:00:00", 30), ("2026-09-21 11:00:00", 40)]
    assert baseline_of(pairs_mix) == (30, "当天第一条", "2026-09-21 09:00:00")
    # 按天: 锚定 09-21 会把两个带时间戳的点都纳入 → 当天第一条 = 09:00(旧点仍被排除)
    assert baseline_asof(pairs_mix, "2026-09-21 09:00:00") == (30, "当天第一条",
                                                             "2026-09-21 09:00:00")
    assert baseline_asof(pairs_mix, "2026-09-21 11:00:00") == (30, "当天第一条",
                                                             "2026-09-21 09:00:00")
    # 明确"旧格式点被排除": 只看带时间戳的点只剩 1 个 → 无基准(未被旧点顶成基准)
    assert baseline_asof([(None, 99), ("2026-09-21 09:00:00", 30)],
                         "2026-09-21 23:00:00") == (None, None, None)
    # 锚定日"含当天": 仅一天的记录在锚定到该日时 len<2 → 无基准
    assert baseline_asof([("2026-09-21 09:00:00", 30)], "2026-09-21") == (None, None, None)
    # 旧式带时分秒的锚点输入与纯日期锚点等价(内部按 [:10] 归一化)
    assert baseline_asof(pairs_ts, "2026-09-22 10:00:00") == \
        baseline_asof(pairs_ts, "2026-09-22")

    # --- Store.children: 不传 cutoff 与传 cutoff=None/空串 结果一致(回归保护) ---
    assert store.children(base) == store.children(base, None)
    assert store.children(base) == store.children(base, "")

    # --- Store 层锚点语义(受控时钟造跨天历史) ---
    an = os.path.join(work, "anchor"); os.makedirs(an)
    ank = os.path.join(an, "kid"); os.makedirs(ank)
    with open(os.path.join(ank, "f.bin"), "wb") as f:
        f.write(b"\0" * 1000)
    base_day_iso = _SELFTEST_BASE_DAY.isoformat()
    next_day_iso = (_SELFTEST_BASE_DAY + datetime.timedelta(days=1)).isoformat()
    real_now2 = globals()["now_str"]
    try:
        globals()["now_str"] = lambda: f"{base_day_iso} 10:00:00"
        sid = store.begin_scan(an); scan_children(an, stop, store, sid)
        globals()["now_str"] = lambda: f"{base_day_iso} 12:00:00"
        sid = store.begin_scan(an); scan_children(an, stop, store, sid)
        globals()["now_str"] = lambda: f"{next_day_iso} 09:00:00"
        sid = store.begin_scan(an); scan_children(an, stop, store, sid)
    finally:
        globals()["now_str"] = real_now2
    latest = {r["name"]: r for r in store.children(an)}["kid"]
    assert latest["size"] == 1000, latest
    assert latest["prev_kind"] == "上一条", latest           # 次日此前无记录 → 上一条
    # 锚定到基准日(按天) → 只看基准日内的 10:00/12:00 两点 → 当天第一条 = 10:00
    anchored = {r["name"]: r
                for r in store.children(an, f"{base_day_iso} 12:00:00")}["kid"]
    assert anchored["prev"] == 1000, anchored
    assert anchored["prev_kind"] == "当天第一条", anchored
    assert anchored["prev_ts"] == f"{base_day_iso} 10:00:00", anchored
    assert anchored["size"] == 1000                          # 当前大小仍是最新值
    # 纯日期锚点与带时分秒锚点(同一天)等价
    anchored_d = {r["name"]: r for r in store.children(an, base_day_iso)}["kid"]
    assert anchored_d["prev_ts"] == f"{base_day_iso} 10:00:00", anchored_d
    # 锚点早于全部历史 → 无基准(且不回落到 prev_size 列; 该列此时非空)
    early = {r["name"]: r
             for r in store.children(an, "2000-01-01 00:00:00")}["kid"]
    assert early["prev"] is None and early["prev_kind"] is None, early
    assert early["size"] == 1000

    # --- 设置持久化(meta 表 ui.* 前缀) ---
    sdb = os.path.join(work, "ui.db")
    s1 = Store(sdb)
    s1.set_settings({"mb": "123", "filter": "仅预警"})
    assert s1.get_settings().get("mb") == "123"
    assert s1.get_settings().get("filter") == "仅预警"
    with s1._lock:                                           # legacy_imported 不受影响
        s1._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_imported','X')")
        s1._conn.commit()
    s1.set_settings({"pct": "7"})
    with s1._lock:
        keep = s1._conn.execute(
            "SELECT value FROM meta WHERE key='legacy_imported'").fetchone()
    assert keep and keep[0] == "X", keep
    s1.close()
    s2 = Store(sdb)                                          # 新实例读回
    got = s2.get_settings()
    assert got.get("mb") == "123" and got.get("filter") == "仅预警" and got.get("pct") == "7", got
    assert "legacy_imported" not in got, got                 # 非 ui. 前缀不混进来
    s2.delete_setting("mb")
    assert "mb" not in s2.get_settings(), s2.get_settings()
    # 新增的两个筛选档位同样能持久化/读回(取值仍是 5 个中文串)
    s2.set_settings({"filter": "仅增长"})
    assert s2.get_settings().get("filter") == "仅增长"
    s2.set_settings({"filter": "仅减少"})
    assert s2.get_settings().get("filter") == "仅减少"
    s2.close()

    # --- recent_scan_days: 去重后的日期列表(倒序) + limit ---
    rdb = os.path.join(work, "recent.db")
    rs = Store(rdb)
    real_now4 = globals()["now_str"]
    try:
        for stamp in ("2026-09-20 08:00:00", "2026-09-20 18:00:00",
                      "2026-09-21 09:00:00"):
            globals()["now_str"] = (lambda s=stamp: s)
            sid = rs.begin_scan(base)
            rs.end_scan(sid, 0, 0)
    finally:
        globals()["now_str"] = real_now4
    # 同一天的多次扫描折叠为一条日期, 且按日期倒序
    assert rs.recent_scan_days() == ["2026-09-21", "2026-09-20"], rs.recent_scan_days()
    assert rs.recent_scan_days(limit=1) == ["2026-09-21"], rs.recent_scan_days(1)
    rs.close()

    # --- curve_click 默认改为关 + 一次性迁移的幂等性 ---
    assert setting_bool({}, "curve_click", False) is False       # 新默认值为关
    assert setting_bool({"curve_click": "0"}, "curve_click", False) is False
    assert setting_bool({"curve_click": "1"}, "curve_click", False) is True
    mdb = os.path.join(work, "mig.db")
    m1 = Store(mdb)
    m1.set_settings({"mb": "7"})                                 # 预置无关键, 迁移不得动它
    assert m1.migrate_curve_click_default_v11() is True          # 首次执行
    got_m = m1.get_settings()
    assert got_m.get("curve_click") == "0", got_m                # 老库的 "1" 被改成 "0"
    assert got_m.get("mb") == "7", got_m                         # 其它键不受影响
    m1.set_settings({"curve_click": "1"})                        # 用户之后手动改回开
    assert m1.migrate_curve_click_default_v11() is False         # 二次调用不再执行
    assert m1.get_settings().get("curve_click") == "1"           # 绝不覆盖用户选择
    m1.close()
    m2 = Store(mdb)                                              # 跨实例幂等(标记已存在)
    assert m2.migrate_curve_click_default_v11() is False
    assert m2.get_settings().get("curve_click") == "1"
    m2.close()
    # 全新空库迁移也不报错
    m3 = Store(os.path.join(work, "mig_empty.db"))
    assert m3.migrate_curve_click_default_v11() is True
    assert m3.get_settings().get("curve_click") == "0"
    m3.close()

    # --- 筛选逻辑: 全部 / 仅预警 / 仅星标 / 仅增长 / 仅减少 (不启动 Tk, 手填实例调用纯逻辑) ---
    app = DiskGuardApp.__new__(DiskGuardApp)
    alert_rec = {"name": "a", "path": "/a", "size": 3 * 1024 * 1024,
                 "prev": 1024 * 1024, "prev_kind": "上一条",
                 "prev_ts": "2026-09-20 08:00:00", "ts": "2026-09-21 09:00:00",
                 "denied": False, "partial": False, "starred": False, "cached": False,
                 "merged": False, "hist": [1024 * 1024, 3 * 1024 * 1024],
                 "hist_ts": ["2026-09-20 08:00:00", "2026-09-21 09:00:00"]}
    star_rec = {"name": "b", "path": "/b", "size": 1000, "prev": 1000,
                "prev_kind": "上一条", "prev_ts": "2026-09-20 08:00:00",
                "ts": "2026-09-21 09:00:00", "denied": False, "partial": False,
                "starred": True, "cached": False, "merged": False, "hist": [1000, 1000],
                "hist_ts": ["2026-09-20 08:00:00", "2026-09-21 09:00:00"]}
    plain_rec = {"name": "c", "path": "/c", "size": 1000, "prev": 1000,
                 "prev_kind": "上一条", "prev_ts": "2026-09-20 08:00:00",
                 "ts": "2026-09-21 09:00:00", "denied": False, "partial": False,
                 "starred": False, "cached": False, "merged": False, "hist": [1000, 1000],
                 "hist_ts": ["2026-09-20 08:00:00", "2026-09-21 09:00:00"]}
    recs3 = [alert_rec, star_rec, plain_rec]
    alert_cnt = app._build_rows(recs3, None, [], 1.0, 200.0, "全部")   # 返回预警数
    rows_all = list(app._rows)                               # 明细在 self._rows
    assert len(rows_all) == 3, rows_all
    assert alert_cnt == 1, [(r["name"], r["alert"]) for r in rows_all]  # 只有 a 预警
    app._build_rows(recs3, None, [], 1.0, 200.0, "仅预警")
    rows_al = list(app._rows)
    assert len(rows_al) == alert_cnt == 1 and rows_al[0]["name"] == "a", rows_al
    app._build_rows(recs3, None, [], 1.0, 200.0, "仅星标")
    rows_st = list(app._rows)
    assert [r["name"] for r in rows_st] == ["b"], rows_st
    # 无权限行即使尺寸达标也不预警 → "仅预警"下被过滤掉
    deny_rec = dict(plain_rec, name="d", path="/d", denied=True,
                    size=10 * 1024 * 1024)
    app._build_rows([deny_rec], None, [], 1.0, 200.0, "仅预警")
    assert app._rows == []

    # --- 新增筛选: 仅增长 / 仅减少(delta 符号判定; delta 为 None 与 cached 行都排除) ---
    down_rec = {"name": "e", "path": "/e", "size": 500, "prev": 1000,
                "prev_kind": "上一条", "prev_ts": "2026-09-20 08:00:00",
                "ts": "2026-09-21 09:00:00", "denied": False, "partial": False,
                "starred": False, "cached": False, "merged": False,
                "hist": [1000, 500],
                "hist_ts": ["2026-09-20 08:00:00", "2026-09-21 09:00:00"]}
    newbase_rec = {"name": "f", "path": "/f", "size": 1000, "prev": None,
                   "prev_kind": None, "prev_ts": None, "ts": "2026-09-21 09:00:00",
                   "denied": False, "partial": False, "starred": False,
                   "cached": False, "merged": False, "hist": [1000], "hist_ts": []}
    cached_rec = dict(plain_rec, name="g", path="/g", cached=True)
    mix = [alert_rec, plain_rec, down_rec, newbase_rec, cached_rec]
    app._build_rows(mix, None, [], 1.0, 200.0, "仅增长")
    assert [r["name"] for r in app._rows] == ["a"], app._rows   # 变大=仅 a; 持平/新建基线/缓存都排除
    app._build_rows(mix, None, [], 1.0, 200.0, "仅减少")
    assert [r["name"] for r in app._rows] == ["e"], app._rows
    # 合并规则不变量: 只有"全部"档才合并(否则被筛出的成员行会被"📦 合并的小文件夹"吞掉)
    assert merge_allowed(True, "全部") is True
    assert merge_allowed(True, "仅预警") is False
    assert merge_allowed(True, "仅星标") is False
    assert merge_allowed(True, "仅增长") is False
    assert merge_allowed(True, "仅减少") is False
    assert merge_allowed(False, "全部") is False
    # 具体场景: "小文件夹"在"仅增长"档下仍逐行可见, 不被合并吞掉
    small_grow = dict(plain_rec, name="s", path="/s", size=2048, prev=1024)
    big_flat = {"name": "big", "path": "/big", "size": 10 * 1024 * 1024,
                "prev": 10 * 1024 * 1024, "prev_kind": "上一条",
                "prev_ts": "2026-09-20 08:00:00", "ts": "2026-09-21 09:00:00",
                "denied": False, "partial": False, "starred": False,
                "cached": False, "merged": False,
                "hist": [10 * 1024 * 1024, 10 * 1024 * 1024],
                "hist_ts": ["2026-09-20 08:00:00", "2026-09-21 09:00:00"]}
    flt = "仅增长"
    assert merge_allowed(True, flt) is False
    # 模拟 _show_records 的合并闸门: 非"全部"档 → 平铺不合并
    if merge_allowed(True, flt):
        display, merged, members = merge_small([small_grow, big_flat], 1.0)
    else:
        display, merged, members = [small_grow, big_flat], None, []
    assert merged is None, merged
    app._build_rows(display, merged, members, 1.0, 200.0, flt)
    assert [r["name"] for r in app._rows] == ["s"], app._rows    # 小文件夹的增长行可见

    # --- 比较时间文本解析(改为按天) ---
    assert DiskGuardApp._parse_asof("2026-09-23") == "2026-09-23"
    assert DiskGuardApp._parse_asof(" 2026-09-23 ") == "2026-09-23"
    # 兼容旧式带时分秒输入(取日期部分)
    assert DiskGuardApp._parse_asof(" 2026-09-23 08:00:00 ") == "2026-09-23"
    assert DiskGuardApp._parse_asof("2026-09-23 08:00") == "2026-09-23"
    # 规范化: 非零填充的日期也能解析成标准形态
    assert DiskGuardApp._parse_asof("2026-9-3") == "2026-09-03"
    assert DiskGuardApp._parse_asof("乱写") is None
    assert DiskGuardApp._parse_asof("") is None and DiskGuardApp._parse_asof(None) is None
    assert DiskGuardApp._parse_asof("2026-09-2x") is None       # 前 10 字符非合法日期
    assert DiskGuardApp._parse_asof("2026-13-40") is None
    assert DiskGuardApp._parse_asof("2026-09") is None          # 不足 10 字符

    # --- 图标路径解析(仅接线, 不要求文件存在) ---
    assert icon_path() == os.path.join(APP_DIR, "assets", "DiskGuard.ico"), icon_path()

    n, mb, _p = store.info()
    assert n >= 2, n
    store.close()
    print(f"SELFTEST OK ({n} rows, db {mb:.2f} MB)")
    return 0


def selftest():
    """运行内置自测 (python disk_guard.py --selftest)。

    全程把 now_str 打桩为"受控合成时钟": 合成基准日 = 真实今天(算成 _SELFTEST_BASE_DAY),
    当天时刻自 08:00:00 起按调用次数递增。这样自测体(含"当天第一条/上一条"基准规则)
    与真实日期完全解耦 —— 既不会在"真实日期恰好等于模拟次日"(日期撞车)当天误报,
    也不受自测恰好跨零点运行影响。结束(含异常)时在 finally 中恢复真实时钟。
    """
    real_now = globals()["now_str"]
    base_day = datetime.date.today()
    tick = [0]

    def _mock_clock():
        tick[0] += 1
        dt = (datetime.datetime.combine(base_day, datetime.time(8, 0, 0))
              + datetime.timedelta(seconds=tick[0]))
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    globals()["now_str"] = _mock_clock
    globals()["_SELFTEST_BASE_DAY"] = base_day
    try:
        return _selftest_body()
    finally:
        globals()["now_str"] = real_now


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    root = tk.Tk()
    # clam 主题才允许自定义按钮/表头颜色(vista 会忽略), 取不到再回退 vista
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
    app = DiskGuardApp(root)
    # 窗口/任务栏图标(开发运行): 图标缺失或冻结状态取不到时静默降级, 绝不影响启动
    try:
        ip = icon_path()
        if os.path.exists(ip):
            root.iconbitmap(default=ip)
    except (tk.TclError, OSError, AttributeError):
        pass
    _mark_own_payload()                     # 给本实例解包目录打标记, 供下次启动识别
    start_temp_cleanup(app.ui_queue)        # 后台清理历史残留, 完成后在界面上提示
    root.mainloop()


if __name__ == "__main__":
    main()
