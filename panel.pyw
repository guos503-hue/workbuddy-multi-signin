#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到面板 —— 图形界面（双击打开，关窗即退）。

功能
----
- 立即签到：后台调用 run_all.py（复用同一引擎），实时把每个账号的结果刷进窗口
- 刷新状态：只读查询（不领取），更新表格里的连签 / 累计积分 / 凭据剩余
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
    """给定时任务/快捷方式用的 pythonw.exe（必须带 tkinter）。

    与启动自愈共用同一套探测逻辑：py 启动器 → 当前解释器 → 常见目录 → PATH，
    并且逐个实测 tkinter 是否可用，不合格的跳过。
    """
    return find_gui_python() or (sys.executable or "")


def run_hidden(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
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
        font_lab = ("Microsoft YaHei UI", 9)

        # 顶部工具栏
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        self.btn_sign = ttk.Button(bar, text="立即签到", command=self.do_signin)
        self.btn_sign.pack(side="left")
        self.btn_status = ttk.Button(bar, text="刷新状态", command=self.do_status)
        self.btn_status.pack(side="left", padx=(8, 0))
        self.btn_add = ttk.Button(bar, text="添加账号", command=self.do_add_account)
        self.btn_add.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="打开日志文件", command=self.open_logfile).pack(side="right")
        ttk.Button(bar, text="创建桌面快捷方式", command=self.do_create_shortcut).pack(side="right", padx=(0, 8))

        # 设置行
        opt = ttk.Frame(self.root, padding=(10, 0, 10, 6))
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
            self.status.set("共 %d 个账号" % len(self.acc_order))
            return

        ev = obj.get("event")
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

    # ------------------------------------------------------------ 按钮 --------

    def do_signin(self):
        self.start_job("signin", ["--json-lines"])

    def do_status(self):
        self.start_job("status", ["--action", "status", "--json-lines"])

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
