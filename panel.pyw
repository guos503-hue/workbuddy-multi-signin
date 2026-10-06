#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到面板 —— 图形界面（双击打开，关窗即退）。

功能
----
- 立即签到：后台调用 run_all.py（复用同一引擎），实时把每个账号的结果刷进窗口
- 刷新状态：只读查询（不领取），更新表格里的连签 / 累计积分 / 凭据剩余
- 可用积分：启动 / 切回前台自动查询，工具栏右侧固定显示（含赠送小计），点击手动重查
- 添加账号：扫描客户端凭据归档，给新账号命名（全新账号需先在客户端登录一次）
- 账号管理：右键重命名 / 停用 / 启用 / 移出
- 可选：打开时自动补签、每天定时自动启动、定时启动签到后自动关闭

与命令行引擎的关系
------------------
本面板是 run_all.py 的图形前端，不复制任何签到逻辑：
- 账号发现 / 解密 / 自持凭据 / 自动续期 全部由 run_all.py 负责
- 面板只解析 run_all.py --json-lines / --list-json 的输出并展示记录

启动参数
--------
    panel.pyw             普通打开
    panel.pyw --auto      定时任务模式：自动签到（签完是否关窗看设置）
    panel.pyw --selftest  自检：开窗 1.5 秒后自动退出
"""

import ctypes
import datetime
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

# ---------------------- 解释器自检与自愈 ----------------------
# pythonw 没有控制台：解释器不合格（例如缺 tkinter）时窗口不会出现、也没有
# 任何报错。所以启动前先验证 tkinter 可用；不可用就找一个能用的解释器重启自己。
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tk_works(exe):
    try:
        r = subprocess.run([exe, "-c", "import tkinter"], capture_output=True,
                           timeout=25, creationflags=NO_WIN)
        return r.returncode == 0
    except Exception:
        return False


def _sibling_pythonw(py_exe):
    """给一个 python.exe，返回同目录的 pythonw.exe（存在且非空才返回）。"""
    if not py_exe:
        return None
    cand = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
        return cand
    return None


def _from_py_launcher():
    """py 启动器登记的各个 Python（python.org 安装默认自带）。"""
    out = []
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands = [os.path.join(sysroot, "py.exe"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Launcher", "py.exe")]
    for exe in cands:
        if not os.path.isfile(exe):
            continue
        for args in (["-0p"], ["-3", "-c", "import sys;print(sys.executable)"]):
            try:
                r = subprocess.run([exe] + args, capture_output=True, text=True,
                                   timeout=25, creationflags=NO_WIN)
            except Exception:
                continue
            for line in (r.stdout or "").splitlines():
                m = re.search(r"([A-Za-z]:\\[^\r\n]*?python\.exe)\s*$", line.strip())
                if m:
                    out.append(m.group(1))
            if out:
                break
        if out:
            break
    return out


def _from_where_pythonw():
    """PATH 里的 pythonw.exe；跳过 Microsoft Store 占位符。"""
    out = []
    try:
        r = subprocess.run(["where", "pythonw"], capture_output=True, text=True,
                           timeout=20, creationflags=NO_WIN)
        for line in (r.stdout or "").splitlines():
            p = line.strip()
            if not p or "windowsapps" in p.lower():
                continue
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                out.append(p)
    except Exception:
        pass
    return out


def _wellknown_pythonw():
    """常见安装位置（python.org / Anaconda / uv / 本机环境）。"""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    roaming = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    pats = [
        os.path.join(local, "Programs", "Python", "Python3*", "pythonw.exe"),
        os.path.join(home, "anaconda3", "pythonw.exe"),
        os.path.join(home, "miniconda3", "pythonw.exe"),
        os.path.join(local, "anaconda3", "pythonw.exe"),
        os.path.join(local, "miniconda3", "pythonw.exe"),
        r"C:\Python3*\pythonw.exe",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Python3*", "pythonw.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Python3*", "pythonw.exe"),
        os.path.join(roaming, "uv", "python", "cpython-3*", "pythonw.exe"),
    ]
    out = []
    for p in pats:
        for hit in sorted(glob.glob(p), reverse=True):
            if os.path.isfile(hit) and os.path.getsize(hit) > 0:
                out.append(hit)
    return out


_PY_MEMO = None


def find_gui_python():
    """找一个能跑 Tkinter 的 Python 3，优先 pythonw.exe；找不到返回 None。"""
    global _PY_MEMO
    if _PY_MEMO:
        return _PY_MEMO
    seen, ordered = set(), []

    def push(p):
        if not p:
            return
        p = os.path.abspath(p)
        k = p.lower()
        if k in seen:
            return
        seen.add(k)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            ordered.append(p)

    push(os.environ.get("WORKBUDDY_PANEL_PYTHON"))            # 0) 显式指定
    for py in _from_py_launcher():                            # 1) py 启动器
        push(_sibling_pythonw(py))
    push(_sibling_pythonw(sys.executable) or sys.executable)  # 2) 当前解释器
    for p in _wellknown_pythonw():                            # 3) 常见目录
        push(p)
    for p in _from_where_pythonw():                           # 4) PATH
        push(p)

    for cand in ordered:
        if _tk_works(cand):
            _PY_MEMO = cand
            return cand
    return None


def _fatal_no_python():
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "找不到可用的 Python 3（需要带 tkinter 的完整安装）。\n\n"
            "请从 python.org 安装 Python 3，安装时勾选\n"
            "「Add python.exe to PATH」，然后重新双击启动。\n\n"
            "如果你确定已安装，可设置环境变量 WORKBUDDY_PANEL_PYTHON\n"
            "指向你的 pythonw.exe 后重试。",
            "WorkBuddy 签到面板", 0x10)
    except Exception:
        pass


try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
except Exception:
    _alt = find_gui_python()
    if _alt and os.environ.get("WB_PANEL_RELAUNCHED") != "1":
        _env = dict(os.environ)
        _env["WB_PANEL_RELAUNCHED"] = "1"
        try:
            subprocess.Popen([_alt, os.path.abspath(__file__)] + sys.argv[1:], env=_env)
            sys.exit(0)
        except Exception:
            pass
    _fatal_no_python()
    sys.exit(2)

# pythonw 无控制台兜底
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "run_all.py")
SETTINGS_FILE = os.path.join(HERE, "panel.json")
ACC_CFG = os.path.join(HERE, "accounts.json")
LOG_FILE = os.path.join(HERE, "signin.log")
STATE_DIR = os.path.join(HERE, "state")

TASK_NAME = "WorkBuddySigninPanel"
CN_DOMAIN = "www.workbuddy.cn"
MUTEX_NAME = "WorkBuddySigninPanel_Mutex_v1"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_SETTINGS = {
    "auto_signin_on_open": True,        # 打开面板时自动补签
    "daily_launch_enabled": False,      # 每天定时自动启动
    "daily_launch_time": "09:00",       # 定时启动时间
    "close_after_auto": False,          # 定时启动时：签完自动关闭窗口
    "geometry": "860x620",
}

MODE_TEXT = {
    "live": "客户端登录中",
    "state": "自持·自动续期",
    "bootstrap": "待建立自持",
}


# ------------------------------------------------------------------ 工具 ------

def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def find_pythonw():
    """给快捷方式/定时任务用的 pythonw.exe（必须带 tkinter，且必须是无控制台的 GUI 子系统）。

    与启动自愈共用同一套探测逻辑（find_gui_python 会逐个实测 import tkinter），
    但额外排除 uv 的 venv trampoline：它虽然叫 pythonw.exe，PE 子系统却是 CONSOLE，
    用它启动仍会短暂分配控制台。真解释器（uv 托管 / python.org）才是 GUI 子系统。
    """
    cand = find_gui_python()
    if cand and not _is_console_subsystem(cand):
        return cand
    # 候选退而求其次也必须带 tkinter
    for alt in _wellknown_pythonw():
        if not _is_console_subsystem(alt) and _tk_works(alt):
            return alt
    return cand or (sys.executable or "")


def _is_console_subsystem(exe):
    """读 PE 头判断子系统；CONSOLE(3) 视为会弹控制台，GUI(2) 才算干净。"""
    try:
        with open(exe, "rb") as f:
            data = f.read(0x400)
        if len(data) < 0x40 or data[:2] != b"MZ":
            return False
        off = int.from_bytes(data[0x3C:0x40], "little")
        if data[off:off + 4] != b"PE\x00\x00":
            return False
        opt = off + 24
        return int.from_bytes(data[opt + 68:opt + 70], "little") == 3
    except Exception:
        return False


def run_hidden(args, timeout=120):
    # ⚠️ 必须显式指定子进程的 stdout 编码。
    # 本机用户环境可能是 cp936/gbk（双击快捷方式时 PYTHONIOENCODING 可能被设为 gbk），
    # 此时子进程（引擎）会按 gbk 编码中文输出，而这里用 utf-8 去读 → 账号名等
    # 「来自引擎的中文」会变成 ♦♦（U+FFFD）。面板自己的字面量不走子进程，所以不受影响，
    # 于是表现出「同一行里 余额 正常、账号 乱码」的诡异现象。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=env,
                          timeout=timeout, creationflags=NO_WINDOW)


def run_ps(command, timeout=120):
    return run_hidden(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-Command", command], timeout=timeout)


def task_state():
    r = run_ps("(Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue).State" % TASK_NAME)
    return (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""


def register_daily_task(hhmm):
    pyw = find_pythonw()
    panel = os.path.join(HERE, "panel.pyw")
    ps = (
        "$ErrorActionPreference='Stop'; "
        "$a=New-ScheduledTaskAction -Execute '{pyw}' -Argument '\"{panel}\" --auto' -WorkingDirectory '{here}'; "
        "$t=New-ScheduledTaskTrigger -Daily -At '{t}'; "
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        "$p=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive; "
        "Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null; "
        "Write-Output 'TASK-OK'"
    ).format(pyw=pyw, panel=panel, here=HERE, t=hhmm, name=TASK_NAME)
    r = run_ps(ps)
    ok = r.returncode == 0 and "TASK-OK" in (r.stdout or "")
    return ok, ((r.stdout or "") + (r.stderr or "")).strip()[:400]


def unregister_daily_task():
    r = run_ps("Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction SilentlyContinue; "
               "Write-Output 'TASK-GONE'" % TASK_NAME)
    ok = r.returncode == 0 and "TASK-GONE" in (r.stdout or "")
    return ok, ((r.stdout or "") + (r.stderr or "")).strip()[:400]


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def valid_hhmm(text):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mi < 60:
        return "%02d:%02d" % (h, mi)
    return None


# ------------------------------------------------------------- 明细窗口 ------

# 第 4 节：状态枚举与固定文案（不得自由发挥）
LEDGER_EMPTY_TEXT = "暂无积分记录"
LEDGER_FILTER_EMPTY_TEXT = "无符合条件的积分记录"
LEDGER_ERROR_TEXT = "加载失败，请检查网络后重试"
LEDGER_LOADMORE_ERROR_TEXT = "加载更多失败"
LEDGER_LOADING_TEXT = "加载中…"
LEDGER_NO_EXPIRE_TEXT = "近期暂无积分到期"

DIR_ALL = "all"
DIR_INCOME = "income"
DIR_EXPENSE = "expense"

PAGE_SIZE = 40          # 每页条数（滚动加载）
EXPIRE_MAX_ROWS = 6     # 最近到期区最多显示几个「到期日」分组（同日期多批合并成一行）

# ---- 来源账号标注（明细条目右侧）----
SRC_PREFIX = "来源："                 # 需求 2：固定前缀
SRC_SYSTEM = "系统"                   # 需求 4：无归属账号时的显示名
SRC_MAX_CN = 7                        # 需求 5：默认保留 7 个汉字宽
SRC_MAX_EN = 15                       # 需求 5：等宽 15 个英文字符
SRC_ELLIPSIS = "…"                    # 截断省略号（单字符，U+2026）


def _is_wide(ch):
    """是否为「全角宽字符」（按汉字 1 个 = 英文 2 个宽计）。"""
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or
            0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
            0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60 or
            0xFFE0 <= o <= 0xFFE6 or o >= 0x20000)


def _truncate_name(name, max_units=None):
    """按「汉字 1 / 英文 0.5」的宽度单位截断，超长加省略号。

    需求 5 给的是**双阈值**：汉字 7 个 **或** 英文字符 15 个。两者宽度并不相等，
    所以不能把其中一个换算成另一个（早期按 7*2=14 半宽单位实现，导致 15 个英文
    被误截断）。这里按「全角计 1 个、半角计 0.5 个」分别累计，任一超限即截断。

    max_units: 仅供内部按像素细分时收窄用；None = 用需求默认双阈值。
    返回截断后的字符串（可能以 SRC_ELLIPSIS 结尾）；未超长则原样返回。
    """
    name = name or ""
    wide = 0.0        # 全角字符个数
    half = 0.0        # 半角字符个数（英文/数字/符号）
    out = []
    for ch in name:
        if _is_wide(ch):
            if wide + 1 > SRC_MAX_CN:
                return "".join(out) + SRC_ELLIPSIS
            wide += 1
        else:
            if half + 1 > SRC_MAX_EN:
                return "".join(out) + SRC_ELLIPSIS
            half += 1
        if max_units is not None and (wide * 2 + half) > max_units:
            return "".join(out) + SRC_ELLIPSIS
        out.append(ch)
    return "".join(out)


def _fit_source_text(full_name, avail_px, font, avail_units=None):
    """按实际可用像素宽度决定「来源：xxx」的最终文案。

    优先级（需求 5，逐级兜底）：
      (0) 够宽 → 原样
      (1) 不够 → 截断（保留前 7 汉字/15 英文字符，加省略号）
      (2) 仍不够 → 逐步减少保留字符数（最少 1 个字符 + 省略号），始终单行
      (3) 连「1 字符 + 省略号」都放不下 → 返回 None（调用方不展示来源字段）

    返回 (text_or_None, truncated_bool)。
      text_or_None: 完整可显示文案（含前缀），None = 最终兜底不展示
      truncated_bool: 是否发生了截断（调用方据此挂悬浮提示）
    """
    name = full_name or ""
    prefix = SRC_PREFIX

    def fits(txt):
        if avail_px is None:
            return True
        try:
            return font.measure(txt) <= avail_px
        except Exception:
            return True

    # (0) 原样
    whole = prefix + name
    if fits(whole):
        return whole, False

    # (1) 默认阈值截断；同时尊重调用方给的额外宽度上限（单位制）
    cand = name
    if len(cand) > 1:
        cand = _truncate_name(name, avail_units)
    full_cand = prefix + cand
    if len(cand) <= 1 and cand != name:
        cand = name          # 极短名字不加省略号
        full_cand = prefix + cand
    if fits(full_cand):
        return full_cand, (cand != name)

    # (2) 逐步减少保留字符数到 1
    chars = list(cand)
    while len(chars) > 1:
        chars.pop()
        trial = prefix + "".join(chars) + SRC_ELLIPSIS
        if fits(trial):
            return trial, True

    # (3) 最少形态（1 个字符 + 省略号）
    if chars:
        minimal = prefix + chars[0] + SRC_ELLIPSIS
    else:
        minimal = prefix.rstrip("：") + SRC_ELLIPSIS
    if fits(minimal):
        return minimal, True

    # 单字符也放不下 → 连前缀都不要，尝试裸省略号；仍不行则最终兜底
    bare = SRC_ELLIPSIS
    if fits(bare):
        return bare, True
    return None, False


def _lvl_rank(lv):
    """预警等级排序权重（danger 最高）。用于同日期多批合并时取最严重的等级。"""
    return {"none": 0, "notice": 1, "warning": 2, "danger": 3}.get(lv or "none", 0)


# ---- 中文字体探测（防「界面乱码」：不假设任何字体族/字重可用）----
#
# 为什么不能靠 tkfont.measure()：Tk/Windows 对缺字形的字符会返回一个"回退宽度"，
# 而非 0。所以 measure()>0 并不能证明字体真含该字形——必须用 GDI 直接问。
# GetGlyphIndicesW 返回 0xFFFF 表示该字体确实没有这个字形。

_FONT_CACHE = {}
_GLYPH_CACHE = {}

# 优先候选；按"中文覆盖度 + 常见度"排序
_FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
    "SimHei", "黑体", "SimSun", "宋体", "MS Gothic", "MingLiU",
)

# 探测样本：常用汉字 + 全角标点 + ASCII（都要能画）
_PROBE_CN = "积分将于到期账号余额明细"
_PROBE_PUNC = "，。（）：、"
_PROBE_ASCII = "0123456789Aa"


def _font_has_glyphs(family, size, bold):
    """用 GDI GetGlyphIndicesW 验证该字体真含中文字形。

    为什么必须用 GDI：
      Tk 的 measure() 对缺字形字符会返回一个"回退宽度"（不是 0），
      所以 measure()>0 完全不能证明字体含该字形——我曾据此误判。
      GetGlyphIndicesW 对**该字体自身**缺失的字符返回 0xFFFF（GGI_MARK_NONEXISTING_GLYPHS），
      这是唯一可靠的判据。

    返回 (ok, detail)。
    """
    try:
        import ctypes
        from ctypes import wintypes
        from tkinter import font as tkfont
        root = tk._default_root

        # 1) 创建字体（Tk 会解析到实际可用的 family，可能发生回退）
        f = tkfont.Font(root=root, family=family, size=size,
                        weight="bold" if bold else "normal")
        # 2) 用 GDI 按**请求的 family**建 LOGFONT，避免 Tk 的回退干扰判断
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        class LOGFONTW(ctypes.Structure):
            _fields_ = [
                ("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
                ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
                ("lfWeight", ctypes.c_long),
                ("lfItalic", ctypes.c_byte), ("lfUnderline", ctypes.c_byte),
                ("lfStrikeOut", ctypes.c_byte), ("lfCharSet", ctypes.c_byte),
                ("lfOutPrecision", ctypes.c_byte), ("lfClipPrecision", ctypes.c_byte),
                ("lfQuality", ctypes.c_byte), ("lfPitchAndFamily", ctypes.c_byte),
                ("lfFaceName", ctypes.c_wchar * 32),
            ]

        lf = LOGFONTW()
        lf.lfHeight = -abs(int(size * 96 / 72)) or -12
        lf.lfWeight = 700 if bold else 400
        lf.lfCharSet = 134        # GB2312_CHARSET
        lf.lfFaceName = family[:31]

        hdc = user32.GetDC(0)
        hf = gdi32.CreateFontIndirectW(ctypes.byref(lf))
        old = gdi32.SelectObject(hdc, hf)
        gdi32.GetGlyphIndicesW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                           ctypes.c_int, ctypes.POINTER(ctypes.c_ushort),
                                           ctypes.c_uint]
        GGI_MARK_NONEXISTING_GLYPHS = 0x0001

        def missing(s):
            n = len(s)
            arr = (ctypes.c_ushort * n)()
            r = gdi32.GetGlyphIndicesW(hdc, s, n, arr, GGI_MARK_NONEXISTING_GLYPHS)
            if r == 0xFFFFFFFF or r == 0:
                return None
            return [arr[i] for i in range(n)].count(0xFFFF)

        try:
            m_cn = missing(_PROBE_CN)
            m_punc = missing(_PROBE_PUNC)
            m_ascii = missing(_PROBE_ASCII)
        finally:
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(hf)
            user32.ReleaseDC(0, hdc)

        if m_cn is None:
            # GDI 不可用 → 退回 measure 启发式（粗但可用）
            w_cn = f.measure(_PROBE_CN)
            return (w_cn > 0), "GDI不可用 measure=%s" % w_cn
        total_missing = (m_cn or 0) + (m_punc or 0) + (m_ascii or 0)
        detail = "miss(cn=%s punc=%s ascii=%s) actual=%s" % (
            m_cn, m_punc, m_ascii, f.actual("family"))
        return (total_missing == 0), detail
    except Exception as e:
        return False, "exc: %s" % e


def _pick_chinese_family(size=9, bold=False):
    """挑一个**实测含中文字形**的字体族名。供 ui_font 与命名字体加固共用。

    返回 (family_name_or_None, diag_list)。None 表示所有候选都不合格。
    """
    from tkinter import font as tkfont
    root = tk._default_root
    try:
        fams = set(tkfont.families(root))
    except Exception:
        fams = set()

    diag = []
    for name in _FONT_CANDIDATES:
        if name not in fams:
            diag.append("%s:(未安装)" % name)
            continue
        ok, detail = _font_has_glyphs(name, size, bold)
        diag.append("%s:%s" % (name, "OK" if ok else detail))
        if ok:
            return name, diag

    # 兜底：枚举所有已装字体
    for name in sorted(fams):
        if name.startswith("@"):
            continue
        ok, _ = _font_has_glyphs(name, size, bold)
        if ok:
            diag.append("兜底命中:%s" % name)
            return name, diag
    return None, diag


def _harden_named_fonts(root):
    """把 Tk 命名字体重定向到实测含中文的字体族。

    覆盖：TkDefaultFont / TkTextFont / TkFixedFont / TkMenuFont /
          TkHeadingFont / TkCaptionFont / TkSmallCaptionFont / TkIconFont /
          TkTooltipFont
    v2 重点：TkDefaultFont 若指向 Tahoma/Segoe UI，中文会变 ♦。
    """
    from tkinter import font as tkfont
    fam, _ = _pick_chinese_family(9, False)
    if not fam:
        return []
    touched = []
    for nf in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
               "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont",
               "TkTooltipFont", "TkFixedFont"):
        try:
            f = tkfont.nametofont(nf, root=root)
            cur = f.actual("family")
            # 只有当当前字体确实缺中文时才改（不动已经正常的）
            ok, _ = _font_has_glyphs(cur, 9, False)
            if not ok:
                f.configure(family=fam)
                touched.append("%s: %s -> %s" % (nf, cur, fam))
        except Exception:
            continue
    return touched


def ui_font(size=9, bold=False):
    """返回本机**确实能渲染中文**的 tkfont.Font 对象。

    与"按名字传参给控件"的本质区别：这里返回的是一个已创建的 Font 对象，
    控件直接持用它，Tk 不再按名字重新解析 → 避免静默回退到无中文字形的字体。

    选择顺序：
      1) 候选中文字体族，逐个用 _font_has_glyphs 实测
      2) 都不行 → 用 GDI 枚举系统已装字体，挑一个中文可用的
      3) 仍不行 → TkDefaultFont（Tk 保证可用）
    """
    from tkinter import font as tkfont
    key = (size, bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    root = tk._default_root

    chosen, diag = _pick_chinese_family(size, bold)

    try:
        if chosen:
            f = tkfont.Font(root=root, family=chosen, size=size,
                            weight="bold" if bold else "normal")
        else:
            f = tkfont.nametofont("TkDefaultFont", root=root).copy()
            f.configure(size=size, weight="bold" if bold else "normal")
    except Exception:
        f = tkfont.nametofont("TkDefaultFont", root=root)

    _FONT_CACHE[key] = f
    try:
        _GLYPH_CACHE[key] = (chosen, "; ".join(diag))
    except Exception:
        pass
    return f


def ui_font_report():
    """返回字体选择的诊断信息（供自检/排查乱码时打印）。"""
    return dict(_GLYPH_CACHE)


class LedgerWindow:
    """积分明细窗口：汇总 + 最近到期 + 筛选 + 事件流列表（含完整状态机）。

    数据全部来自引擎算好的 ledger 载荷；本类只负责展示与交互，
    不自行计算预警状态、不自行汇总「已到期扣减」。
    """

    def __init__(self, master, ledger_map, order, acc_cfg, on_refresh=None, on_close=None):
        self.ledger_map = ledger_map
        self.order = order
        self.acc_cfg = acc_cfg
        self.on_refresh = on_refresh
        self.on_close = on_close

        self.sel_acc = None         # 当前查看的账号 uid（None = 全部合并）
        self.sel_sources = set()    # 来源多选集合（空 = 全部）
        self.sel_direction = DIR_ALL
        self.page = 1
        self.events = []            # 当前筛选后的全集
        self.state = "init"         # init|loading|ok|empty|filter_empty|error
        self.loadmore_state = None  # None|loading|error
        self._last_req = None       # 上次失败请求参数（重试沿用）

        self.win = tk.Toplevel(master)
        self.win.title("积分明细")
        self.win.geometry("820x640")
        self.win.minsize(680, 480)
        self.win.transient(master)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self.refresh_data(ledger_map, order, acc_cfg)

    def _on_close(self):
        """关窗：销毁并通知宿主清空引用，保证下次点入口能开出新窗口。"""
        try:
            self.win.destroy()
        finally:
            cb = getattr(self, "on_close", None)
            if cb:
                cb()

    # ------------------------------------------------------------ 构建 --------

    def _build(self):
        font = ui_font(9)
        self.font = font
        # 需求 3：来源信息用次要样式 —— 灰色、比主文字略小（9 → 8）
        self.font_src = ui_font(8)
        outer = ttk.Frame(self.win, padding=(12, 10, 12, 10))
        outer.pack(fill="both", expand=True)

        # 顶部：可用积分总额 + 累计已到期扣减（后端算好，前端仅展示）
        top = ttk.Frame(outer)
        top.pack(fill="x")
        self.lbl_total = ttk.Label(top, text="可用积分：—", font=ui_font(13, bold=True),
                                   foreground="#1565c0")
        self.lbl_total.pack(side="left")
        self.lbl_expired = ttk.Label(top, text="累计已到期扣减：—", font=font, foreground="#757575")
        self.lbl_expired.pack(side="left", padx=(16, 0))
        self.btn_refresh = ttk.Button(top, text="刷新", command=self._on_refresh)
        self.btn_refresh.pack(side="right")
        self.cmb_acc = ttk.Combobox(top, state="readonly", width=20, font=font)
        self.cmb_acc.pack(side="right", padx=(0, 8))
        self.cmb_acc.bind("<<ComboboxSelected>>", lambda e: self._on_acc_change())

        # 最近到期区（固定区域，可用总额下方）。始终占位于此，仅内容随数据切换：
        # 有临近到期批次 → 展示；无 → 整块隐藏（按需求：隐藏或空态二选一，全站一致）。
        self.frm_expire = tk.Frame(outer, background="#fffde7")
        self.lbl_expire_title = tk.Label(self.frm_expire, text="最近到期积分", anchor="w",
                                         background="#fffde7", foreground="#33691e",
                                         font=ui_font(9, bold=True), padx=10, pady=6)
        self.lbl_expire_title.pack(fill="x")
        self.frm_expire_body = tk.Frame(self.frm_expire, background="#fffde7")
        self.frm_expire_body.pack(fill="x", padx=10, pady=(0, 6))
        self._expire_shown = False
        # 到期区与列表之间的分隔线（仅在到期区可见时显示，避免两组数据视觉混同）
        self.sep_expire = ttk.Separator(outer, orient="horizontal")
        self._sep_shown = False

        # 筛选区
        flt = ttk.Frame(outer, padding=(0, 8, 0, 4))
        flt.pack(fill="x")
        ttk.Label(flt, text="来源：", font=font).pack(side="left")
        self.frm_src = ttk.Frame(flt)
        self.frm_src.pack(side="left")
        ttk.Label(flt, text="收支：", font=font).pack(side="left", padx=(16, 0))
        self.var_dir = tk.StringVar(value=DIR_ALL)
        for val, txt in ((DIR_ALL, "全部"), (DIR_INCOME, "收入"), (DIR_EXPENSE, "支出")):
            ttk.Radiobutton(flt, text=txt, value=val, variable=self.var_dir,
                            command=self._on_filter_change).pack(side="left", padx=(0, 6))
        self.btn_clear = ttk.Button(flt, text="清除筛选", command=self._clear_filter)
        # 仅在「筛选无结果」态显示；默认不 pack

        # 最近到期区：初始不 pack，由 _render_expire 按数据决定显隐（固定位置：筛选区之后）

        # 列表区（ScrollLoadMore）
        listf = ttk.Frame(outer)
        listf.pack(fill="both", expand=True, pady=(4, 0))
        self._list_holder = listf
        self.canvas = tk.Canvas(listf, highlightthickness=0, background="#ffffff")
        sb = ttk.Scrollbar(listf, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.inner_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.win.bind("<Destroy>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # 首屏 / 空 / 异常态 覆盖层（与列表同区切换）
        self.state_frame = ttk.Frame(outer)
        self.lbl_state = ttk.Label(self.state_frame, text="", font=font, foreground="#757575",
                                   justify="center")
        self.lbl_state.pack(pady=30)
        self.btn_retry = ttk.Button(self.state_frame, text="重试", command=self._on_retry)

        # 底部加载更多区
        self.frm_bottom = ttk.Frame(outer)
        self.lbl_bottom = ttk.Label(self.frm_bottom, text="", font=font, foreground="#757575")
        self.lbl_bottom.pack(side="left")
        self.btn_more = ttk.Button(self.frm_bottom, text="重试", command=self._on_retry_more)

    # ------------------------------------------------------------ 数据 --------

    def _active_uids(self):
        disabled = set(self.acc_cfg.get("_disabled") or [])
        return [u for u in self.order
                if u not in disabled and not (self.acc_meta(u).get("disabled"))]

    def acc_meta(self, uid):
        for u, r in self.ledger_map.items():
            if u == uid:
                return {"label": r.get("label")}
        return {}

    def _combined(self):
        """按当前账号选择，合并出 (events, summary, upcoming, alert, sources, errors)。"""
        uids = self._active_uids() if self.sel_acc in (None, "__all__") else [self.sel_acc]
        events, sources = [], {}
        available = 0
        expired = 0
        upcoming = []
        alert = None
        errs = 0
        oks = 0
        for u in uids:
            r = self.ledger_map.get(u)
            if not isinstance(r, dict):
                continue
            if not r.get("ok"):
                errs += 1
                continue
            oks += 1
            sm = r.get("summary") or {}
            available += int(sm.get("available") or 0)
            expired += int(sm.get("expiredDeducted") or 0)
            events.extend(r.get("events") or [])
            upcoming.extend((r.get("upcoming") or {}).get("batches") or [])
            for s in (r.get("sources") or []):
                cur = sources.setdefault(s["name"], {"name": s["name"], "count": 0, "amount": 0.0})
                cur["count"] += s.get("count") or 0
                cur["amount"] = round(cur["amount"] + float(s.get("amount") or 0), 2)
            if r.get("alert") and r["alert"].get("show"):
                alert = alert or r["alert"]
        events.sort(key=lambda e: (-(e.get("timeMs") or 0), e.get("id") or ""))
        upcoming.sort(key=lambda x: (x.get("expireAt") or "9999", x.get("id") or ""))
        return events, available, expired, upcoming, alert, sources, oks, errs

    def refresh_data(self, ledger_map, order, acc_cfg):
        self.ledger_map = ledger_map
        self.order = order or self.order
        self.acc_cfg = acc_cfg or self.acc_cfg
        self._sync_acc_combo()
        self._rebuild_sources()
        self._apply(reload=True)

    def _sync_acc_combo(self):
        uids = self._active_uids()
        names = ["全部账号"] + [(self.ledger_map.get(u) or {}).get("label") or u[:8] for u in uids]
        self._combo_uids = [None] + uids
        self.cmb_acc["values"] = names
        idx = 0
        if self.sel_acc in uids:
            idx = uids.index(self.sel_acc) + 1
        self.cmb_acc.current(idx)

    def _on_acc_change(self):
        i = self.cmb_acc.current()
        self.sel_acc = self._combo_uids[i] if 0 <= i < len(self._combo_uids) else None
        self._rebuild_sources()
        self._apply(reload=True)

    def _rebuild_sources(self):
        for w in self.frm_src.winfo_children():
            w.destroy()
        _, _, _, _, _, sources, _, _ = self._combined()
        self._src_vars = {}
        for name in sorted(sources.keys()):
            var = tk.BooleanVar(value=name in self.sel_sources)
            self._src_vars[name] = var
            ttk.Checkbutton(self.frm_src, text=name, variable=var,
                            command=self._on_filter_change).pack(side="left", padx=(0, 6))

    # -------------------------------------------------------- 筛选/状态 --------

    def _on_filter_change(self):
        self.sel_sources = set(n for n, v in getattr(self, "_src_vars", {}).items() if v.get())
        self.sel_direction = self.var_dir.get()
        self._apply(reload=True)

    def _clear_filter(self):
        self.sel_sources = set()
        for v in getattr(self, "_src_vars", {}).values():
            v.set(False)
        self.var_dir.set(DIR_ALL)
        self._apply(reload=True)

    def _filtered(self):
        ev = self.events
        if self.sel_sources:
            ev = [e for e in ev if e.get("source") in self.sel_sources]
        if self.sel_direction != DIR_ALL:
            ev = [e for e in ev if e.get("direction") == self.sel_direction]
        return ev

    def _apply(self, reload=False):
        if reload:
            self.page = 1
            self.loadmore_state = None
        all_ev, available, expired, upcoming, alert, sources, oks, errs = self._combined()

        # 账号级异常：全部失败 → 请求异常态；部分失败 → 仍展示已到的
        if oks == 0 and errs > 0:
            self.events = []
            self._render_summary(None, None)
            self._render_expire([])
            self._set_state("error")
            return
        if not self.ledger_map or (oks == 0 and errs == 0):
            self.events = []
            self._render_summary(None, None)
            self._render_expire([])
            self._set_state("loading")
            return

        self.events = all_ev
        self._render_summary(available, expired)

        has_filter = bool(self.sel_sources) or self.sel_direction != DIR_ALL
        if not all_ev:
            self._set_state("filter_empty" if has_filter else "empty")
            self._render_expire(upcoming)
            return
        shown = self._filtered()
        if not shown:
            self._set_state("filter_empty")
            self._render_expire(upcoming)
            return
        self._set_state("ok")
        self._render_expire(upcoming)
        self._render_rows(shown)

    def _set_state(self, state):
        self.state = state
        # 列表容器：统一管理（canvas + 滚动条同属 listf）
        self._list_holder.pack_forget()
        self.state_frame.pack_forget()
        for w in self.state_frame.winfo_children():
            w.pack_forget()
        if state == "ok":
            self.frm_bottom.pack(fill="x", pady=(4, 0))
            self._list_holder.pack(fill="both", expand=True, pady=(4, 0), before=self.frm_bottom)
        elif state in ("loading", "empty", "filter_empty", "error"):
            text, color = {
                "loading": (LEDGER_LOADING_TEXT, "#757575"),
                "empty": (LEDGER_EMPTY_TEXT, "#757575"),
                "filter_empty": (LEDGER_FILTER_EMPTY_TEXT, "#757575"),
                "error": (LEDGER_ERROR_TEXT, "#c62828"),
            }[state]
            self.lbl_state.configure(text=text, foreground=color)
            self.state_frame.pack(fill="both", expand=True)
            self.lbl_state.pack(pady=(30, 6))
            if state == "filter_empty":
                self.btn_clear.pack()
            elif state == "error":
                self.btn_retry.pack()

    def _render_summary(self, available, expired):
        if available is None:
            self.lbl_total.configure(text="可用积分：—")
            self.lbl_expired.configure(text="累计已到期扣减：—")
        else:
            self.lbl_total.configure(text="可用积分：%d" % available)
            self.lbl_expired.configure(text="累计已到期扣减：%d" % expired)

    def _render_expire(self, batches):
        """最近到期区：按到期时间由近及远；无数据时隐藏该区域且不影响其余功能。"""
        for w in self.frm_expire_body.winfo_children():
            w.destroy()
        if not batches:
            if self._expire_shown:
                self.frm_expire.pack_forget()
                self._expire_shown = False
            if self._sep_shown:
                self.sep_expire.pack_forget()
                self._sep_shown = False
            return
        if not self._expire_shown:
            # 参照窗口必须先已 pack，否则 Tk 抛 "isn't packed"。
            # 该区固定排在筛选区之后、列表区（或底部栏）之前。
            ref = self._list_holder if self._list_holder.winfo_manager() else None
            if ref is None and self.frm_bottom.winfo_manager():
                ref = self.frm_bottom
            if ref is not None:
                self.sep_expire.pack(fill="x", pady=(4, 0), before=ref)
            else:
                self.sep_expire.pack(fill="x", pady=(4, 0))
            self.frm_expire.pack(fill="x", pady=(6, 0), before=self.sep_expire)
            self._expire_shown = True
            self._sep_shown = True
        f = self.font
        # 按到期日聚合：同一到期日的多批合并成一行（显示该日合计与批数），
        # 再按到期日由近及远排列。这样既符合「多批按近→远」，也不会因批次
        # 过多而淹没首屏（每行自带「N 批」，信息不丢）。
        groups = []
        for b in batches:
            d = b.get("expireDate") or ""
            if groups and groups[-1]["date"] == d:
                groups[-1]["amount"] += b.get("amount") or 0
                groups[-1]["n"] += 1
                lv = b.get("warnLevel") or "none"
                if _lvl_rank(lv) > _lvl_rank(groups[-1]["level"]):
                    groups[-1]["level"] = lv
                dl = b.get("daysLeft")
                if dl is not None and (groups[-1]["daysLeft"] is None or dl < groups[-1]["daysLeft"]):
                    groups[-1]["daysLeft"] = dl
                cr = (b.get("created") or "")[:10]
                if cr and cr < groups[-1]["created"]:
                    groups[-1]["created"] = cr
            else:
                groups.append({"date": d, "amount": b.get("amount") or 0, "n": 1,
                               "level": b.get("warnLevel") or "none",
                               "daysLeft": b.get("daysLeft"),
                               "created": (b.get("created") or "")[:10]})
        shown_groups = groups[:EXPIRE_MAX_ROWS]
        for g in shown_groups:
            row = tk.Frame(self.frm_expire_body, background="#fffde7")
            row.pack(fill="x", pady=1)
            color = {"danger": "#c62828", "warning": "#ef6c00", "notice": "#f9a825"}.get(g["level"], "#558b2f")
            tag = ""
            d = g.get("daysLeft")
            if g["level"] in ("danger", "warning") and d is not None:
                tag = "  ⚠ %d 天内到期" % d if d > 0 else "  ⚠ 即将到期"
            tk.Label(row, text="•", background="#fffde7", foreground=color).pack(side="left")
            txt = " %.2f 积分" % g["amount"] if g["n"] > 1 else " %s 积分" % g["amount"]
            tk.Label(row, text=txt, background="#fffde7", font=ui_font(9, bold=True),
                     foreground=color).pack(side="left")
            if g["n"] > 1:
                tk.Label(row, text="(%d 批)" % g["n"], background="#fffde7", font=f,
                         foreground="#9e9d24").pack(side="left", padx=(4, 0))
            tk.Label(row, text="｜到期 %s" % g["date"], background="#fffde7", font=f,
                     foreground="#33691e").pack(side="left", padx=(6, 0))
            if g.get("created"):
                tk.Label(row, text="获得 %s" % g["created"], background="#fffde7", font=f,
                         foreground="#558b2f").pack(side="left", padx=(6, 0))
            if tag:
                tk.Label(row, text=tag, background="#fffde7", font=ui_font(9, bold=True),
                         foreground=color).pack(side="left")
        if len(groups) > len(shown_groups):
            rest = groups[len(shown_groups):]
            more = sum(x["n"] for x in rest)
            tk.Label(self.frm_expire_body, text="…其余 %d 批更晚到期（合计 %.2f 积分）" % (
                more, sum(x["amount"] for x in rest)),
                background="#fffde7", font=f, foreground="#827717", anchor="w").pack(fill="x", pady=(2, 0))

    def _render_rows(self, shown):
        for w in self.inner.winfo_children():
            w.destroy()
        end = min(len(shown), self.page * PAGE_SIZE)
        f = self.font
        for e in shown[:end]:
            self._row(e, f)
        # 底部状态
        if end < len(shown):
            if self.loadmore_state == "loading":
                self.lbl_bottom.configure(text=LEDGER_LOADING_TEXT, foreground="#757575")
                self.btn_more.pack_forget()
            elif self.loadmore_state == "error":
                self.lbl_bottom.configure(text=LEDGER_LOADMORE_ERROR_TEXT, foreground="#c62828")
                self.btn_more.pack(side="left", padx=(8, 0))
            else:
                self.lbl_bottom.configure(text="滚动加载更多…", foreground="#9e9e9e")
                self.btn_more.pack_forget()
        else:
            self.lbl_bottom.configure(text="已全部加载（%d 条）" % len(shown) if shown else "",
                                      foreground="#9e9e9e")
            self.btn_more.pack_forget()
        self._shown_end = end

    def _row(self, e, f):
        inc = e.get("direction") == "income"
        row = ttk.Frame(self.inner)
        row.pack(fill="x", pady=1)
        amt = e.get("amount") or 0
        txt = ("+%g" % amt) if inc else ("-%g" % amt)
        col = "#2e7d32" if inc else "#c62828"
        tk.Label(row, text=txt, font=ui_font(9, bold=True), foreground=col, width=10,
                 anchor="e").pack(side="left")
        tk.Label(row, text=e.get("source") or "", font=f, width=18, anchor="w").pack(side="left", padx=(8, 0))
        tk.Label(row, text=(e.get("time") or "").replace("T", " ")[:19], font=f,
                 foreground="#616161", width=20, anchor="w").pack(side="left")
        note = ""
        if inc and e.get("kind") == "grant":
            used = e.get("used") or 0
            if used > 0:
                note = "（已用 %g/%g）" % (used, e.get("total") or 0)
            elif e.get("expireAt"):
                note = "｜到期 %s" % (e.get("expireAt") or "")[:10]
        elif e.get("kind") == "consume":
            note = "（消耗）"
        elif e.get("kind") == "expire":
            note = "（到期作废）"
        if note:
            tk.Label(row, text=note, font=f, foreground="#9e9e9e").pack(side="left", padx=(6, 0))
        # 需求 1：来源账号接在「描述文字之后」；需求 3：次要样式（灰、略小）
        self._pack_source(row, e)

    # ---- 来源账号标注（需求 1~6）----

    def _source_name(self, e):
        """取该条事件的来源账号显示名。

        需求 4：无归属账号 → 「系统」。
        需求 6：缺失来源数据 → 返回 None（该条目不显示来源字段）。
        """
        if not isinstance(e, dict):
            return None
        label = e.get("label")
        if label is None:
            label = ""
        label = str(label).strip()
        if label:
            return label
        # 有归属（uid 存在）但 label 缺失 → 按需求 4 归为系统；无 uid → 同样系统
        return SRC_SYSTEM

    def _pack_source(self, row, e):
        """在条目行右侧追加「来源：账号名」。

        截断分两层（需求 5）：
          A. 固定阈值：超 7 汉字 / 15 英文字符 → 必截断（与容器宽度无关）
          B. 动态宽度：量出实际可用像素，若放不下则按「动态宽度优先」进一步收窄
        """
        name = self._source_name(e)
        if name is None:
            return

        # A. 先做固定阈值截断：保证任何情况下都不会超过需求上限
        base = _truncate_name(name)
        fixed_truncated = base != name

        # B. 再量实际可用宽度，按动态宽度收窄（优先级高于固定阈值）
        avail = None
        try:
            row.update_idletasks()
            used = 0
            for w in row.winfo_children():
                if w is row:
                    continue
                used += w.winfo_reqwidth()
            w_row = row.winfo_width()
            if w_row <= 1:
                # 行还没被布局（宽度未定）→ 用父容器宽度估算
                w_row = self.inner.winfo_width()
            if w_row > 1:
                avail = w_row - used - 24              # 留 24px 呼吸位
        except Exception:
            avail = None

        # 用「已按固定阈值截断后的名字」作为动态收窄的起点
        text, dyn_trunc = _fit_source_text(base, avail, self.font_src)
        if not text:
            return                                      # 需求 5 最终兜底：不展示
        lbl = tk.Label(row, text=text, font=self.font_src,
                       foreground="#9e9e9e", anchor="e")
        lbl.pack(side="right")
        # 需求 5：除最终兜底外，截断前的完整名称支持悬浮提示
        if fixed_truncated or dyn_trunc:
            self._attach_tip(lbl, "来源：%s" % name)

    def _attach_tip(self, widget, full):
        """轻量悬浮提示（不依赖第三方库）。"""
        tip = {"win": None}

        def show(_e=None):
            if tip["win"] is not None:
                return
            try:
                t = tk.Toplevel(widget)
                t.wm_overrideredirect(True)
                t.attributes("-topmost", True)
                tk.Label(t, text=full, font=self.font_src, background="#fafafa",
                         foreground="#424242", relief="solid", borderwidth=1,
                         padx=6, pady=2).pack()
                x = widget.winfo_rootx() + 8
                y = widget.winfo_rooty() + widget.winfo_height() + 2
                t.wm_geometry("+%d+%d" % (x, y))
                tip["win"] = t
            except Exception:
                tip["win"] = None

        def hide(_e=None):
            if tip["win"] is not None:
                try:
                    tip["win"].destroy()
                except Exception:
                    pass
                tip["win"] = None

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")

    # -------------------------------------------------------- 滚动/加载 --------

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass
        self._maybe_loadmore()

    def _maybe_loadmore(self):
        if self.state != "ok" or self.loadmore_state == "loading":
            return
        shown = self._filtered()
        if getattr(self, "_shown_end", 0) >= len(shown):
            return
        try:
            first, last = self.canvas.yview()
        except Exception:
            return
        if last >= 0.995:
            self.loadmore_state = "loading"
            self._render_rows(shown)
            # 本地翻页（数据已全量在手），模拟异步避免卡顿
            self.win.after(120, lambda: self._finish_loadmore(shown))

    def _finish_loadmore(self, shown):
        self.loadmore_state = None
        self.page += 1
        self._render_rows(shown)

    def _on_retry(self):
        self.state = "loading"
        self._set_state("loading")
        if self.on_refresh:
            self.on_refresh()

    def _on_retry_more(self):
        self.loadmore_state = None
        self._render_rows(self._filtered())

    def _on_refresh(self):
        self.state = "loading"
        self._set_state("loading")
        if self.on_refresh:
            self.on_refresh()


# ------------------------------------------------------------------ 面板 ------

class Panel:
    def __init__(self, auto=False, selftest=False):
        self.auto = auto
        self.selftest = selftest

        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(load_json(SETTINGS_FILE, {}) or {})
        self.acc_cfg = load_json(ACC_CFG, {}) or {}
        self.acc_meta = {}          # uid -> list-json 元数据（label/mode/days/disabled）
        self.acc_runtime = {}       # uid -> 最近一轮运行结果（checked/streak/total/ok）
        self.acc_order = []         # 渲染顺序
        self.job = None             # 当前后台任务种类
        self.job_proc = None
        self.next_list = False      # 任务结束后自动刷新账号列表
        self.pending_signin = False # 列表拉完后是否接着自动补签
        self.queue = queue.Queue()

        self.bal = {}               # uid -> 最近一次余额查询结果
        self.bal_job = False        # 余额查询线程是否在跑（独立于 self.job）
        self.bal_ts = 0.0           # 上次发起查询的时间（切换前台节流用）
        self.bal_first = True       # 首次列表到齐后自动查一次余额

        self.ledger = {}            # uid -> 最近一次积分明细结果（供顶部到期提醒条）
        self.ledger_job = False     # 明细查询线程是否在跑
        self.ledger_ts = 0.0        # 上次发起明细查询的时间（节流用）
        self.ledger_first = True    # 首次列表到齐后自动查一次明细
        self._ledger_win = None     # 明细窗口（单例）

        self._build_ui()
        self._sync_daily_task_ui()
        self.after_polls()

        # 开窗先拉一次账号列表（表格立即有内容）；拉完再按需自动补签
        # （两个任务不能并跑，用 next_signin 串起来）；自检模式除外
        if not self.selftest:
            if self.auto or self.settings.get("auto_signin_on_open"):
                self.pending_signin = True
            self.root.after(150, lambda: self.start_job("list", ["--list-json"], quiet=True))

    # ------------------------------------------------------------- UI --------

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("WorkBuddy 签到面板")
        self.root.geometry(self.settings.get("geometry") or "860x620")
        self.root.minsize(720, 480)
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # 把 Tk 的命名字体也重定向到**实测含中文**的字体族。
        # 必要性：菜单、messagebox、ttk 默认样式等拿不到控件的字体由 Tk 按名字解析，
        # 若 TkDefaultFont 正好落在 Tahoma / Segoe UI 这类无中文字形的字体上，
        # 中文就会渲染成 ♦（缺字形），而我无法逐个控件覆盖。
        try:
            _harden_named_fonts(self.root)
        except Exception:
            pass

        font_lab = ui_font(9)

        # 顶部工具栏
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        self.btn_sign = ttk.Button(bar, text="立即签到", command=self.do_signin)
        self.btn_sign.pack(side="left")
        self.btn_status = ttk.Button(bar, text="刷新状态", command=self.do_status)
        self.btn_status.pack(side="left", padx=(8, 0))
        self.btn_add = ttk.Button(bar, text="添加账号", command=self.do_add_account)
        self.btn_add.pack(side="left", padx=(8, 0))
        self.btn_ledger = ttk.Button(bar, text="积分明细", command=self.open_ledger)
        self.btn_ledger.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开日志文件", command=self.open_logfile).pack(side="right")
        ttk.Button(bar, text="创建桌面快捷方式", command=self.do_create_shortcut).pack(side="right", padx=(0, 8))

        # 可用积分（固定位置：工具栏右侧，启动即可见、零点击；加载/失败态可点击重试）
        self.bal_var = tk.StringVar(value="可用积分：—")
        self.lbl_bal = ttk.Label(bar, textvariable=self.bal_var, font=font_lab,
                                 foreground="#1565c0", cursor="hand2")
        self.lbl_bal.pack(side="right", padx=(0, 14))
        self.lbl_bal.bind("<Button-1>", lambda e: self.do_query_balance(manual=True))

        # 到期提醒条（存在临近到期积分时才显示；文案由引擎算好，面板不自行判断）
        # 布局：左「文案区（可点击跳转）」+ 右「× 关闭按钮」，两块控件级隔离，
        # 关闭不影响跳转。文字区用 side=left+expand 吃掉剩余宽度，× 固定最右。
        self.alert_var = tk.StringVar(value="")
        self.frm_alert = tk.Frame(self.root, background="#fff8e1")
        self.lbl_alert = tk.Label(self.frm_alert, textvariable=self.alert_var,
                                  background="#fff8e1", foreground="#e65100",
                                  font=ui_font(9, bold=True), anchor="w",
                                  cursor="hand2", padx=10, pady=5)
        self.lbl_alert.pack(side="left", fill="x", expand=True)
        self.lbl_alert.bind("<Button-1>", lambda e: self.open_ledger())
        self.btn_alert_close = tk.Label(self.frm_alert, text="×",
                                        background="#fff8e1", foreground="#e65100",
                                        font=ui_font(11, bold=True),
                                        cursor="hand2", padx=10, pady=3)
        self.btn_alert_close.pack(side="right")
        self.btn_alert_close.bind("<Button-1>", lambda e: self._dismiss_alert())
        # 悬停反馈（可选，纯视觉）
        self.btn_alert_close.bind("<Enter>", lambda e: self.btn_alert_close.configure(
            background="#ffecb3"))
        self.btn_alert_close.bind("<Leave>", lambda e: self.btn_alert_close.configure(
            background="#fff8e1"))
        self._alert_shown = False   # 提醒条默认不占位，有临近到期数据才显示
        self._alert_dismissed_key = None   # 本会话内被关闭时的条幅指纹；数据变化则失效重现

        # 设置行
        opt = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        self.opt_frame = opt
        opt.pack(fill="x")
        self.var_onopen = tk.BooleanVar(value=bool(self.settings.get("auto_signin_on_open")))
        ttk.Checkbutton(opt, text="打开时自动补签", variable=self.var_onopen,
                        command=self._save_settings_from_ui).pack(side="left")

        ttk.Separator(opt, orient="vertical").pack(side="left", fill="y", padx=10)

        self.var_daily = tk.BooleanVar(value=bool(self.settings.get("daily_launch_enabled")))
        ttk.Checkbutton(opt, text="每天定时自动启动", variable=self.var_daily,
                        command=self._on_toggle_daily).pack(side="left")
        self.var_time = tk.StringVar(value=self.settings.get("daily_launch_time", "09:00"))
        self.entry_time = ttk.Entry(opt, textvariable=self.var_time, width=6, font=font_lab)
        self.entry_time.pack(side="left", padx=(6, 0))
        self.entry_time.bind("<FocusOut>", lambda e: self._on_time_changed())
        self.entry_time.bind("<Return>", lambda e: self._on_time_changed())

        self.var_close = tk.BooleanVar(value=bool(self.settings.get("close_after_auto")))
        ttk.Checkbutton(opt, text="定时启动签完自动关窗", variable=self.var_close,
                        command=self._save_settings_from_ui).pack(side="left", padx=(14, 0))

        # 账号表格
        mid = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        mid.pack(fill="x")
        cols = ("label", "mode", "today", "streak", "total", "cred")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=6)
        for c, text, w, anchor in (
            ("label", "账号", 150, "w"),
            ("mode", "来源", 130, "w"),
            ("today", "今日", 70, "center"),
            ("streak", "连签(天)", 70, "center"),
            ("total", "累计积分", 80, "center"),
            ("cred", "凭据剩余", 90, "center"),
        ):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor)
        self.tree.pack(fill="x")

        self.tree.tag_configure("disabled", foreground="#999999")
        self.tree.tag_configure("err", foreground="#c62828")

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="重命名", command=self.do_rename)
        self.menu.add_command(label="停用 / 启用", command=self.do_toggle_disable)
        self.menu.add_separator()
        self.menu.add_command(label="移出列表（含删除本地缓存）", command=self.do_remove)
        self.tree.bind("<Button-3>", self._popup_menu)

        # 日志区
        logf = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        logf.pack(fill="both", expand=True)
        self.txt = tk.Text(logf, wrap="word", state="disabled", font=font_lab,
                           background="#fafafa", relief="solid", borderwidth=1)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("ok", foreground="#2e7d32")
        self.txt.tag_configure("err", foreground="#c62828")
        self.txt.tag_configure("note", foreground="#ef6c00")
        self.txt.tag_configure("dim", foreground="#9e9e9e")
        self.txt.tag_configure("info", foreground="#1565c0")

        # 状态栏
        self.status = tk.StringVar(value="就绪")
        sf = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        sf.pack(fill="x")
        ttk.Label(sf, textvariable=self.status, font=font_lab, foreground="#555555").pack(side="left")

        self._load_history_log()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<FocusIn>", self._on_focus)

    def _load_history_log(self):
        """加载历史日志：里层 JSON 折叠成一句人话，避免铺满原始报文。"""
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-120:]
        except Exception:
            self._log("（暂无历史日志）", "dim")
            return
        self._log("──── 历史日志（最近 %d 行，来自 signin.log）────" % len(lines), "dim")
        for ln in lines:
            self._log(self._tidy_log_line(ln.rstrip()), "dim")
        self._log("──── 以下为本次运行 ────", "dim")

    @staticmethod
    def _tidy_log_line(line):
        """把日志行里的长 JSON 折成人话。

        不整体 json.loads —— signin.log 里的报文可能被截断，解析必失败；
        直接用正则从文本里抽关键字段，健壮得多。
        """
        if '"step"' not in line and '"body"' not in line:
            return line
        head = line.split("{", 1)[0].rstrip()
        bits = []

        m = re.search(r'"http"\s*:\s*(\d+)', line)
        if m:
            bits.append("http=%s" % m.group(1))
        m = re.search(r'"today_checked_in"\s*:\s*(true|false)', line)
        if m:
            bits.append("今日%s" % ("已签" if m.group(1) == "true" else "未签"))
        m = re.search(r'"streak_days"\s*:\s*(\d+)', line)
        if m:
            bits.append("连签%s天" % m.group(1))
        m = re.search(r'"(?:total_credits|daily_credit|today_credit)"\s*:\s*(\d+)', line)
        if m:
            bits.append("积分%s" % m.group(1))
        m = re.search(r'"msg"\s*:\s*"([^"]*)"', line)
        if m and m.group(1) and m.group(1) != "OK":
            bits.append(m.group(1))

        body = "  [" + " · ".join(bits) + "]" if bits else "  [报文略]"
        return head + body

    def _log(self, text, tag=None):
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag or ())
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > 1500:
            self.txt.delete("1.0", "%d.0" % (lines - 1200))
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ------------------------------------------------------- 设置与任务 --------

    def _save_settings_from_ui(self):
        self.settings["auto_signin_on_open"] = bool(self.var_onopen.get())
        self.settings["close_after_auto"] = bool(self.var_close.get())
        self.settings["geometry"] = self.root.geometry()
        save_json_atomic(SETTINGS_FILE, self.settings)

    def _on_time_changed(self):
        t = valid_hhmm(self.var_time.get())
        if t is None:
            messagebox.showwarning("时间格式", "请输入 HH:MM 格式，例如 09:00", parent=self.root)
            self.var_time.set(self.settings.get("daily_launch_time", "09:00"))
            return
        self.var_time.set(t)
        self.settings["daily_launch_time"] = t
        self._save_settings_from_ui()
        if self.var_daily.get():
            self._apply_daily_task(True)

    def _sync_daily_task_ui(self):
        st = task_state()
        enabled = bool(st)
        self.var_daily.set(enabled)
        self.settings["daily_launch_enabled"] = enabled
        save_json_atomic(SETTINGS_FILE, self.settings)

    def _on_toggle_daily(self):
        want = bool(self.var_daily.get())
        if want:
            t = valid_hhmm(self.var_time.get())
            if t is None:
                self.var_daily.set(False)
                messagebox.showwarning("时间格式", "请先填写正确的时间（HH:MM）", parent=self.root)
                return
            self.var_time.set(t)
            self.settings["daily_launch_time"] = t
        self._apply_daily_task(want)

    def _apply_daily_task(self, enable):
        self.status.set("正在%s定时任务…" % ("注册" if enable else "移除"))
        self.root.update_idletasks()
        if enable:
            ok, detail = register_daily_task(self.settings.get("daily_launch_time", "09:00"))
        else:
            ok, detail = unregister_daily_task()
        st = task_state()
        really = bool(st) if enable else (not st)
        if not (ok and really):
            self.var_daily.set(not enable)
            messagebox.showerror("定时任务", "操作失败：\n%s" % (detail or "未知错误"), parent=self.root)
            self.status.set("定时任务操作失败")
            return
        self.settings["daily_launch_enabled"] = enable
        self._save_settings_from_ui()
        if enable:
            self._log("[%s] 已设置每天 %s 自动启动面板" % (now_str(), self.settings["daily_launch_time"]), "info")
            self.status.set("已开启：每天 %s 自动启动" % self.settings["daily_launch_time"])
        else:
            self._log("[%s] 已取消每天自动启动" % now_str(), "info")
            self.status.set("已关闭每天自动启动")

    # --------------------------------------------------------- 后台任务 --------

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_sign, self.btn_status, self.btn_add):
            b.configure(state=state)

    def start_job(self, kind, args, quiet=False):
        if self.job:
            self._log("[%s] 已有任务在运行，等它结束" % now_str(), "note")
            return
        self.job = kind
        if not quiet:
            self._log("[%s] 开始：%s" % (now_str(), {"signin": "立即签到", "status": "刷新状态", "list": "读取账号"}[kind]), "info")
        self._set_busy(True)
        self.status.set("运行中…")
        th = threading.Thread(target=self._worker, args=(list(args),), daemon=True)
        th.start()

    def _worker(self, args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.Popen(
                [sys.executable, ENGINE] + args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, cwd=HERE, creationflags=NO_WINDOW)
        except Exception as exc:
            self.queue.put(("raw", "启动引擎失败：%s" % exc))
            self.queue.put(("exit", -1))
            return
        self.job_proc = proc
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    self.queue.put(("raw", line))
                    continue
                self.queue.put(("json", obj))
        finally:
            rc = proc.wait()
            self.job_proc = None
            self.queue.put(("exit", rc))

    def after_polls(self):
        self.root.after(120, self._poll)

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "json":
                    self._handle_json(payload)
                elif kind == "raw":
                    self._log("  %s" % payload, "dim")
                elif kind == "exit":
                    self._handle_exit(payload)
                elif kind == "balance_end":
                    self._handle_balance_end(payload)
                elif kind == "ledger_end":
                    self._handle_ledger_end(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _handle_exit(self, rc):
        self.job = None
        self._set_busy(False)
        if self.pending_signin:
            self.pending_signin = False
            self.start_job("signin", ["--json-lines"])
            return
        if self.next_list:
            self.next_list = False
            self.start_job("list", ["--list-json"], quiet=True)

    def _handle_json(self, obj):
        if isinstance(obj.get("accounts"), list):
            self.acc_meta = {}
            self.acc_order = []
            for a in obj["accounts"]:
                uid = a.get("uid")
                if not uid:
                    continue
                self.acc_meta[uid] = a
                self.acc_order.append(uid)
            self._render_accounts()
            self._render_balance()
            self.status.set("共 %d 个账号" % len(self.acc_order))
            if self.bal_first and not self.selftest:
                # 冷启动：拿到列表后自动检测一次可用积分
                self.bal_first = False
                self.do_query_balance()
            if self.ledger_first and not self.selftest:
                # 冷启动：顺手取一次明细（供顶部到期提醒条）
                self.ledger_first = False
                self.do_query_ledger()
            return

        ev = obj.get("event")
        if ev == "ledger":
            uid = obj.get("uid") or ""
            if uid:
                self.ledger[uid] = obj
            label = obj.get("label") or uid[:8]
            if obj.get("ok"):
                sm = obj.get("summary") or {}
                self._log("[%s] 明细 %s：可用 %s · 已到期扣减 %s · 事件 %d 条" % (
                    now_str(), label, sm.get("available"), sm.get("expiredDeducted"),
                    len(obj.get("events") or [])), "info")
            else:
                self._log("[%s] 明细 %s：读取失败（%s）" % (
                    now_str(), label, obj.get("report") or obj.get("error")), "note")
            self._render_alert()
            self._push_ledger_to_window()
            return

        if ev == "balance":
            uid = obj.get("uid") or ""
            if uid:
                self.bal[uid] = obj
            label = obj.get("label") or uid[:8]
            if obj.get("ok"):
                self._log("[%s] 余额 %s：可用 %s（含赠送 %s）" % (
                    now_str(), label, obj.get("balance"), obj.get("gift")), "info")
            else:
                self._log("[%s] 余额 %s：读取失败（%s）" % (
                    now_str(), label, obj.get("report") or obj.get("error")), "note")
            self._render_balance()
            return

        if ev == "account":
            uid = obj.get("uid") or ""
            r = self.acc_runtime.setdefault(uid, {})
            for k in ("checked_today", "streak_days", "total_credits", "today_credit"):
                if k in obj:
                    r[k] = obj[k]
            r["ok"] = obj.get("ok")
            label = obj.get("label") or uid[:8]
            ok = bool(obj.get("ok"))
            line = "[%s] %-8s %s  %s" % (obj.get("stamp", now_str()), label,
                                         "OK " if ok else "ERR", obj.get("summary", ""))
            self._log(line, "ok" if ok else "err")
            for n in (obj.get("notes") or []):
                self._log("    " + str(n), "note")
            self._render_accounts()
        elif ev == "done":
            all_ok = bool(obj.get("all_ok"))
            self._log("[%s] 完成：%s（%s 个账号）" % (obj.get("stamp", now_str()),
                       "全部成功" if all_ok else "有失败项", obj.get("count", "?")),
                      "info" if all_ok else "err")
            if self.job == "signin" or self.job == "status":
                self.next_list = True
            if self.auto and self.settings.get("close_after_auto") and self.job == "signin":
                self._countdown_close(5)

    def _countdown_close(self, secs):
        if secs <= 0:
            self.root.destroy()
            return
        self.status.set("%d 秒后自动关闭…" % secs)
        self.root.after(1000, lambda: self._countdown_close(secs - 1))

    def _render_accounts(self):
        sel = self.tree.selection()
        keep = sel[0] if sel else None
        self.tree.delete(*self.tree.get_children())
        disabled = set(self.acc_cfg.get("_disabled") or [])
        for uid in self.acc_order:
            a = self.acc_meta.get(uid) or {}
            r = self.acc_runtime.get(uid) or {}
            label = a.get("label") or uid[:8]
            is_dis = uid in disabled or a.get("disabled")
            mode = MODE_TEXT.get(a.get("mode"), a.get("mode") or "—")
            if is_dis:
                label = label + "（已停用）"
                mode = "已停用"
            checked = r.get("checked_today")
            today = "已签" if checked is True else ("未签" if checked is False else "—")
            streak = r.get("streak_days")
            total = r.get("total_credits")
            cred = a.get("refreshDays")
            cred_s = ("%.0f 天" % cred) if isinstance(cred, (int, float)) and cred > 0 else "—"
            tag = ("disabled",) if is_dis else (("err",) if r.get("ok") is False else ())
            self.tree.insert("", "end", iid=uid, tags=tag, values=(
                label, mode, today,
                str(streak) if streak is not None else "—",
                str(total) if total is not None else "—",
                cred_s))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    # -------------------------------------------------------- 可用积分 --------

    def _set_balance_text(self, text, fg):
        self.bal_var.set(text)
        try:
            self.lbl_bal.configure(foreground=fg)
        except Exception:
            pass

    def _handle_balance_end(self, rc):
        self.bal_job = False
        if not self.bal:
            if not self.acc_order:
                self._set_balance_text("可用积分：—", "#9e9e9e")
            else:
                # 一行结果都没拿到（进程起不来/超时等）→ 明确失败态
                self._set_balance_text("可用积分：读取失败（点此重试）", "#c62828")
        else:
            self._render_balance()

    def _on_focus(self, event=None):
        """切回前台自动检测（节流在 do_query_balance 内部：60s 内不重复）。"""
        self.do_query_balance()
        self.do_query_ledger()

    def _render_balance(self):
        """把各账号余额汇总渲染到工具栏标签。

        多账号显示合计（含赠送小计），单账号时即该账号自身；禁用账号不计入。
        失败按类型给明确提示，标签可点击重试。数据未到齐时显示加载态。
        """
        disabled = set(self.acc_cfg.get("_disabled") or [])
        enabled = [u for u in self.acc_order
                   if u not in disabled and not (self.acc_meta.get(u) or {}).get("disabled")]
        have = [r for r in (self.bal.get(u) for u in enabled) if isinstance(r, dict)]
        missing = [u for u in enabled if not isinstance(self.bal.get(u), dict)]
        oks = [r for r in have if r.get("ok")]
        fails = [r for r in have if not r.get("ok")]

        if not enabled:
            self._set_balance_text("可用积分：—", "#9e9e9e")
            return
        if not oks and not fails:
            self._set_balance_text(
                "可用积分：读取中…" if self.bal_job else "可用积分：—", "#9e9e9e")
            return
        if not oks:
            err = fails[0].get("error")
            if err == "auth":
                self._set_balance_text("可用积分：登录已过期，请在客户端重新登录", "#c62828")
            elif err == "no_accounts":
                self._set_balance_text("可用积分：未发现账号", "#9e9e9e")
            else:
                self._set_balance_text("可用积分：读取失败（点此重试）", "#c62828")
            return
        total = sum(int(r.get("balance") or 0) for r in oks)
        gift = sum(int(r.get("gift") or 0) for r in oks)
        text = "可用积分：%d" % total
        if gift > 0:
            text += "（含赠送 %d）" % gift
        if fails or missing:
            text += "（部分失败）"
        self._set_balance_text(text, "#1565c0" if not (fails or missing) else "#ef6c00")

    # -------------------------------------------------------- 积分明细 --------

    def _alert_key(self):
        """条幅指纹 = 各账号 (label, 金额, 到期日) 的有序元组。

        金额或到期日任一变化 → 指纹变 → 之前的手动关闭失效，条幅重新出现。
        """
        parts = []
        for u in self.acc_order:
            r = self.ledger.get(u)
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            al = r.get("alert") or {}
            if al.get("show"):
                parts.append((u, al.get("total"), al.get("date")))
        return tuple(parts)

    def _dismiss_alert(self):
        """手动关闭条幅：记下当前指纹（本会话内不再显示），并立即回收布局不留空白。"""
        self._alert_dismissed_key = self._alert_key()
        self.frm_alert.pack_forget()
        self._alert_shown = False

    def _render_alert(self):
        """顶部到期提醒条：有临近到期积分才显示（文案来自引擎 alert.text，不自行计算）。

        关闭记忆：条目指纹与「被关闭时的指纹」相同则保持隐藏；
        金额或到期时间变化 → 指纹不同 → 自动重新出现。
        """
        texts = []
        for u in self.acc_order:
            r = self.ledger.get(u)
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            al = r.get("alert")
            if al and al.get("show"):
                texts.append(al.get("text") or "")
        text = "、".join(t for t in texts if t)
        key = self._alert_key()
        if text and key != self._alert_dismissed_key:
            self.alert_var.set("%s，点击查看积分明细 →" % text)
            if not self._alert_shown:
                self.frm_alert.pack(fill="x", before=self.opt_frame)
                self._alert_shown = True
        else:
            if self._alert_shown:
                self.frm_alert.pack_forget()
                self._alert_shown = False
            self.alert_var.set("")

    def _handle_ledger_end(self, rc):
        self.ledger_job = False
        self._render_alert()

    def do_query_ledger(self, manual=False):
        """查询积分明细：独立线程，不占 job 互斥槽，不阻塞其他功能。

        manual=True（点按钮/刷新）绕过 60s 节流；selftest 模式绝不发起。
        """
        if self.selftest or self.ledger_job:
            return
        if not manual and time.time() - self.ledger_ts < 60:
            return
        self.ledger_ts = time.time()
        self.ledger_job = True
        threading.Thread(target=self._ledger_worker, daemon=True).start()

    def _ledger_worker(self):
        """后台跑 run_all.py --ledger-json，逐行投递到 UI 队列。"""
        try:
            r = run_hidden([sys.executable, ENGINE, "--ledger-json"], timeout=180)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("event") == "ledger":
                    self.queue.put(("json", obj))
            self.queue.put(("ledger_end", r.returncode))
        except Exception as exc:
            self.queue.put(("raw", "积分明细查询启动失败：%s" % exc))
            self.queue.put(("ledger_end", -1))

    def open_ledger(self):
        """打开积分明细窗口（单例）。先发一次查询（节流内不重发），有数据即渲染。"""
        if self.selftest:
            return
        if self._ledger_win is not None and self._ledger_win.win.winfo_exists():
            self._ledger_win.win.lift()
            self._ledger_win.win.focus_force()
            self._ledger_win.refresh_data(self.ledger, self.acc_order, self.acc_cfg)
            return
        self._ledger_win = LedgerWindow(self.root, self.ledger, self.acc_order, self.acc_cfg,
                                        on_refresh=self._refresh_ledger_now,
                                        on_close=self._on_ledger_closed)
        self.do_query_ledger(manual=True)

    def _on_ledger_closed(self):
        self._ledger_win = None

    def _refresh_ledger_now(self):
        self.do_query_ledger(manual=True)

    def _push_ledger_to_window(self):
        if self._ledger_win is not None and self._ledger_win.win.winfo_exists():
            self._ledger_win.refresh_data(self.ledger, self.acc_order, self.acc_cfg)

    # ------------------------------------------------------------ 按钮 --------

    def do_signin(self):
        self.start_job("signin", ["--json-lines"])

    def do_status(self):
        self.start_job("status", ["--action", "status", "--json-lines"])

    def do_query_balance(self, manual=False):
        """查询可用积分：独立轻量线程，不占 job 互斥槽，不阻塞其他功能。

        manual=True（点击标签）绕过 60s 节流；失败态同样可点击重试。
        selftest 模式绝不发起（保持自检零副作用）。
        """
        if self.selftest or self.bal_job:
            return
        if not manual and time.time() - self.bal_ts < 60:
            return
        self.bal_ts = time.time()
        self.bal_job = True
        self._set_balance_text("可用积分：读取中…", "#9e9e9e")
        threading.Thread(target=self._balance_worker, daemon=True).start()

    def _balance_worker(self):
        """后台跑 run_all.py --balance-json，逐行投递到 UI 队列。"""
        try:
            r = run_hidden([sys.executable, ENGINE, "--balance-json"], timeout=90)
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("event") == "balance":
                    self.queue.put(("json", obj))
            self.queue.put(("balance_end", r.returncode))
        except Exception as exc:
            self.queue.put(("raw", "余额查询启动失败：%s" % exc))
            self.queue.put(("balance_end", -1))

    def do_add_account(self):
        if self.job:
            self._log("[%s] 忙，稍后再扫描" % now_str(), "note")
            return
        try:
            r = run_hidden([sys.executable, ENGINE, "--list-json"], timeout=120)
        except Exception as exc:
            messagebox.showerror("添加账号", "扫描失败：%s" % exc, parent=self.root)
            return
        text = (r.stdout or "").strip().splitlines()
        accounts = []
        for ln in reversed(text):
            try:
                obj = json.loads(ln)
                accounts = obj.get("accounts") or []
                break
            except Exception:
                continue
        if not accounts:
            messagebox.showerror("添加账号", "扫描不到账号数据：\n%s" % ((r.stderr or r.stdout or "无输出")[:300]),
                                 parent=self.root)
            return
        known = set(k for k in self.acc_cfg.keys() if not k.startswith("_"))
        new = [a for a in accounts if a.get("uid") not in known]
        if not new:
            messagebox.showinfo("添加账号",
                                "没有发现新账号。\n\n如需添加一个全新账号：\n"
                                "先在 WorkBuddy 客户端里登录它一次（会留下凭据归档），\n"
                                "再回到本面板点「添加账号」。", parent=self.root)
            return
        added = 0
        for a in new:
            uid = a["uid"]
            name = simpledialog.askstring("命名新账号",
                                          "发现账号 %s\n给它起个名字（留空跳过）：" % uid[:8],
                                          initialvalue="账号" + uid[:4], parent=self.root)
            if name:
                self.acc_cfg[uid] = name.strip()
                added += 1
        if added:
            save_json_atomic(ACC_CFG, self.acc_cfg)
            self._log("[%s] 已添加 %d 个账号，重新读取列表…" % (now_str(), added), "info")
            self.start_job("list", ["--list-json"])

    def open_logfile(self):
        try:
            if os.path.isfile(LOG_FILE):
                os.startfile(LOG_FILE)
            else:
                messagebox.showinfo("日志", "还没有日志文件（先跑一轮签到）", parent=self.root)
        except Exception as exc:
            messagebox.showerror("日志", str(exc), parent=self.root)

    def do_create_shortcut(self):
        """在桌面建一个快捷方式，用当前解释器直接启动本面板（无控制台）。"""
        pyw = find_gui_python() or sys.executable
        panel = os.path.abspath(__file__)
        # 桌面路径（含 OneDrive 重定向的情况）
        desktop = None
        try:
            r = run_hidden(["powershell.exe", "-NoProfile", "-Command",
                            "[Environment]::GetFolderPath('Desktop')"], timeout=30)
            if (r.stdout or "").strip():
                desktop = r.stdout.strip().splitlines()[-1].strip()
        except Exception:
            pass
        if not desktop or not os.path.isdir(desktop):
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        lnk = os.path.join(desktop, "WorkBuddy签到面板.lnk")
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$l = $ws.CreateShortcut('{lnk}'); "
            "$l.TargetPath = '{pyw}'; "
            "$l.Arguments = '\"{panel}\"'; "
            "$l.WorkingDirectory = '{here}'; "
            "$l.Description = 'WorkBuddy 多账号签到面板'; "
            "$l.WindowStyle = 7; "
            "$l.Save(); "
            "if (Test-Path '{lnk}') {{ 'LNK-OK' }} else {{ 'LNK-FAIL' }}"
        ).format(lnk=lnk, pyw=pyw, panel=panel, here=HERE)
        try:
            r = run_ps(ps, timeout=60)
        except Exception as exc:
            messagebox.showerror("快捷方式", "创建失败：%s" % exc, parent=self.root)
            return
        if "LNK-OK" in (r.stdout or ""):
            self._log("[%s] 已创建桌面快捷方式：%s" % (now_str(), lnk), "info")
            messagebox.showinfo("快捷方式", "已创建：\n%s\n\n双击它即可打开面板（无黑窗）。" % lnk,
                                parent=self.root)
        else:
            messagebox.showerror("快捷方式", "创建失败：\n%s" % ((r.stdout or "") + (r.stderr or ""))[:400],
                                 parent=self.root)

    # ------------------------------------------------------- 右键菜单 --------

    def _sel_uid(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _popup_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def do_rename(self):
        uid = self._sel_uid()
        if not uid:
            return
        cur = self.acc_cfg.get(uid) or (self.acc_meta.get(uid, {}).get("label")) or uid[:8]
        name = simpledialog.askstring("重命名", "新的名字：", initialvalue=cur, parent=self.root)
        if name and name.strip():
            self.acc_cfg[uid] = name.strip()
            save_json_atomic(ACC_CFG, self.acc_cfg)
            self.start_job("list", ["--list-json"], quiet=True)

    def do_toggle_disable(self):
        uid = self._sel_uid()
        if not uid:
            return
        dis = list(self.acc_cfg.get("_disabled") or [])
        if uid in dis:
            dis.remove(uid)
            self._log("[%s] 已启用 %s" % (now_str(), self.acc_cfg.get(uid) or uid[:8]), "info")
        else:
            dis.append(uid)
            self._log("[%s] 已停用 %s（下次签到起不再参与）" % (now_str(), self.acc_cfg.get(uid) or uid[:8]), "info")
        self.acc_cfg["_disabled"] = dis
        save_json_atomic(ACC_CFG, self.acc_cfg)
        self.start_job("list", ["--list-json"], quiet=True)

    def do_remove(self):
        uid = self._sel_uid()
        if not uid:
            return
        name = self.acc_cfg.get(uid) or uid[:8]
        if not messagebox.askyesno("移出列表",
                                   "将「%s」移出列表？\n\n本地自持凭据缓存（state/）会一并删除；\n"
                                   "客户端里登录过的事实不变，之后仍可通过「添加账号」找回。" % name,
                                   parent=self.root):
            return
        self.acc_cfg.pop(uid, None)
        dis = list(self.acc_cfg.get("_disabled") or [])
        if uid in dis:
            dis.remove(uid)
            self.acc_cfg["_disabled"] = dis
        save_json_atomic(ACC_CFG, self.acc_cfg)
        for suffix in (".dat", ".meta.json"):
            try:
                os.remove(os.path.join(STATE_DIR, uid + suffix))
            except OSError:
                pass
        self._log("[%s] 已移出 %s" % (now_str(), name), "info")
        self.start_job("list", ["--list-json"], quiet=True)

    # ------------------------------------------------------------ 收尾 --------

    def _on_close(self):
        self.settings["geometry"] = self.root.geometry()
        self._save_settings_from_ui()
        try:
            if self.job_proc and self.job_proc.poll() is None:
                self.job_proc.terminate()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ------------------------------------------------------------------ 单实例 ----

def already_running():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main():
    args = set(sys.argv[1:])
    auto = "--auto" in args
    selftest = "--selftest" in args

    if already_running():
        if auto or selftest:
            return 0
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("WorkBuddy 签到面板", "面板已经在运行了。")
        root.destroy()
        return 0

    if not os.path.isfile(ENGINE):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("WorkBuddy 签到面板", "找不到引擎文件：\n%s" % ENGINE)
        root.destroy()
        return 2

    app = Panel(auto=auto, selftest=selftest)
    if selftest:
        app.root.after(1500, app.root.destroy)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
