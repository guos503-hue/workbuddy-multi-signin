#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 多账号自动签到 —— 无需在客户端里切换账号。

原理
----
WorkBuddy 桌面端每次登录，都会把当时的登录态归档一份到：

    %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\
        workbuddy-desktop.<时间戳>.<pid>.<uuid>.info

归档各自独立保留，不因之后登录了别的号而被覆盖。签到只需要
(accessToken, uid)，与「此刻客户端登录的是哪个号」无关 —— 所以可以在
只登录 A 号的情况下，用 B 号的历史归档替 B 签到。

token 处理
----------
- 归档里的 token 是 sym-v1 信封加密，本目录的 signin.py 会请客户端二进制
  自行解密（不打印、不外传）。
- 对「非当前登录」的账号，首次使用时用其 refreshToken 换取新 token 链，
  并以 DPAPI（仅本 Windows 用户可解）加密存到 state/<uid>.dat 自持；
  临近过期时自动续期，无需你再登录它。
- 「当前登录」的账号直接读客户端写出的凭据文件（只读，不碰它的 token 链，
  以免与客户端抢刷新导致掉登录）。

用法
----
    python run_all.py                  # 每个号跑一轮，结果写 signin.log（默认动作 auto）
    python run_all.py --verbose        # 同时把原始输出打到屏幕
    python run_all.py --list           # 仅列出账号与凭据状态，不发请求
    python run_all.py --action status  # 只读查询（不领取）

退出码 0 = 所有账号健康。
"""

import argparse
import ctypes
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

# pythonw 无控制台：stdout/stderr 可能是 None，print 会出错；统一兜底到空设备。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNIN = os.path.join(HERE, "signin.py")
LOG = os.path.join(HERE, "signin.log")
CONFIG = os.path.join(HERE, "accounts.json")
STATE_DIR = os.path.join(HERE, "state")

AUTH_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "CodeBuddyExtension", "Data", "Public", "auth")
LIVE_NAME = "workbuddy-desktop.info"
CN_DOMAIN = "www.workbuddy.cn"

# WorkBuddy 客户端路径由上游 signin.py 自动探测（常见安装目录 + 注册表），
# 也可以给 WORKBUDDY_EXE 环境变量手动指定。不要写死本机路径。

# 续期阈值（天）：accessToken 剩余低于 AT_DAYS 或 refreshToken 剩余低于
# RT_DAYS 时自动刷新一次（仅限自持 state 的账号；当前登录账号由客户端负责）。
REFRESH_AT_DAYS = float(os.environ.get("WORKBUDDY_SIGNIN_REFRESH_AT_DAYS", "10"))
REFRESH_RT_DAYS = float(os.environ.get("WORKBUDDY_SIGNIN_REFRESH_RT_DAYS", "15"))
WARN_AT_DAYS = 10           # 当前登录账号临近过期时在日志里提醒

REFRESH_URLS = [
    "https://www.codebuddy.cn/v2/plugin/auth/token/refresh",
    "https://copilot.tencent.com/v2/plugin/auth/token/refresh",
]
REFRESH_UA = "CLI/2.143.1 CodeBuddy/2.143.1"
SUMMARY_CAP = 800           # 日志里单行摘要上限
LOG_ROTATE_BYTES = 2_000_000

_LOCK = os.path.join(STATE_DIR, ".lock")
_LOCK_STALE = 15 * 60


# ---------------------------------------------------------------- DPAPI ------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not fn(ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_protect(raw: bytes) -> bytes:
    return _dpapi(raw, True)


def dpapi_unprotect(raw: bytes) -> bytes:
    return _dpapi(raw, False)


# ------------------------------------------------------------- utilities -----

_signin_lib = None


def signin_lib():
    global _signin_lib
    if _signin_lib is None:
        spec = importlib.util.spec_from_file_location("signin_lib", SIGNIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _signin_lib = mod
    return _signin_lib


def resolve_exe():
    """定位 WorkBuddy 客户端：优先环境变量，其次用上游的自动探测。

    上游 find_workbuddy_runtime() 覆盖：常见安装目录
    （%LOCALAPPDATA%\\Programs\\WorkBuddy、Program Files）+ 注册表卸载项，
    在各机器上通用。失败时才抛错。
    """
    override = os.environ.get("WORKBUDDY_EXE")
    if override and os.path.isfile(override):
        return override
    return signin_lib().find_workbuddy_runtime()


def decrypt_field(val):
    """sym-v1 信封 -> 明文 token（经客户端运行时助手）。"""
    if isinstance(val, dict):
        exe = resolve_exe()
        if not os.path.isfile(exe):
            raise RuntimeError("找不到 WorkBuddy 客户端（%s），无法解密凭据" % exe)
        return signin_lib()._run_auth_helper(exe, {"operation": "decrypt", "value": val})["accessToken"]
    return val


def now_ms():
    return int(time.time() * 1000)


def days_left(ts_ms):
    if not ts_ms:
        return -1.0
    return (ts_ms / 1000.0 - time.time()) / 86400.0


def load_labels():
    labels = {}
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as fh:
                labels = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
        except Exception:
            pass
    return labels


def load_disabled():
    """accounts.json 里 "_disabled": [uid, ...] —— 面板里停用的账号不参与签到。"""
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            val = data.get("_disabled")
            if isinstance(val, list):
                return set(str(x) for x in val)
        except Exception:
            pass
    return set()


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- state ------

def state_dat(uid):
    return os.path.join(STATE_DIR, "%s.dat" % uid)


def state_meta(uid):
    return os.path.join(STATE_DIR, "%s.meta.json" % uid)


def state_legacy(uid):
    return os.path.join(STATE_DIR, "%s.json" % uid)


def save_state(uid, session, source):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {"version": 2, "savedAt": now_ms(), "session": session}
    blob = dpapi_protect(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    tmp = state_dat(uid) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, state_dat(uid))
    auth = session.get("auth") or {}
    write_json_atomic(state_meta(uid), {
        "version": 2, "uid": uid, "source": source, "savedAt": payload["savedAt"],
        "expiresAt": auth.get("expiresAt"), "refreshExpiresAt": auth.get("refreshExpiresAt"),
    })


def load_state(uid):
    with open(state_dat(uid), "rb") as fh:
        blob = fh.read()
    payload = json.loads(dpapi_unprotect(blob).decode("utf-8"))
    return payload.get("session") or {}


def migrate_legacy(uid):
    """把旧版明文 state/<uid>.json 迁移为 DPAPI 加密的 .dat，然后删除明文。"""
    legacy = state_legacy(uid)
    if not os.path.isfile(legacy):
        return False
    sess = read_json(legacy)
    if not sess:
        return False
    save_state(uid, sess, "legacy-migrated")
    try:
        os.remove(legacy)
    except OSError:
        pass
    return True


# --------------------------------------------------------------- refresh -----

def refresh_tokens(rt, session):
    """用 refreshToken 换新 token。返回 (new_at, new_rt, expires_in, refresh_expires_in)；失败抛异常。"""
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": rt,
        "X-Auth-Refresh-Source": "plugin",
        "User-Agent": REFRESH_UA,
    }
    acct = session.get("account") or {}
    if acct.get("enterpriseId"):
        hdrs["X-Enterprise-Id"] = acct["enterpriseId"]
        hdrs["X-Tenant-Id"] = acct["enterpriseId"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = "no endpoint tried"
    for url in REFRESH_URLS:
        req = urllib.request.Request(url, data=b"{}", headers=hdrs, method="POST")
        try:
            with opener.open(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            data = j.get("data") or {}
            if data.get("accessToken"):
                return (data["accessToken"], data.get("refreshToken") or rt,
                        int(data.get("expiresIn") or 0), int(data.get("refreshExpiresIn") or 0))
            last = "code=%s msg=%s" % (j.get("code"), j.get("msg"))
        except Exception as e:
            last = "%s %s" % (type(e).__name__, str(e)[:200])
    raise RuntimeError("刷新失败: %s" % last)


def apply_fresh_tokens(session, at, rt, exp_in, ref_in):
    auth = dict(session.get("auth") or {})
    t = now_ms()
    auth["accessToken"] = at
    auth["refreshToken"] = rt
    auth["lastRefreshTime"] = t
    if exp_in:
        auth["expiresAt"] = t + exp_in * 1000
    if ref_in:
        auth["refreshExpiresAt"] = t + ref_in * 1000
    out = dict(session)
    out["auth"] = auth
    return out


def refresh_state(uid, session):
    """对自持账号执行一次刷新并落盘。返回 (session, expires_in, refresh_expires_in)。"""
    rt = (session.get("auth") or {}).get("refreshToken")
    if not rt:
        raise RuntimeError("state 里没有 refreshToken")
    at, new_rt, exp_in, ref_in = refresh_tokens(rt, session)
    fresh = apply_fresh_tokens(session, at, new_rt, exp_in, ref_in)
    save_state(uid, fresh, "auto-refresh")
    return fresh, exp_in, ref_in


# ------------------------------------------------------------- discovery -----

def _snapshot_info(path):
    """读一个凭据文件的最小信息；非国内站或结构不对返回 None。"""
    sess = read_json(path)
    if not sess:
        return None
    uid = ((sess.get("account") or {}).get("uid") or "").strip()
    auth = sess.get("auth") or {}
    if not uid or auth.get("domain") != CN_DOMAIN:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return {
        "uid": uid, "path": path, "mtime": mtime,
        "expiresAt": auth.get("expiresAt") or 0,
        "refreshExpiresAt": auth.get("refreshExpiresAt") or 0,
    }


def discover(include_disabled=False):
    """返回每个 uid 的凭据安排：mode = live | state | bootstrap。"""
    labels = load_labels()
    live = None
    archives = {}

    live_path = os.path.join(AUTH_DIR, LIVE_NAME)
    if os.path.isfile(live_path):
        live = _snapshot_info(live_path)

    if os.path.isdir(AUTH_DIR):
        for fn in sorted(os.listdir(AUTH_DIR)):
            if fn.startswith("workbuddy-desktop-ai"):
                continue
            if not fn.startswith("workbuddy-desktop"):
                continue
            if fn == LIVE_NAME:
                continue
            info = _snapshot_info(os.path.join(AUTH_DIR, fn))
            if not info:
                continue
            prev = archives.get(info["uid"])
            if prev is None or info["mtime"] > prev["mtime"]:
                archives[info["uid"]] = info

    # 旧版明文 state 迁移
    if os.path.isdir(STATE_DIR):
        for fn in os.listdir(STATE_DIR):
            if fn.endswith(".json") and ".meta." not in fn and not fn.startswith("."):
                migrate_legacy(fn[:-5])

    metas = {}
    if os.path.isdir(STATE_DIR):
        for fn in os.listdir(STATE_DIR):
            if fn.endswith(".meta.json"):
                uid = fn[:-len(".meta.json")]
                m = read_json(os.path.join(STATE_DIR, fn))
                if m and os.path.isfile(state_dat(uid)):
                    metas[uid] = m

    uids = set()
    if live:
        uids.add(live["uid"])
    uids.update(archives)
    uids.update(metas)

    disabled = load_disabled()
    out = []
    for uid in sorted(uids):
        if uid in disabled and not include_disabled:
            continue
        label = labels.get(uid) or uid[:8]
        if live and uid == live["uid"]:
            out.append({"uid": uid, "label": label, "mode": "live",
                        "path": live["path"], "meta": live})
            continue
        meta = metas.get(uid)
        arc = archives.get(uid)
        if meta is None:
            if arc is None:
                continue
            out.append({"uid": uid, "label": label, "mode": "bootstrap", "path": arc["path"], "meta": arc})
            continue
        # 有自持 state；归档仅在「比 state 更新」时才用于重建 —— 即客户端
        # 在我们上次同步之后又重新登录过该号（token 链更新了）。
        # 不能用 refreshExpiresAt 比较：刷新响应给的新 RT 窗口（~40 天）
        # 天然短于归档里的剩余窗口（~50 天），会导致每轮都误判需要重建、
        # 反复刷新 token 链。正确的信号是「客户端归档的写入时间」。
        arc_mtime = (arc.get("mtime") or 0) if arc else 0
        state_saved = (meta.get("savedAt") or 0) / 1000.0
        if arc and arc_mtime > state_saved + 300:
            out.append({"uid": uid, "label": label, "mode": "bootstrap", "path": arc["path"], "meta": arc})
        else:
            out.append({"uid": uid, "label": label, "mode": "state",
                        "path": state_dat(uid), "meta": {"expiresAt": meta.get("expiresAt") or 0,
                                                         "refreshExpiresAt": meta.get("refreshExpiresAt") or 0,
                                                         "mtime": (meta.get("savedAt") or 0) / 1000.0}})
    return out


def bootstrap_state(uid, archive_path):
    """从归档凭据建立自持 state（解密 -> 刷新 -> DPAPI 落盘）。返回 (session, note)。"""
    sess = read_json(archive_path)
    if not sess:
        raise RuntimeError("归档文件无法解析: %s" % archive_path)
    at = decrypt_field((sess.get("auth") or {}).get("accessToken"))
    rt = decrypt_field((sess.get("auth") or {}).get("refreshToken"))
    if not at or not rt:
        raise RuntimeError("归档缺少 token 字段")
    base_sess = dict(sess)
    auth = dict(sess.get("auth") or {})
    auth["accessToken"], auth["refreshToken"] = at, rt
    base_sess["auth"] = auth
    fresh, exp_in, ref_in = refresh_state(uid, base_sess)  # 内部已 save_state
    note = "已建立自持凭据（新 token %.0f 天）" % (exp_in / 86400.0) if exp_in else "已建立自持凭据"
    return fresh, note


# ------------------------------------------------------------------ run ------

def make_session_file(uid, session):
    """把自持 session 写为临时明文文件供 signin.py 读取；调用方负责删除。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, ".run-%s.json" % uid)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False)
    return path


def _humanize_payload(payload):
    """把 signin.py 的输出整理成一句人话（status 查询也要好看）。"""
    if payload.get("report"):
        return str(payload["report"])
    body = payload.get("body")
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and "today_checked_in" in data:
        return "今日%s · 连签 %s 天 · 累计 %s 积分" % (
            "已签" if data.get("today_checked_in") else "未签",
            data.get("streak_days", "?"), data.get("total_credits", "?"))
    if payload.get("result"):
        return str(payload["result"])
    return json.dumps(payload, ensure_ascii=False)[:200]


def summarize_raw(raw):
    """从 signin.py 的输出里抽结构化字段（供面板表格用）。"""
    payload = None
    for line in reversed((raw or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(payload, dict):
        return {}
    out = {}
    body = payload.get("body")
    data = (body or {}).get("data") if isinstance(body, dict) else None
    for src in (data, payload):
        if not isinstance(src, dict):
            continue
        if "today_checked_in" in src and "checked_today" not in out:
            out["checked_today"] = bool(src["today_checked_in"])
        for k in ("streak_days", "total_credits", "today_credit"):
            if k in src and isinstance(src[k], (int, float)) and k not in out:
                out[k] = src[k]
    return out


def run_signin(auth_file, action):
    env = dict(os.environ)
    try:
        env["WORKBUDDY_EXE"] = resolve_exe()
    except Exception as e:
        return (False,
                "找不到 WorkBuddy 客户端：%s（请先安装并登录客户端，"
                "或用环境变量 WORKBUDDY_EXE 指定 WorkBuddy.exe 路径）" % str(e)[:160],
                True, "", {})
    env["WORKBUDDY_AUTH_FILE"] = auth_file
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, SIGNIN, action], env=env,
                              capture_output=True, timeout=900,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT（超过 900 秒）", True, ""
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()

    payload = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if payload is None:
        return False, "无法解析输出：%s" % ((err or out or "no output")[:300]), True, out, {}
    summary = _humanize_payload(payload)[:SUMMARY_CAP]
    attention = bool(payload.get("needs_attention"))
    result = str(payload.get("result") or "")
    ok = proc.returncode == 0 and not attention and result not in ("AUTH_ERROR", "NO_AUTH")
    return ok, summary, attention, out, summarize_raw(out)


def handle_account(acc, action):
    """处理一个账号（含自持/续期逻辑），返回 (ok, summary, attention, notes, raw, info)。"""
    uid, mode = acc["uid"], acc["mode"]
    notes = []
    session = None
    temp_file = None

    try:
        if mode == "bootstrap":
            try:
                session, note = bootstrap_state(uid, acc["path"])
                notes.append(note)
            except Exception as e:
                notes.append("自持凭据建立失败：%s，改用归档原文件（本轮仍可签到）" % str(e)[:200])
                ok, summary, att, raw, info = run_signin(acc["path"], action)
                return ok, summary, att, notes, raw, info
        if session is None and mode in ("state", "bootstrap"):
            session = load_state(uid)
        if session is not None:
            # 邻期自动续期（仅自持账号）
            at_days = days_left((session.get("auth") or {}).get("expiresAt"))
            rt_days = days_left((session.get("auth") or {}).get("refreshExpiresAt"))
            if at_days < REFRESH_AT_DAYS or rt_days < REFRESH_RT_DAYS:
                try:
                    session, exp_in, _ = refresh_state(uid, session)
                    notes.append("已自动续期（新 token %.0f 天）" % (exp_in / 86400.0))
                except Exception as e:
                    notes.append("自动续期失败：%s（本轮用现有 token 继续）" % str(e)[:160])
            temp_file = make_session_file(uid, session)
            auth_file = temp_file
        else:
            # live 模式：只读客户端凭据文件，不碰它的 token 链
            auth_file = acc["path"]
            m = acc.get("meta") or {}
            at_days = days_left(m.get("expiresAt"))
            if 0 <= at_days < WARN_AT_DAYS:
                notes.append("⚠ 客户端凭据 %.1f 天后到期，请打开客户端或重新登录一次" % at_days)

        ok, summary, att, raw, info = run_signin(auth_file, action)
        return ok, summary, att, notes, raw, info
    finally:
        if temp_file:
            try:
                os.remove(temp_file)
            except OSError:
                pass


# ------------------------------------------------------------------ main -----

def rotate_log():
    try:
        if os.path.getsize(LOG) > LOG_ROTATE_BYTES:
            if os.path.exists(LOG + ".1"):
                os.remove(LOG + ".1")
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def append_log(lines):
    if not lines:
        return
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def acquire_lock():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.isfile(_LOCK) and time.time() - os.path.getmtime(_LOCK) < _LOCK_STALE:
            return False
        with open(_LOCK, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    except OSError:
        return True


def release_lock():
    try:
        os.remove(_LOCK)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--action", default="auto", help="传给 signin.py 的动作（auto / status / ...）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--list", action="store_true", dest="list_only")
    ap.add_argument("--json-lines", action="store_true", dest="json_lines",
                    help="每个账号一行 JSON 输出（供面板实时消费）")
    ap.add_argument("--list-json", action="store_true", dest="list_json",
                    help="以 JSON 输出账号与凭据状态（供面板绘制表格）")
    args = ap.parse_args()

    if not os.path.isfile(SIGNIN):
        print("缺少 signin.py：%s" % SIGNIN)
        return 2

    if args.list_json:
        payload = []
        disabled = load_disabled()
        for a in discover(include_disabled=True):
            m = a.get("meta") or {}
            payload.append({
                "uid": a["uid"], "label": a["label"], "mode": a["mode"],
                "disabled": a["uid"] in disabled,
                "accessDays": round(days_left(m.get("expiresAt")), 1),
                "refreshDays": round(days_left(m.get("refreshExpiresAt")), 1),
            })
        print(json.dumps({"accounts": payload}, ensure_ascii=False))
        return 0

    accounts = discover()
    if not accounts:
        msg = "未发现任何国内站凭据。请先登录一次 WorkBuddy 客户端。"
        print(msg)
        append_log(["=== %s ===" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg])
        return 2

    if args.list_only:
        print("发现 %d 个账号：" % len(accounts))
        for a in accounts:
            mode_cn = {"live": "客户端当前登录（只读）",
                       "state": "自持凭据（自动续期）",
                       "bootstrap": "待建立自持凭据（将用归档）"}[a["mode"]]
            m = a.get("meta") or {}
            print("  %-8s uid=%s" % (a["label"], a["uid"]))
            print("             来源: %s" % mode_cn)
            print("             accessToken 剩 %.1f 天 | refreshToken 剩 %.1f 天" % (
                days_left(m.get("expiresAt")), days_left(m.get("refreshExpiresAt"))))
        return 0

    if not acquire_lock():
        print("已有另一轮在运行，跳过。")
        return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = "=== %s  共 %d 个账号  动作=%s ===" % (stamp, len(accounts), args.action)
    if args.verbose:
        print(header)

    log_lines = [header]
    all_ok = True
    try:
        for acc in accounts:
            if args.verbose:
                print("  [%s] %s (%s)" % (acc["label"], acc["mode"], os.path.basename(str(acc["path"]))))
            ok, summary, att, notes, raw, info = handle_account(acc, args.action)
            all_ok = all_ok and ok
            if args.verbose and raw:
                print("    raw: %s" % raw[:1500])
            line = "[%s] %-8s %s  %s" % (stamp, acc["label"], "OK " if ok else "ERR", summary)
            if notes:
                line += "（" + "；".join(notes) + "）"
            log_lines.append(line)
            if args.json_lines:
                # 面板消费：每个账号一行 JSON，立即刷出，绝不缓冲。
                row = {
                    "event": "account", "uid": acc["uid"], "label": acc["label"],
                    "mode": acc["mode"], "ok": ok, "attention": att,
                    "summary": summary, "notes": notes, "stamp": stamp,
                }
                row.update(info or {})
                print(json.dumps(row, ensure_ascii=False), flush=True)
            elif args.verbose or not ok or notes:
                print(line, flush=True)
    finally:
        release_lock()

    if args.json_lines:
        print(json.dumps({"event": "done", "all_ok": all_ok,
                          "count": len(accounts), "stamp": stamp}, ensure_ascii=False), flush=True)

    rotate_log()
    append_log(log_lines)
    if args.verbose:
        print("已追加到 %s" % LOG)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
