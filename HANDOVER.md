# WorkBuddy 多账号自动签到 —— 维护者交接文档

> 交接对象：新接手的维护者
> 基于 2026-10-05 的仓库实测（commit `a70ebc4`，Release v1.0）撰写
> 仓库：https://github.com/guos503-hue/workbuddy-multi-signin （public）

---

## 1. 项目简介

### 用途

为 WorkBuddy（国内站，`www.workbuddy.cn`）桌面客户端用户提供**多账号免切换自动签到**：不用在客户端里退出/登录换号，即可为一台机器上所有登录过的账号领取每日签到积分与成长中心奖励。

### 解决的核心问题

1. **切换成本**：官方客户端一次只能登录一个账号；手动换号签到繁琐。
2. **凭据复用**：客户端每次登录都会把登录态归档一份到本机（互不覆盖），脚本读取这些归档，即可为"当前未登录"的历史账号签到。
3. **长期无人值守**：为历史账号换取自持令牌（DPAPI 加密落盘），临近过期自动续期，无需反复登录。

### 主要功能清单

| 功能 | 说明 |
|---|---|
| 免切换签到 | 发现本机所有历史归档 → 逐账号签到 + 成长中心 |
| 图形面板 | Tkinter 面板：实时日志、账号表格、添加/停用/重命名/移出账号 |
| 自持凭据 | 非当前登录账号用 refreshToken 换取新令牌，DPAPI 加密存 `state/`，自动续期 |
| 当前账号只读 | 只读客户端写出的凭据文件，不碰其 token 链（避免与客户端抢刷新掉登录） |
| 可选定时 | 面板内勾选"每天定时自动启动"；或纯静默计划任务（不开面板） |
| 解释器自愈 | 启动时自动探测带 tkinter 的 Python，不合格自动换一个重启 |
| 日志 | 每轮结果写 `signin.log`；面板折叠原始 JSON 为人话 |
| 单实例保护 | 面板互斥锁；引擎文件锁（15 分钟过期）防并发 |

**平台范围**：仅 Windows 桌面端 + 国内站（`www.workbuddy.cn`）。国际版账号不适配；`signin.py` 上游本身支持 macOS / Linux CLI 凭据（本封装未在那些平台验证）。

---

## 2. 仓库与目录结构

### 线上仓库

| 项 | 值 |
|---|---|
| 仓库 | `guos503-hue/workbuddy-multi-signin`（public，MIT） |
| 默认分支 | `main`，当前唯一 commit `a70ebc4`（2026-10-05） |
| Release | `v1.0`，资产 `WorkBuddySignin-v1.0.zip`（58,648 字节） |
| 本地发布副本 | `D:\WorkBuddySignin-Dist`（已关联 origin，工作区干净） |
| 自用工作副本 | `D:\WorkBuddySignin`（含本机修复与运行时文件，**不要**直接发布） |

### 仓库文件清单

| 文件/目录 | 一句话职责 |
|---|---|
| `run_all.py` | **引擎**：发现账号 → 建立/续期自持凭据 → 逐账号调起 signin.py → 写日志。CLI 入口 |
| `signin.py` | **签到实现**（来自上游 88lin/workbuddy-auto-signin，MIT）：凭据解密、HTTP 请求、签到与成长中心全流程 |
| `panel.pyw` | **图形面板**：run_all.py 的 Tkinter 前端（不复制签到逻辑），双击即开 |
| `启动面板.bat` | 面板启动器：自动找可用 pythonw.exe 并拉起面板（纯 ASCII + CRLF） |
| `install-tasks.ps1` | **本包**的多账号静默任务注册脚本（WorkBuddySigninMulti / MultiPoll） |
| `install-windows.ps1` | **上游**的单账号静默安装脚本（WorkBuddyAutoSignin / GrowthPoll），本包原样保留 |
| `README.md` | 面向使用者的说明（风险声明、用法、FAQ、目录表） |
| `LICENSE` | 上游 MIT 许可证（Copyright 2026 88lin） |
| `.gitignore` | 排除运行时生成物：`state/`、`accounts.json`、`panel.json`、`signin.log*`、`__pycache__/` |
| `.gitattributes` | 行尾策略：`.bat`/`.ps1` 强制 CRLF，`.py`/`.pyw`/`.md` 用 LF |

### 运行时生成物（不入库，在本机出现）

| 路径 | 说明 |
|---|---|
| `accounts.json` | 账号显示名映射（uid → 名称）+ `_disabled` 停用列表 |
| `panel.json` | 面板设置（自动补签开关、定时设置、窗口几何） |
| `state/<uid>.dat` | DPAPI 加密的自持凭据（仅本机本用户可解） |
| `state/<uid>.meta.json` | 自持凭据的明文元数据（过期时间、来源，**不含 token**） |
| `state/.lock` | 引擎文件锁（15 分钟过期） |
| `signin.log` / `signin.log.1` | 签到日志（超 2MB 自动轮转） |

---

## 3. 技术栈

| 项 | 值 |
|---|---|
| 语言 | Python 3（纯标准库，**零第三方依赖**） |
| 已验证解释器 | Python 3.14.7（本机实测运行） |
| 面板要求 | 必须带 **tkinter** 的完整安装（python.org 安装包默认含） |
| 最低版本建议 | Python 3.8+（代码未用 f-string/walrus 等特性，保守兼容；未实测低版本，标 ⚠️ 待验证） |
| 平台 | Windows 10/11（DPAPI、计划任务、'py' 启动器均为 Windows 特性） |

### 关键依赖（全部为系统内置，无 pip 安装项）

| 组件 | 用途 |
|---|---|
| `ctypes` + `crypt32` | **DPAPI**（CryptProtectData/UnprotectData）加解密自持凭据 |
| `tkinter` / `ttk` | 图形面板 |
| `urllib.request` | 全部 HTTP 请求（签到/成长中心/令牌刷新） |
| `subprocess` | 调起 signin.py 子进程、PowerShell（计划任务管理）、客户端解密助手 |
| `sqlite3` | ❌ 未使用（无本地数据库） |
| WorkBuddy 客户端本体 | 提供 `_run_auth_helper` 解密能力（`ELECTRON_RUN_AS_NODE=1` 运行内置 JS） |

### 架构要点

- **引擎/面板分离**：面板通过 `run_all.py --json-lines` / `--list-json` 的子进程 stdout 通信，不 import 引擎，不复制逻辑。
- **signin.py 是上游文件**：与上游保持同步是维护动作之一（见 §9）。

---

## 4. 环境搭建

### 前置条件

1. Windows 10/11
2. [WorkBuddy 桌面客户端]（国内版）已安装，且**至少登录过一次**要签到的账号
3. Python 3（python.org 完整安装，勾选 "Add python.exe to PATH"）

### 安装命令（一键可复制）

```powershell
# 1) 获取代码（方式任选）
git clone https://github.com/guos503-hue/workbuddy-multi-signin.git
#    或解压 Release 的 WorkBuddySignin-v1.0.zip

# 2) 进入目录，先跑只读检查（不发任何请求）
cd workbuddy-multi-signin
python run_all.py --list

# 3) 跑一轮真实签到（屏幕可见结果）
python run_all.py --verbose
```

无需 `pip install` —— 项目零第三方依赖。

### 环境变量清单

| 名称 | 含义 | 必填 | 使用方 |
|---|---|---|---|
| `WORKBUDDY_EXE` | 手动指定 WorkBuddy.exe 路径（自动探测失败时用） | 否 | run_all.py / signin.py |
| `WORKBUDDY_AUTH_FILE` | 手动指定凭据文件路径（默认按平台自动探测） | 否 | run_all.py / signin.py |
| `WORKBUDDY_SIGNIN_LOG` | 自定义日志文件路径（默认脚本同目录 `signin.log`） | 否 | signin.py |
| `WORKBUDDY_BUDGET_SECONDS` | 单次运行时间预算（秒）。非法值自动回落并告警；上限：签到 540s / 轮询 240s | 否 | signin.py |
| `WORKBUDDY_SIGNIN_REFRESH_AT_DAYS` | 自持 accessToken 剩余天数低于此值时自动续期（默认 10） | 否 | run_all.py |
| `WORKBUDDY_SIGNIN_REFRESH_RT_DAYS` | 自持 refreshToken 剩余天数低于此值时自动续期（默认 15） | 否 | run_all.py |
| `WORKBUDDY_PANEL_PYTHON` | 手动指定面板的 pythonw.exe（跳过自动探测） | 否 | panel.pyw |
| `WORKBUDDY_GROWTH_LOG_EMPTY` | 置 1 时轮询空跑也写日志（默认空跑不落盘） | 否 | signin.py |
| `WB_PANEL_RELAUNCHED` | 内部变量：面板自愈重启防循环 | 否（内部） | panel.pyw |

### 本地启动成功判据

```powershell
# 判据 A（命令行）：--list 有输出，且列出 ≥1 个账号
python run_all.py --list
# 期望输出形如：
#   发现 1 个账号：
#     账号A    uid=xxxxxxxx-xxxx-...
#               来源: 客户端当前登录（只读）
#               accessToken 剩 23.5 天 | refreshToken 剩 53.1 天

# 判据 B（面板）：双击 启动面板.bat → 窗口出现、表格列出账号、无报错弹窗
```

判据 A 输出 `未发现任何国内站凭据` = 前置条件 2 未满足（先在客户端登录一次）。

---

## 5. 核心流程

### 5.1 签到主流程（CLI 引擎 `run_all.py`）

```
main()
 ├─ discover()                         # 扫描凭据，为每个 uid 定模式
 │   ├─ 读 %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\
 │   │    ├─ workbuddy-desktop.info          → live 模式（当前登录）
 │   │    └─ workbuddy-desktop-<时间戳>.*.info → archives（历史归档）
 │   ├─ 读 state/*.meta.json                  → 自持凭据
 │   └─ 每 uid 决定 mode:
 │        live      客户端当前登录（只读）
 │        state     自持凭据（自动续期）
 │        bootstrap 待建立自持（将用归档）
 └─ 逐账号 handle_account(acc, action)
     ├─ bootstrap: bootstrap_state(uid, archive)
     │     ├─ decrypt_field()  ← 调客户端解密 sym-v1 信封（不落盘明文）
     │     ├─ refresh_tokens() ← POST 换取新 token 链
     │     └─ save_state()     ← DPAPI 加密写 state/<uid>.dat
     ├─ state: load_state() → 临期则 refresh_state()（AT<10d 或 RT<15d）
     ├─ 自持账号将 session 写临时明文文件 state/.run-<uid>.json
     ├─ run_signin(auth_file, action)
     │     └─ subprocess: python signin.py <action>
     │         ← 环境变量 WORKBUDDY_AUTH_FILE / WORKBUDDY_EXE 传入
     │         ← signin.py: find_auth_file → load_session_retry → resolve_session
     │                        → build_headers → post(签到接口)
     ├─ 删除临时明文文件（finally 保证）
     └─ 汇总 → append_log() / stdout / --json-lines
```

**signin.py 侧签到子流程（`auto` / `silent` 动作）**：

```
_run("auto")
 ├─ find_auth_file()       # 定位凭据（环境变量优先）
 ├─ load_session_retry()   # 带 3 次重试（客户端刷新 token 时会短暂独占文件）
 ├─ resolve_session()      # sym-v1 加密 → 调客户端解密为明文 token（仅内存）
 ├─ build_headers()        # 组装请求头
 ├─ run_daily(headers, endpoint)
 │   ├─ run_auto():
 │   │   ├─ POST /v2/billing/meter/checkin-activity-status   # 查状态
 │   │   ├─ 已签 → 直接汇报（幂等，不计失败）
 │   │   ├─ 未签 → POST /v2/billing/meter/daily-checkin     # 领积分
 │   │   └─ 再查一次状态 → 汇报（连签/累计）
 │   └─ run_growth(headers, endpoint)   # 成长中心
 │       ├─ 领旅行礼物 → 派 Buddy → 领任务 → 补登 → 连登兑换 → 开盲盒 → 能量
 │       └─ ⚠️ 各子步骤独立 try，一段失败不影响其余
 └─ emit()                 # 落盘或打印（含敏感值 REDACTED 兜底）
```

### 5.2 关键函数索引

| 模块 | 函数 | 职责 |
|---|---|---|
| run_all.py | `discover()` | 扫描归档 + state，判定每账号 mode |
| run_all.py | `bootstrap_state()` | 归档 → 解密 → 刷新 → DPAPI 落盘（建立自持） |
| run_all.py | `refresh_state()` | 用 refreshToken 换新令牌并落盘 |
| run_all.py | `handle_account()` | 单账号完整处理（含续期、临时文件、清理） |
| run_all.py | `run_signin()` | 调起 signin.py 子进程并解析输出 |
| signin.py | `find_workbuddy_runtime()` | 定位客户端 exe（常见目录 + 注册表） |
| signin.py | `_run_auth_helper()` | 调客户端运行时解密凭据（管道有界、超时兜底） |
| signin.py | `run_auto()` | 查状态 → 未签才领 → 汇报 |
| signin.py | `run_growth()` | 成长中心全套 |
| signin.py | `run_daily()` | auto/silent/poll 共用：签到 + 成长中心 |
| signin.py | `emit()` | 输出落盘（silent 前缀判定，REDACTED 兜底） |
| panel.pyw | `find_gui_python()` | 探测带 tkinter 的 Python（自愈核心） |
| panel.pyw | `Panel.start_job()` / `_worker()` | 后台调起引擎、消费 JSON 行 |
| panel.pyw | `register_daily_task()` / `unregister_daily_task()` | 面板的定时任务管理 |

### 5.3 面板数据流

```
用户点「立即签到」
 → Panel.do_signin() → start_job("signin", ["--json-lines"])
   → 后台线程 _worker() → subprocess: python run_all.py --json-lines
     → 每账号一行 JSON（{"event":"account",...}）+ 结束行（{"event":"done",...}）
       → queue → _poll() → _handle_json() → 日志区 + 表格刷新
         → (设置允许时) 下一轮自动刷新列表 / 自动关窗
```

---

## 6. 配置文件与密钥管理

### 6.1 配置文件

| 文件 | 位置 | 字段 | 含义 |
|---|---|---|---|
| `accounts.json` | 程序目录 | `<uid>` : `"显示名"` | 账号显示名（只影响日志称呼） |
| | | `_disabled`: `[uid, ...]` | 停用账号（不参与签到） |
| | | `_comment` | 备注（可忽略） |
| `panel.json` | 程序目录 | `auto_signin_on_open` | 开窗自动补签（默认 true） |
| | | `daily_launch_enabled` | 定时启动开关（默认 false） |
| | | `daily_launch_time` | 定时时间 `HH:MM`（默认 "09:00"） |
| | | `close_after_auto` | 定时启动签完自动关窗（默认 false） |
| | | `geometry` | 窗口几何 |

`accounts.json` 脱敏示例：

```json
{
  "_comment": "uid -> 显示名。uid 可在 run_all.py --list 里看到。",
  "4d540dbf-****-****-****-************": "账号A",
  "c4e25c56-****-****-****-************": "账号B",
  "_disabled": []
}
```

### 6.2 密钥/凭据保管方式（脱敏说明）

| 凭据 | 存放位置 | 保护方式 |
|---|---|---|
| 客户端凭据归档 | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop*.info` | 客户端自身 sym-v1 信封加密（脚本**只读**） |
| 自持凭据（token 链） | `state/<uid>.dat` | **DPAPI**（Windows 数据保护；仅本机本 Windows 用户可解密） |
| 自持凭据元数据 | `state/<uid>.meta.json` | 明文，但**不含任何 token**（只有过期时间戳） |
| 临时明文 session | `state/.run-<uid>.json` | 仅在单次会话内存在，`finally` 保证删除 |

**安全约定（维护时不得破坏）**：
- 任何 token **不打印、不入日志**；`emit()` 有 `_sensitive_values` REDACTED 兜底。
- 日志只有结果与积分数字（见下例）。
- 换电脑后 `state/` 旧凭据解不开属**预期行为**——不会迁移，在新机重新登录客户端即可。

日志脱敏示例：

```
=== 2026-10-05 13:00:02  共 2 个账号  动作=auto ===
[2026-10-05 13:00:02] 账号A      OK   今日已签过（今日 +100，连续 6 天，累计 600 积分）
[2026-10-05 13:00:02] 账号B      OK   今日已签过（今日 +100，连续 5 天，累计 500 积分）
```

### 6.3 陷阱字段（⚠️ 改动前必读）

| 位置 | 含义 |
|---|---|
| `run_all.py: discover()` 中 `arc_mtime > state_saved + 300` | **新旧判定用归档写入时间，不能用 refreshExpiresAt 比较**——刷新响应给的新 RT 窗口（~40 天）天然短于归档剩余窗口（~50 天），用后者会导致每轮误判重建、反复刷新 token 链 |
| `signin.py` 的 `NETWORK_RETRY_DELAYS = (5,15,30,60,90)` | 定时任务最常撞"刚开机网络没就绪"，退避必须分钟级；固定"5 秒重试一次"是错误的旧方案 |
| `signin.py` 的预算常量 `MAX_BUDGET_SECONDS=540` / `POLL_MAX_BUDGET_SECONDS=240` | **与 install-*.ps1 的 ExecutionTimeLimit 一一对应**（各留 60s 收尾）；改一处必须同步另一处 |
| `panel.pyw` 的 `find_gui_python()` | WorkBuddy 自带 Python **没有 tkinter**，不可用；探测逻辑必须实测 `import tkinter` |
| `signin.py` 的 `POLL_ACTIONS = ("silent-poll", "silent-growth")` | 旧名必须保留——已安装的旧计划任务仍在用 `silent-growth` |

---

## 7. 部署与定时任务

### 7.1 部署方式（二选一或并用）

| 方式 | 命令/操作 | 适用场景 |
|---|---|---|
| **面板模式** | 双击 `启动面板.bat`；可选"创建桌面快捷方式" | 想看见结果、手动补签 |
| **哨兵模式**（纯静默） | `powershell -ExecutionPolicy Bypass -File .\install-tasks.ps1` | 无人值守，不开任何窗口 |

两种模式的任务名不同、互不影响：面板管 `WorkBuddySigninPanel`（面板内开关）；静默任务为 `WorkBuddySigninMulti` + `WorkBuddySigninMultiPoll`。上游脚本 `install-windows.ps1` 注册的是另一组（`WorkBuddyAutoSignin` + `WorkBuddyGrowthPoll`），三组任务名均不冲突，但**不要重复注册多组做同一件事**。

### 7.2 定时任务配置示例

**A. 静默任务（install-tasks.ps1，多账号）**

```powershell
powershell -ExecutionPolicy Bypass -File .\install-tasks.ps1
# 注册两个：
#   WorkBuddySigninMulti      每天 00:05                   签到 + 成长中心
#   WorkBuddySigninMultiPoll  每天 01/05/09/13/17/21 点    补签 + 成长中心
#
# 卸载：
Unregister-ScheduledTask -TaskName "WorkBuddySigninMulti" -Confirm:$false
Unregister-ScheduledTask -TaskName "WorkBuddySigninMultiPoll" -Confirm:$false
```

**B. 面板定时（GUI 内勾选）**

面板勾选「每天定时自动启动」+ 填时间（如 `09:00`）→ 注册任务 `WorkBuddySigninPanel`，到点运行 `panel.pyw --auto`。**这不是开机自启**。子选项「定时启动签完自动关窗」控制签完是否退出。

**C. 上游单账号任务（install-windows.ps1，如需）**

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
#   WorkBuddyAutoSignin   每天 00:05    silent
#   WorkBuddyGrowthPoll   每 4 小时     silent-poll
```

任务关键设置对比：

| 脚本 | 设置 |
|---|---|
| `install-tasks.ps1` | `-StartWhenAvailable` + `-MultipleInstances IgnoreNew`（不并发）；两个任务时限均 **30 分钟** |
| `install-windows.ps1`（上游） | `-StartWhenAvailable`；时限分别为 **10 / 5 分钟** |

所有任务以当前用户 `Interactive` 身份运行（`New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive`）。

### 7.3 日志查看方式

```powershell
# 完整日志（引擎与 signin.py 共用）
Get-Content .\signin.log -Tail 30 -Wait     # 实时追

# 面板内：按钮「打开日志文件」；面板底部窗口显示最近 120 行（折叠 JSON）

# 任务是否跑过 / 下次何时跑
Get-ScheduledTaskInfo -TaskName "WorkBuddySigninMulti" | Select-Object LastRunTime,LastTaskResult,NextRunTime

# 手动验证一轮（不开面板）
python run_all.py --verbose
```

---

## 8. 常见问题排查

### Q1. 面板打开了但表格是空的 / `--list` 输出"未发现任何国内站凭据"

**排查步骤**：
1. 确认 WorkBuddy 客户端**已登录至少一个国内站账号**（国际版不适用）；
2. 检查归档目录是否有文件：`dir %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`；
3. 若目录为空 → 在客户端登录一次，重新扫描；
4. 若目录有文件但脚本不认 → 检查文件是否有 `"domain":"www.workbuddy.cn"` 字段；国际市场账号（domain 不同）会被刻意跳过。

### Q2. 双击 `启动面板.bat` 闪一下就没了

**排查步骤**：
1. 右键 `启动面板.bat` → 用 PowerShell 运行，看 `[ERROR]` 文本；
2. 大概率是没装 Python 或缺 tkinter → `python -c "import tkinter"` 验证；
3. python.org 重装勾选 "Add python.exe to PATH"；或设 `WORKBUDDY_PANEL_PYTHON` 指向正确的 pythonw.exe；
4. 面板本身有自愈逻辑（`find_gui_python` → 重试 → 弹窗告知），若连弹窗都没有，按 1–3 走。

### Q3. 某个号长期不签会失效吗 / 日志出现"自动续期失败"

**排查步骤**：
1. 自持凭据逻辑：AT 剩 <10 天或 RT 剩 <15 天时自动刷新一次；
2. 若超过 ~50 天窗口一直没用，refreshToken 过期 → 需要在客户端**重新登录一次**该账号（产生新归档 → 面板重新「添加账号」或直接跑一轮）；
3. 查看 `state/<uid>.meta.json` 的 `refreshExpiresAt` 判断剩余窗口；
4. "自持凭据建立失败" 常见原因：客户端版本升级导致解密失败（`AUTH_REASONS` 表里有对应人话）；此时该轮**仍会用归档原文件签到**（降级策略）。

### Q4. 每天定时任务有时没跑

1. 任务依赖系统电源状态；`StartWhenAvailable` 会在下次唤醒后补跑一轮，但睡过头跨天就错过；
2. `ExecutionTimeLimit` 杀进程会让该轮日志丢失（脚本有预算保护，但仍可能发生在极端情况）；
3. 检查 `Get-ScheduledTaskInfo ... LastTaskResult`：`0x0` 成功；`0x41303` 未运行；其他值查 Windows 任务计划错误码；
4. 时间不对：面板定时（`WorkBuddySigninPanel`）和静默任务（Multi/MultiPoll）是两套，分别检查。

### Q5. 签到报 HTTP 401/403 或 "结果异常"

1. 401/403 → 服务端拒绝认证：客户端重新登录，或确认凭据对应的服务地址；
2. 接口结构变更（服务端改版）→ 检查上游 88lin/workbuddy-auto-signin 是否有更新，同步 `signin.py`；
3. 先跑 `python signin.py doctor`（离线检查凭据格式与运行时能力，不解密、不联网）定位是本地问题还是服务端问题。

### Q6. 权限/环境类报错

| 现象 | 排查 |
|---|---|
| "找不到 WorkBuddy 客户端" | 安装客户端；或设 `WORKBUDDY_EXE` 指向 WorkBuddy.exe |
| `PermissionError` 读凭据 | 客户端刷新 token 时会短暂独占文件；脚本有 3 次重试（间隔 2s）。持续失败 → 关闭客户端重试 |
| 计划任务不执行 | 任务以当前用户 Interactive 身份运行；确认用户已登录且非"仅密码"环境 |

---

## 9. 维护注意事项

### 已知风险点

| 风险 | 说明 | 缓解 |
|---|---|---|
| **接口逆向** | 签到/成长中心接口来自客户端逆向，服务端改一版就可能失效 | README 有声明；关注上游仓库更新；`signin.py` 与上游保持一致 |
| **风控** | 第三方脚本操作账号存在被限制的可能，尺度不透明 | README 首段风险声明；建议仅本人账号使用 |
| **凭据不可迁移** | DPAPI 与机器+用户绑定，换机后 `state/` 全部失效 | 文档已说明；换机重新登录客户端 |
| **单账号多机** | 同一账号在两台机器各自续期可能导致 token 链互相踢掉 | 未在代码层防护 ⚠️；建议单机使用 |
| **成长中心幂等性** | 部分写操作（抽奖等）**刻意不重试**（POST 默认 retry=False，防重复提交） | 维护时不要把 POST 改成默认重试 |
| **文件锁竞争** | 面板与静默任务可能同时启动 | 引擎有 `.lock`（15 分钟过期）+ 任务 `MultipleInstances IgnoreNew` |
| **日志增长** | `signin.log` 超 2MB 轮转为 `.1`（只保留一代） | 如需长历史自行归档 |

### 改动时需同步的文件/位置

| 改动 | 必须同步 |
|---|---|
| 预算常量（`signin.py`） | `install-windows.ps1` / `install-tasks.ps1` 的 `ExecutionTimeLimit`（各留 60s） |
| 任务名 / 任务结构 | 面板 `TASK_NAME`（panel.pyw）、README、两个 ps1 脚本 |
| `run_all.py` 的 JSON 输出格式（--json-lines / --list-json） | `panel.pyw` 的 `_handle_json()` / `do_add_account()` 解析逻辑 |
| 面板设置键名 | `DEFAULT_SETTINGS`（panel.pyw）与 README |
| `accounts.json` / `state/` 结构 | `run_all.py` 的 `save_state/load_state/load_labels/load_disabled` + `panel.pyw` 的读写 |
| `signin.py`（上游文件） | 记录上游版本；更新时对照上游 diff，避免破坏本包的 `WORKBUDDY_EXE` 注入点 |
| README 的功能/FAQ | 与实际行为保持一致 |

### 发布/分发检查清单

- [ ] `.bat` 保持纯 ASCII + CRLF（`file` 或 git 的 `--eol` 检查）
- [ ] 不包含运行时文件（`state/`、`accounts.json`、`panel.json`、`signin.log`）
- [ ] 无本机路径硬编码（发布版不得出现 `D:\workbuddy1` 等本机路径——自用版有，发布版已泛化）
- [ ] 无 uid / 用户名 / token 残留（发布 zip 前扫描）
- [ ] LICENSE 与出处（88lin）保留
- [ ] Release zip 内容与仓库一致

---

## 10. 后续待办

| # | 事项 | 优先级 | 来源 |
|---|---|---|---|
| 1 | 发布后跟踪：若有用户反馈"添加账号"找不到归档，核实非标准安装路径的归档目录 | P2 | 推断（未实测跨机型） |
| 2 | 自动化测试缺失：无任何测试文件（引擎输出格式、`discover()` 模式判定、日志折叠最值得加） | P2 | 仓库实测（无 tests/ 目录） |
| 3 | README 补充 macOS/Linux 说明或明确"仅 Windows"（上游 signin.py 支持 CLI 凭据，本封装未验证） | P3 | 代码对比 |
| 4 | 多机同账号 token 链互相影响的防护或文档补充 | P3 | 风险盘点 |
| 5 | 上游 signin.py 同步机制：记录当前版本来源（v1.0 时点），后续订阅上游变化 | P3 | 维护需要 |
| 6 | GitHub 连接方式未文档化：本机实测**直连可用但偶发失败**（`git ls-remote`/发布/同步需重试）；代理 7897 并非始终运行。发布机需自备可用的 GitHub 连接方案 | P3 | 实测 |
| 7 | 面板"添加账号"对 `workbuddy-desktop-ai` 前缀文件（已跳过）的说明文档化 | P4 | 代码注释 |

**优先级定义**：P1 = 阻塞使用/发布；P2 = 影响可靠性/体验；P3 = 增强/整洁；P4 = 文档类。

---

## 11. 需要补充的信息与建议确认对象

以下信息在仓库/代码中**无法确认**或**需要外部确认**，以 ⚠️ 占位标明：

| # | 待确认事项 | 建议确认对象 |
|---|---|---|
| 1 | ⚠️ **Python 最低支持版本**：代码未见高版本特性，但只在 3.14.7 实测过；README 只说"Python 3" | 维护者自测（3.8/3.9 快速验证） |
| 2 | ⚠️ **GitHub 网络环境**：本机实测直连可用但偶发失败、代理 7897 并非始终运行；发布/同步的实际网络方案未文档化 | 原维护者 / 发布机环境说明 |
| 3 | ⚠️ **风控实际尺度**：README 有风险声明，但实际使用中是否出现过账号被限制的案例 | 原维护者（如有用户反馈） |
| 4 | ⚠️ **上游 88lin/workbuddy-auto-signin 的同步约定**：本包 signin.py 是哪个上游版本、如何跟进 | 原维护者 |
| 5 | ⚠️ **未来功能意向**：是否计划支持国际版 / 更多平台 / Web 界面等 | 原维护者 / 仓库 owner（guos503-hue） |
| 6 | ⚠️ **Release 流程**：v1.0 的 zip 打包步骤未见脚本化（手动？） | 原维护者 |
| 7 | ⚠️ **许可再分发细节**：LICENSE 为上游 88lin 的 MIT；本包新增部分的版权声明方式 | 原维护者 / 法律视角 |
| 8 | ⚠️ **issues 模板/贡献指南**：仓库无 .github 目录、无 CI | 仓库 owner |

---

*文档依据仓库 commit `a70ebc4` 与 Release v1.0 的实测编写（2026-10-05）。任何代码变更后，请核对 §2 文件清单、§6.3 陷阱字段、§9 同步清单三处是否需要更新。*
