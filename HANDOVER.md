# DiskGuard 项目说明（开发者交接文档）

> **这是什么**：一份面向后续接手者的项目全景说明。读完它，你应该知道这个工具解决什么问题、
> 代码为什么长成现在这样、哪些地方一改就会出事、以及怎么打包和验证。
>
> **建议阅读顺序**：§1 → §3 → §8（契约）→ §10（验证）→ 其余按需查阅。
> 如果你只改界面，§8 和 §10 必读；如果你要动数据库或迁移逻辑，§6、§7、§8 全读。
>
> **文档时点**：2026-09-24（公开发布 1.0.0 时）。此后代码演进以源码与实际仓库为准。

---

## 1. 一句话

DiskGuard 是一个 **Windows 桌面磁盘空间监控小工具**：扫描某个盘/文件夹的**一级子目录**体积，
与"比较基准"对比，增长超阈值就标红预警，帮你在磁盘被悄悄吃满之前找到那个正在变大的目录。

**关键指标**：单文件实现（约 3200 行 / 138 个函数与方法 / 6 个类）· Python 3.11 + Tkinter ·
**运行时零第三方依赖**（只用标准库）· 单进程：UI 主线程 + 1 扫描线程 + 1 临时清理线程 +
扫描线程内的 `ThreadPoolExecutor`（≤16 worker）。

## 2. 它解决什么问题

磁盘变满很少是一次性事件。真正难查的是**缓慢增长**：缓存目录、日志、构建产物、
聊天软件的接收文件夹——每次只涨几十兆，等你注意到时已经几十个 G。

系统的"按大小排序"只能回答"现在谁最大"，回答不了"**谁在变大**"。DiskGuard 的立足点就是
把"现在多大"和"之前多大"放在一起看，并让"之前"可以是一个**历史日期**（时间锚点），
而不只是"上次扫描"。

## 3. 功能全景

| 能力 | 说明 |
|---|---|
| 一级子目录扫描 | 任选盘符或文件夹，并行扫描其**下一级**子目录，按体积排名。更深处靠双击下钻按需读库 |
| 持久化历史 | 结果写入 SQLite，每个文件夹保留最近若干次的大小历史点，重启不丢 |
| 增长对比与预警 | 与比较基准比对，达到**绝对增量**或**百分比阈值**即标红 |
| 时间锚点（按天） | 把基准换成"某个历史日期及更早的值"，用来回溯"从那天到现在涨了多少" |
| 五档筛选 | 全部 / 仅预警 / 仅星标 / 仅增长 / 仅减少 |
| 星标 | 标记重点目录，独立视图查看 |
| 曲线窗 | 单击行弹出该目录的历史趋势折线（Canvas 手绘，含迷你 sparkline） |
| 删除到回收站 | 唯一的数据修改动作，弹二次确认，走 `SHFileOperationW` + `FOF_ALLOWUNDO`，可还原 |
| 界面设置持久化 | 路径、阈值、筛选、锚点、自动扫描间隔等记在库里，下次启动复原 |
| 内置自测 | `python disk_guard.py --selftest`，纯断言、不依赖 pytest，不弹窗 |

## 4. 技术选型与硬约束

| 维度 | 选择 | 为什么 |
|---|---|---|
| 语言/GUI | Python 3.11+ / Tkinter + ttk | 免安装、单文件、无外部依赖；`theme_use("clam")` 优先、回退 `vista` |
| 依赖 | **零第三方运行时依赖** | 分发成本最低。这是硬约束：新增依赖需极强理由，并确认打包后可用 |
| 存储 | SQLite（WAL） | 单文件、无需服务、支持 JSON1 函数 |
| 打包 | PyInstaller onefile + windowed | 单个 exe，双击即用 |
| 自测 | 内置断言 + 打桩时钟 | 不引入 pytest，测试库建在系统临时目录 |

> **零依赖这条必须守住**。当前全部 import 均来自标准库：
> `concurrent.futures` `ctypes` `datetime` `hashlib` `json` `os` `queue` `shutil` `sqlite3`
> `stat` `string` `sys` `tempfile` `threading` `time` `tkinter`。
> `tools/make_icon.py` 用到 `numpy` + `Pillow`，但它是**离线开发工具**，不进运行时、不进打包。

## 5. 代码结构（单文件内分层）

`disk_guard.py` 用"注释分节 + 函数/类聚合"实现逻辑分层，依赖方向自上而下、下层不反向依赖上层：

```
入口层        main() / --selftest
自测层        selftest()（时钟打桩包装）/ _selftest_body()
GUI 层        DiskGuardApp / CurveWindow / Tooltip / delete_to_recycle_bin
              卡片布局、筛选 pill、进度、快捷键守卫、锚点交互、设置持久化
扫描调度层    scan_children(progress_cb)
遍历核心      walk_dir / _Frame / is_reparse_point / quick_sig / sig_hash
              merge_small / merge_allowed
存储层        Store / SCHEMA / _UPSERT / _TOUCH_SUBTREE / resolve_db_path
              _rec(cutoff) / children(cutoff) / recent_scan_days
              get_settings / set_settings / delete_setting
              migrate_curve_click_default_v11 / import_legacy_if_empty
临时文件治理  cleanup_temp_extract_dirs / start_temp_cleanup / _looks_like_our_bundle
常量与工具    icon_path / norm / now_str / format_size / list_fixed_drives / pick_ui_family
              baseline_of / baseline_asof / sparkline / sum_series / trend_of / hist_pairs
              path_esc / setting_bool / setting_text
```

**索引方式**：请用**函数名 / 类名 + 语义**定位，不要依赖行号——行号随代码演进立即过期。

**单文件是刻意的取舍**：无模块划分，可读性靠分段注释维持。好处是分发和打包极简，
坏处是文件越来越长。若要拆模块，先解决"打包时 onefile 的 import 解析"再动手。

## 6. 数据模型

数据库 `diskguard.db`，默认与 exe/脚本**同目录**；该目录不可写时（例如装在 `C:\Program Files`）
自动回退到 `~\.diskguard\diskguard.db`。

| 表 | 职责 | 关键列 |
|---|---|---|
| `folders` | 每个目录一行，以**规范化路径**为主键 | 路径、显示名、父路径、最新大小、上一次大小、扫描时间戳、浅层签名、无权限标记、星标、最后写入批次号、历史大小 JSON 数组（上限 24 点） |
| `scans` | 扫描批次 | 自增 id、目标路径、开始/结束时间、总目录数、已完成数、备注 |
| `meta` | 键值杂项 | 界面设置统一 `ui.*` 前缀；另有一次性的**迁移标记键** |

**没有额外表、没有额外索引、没有清理任务**——这是刻意的设计，历史点在行内 JSON 中管理，
不需要 join 与后台维护。

## 7. 六个必须理解的机制

### 7.1 一级子目录扫描与"浅层签名"
只展开目标目录的下一层。为了支持"跳过无变化"的加速，每个目录会算一个**浅层签名**
（`quick_sig` / `sig_hash`）：只看**直接子项**的名字、大小、mtime。
签名不变就跳过重扫。

> **代价**：更深处文件的变化如果没引起中间目录时间戳变化，会被判为"无变化"而漏检。
> 这是已知取舍，不要把它当 bug 修——要修就得改成读 NTFS MFT 级别。

### 7.2 "上一次大小"滚动更新
写库时把旧值滚动为"上一次"。所以「上次大小 / 增长率」在任何视图、任何时刻（含重启后）
都能直接算出来，**不依赖内存快照**。

### 7.3 历史点用 SQLite JSON1 原地追加
写库语句直接对 JSON 数组追加 `[时间, 大小]`，超过上限（24）同时移除最旧一个。
不需要单独的历史表，也不需要清理任务。

### 7.4 比较基准与按天锚点
- 默认基准：相对**最新一次扫描**的上一次大小。
- 锚点模式：基准取"锚定日**及更早**"的值。`_parse_asof` 返回 `YYYY-MM-DD`，
  数据层用 `_rec(cutoff)` / `children(cutoff)` 按日期截断。
- **有锚点时不会回落到"上一次扫描"的值**——那个值与锚定日无关，回落会给出过新的错误基准。
- 锚点粒度是**天**：表达不了"今天下午 3 点之前"，但日期比时间戳好填得多，日常排查够用。
- 切换锚点会**清空"返回上一层"的历史栈**，视图回到当前目录的一级子目录列表。

> 语义陷阱：启用锚点后，`⚠ 预警` 的含义变成"从锚定日那天到现在涨幅超阈值"，
> **与默认视图（相对最新一次扫描）不是同一件事**。跨视图比较数字时必须留意口径。

### 7.5 五档筛选与"小文件夹合并"
五档集中在模块常量 `FILTERS` 里，是**唯一来源**；合并判据收敛到单一函数（`merge_small` / `merge_allowed`）。

有一个容易踩的联动：切到「仅预警」「仅增长」「仅减少」时，**小文件夹合并会被关闭**
（目标行必须逐行可见）；切回「全部」才恢复合并。

界面元素（pill）与逻辑判据必须读同一份 `FILTERS`，否则会出现"控件里能选、逻辑上被判非法"这类难查的不一致。

### 7.6 线程模型
- **UI 主线程**：Tk 必须在主线程，扫描进度经 `queue.Queue` 回传。
- **1 个扫描线程**：跑 `scan_children`，内部用 `ThreadPoolExecutor`（≤16 worker）遍历子目录。
- **1 个临时清理线程**：清理 PyInstaller onefile 解包残留的 `_MEIxxxxx` 临时目录，
  用 `_looks_like_our_bundle` 判定，**避免误删其他程序的解包目录**。

扫描速度瓶颈在磁盘 I/O，多线程只能把串行扫描提速有限——不要指望加线程能线性加速。

## 8. 不可破坏的契约（改之前必读）

### 8.1 `ui.curve_click_default_v11` 这个键名不能改

`meta` 表里有**两个不同的键**，千万不要混：

| 键 | 含义 |
|---|---|
| `ui.curve_click` | **用户设置**："单击行是否弹出曲线"，用户可改 |
| `ui.curve_click_default_v11` | **一次性迁移的标记键**，程序自己写，用户不可见 |

背景：`curve_click` 的默认值曾由**开**改为**关**。但老库里已经存着 `ui.curve_click = "1"`——
**只改代码里的默认值对老用户无效**（设置存在即被读回）。所以用"带版本号的标记键"做一次性迁移：
标记不存在 → 把 `ui.curve_click` 写成 `"0"` 并落标记；标记存在 → 直接返回，绝不重复写。

> **为什么 `_v11` 不能为了"看起来干净"而重命名**：这个键**已经写进了用户磁盘上的数据库**。
> 一旦改名，老库找不到标记 → 迁移重跑 → **把用户后来手动改回的设置静默改回 `"0"`**，
> 直接违反"永不覆盖用户选择"这条保证。
> 若确实需要换名，只能"**新增键 + 双读兼容**"，不可原地改名。

迁移的两条硬性时序要求：
1. 必须在 `__init__` 里 `get_settings()` **之前**调用，否则界面先把迁移前的旧值读回去了；
2. 外层用 `try/except sqlite3.Error` 兜底，迁移失败不能阻断启动。

### 8.2 `baseline.json` 不能进发布仓库
它是旧版本留下的基线文件，**首次运行会自动导入**为起始基线，导入后即弃用。
内容里带着生成机器的路径信息——对外部使用者毫无价值，却会泄漏环境。已在 `.gitignore` 中排除。

### 8.3 零第三方运行时依赖
见 §4。新增依赖前先想清楚：打包后还能不能用？会不会让 onefile 体积暴涨？有没有标准库替代？

### 8.4 删除只到回收站
唯一的数据修改动作。走 `SHFileOperationW` + `FOF_ALLOWUNDO`，弹二次确认，**不提供永久删除**。
不要为了"清理得更彻底"改成 `os.remove`——那会让这个工具从"救命的"变成"致命的"。

### 8.5 设置存在数据库里
界面设置落在 `meta` 表的 `ui.*` 键，**与数据库同生命周期**。
用户若为了腾空间直接删掉 `diskguard.db`，界面设置会一并丢失（回落默认值）。这是已知取舍。

## 9. 打包与分发

### 9.1 两个产物

| 产物 | 构建命令 | 用途 |
|---|---|---|
| `dist/DiskGuard.exe` | `python build_exe.py` | 发布主程序，onefile + **windowed**（无控制台），约 12 MB，已内嵌图标 |
| `dist_diag/DiskGuardDiag.exe` | `python build_with_conda.py --distpath dist_diag DiskGuardDiag.spec` | 诊断版，**带控制台**，用于抓 stderr |

**为什么需要诊断版**：windowed 版 exe 的 Tk 回调异常走 stderr 是**静默**的。
排查启动期问题必须用带控制台的版本，否则你看不到任何报错。

### 9.2 conda 环境下的原生 DLL（最容易出事的一环）

Anaconda 会把一批原生 DLL 放在 `<conda 根>\Library\bin`。只靠 `PATH` 让 PyInstaller 去搜**不可靠**，
漏收会导致运行时报：

```
ImportError: DLL load failed while importing _ctypes
```

必须**显式列出**这 8 个：

```
ffi.dll                  ctypes/cffi 底层
LIBBZ2.dll               bz2
liblzma.dll              lzma
libcrypto-1_1-x64.dll    OpenSSL crypto
libssl-1_1-x64.dll       OpenSSL ssl
sqlite3.dll              sqlite3 扩展
tcl86t.dll               Tcl 运行时
tk86t.dll                Tk 运行时
```

DLL 目录按三级顺序解析（`build_exe.py` 的 `resolve_conda_lib()`）：
1. 环境变量 `DISKGUARD_CONDA_LIB`（若已设置且目录存在）
2. 由当前解释器推导 `<sys.prefix>\Library\bin`
3. 都不存在 → **只打印警告并跳过**，即"尽力而为地打包"，绝不让脚本失败（非 conda 环境属正常现象）

### 9.3 `build_with_conda.py` 为什么存在
部分 Anaconda 的 site-packages 残留 obsolete backport（`enum34` / `typing` / `pathlib`），
在纯 Python 3.11 下无害，但会让 PyInstaller 的依赖检测报错。这个包装脚本在导入 PyInstaller
之前 monkeypatch `importlib.metadata.distribution`，屏蔽对这几个包的检测。

> **不要直接用 `python -m PyInstaller`**。在 conda 环境下会被同样的问题卡住而失败。
> 统一经 `build_with_conda.py` 驱动。

### 9.4 `--distpath` 不是可选项
spec 文件**本身无法指定输出目录**，PyInstaller 默认落到 `dist/`。
所以构建诊断版必须显式给 `--distpath dist_diag`，否则产物会跑进 `dist/`，
与发布版混在一起（也可能被发布版的重命名逻辑碰上）。

### 9.5 打包后自动自测
`build_exe.py` 在写完日志、报完体积之后，会**自动跑一次** `<exe> --selftest`：

- **只看退出码**，不依赖 stdout（windowed exe 的 stdout 在某些环境拿不到）
- 失败**只告警、不中断**——延续"绝不让打包失败"的基调
- `cwd` 设在系统临时目录，避免自测把 `diskguard.db` 写进仓库

### 9.6 图标的优雅降级
图标不是功能依赖。整条链路都做降级：`tools/make_icon.py` 生成多帧 `.ico` →
`build_exe.py` 存在图标时才追加 `--icon`，缺失只告警 → `main()` 里 `root.iconbitmap()` 包在
`except (tk.TclError, OSError, AttributeError)` 里静默降级。

> `icon_path()` 走 `app_dir()`：冻结时取 **exe 同目录**，因为 onefile 的临时解包目录里没有 `assets`。

## 10. 开发与验证

### 10.1 日常三件事
```bat
python disk_guard.py --selftest   :: 跑自测（不弹窗，纯断言）
python disk_guard.py              :: 直接运行源码
python build_exe.py               :: 打包（含自动自测）
```

### 10.2 环境要求
需要**带 tkinter 的 Python 3.11+**。若报 `ModuleNotFoundError: No module named 'tkinter'`，
说明当前解释器未编译该模块，换一个带 Tk 的发行版（例如带 Tk 的 CPython 或 conda 环境）。

### 10.3 改动后的验收要求
- **必须跑 `--selftest`**，且要**在冻结态（exe）也跑一遍**——避免"脚本模式通过、打包后失败"。
- **打包是否生效不能用体积判断**：若改动只是几个颜色常量，新旧 exe 体积几乎相同。
  可靠判据是**结构性差异**（读窗口矩形，或截图肉眼比对）。
- 涉及界面/图标的改动，用诊断版复核 stderr 无异常。

### 10.4 一条血泪教训：判据必须自动化，否则等于不存在

发布前验证清单里早就写着「**产物 exe 体积在预期区间（缺 DLL 会显著偏小）**」。
这条判据在文档里躺了很久，**但从来没人据此真的跑过诊断版**。

直到实跑才发现：诊断版 8.24 MB、发布版 12.37 MB，正是"显著偏小"——它的 exe **运行即崩**。
根因是 `DiskGuardDiag.spec` 自己留了 `binaries=[]`，与 `build_exe.py` 里那套显式 `--add-binary`
**各维护一份、只有一份有效**。

修复方式不是"把 8 个 DLL 抄进 spec"，而是让 spec **复用** `build_exe.py` 的同一份来源
（`from build_exe import DLLS, resolve_conda_lib`），从根上消灭第二份真相。

**教训**：与其在文档里多写十条清单，不如给脚本加一段自动检查。

## 11. 演进史（为什么代码里还留着历史包袱）

理解这段，你就不会觉得某些命名莫名其妙。

| 阶段 | 带来了什么 |
|---|---|
| `init.` | 最初版本 |
| v9 界面改造 | 卡片式布局、浅色现代风、统一调色板 |
| v10 | 比较时间锚点、筛选下拉、设置持久化（设置开始进 `meta` 表） |
| v11 | 锚点改为**按天**、筛选扩到**五档**、界面卡片化重构、新增应用图标；并引入 `ui.curve_click_default_v11` 迁移标记 |
| 文档同步 / 公开发布 | 面向外部贡献者重写 README 与架构文档；整理出独立发布仓库；git 历史瘦身（`.git` 71 MB → 7.6 MB） |

**历史包袱的具体表现**：
- 函数名 `migrate_curve_click_default_v11`、标记键 `ui.curve_click_default_v11` 带 `_v11` 后缀 —— 见 §8.1，**不能改**。
- 代码里可能还有零星内部版本的表述，那是演进痕迹，不是错误。

## 12. 已知陷阱清单

按"踩到的概率 × 危害"排序：

| # | 陷阱 | 应对 |
|---|---|---|
| 1 | 打包后启动报 `DLL load failed while importing _ctypes` | 显式 `--add-binary` 列 8 个 conda DLL，不依赖 PATH（§9.2） |
| 2 | 两个打包入口各维护一份 DLL 逻辑，只有一个是对的 | 让 spec **复用** `build_exe.py` 的 `DLLS` / `resolve_conda_lib()` |
| 3 | `python -m PyInstaller` 在 conda 下失败 | 经 `build_with_conda.py` 驱动（§9.3） |
| 4 | 诊断版产物落到 `dist/` 而不是 `dist_diag/` | 必须显式 `--distpath`（§9.4） |
| 5 | 体积正常但 exe 运行即崩 | 打包后自动跑 `--selftest` 看退出码（§9.5） |
| 6 | Windows `core.autocrlf=true` 让 `git status` 出现一批"幽灵 M"（`git diff` 却为空） | `.gitattributes` 写 `* text=auto eol=lf`；**绝不用** `git reset --hard` 去"修"（会把用户文件改成 CRLF） |
| 7 | `git commit --amend` 后旧内容仍可被恢复 | `reflog expire --expire=now --all` + `gc --prune=now`；且必须扫**全部对象**，只扫可达对象查不出来 |
| 8 | `baseline.json` 会把生成机器的路径带进仓库 | 不提交、`.gitignore` 排除（§8.2） |
| 9 | 删库会连带丢掉界面设置 | 已知取舍，文档里明确告知用户（§8.5） |
| 10 | 设置改动有防抖窗口，窗口内强杀进程会丢最后一次变更 | 正常关闭不受影响；文档已注明 |
| 11 | 覆盖正在使用的 exe 可能被安全软件/沙箱拦截 | 先把旧 exe **改名让路**（`os.replace` → `DiskGuard.old.exe`），不要直接覆盖 |
| 12 | `%TEMP%` 残留 `_MEIxxxxx` 解包目录 | 内置临时清理线程；用 `_looks_like_our_bundle` 判定，别误删别家的 |
| 13 | 自测用**替换模块全局 `now_str`** 打桩时钟（侵入式手法） | 新增取时路径若绕过 `now_str`，自测的日期隔离会失效 |
| 14 | 锚点视图下"预警"语义与默认视图不同 | 跨视图比较数字时留意口径（§7.4） |
| 15 | 进度条是**估算**（已完成子目录数 / 总数） | 单个超大目录遍历时进度会停住不动，属正常现象 |
| 16 | 浅层签名可能漏检深层变化 | 已知取舍，不修（§7.1） |
| 17 | 写库关闭同步 + WAL 换吞吐，极端掉电可能丢最近一批 | 已知取舍 |
| 18 | 只比一级子目录 | 设计如此，更深靠下钻 |
| 19 | 无导出功能 | 目前只能看，不支持导出 CSV/报表 |

## 13. 文件职责（公开发布仓库）

| 文件 | 职责 |
|---|---|
| `disk_guard.py` | 全部程序逻辑（单文件） |
| `README.md` | 面向**使用者**：功能、截图、安装、使用、已知限制 |
| `ARCHITECTURE.md` | 面向**想改源码的人**：分层、时序、算法、坑位、符号索引 |
| `CHANGELOG.md` | Keep a Changelog 格式的变更记录 |
| `LICENSE` | MIT |
| `build_exe.py` | 一键打包发布版；含 DLL 解析、图标接线、旧 exe 让路、冻结态自测 |
| `build_with_conda.py` | PyInstaller 驱动包装（屏蔽 conda 的 obsolete backport 检测） |
| `DiskGuardDiag.spec` | 诊断版打包规格；**与 `build_exe.py` 共用同一份 DLL 收集规则** |
| `tools/make_icon.py` | 离线生成多帧图标（依赖 numpy + Pillow，**仅开发用、不进打包**） |
| `assets/DiskGuard.ico` | 应用图标（多尺寸，16→256 共 7 帧，BMP/DIB 帧而非 PNG 压缩帧） |
| `assets/icon_preview.png` | 图标预览图 |
| `docs/screenshot-*.png` | README 用的界面截图 |
| `requirements-dev.txt` | **仅构建期**依赖（pyinstaller、pillow） |
| `.gitattributes` | 统一行尾 LF、标记二进制类型（防幽灵修改） |
| `.gitignore` | 构建产物、运行时数据库、旧 exe、日志、编辑器杂项 |

> 发布二进制请走 **GitHub Releases**，不要把 `dist/` 下的 exe 提交进仓库。

## 14. 后续改进方向

按性价比排序，供接手时挑选：

1. **迁移机制外置**：把多个一次性迁移收敛成 `_migrations = [(标记键, 函数), ...]` 按序执行，
   降低"忘了放在 `get_settings()` 之前"这类时序风险。
2. **扫描加速**：真正的瓶颈在 I/O。要再快需直接读 NTFS MFT，而不是加线程。
3. **导出能力**：CSV / 报表——目前只能看，是使用者最常提的缺口。
4. **掉电保护**：当前以关闭同步换吞吐；若要可靠，需在关键写入点开同步或加检查点。
5. **拆模块**：单文件已约 3200 行。拆之前先解决 onefile 打包的 import 解析问题。

## 15. 接手时请核实的项

本文档依据 1.0.0 发布时的源码与验证记录整理。**以下内容会随代码演进过期，请用实际仓库核对**：

- [ ] `disk_guard.py` 的实际行数、函数/类数量（本文写"约 3200 行 / 138 函数与方法 / 6 类"）
- [ ] `meta` 表实际存在的 `ui.*` 键集合（本文列举：`skip` `drill` `curve_click` `auto` `interval`
      `filter` `asof` `path` `drive`，以及标记键 `ui.curve_click_default_v11`）
- [ ] §5 列出的函数/类名是否仍然存在（部分可能已重命名）
- [ ] `.gitignore` 是否仍覆盖 `dist/`、`dist_diag/`、`*.db`、`build_log.txt`、`DiskGuard.spec`
- [ ] 发布仓库的提交哈希与文件清单（记录时：单条提交 `c869aaf`「初始发布 v1.0.0」，17 个文件）
- [ ] `ARCHITECTURE.md` 中的符号索引（附录）是否与代码一致

> 若你发现本文档与代码不一致，**以代码为准**，并顺手把本文档改对——这份文档的价值就在于它准确。
