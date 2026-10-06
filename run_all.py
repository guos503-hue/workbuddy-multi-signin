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

# ⚠️ 强制 stdout/stderr 用 UTF-8，不管外部环境怎么设。
# 本脚本被面板以子进程方式调用（--balance-json / --ledger-json / --list-json），
# 面板按 utf-8 读回。若本进程按 cp936/gbk 输出（当 PYTHONIOENCODING=gbk 时），
# 中文（账号名 label 等）会被编成 GBK 字节，面板解出来就是 ♦♦（U+FFFD）。
# 这里主动重绑，使输出编码与本脚本自身无关，杜绝该乱码。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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


# -------------------------------------------------------------- balance ------

# 客户端账号面板「积分余额」的同源接口。注意：路径**不带 /v2**——
# 客户端 CloudAccountRepo.resourcePrefix 为空串（这三条新接口的网关路由声明的是
# 无前缀路径），带 /v2 会 404（实测）。
BALANCE_PATH = "/billing/meter/get-user-resource-summary"

# 套餐基础积分包（与客户端 CommodityCode + PLAN_BASE_CODES 同源）。
# 国内版 freeMon 是版本基础用量，计入基础；其余包（运营裂变 / 拉新权益 / 礼包）
# 计入「赠送」。
PLAN_BASE_CODES = {
    "TCACA_code_002_AkiJS3ZHF5",   # proMon
    "TCACA_code_005_maRGyrHhw1",   # proMonPlus
    "TCACA_code_003_FAnt7lcmRT",   # proYear
    "TCACA_code_023_4xbGhMrE6q",   # youth
    "TCACA_code_026_BaESVICNoi",   # advanced
    "TCACA_code_027_0FCGVA6vSa",   # flagship
    "TCACA_code_008_cfWoLwvjU4",   # freeMon（国内版：版本基础用量）
}


def _to_count(v):
    """安全转数字；坏值/NaN/±inf 一律按 0 处理（json.loads 默认接受 Infinity 字面量）。"""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return f


def parse_balance(body):
    """从 get-user-resource-summary 返回体算「可用 / 赠送」；结构不对返回 None。

    可用 = 各包周期剩余之和；赠送 = 其中非套餐基础包（裂变/拉新/礼包）的部分。
    单取一个 PackageCode 会漏（同一码会有多条账号记录），必须逐条累加。
    """
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    packages = data.get("Packages")
    if not isinstance(packages, list):
        return None
    total = 0.0
    gift = 0.0
    for item in packages:
        if not isinstance(item, dict):
            continue
        remain = _to_count(item.get("CycleRemainCapacity"))
        if remain <= 0:
            remain = _to_count(item.get("CapacityRemainPrecise"))
        if remain <= 0:
            remain = _to_count(item.get("CapacityRemain"))
        if remain <= 0:
            continue
        total += remain
        if item.get("PackageCode") not in PLAN_BASE_CODES:
            gift += remain
    return {
        "balance": int(round(total)),
        "gift": int(round(gift)),
        "unit": "credits",
        "is_paid": bool(data.get("IsPaidUser")),
        "plan": str(data.get("SubscriptionPackageName") or ""),
    }


def balance_auth_file(acc):
    """给一个账号准备只读凭据文件；返回 (path, cleanup)。

    只读、不续期——余额查询不该转动 token 链（续期是签到路径的事）。
    live 模式直接用客户端归档原文件；自持账号读 DPAPI state 写成临时文件，
    文件名与签到用的 `.run-<uid>.json` **不同**，以免两者并发时互相覆盖。
    """
    if acc.get("mode") != "live":
        try:
            session = load_state(acc["uid"])
            os.makedirs(STATE_DIR, exist_ok=True)
            path = os.path.join(STATE_DIR, ".run-balance-%s.json" % acc["uid"])
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(session, fh, ensure_ascii=False)
            return path, True
        except Exception:
            pass  # 自持 state 尚未建立/解密失败 → 退回归档原文件
    return acc["path"], False


def query_balance(acc):
    """查询单个账号的可用积分余额。任何异常都收敛为 ok=False，绝不抛出。"""
    out = {"uid": acc["uid"], "label": acc["label"], "mode": acc["mode"], "ok": False}
    lib = signin_lib()
    path, cleanup = balance_auth_file(acc)
    try:
        try:
            session = lib.resolve_session(lib.load_session_retry(path))
            headers = lib.build_headers(session)
            endpoint = ((session.get("auth") or {}).get("endpoint")
                        or lib.DEFAULT_ENDPOINT).rstrip("/")
        except Exception as e:
            out["error"] = "auth"
            out["report"] = "凭据不可用：%s" % str(e)[:140]
            return out
        try:
            # retry=False：余额是面板上的即时读数，不为一格数字反复重试拖住界面
            code, body = lib.post(endpoint + BALANCE_PATH, headers, {}, retry=False)
        except Exception as e:
            out["error"] = "network"
            out["report"] = "查询失败：%s" % str(e)[:140]
            return out
        if code in (401, 403):
            out["error"] = "auth"
            out["report"] = "登录态过期（HTTP %s），请在客户端重新登录" % code
            return out
        parsed = parse_balance(body)
        if not parsed:
            bcode = body.get("code") if isinstance(body, dict) else "?"
            out["error"] = "data"
            out["report"] = "接口返回异常（http=%s code=%s）" % (code, bcode)
            return out
        out.update(parsed)
        out["ok"] = True
        return out
    except Exception as e:  # 兜底：任何意外都不能把面板启动带崩
        out["error"] = "unknown"
        out["report"] = "%s: %s" % (type(e).__name__, str(e)[:140])
        return out
    finally:
        if cleanup:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------- ledger ------

# 积分明细（批次事件流）。
#
# 平台只提供**批次级**数据，没有逐笔消耗流水（客户端 app.asar 全量确认：
# 「积分流水」「积分记录」「creditRecord」等零命中，客户端自身也没有流水页）。
# 每个批次 = 一次发放，故把批次派生为事件流：
#   - 每批 → 一条收入事件（来源 / 数量 / 获得时间）
#   - 「已用完」批次 → 追加一条消耗支出事件，时间取 ExpiredTime（实测即用完那一刻，精确到秒）
#   - 「已过期未用完」批次 → 追加一条到期作废支出事件，时间取 DeductionEndTime
#   - 进行中的消耗（用了但没用完）没有时间戳，以「已用 X/Y」备注在收入事件上，不伪造时间
#
# 与余额查询同一纪律：只读、不占 .lock、不写 signin.log、不转动 token 链。

LEDGER_FREE_PATH = "/billing/meter/get-user-resource-free-packages"
LEDGER_PAID_PATH = "/billing/meter/get-user-resource-paid-packages"
LEDGER_PAGE_SIZE = 200      # 契约上限 [1,200]，超出后端直接 ParameterInvalid
LEDGER_MAX_PAGES = 10       # 与客户端 fetchAllPages 同限；触顶会告警而非静默少算

LEDGER_SLICE_TYPE = 4       # 每日刷新的分片包，与客户端一致：不计入明细与总额

LEDGER_CONFIG = os.path.join(HERE, "ledger.json")
EXPIRE_WARN_DEFAULT = 30
EXPIRE_WARN_MIN = 1
EXPIRE_WARN_MAX = 365

# 商品码全集（客户端 CommodityCode 常量）。free/paid 两个接口各有自己的码白名单，
# 必须按白名单分流：把付费码传给 free 接口会被静默过滤成空列表。
LEDGER_FREE_CODES = [
    "TCACA_code_001_PqouKr6QWV",   # free
    "TCACA_code_008_cfWoLwvjU4",   # freeMon
    "TCACA_code_035_ArVxJcGDsm",   # freeMonIntl
    "TCACA_code_006_DbXS0lrypC",   # gift
    "TCACA_code_039_KRcQj7wUat",   # proTrialMon
    "TCACA_code_040_mi9rCYg46x",   # proTrialYear
    "TCACA_code_007_nzdH5h4Nl0",   # activity（运营裂变包）
    "TCACA_code_028_NtpWi0jzXs",   # bonus28
    "TCACA_code_029_6wCGEWquYy",   # bonus29
    "TCACA_code_030_BjSt89qTvr",   # bonus30（拉新权益包）
    "TCACA_code_037_WxOD3MpI2o",   # bonusIntl
]
LEDGER_PAID_CODES = [
    "TCACA_code_002_AkiJS3ZHF5",   # proMon
    "TCACA_code_005_maRGyrHhw1",   # proMonPlus
    "TCACA_code_003_FAnt7lcmRT",   # proYear
    "TCACA_code_023_4xbGhMrE6q",   # youth
    "TCACA_code_026_BaESVICNoi",   # advanced
    "TCACA_code_027_0FCGVA6vSa",   # flagship
    "TCACA_code_009_0XmEQc2xOf",   # extra（加量包）
    "TCACA_code_038_OhvqZtiPKr",   # extra38
    "TCACA_code_036_lupO5WgNdG",   # extraIntl
]

_BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def ledger_config():
    """预警阈值（天）：配置在积分模块配置中心（ledger.json 的 expireWarningDays）。

    缺失/损坏回落默认 30；越界夹到 [1,365]。预警只是提示，不该因为一个坏配置让明细打不开。
    """
    raw = read_json(LEDGER_CONFIG) or {}
    try:
        days = int(raw.get("expireWarningDays"))
    except (TypeError, ValueError):
        return EXPIRE_WARN_DEFAULT
    return max(EXPIRE_WARN_MIN, min(EXPIRE_WARN_MAX, days))


def _parse_billing_ms(value):
    """计费时间 → 毫秒时间戳。字段既可能是毫秒数（或数字串），也可能是 'YYYY-MM-DD HH:MM:SS'（北京时间）。"""
    if value in (None, "", 0) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ms = int(value)
        return ms if ms > 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        ms = int(s)
        return ms if ms > 0 else None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.datetime.strptime(s, fmt).replace(tzinfo=_BJ_TZ).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _iso_local(ms):
    """毫秒时间戳 → ISO 8601 带本机时区偏移（前端按本地时区只做展示）。"""
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _batch_capacity(row):
    """(总量, 已用量)。累积型优先，回落周期型（与客户端 resolvePackageCapacity 同口径）。"""
    total = _to_count(row.get("CapacitySizePrecise"))
    if total > 0:
        raw_used = row.get("CapacityUsedPrecise")
        used = _to_count(raw_used) if raw_used not in (None, "") else max(0.0, total - _to_count(row.get("CapacityRemainPrecise")))
        return total, used
    total = _to_count(row.get("CycleCapacitySizePrecise")) or _to_count(row.get("CycleCapacitySize"))
    if total <= 0:
        return 0.0, 0.0
    raw_used = row.get("CycleCapacityUsedPrecise")
    if raw_used not in (None, ""):
        used = _to_count(raw_used)
    else:
        remain = _to_count(row.get("CycleCapacityRemainPrecise")) or _to_count(row.get("CycleCapacityRemain"))
        used = max(0.0, total - remain)
    return total, used


def _batch_source(row):
    """来源标签：优先 grantReason（Buddy 加油站签到 / 成长计划奖励 / 官方活动发放），回落包名。"""
    for a in (row.get("AccountAttributes") or []):
        if isinstance(a, dict) and a.get("Key") == "grantReason":
            v = str(a.get("Value") or "").strip()
            if v:
                return v
    return str(row.get("PackageName") or "").strip() or "其他"


def _fetch_ledger_rows(lib, endpoint, headers):
    """拉全量批次（free + paid 两路，去重合并）。返回 (rows, truncated)。"""
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept-Language", "zh-CN")
    rows, seen, truncated = [], set(), False
    reported = 0
    for path, codes in ((LEDGER_FREE_PATH, LEDGER_FREE_CODES), (LEDGER_PAID_PATH, LEDGER_PAID_CODES)):
        if not codes:
            continue
        got = 0
        for page in range(1, LEDGER_MAX_PAGES + 1):
            body = {"PackageCodes": codes, "PageNumber": page, "PageSize": LEDGER_PAGE_SIZE,
                    "IsDisplayTotalInfo": True}
            code, resp = lib.post(endpoint + path, hdrs, body, retry=False)
            if code != 200 or not isinstance(resp, dict):
                raise RuntimeError("明细接口返回异常（http=%s，%s）" % (code, path.rsplit("/", 1)[-1]))
            data = resp.get("data")
            if not isinstance(data, dict):
                break
            accs = data.get("Accounts")
            if not isinstance(accs, list) or not accs:
                break
            reported = max(reported, int(_to_count(data.get("TotalCount"))))
            for r in accs:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("ResourceId") or "").strip()
                key = rid or "%s|%s|%s" % (r.get("PackageCode"), r.get("CreateTime"), r.get("DealName"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
                got += 1
            if len(accs) < LEDGER_PAGE_SIZE:
                break
        # 单路已取尽但服务端声称更多 → 触顶截断，必须告警（客户端同款风险）
        if reported > len(rows):
            truncated = True
    return rows, truncated


def build_ledger(rows, warn_days, now_ms=None, plan=None, is_paid=False,
                 uid=None, label=None):
    """批次 → 事件流 + 汇总 + 最近到期 + 预警（全部在此算好，面板只负责展示）。

    uid/label：本批数据的归属账号。会写进每条事件（含 upcoming），供面板在
    「全部账号」合并视图里标注「来源：账号名」。面板不自行推断归属，避免串号。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    today = datetime.datetime.fromtimestamp(now_ms / 1000.0, tz=_BJ_TZ).date()
    events, upcoming, sources = [], [], {}
    available = 0.0
    expired_deducted = 0.0
    expired_batches = 0
    batch_count = 0

    def _day_diff(ms):
        """按自然日计的剩余天数（与用户本地日历一致）。"""
        return (datetime.datetime.fromtimestamp(ms / 1000.0, tz=_BJ_TZ).date() - today).days

    for r in rows:
        if _to_count(r.get("CapacityType")) == LEDGER_SLICE_TYPE:
            continue
        total, used = _batch_capacity(r)
        if total <= 0:
            continue
        batch_count += 1
        remain = max(0.0, total - used)
        status = r.get("Status")
        created = _parse_billing_ms(r.get("CreateTime"))
        end_ms = _parse_billing_ms(r.get("DeductionEndTime")) or _parse_billing_ms(r.get("CycleEndTime"))
        source = _batch_source(r)
        name = str(r.get("PackageName") or "").strip()
        rid = str(r.get("ResourceId") or "").strip() or ("b%d" % batch_count)

        src = sources.setdefault(source, {"name": source, "count": 0, "amount": 0.0})
        src["count"] += 1
        src["amount"] = round(src["amount"] + total, 2)

        events.append({
            "id": rid, "direction": "income", "kind": "grant",
            "source": source, "name": name,
            "uid": uid, "label": label,
            "amount": round(total, 2), "time": _iso_local(created), "timeMs": created,
            "used": round(used, 2), "remain": round(remain, 2), "total": round(total, 2),
            "status": status, "expireAt": _iso_local(end_ms),
        })

        # 支出：先按「已用完」（消耗），再按「已过期」（到期作废），两者互不重复计量
        if status == 3 and used > 0:
            t = _parse_billing_ms(r.get("ExpiredTime")) or end_ms
            events.append({
                "id": rid + ":used", "direction": "expense", "kind": "consume",
                "source": source, "name": name,
                "uid": uid, "label": label,
                "amount": round(used, 2), "time": _iso_local(t), "timeMs": t,
            })
            # 已用完的批次若也已过到期日，剩余为 0，不产生到期扣减
            if end_ms and end_ms <= now_ms:
                expired_batches += 1
        elif status == 2:
            if remain > 0:
                events.append({
                    "id": rid + ":expired", "direction": "expense", "kind": "expire",
                    "source": source, "name": name,
                    "uid": uid, "label": label,
                    "amount": round(remain, 2), "time": _iso_local(end_ms), "timeMs": end_ms,
                })
            if end_ms and end_ms <= now_ms:
                expired_deducted += remain
                expired_batches += 1

        # 汇总口径：已到期批次按批次累计扣减（不按余额或到期时间聚合）
        if end_ms and end_ms <= now_ms:
            continue
        if status in (0, 3) and remain > 0:
            available += remain
            days_left = _day_diff(end_ms) if end_ms else None
            upcoming.append({
                "id": rid, "source": source, "name": name,
                "uid": uid, "label": label,
                "amount": round(remain, 2), "total": round(total, 2), "used": round(used, 2),
                "expireAt": _iso_local(end_ms), "expireDate": (
                    datetime.datetime.fromtimestamp(end_ms / 1000.0, tz=_BJ_TZ).strftime("%Y-%m-%d") if end_ms else None),
                "created": _iso_local(created), "daysLeft": days_left,
                "warn": bool(days_left is not None and days_left <= warn_days),
            })

    upcoming.sort(key=lambda x: (x["expireAt"] or "9999", x["id"]))
    for b in upcoming:
        d = b.get("daysLeft")
        b["warnLevel"] = ("danger" if d is not None and d <= 1 else
                          "warning" if d is not None and d <= 3 else
                          "notice" if b.get("warn") else "none")

    # 事件按时间倒序；无时间戳的排最后（稳定按 id）
    events.sort(key=lambda e: (-(e.get("timeMs") or 0), e.get("id") or ""))

    within = [b for b in upcoming if b.get("warn")]
    alert = None
    if within:
        # 口径（唯一规则）：条幅只报「最早到期日」当天的剩余积分合计。
        # upcoming 已按 expireAt 升序 → within[0] 即最早；同一天的多批才合并。
        # 严禁把更晚到期的批次聚合进来（需求原文明确禁止）。
        earliest = within[0]["expireDate"]
        same_day = [b for b in within if b.get("expireDate") == earliest]
        amt = int(round(sum(b["amount"] for b in same_day)))
        dt = datetime.datetime.strptime(earliest, "%Y-%m-%d")
        alert = {
            "show": True,
            "total": amt,                       # 仅最早到期日当天的合计
            "raw": round(sum(b["amount"] for b in same_day), 2),
            "date": earliest,                   # 唯一日期规则：取最早
            "batchCount": len(same_day),        # 当天批数
            "withinTotal": int(round(sum(b["amount"] for b in within))),  # 预警窗口内全部合计（明细页用）
            "withinCount": len(within),
            "text": "%d 积分将于 %d 月 %d 日到期" % (amt, dt.month, dt.day),
        }

    return {
        "generatedAt": _iso_local(now_ms),
        "warnDays": warn_days,
        "summary": {
            "available": int(round(available)),
            "availableRaw": round(available, 2),
            "expiredDeducted": int(round(expired_deducted)),
            "expiredDeductedRaw": round(expired_deducted, 2),
            "expiredBatches": expired_batches,
            "batches": batch_count,
            "isPaid": bool(is_paid),
            "plan": str(plan or ""),
        },
        "upcoming": {
            "count": len(upcoming),
            "withinWarn": len(within),
            "batches": upcoming[:50],
            "nearest": upcoming[0] if upcoming else None,
        },
        "alert": alert,
        "sources": sorted(sources.values(), key=lambda s: (-s["count"], s["name"])),
        "events": events,
    }


def query_ledger(acc):
    """查询单个账号的积分明细。任何异常都收敛为 ok=False，绝不抛出。"""
    out = {"uid": acc["uid"], "label": acc["label"], "mode": acc["mode"], "ok": False}
    lib = signin_lib()
    path, cleanup = balance_auth_file(acc)
    try:
        try:
            session = lib.resolve_session(lib.load_session_retry(path))
            headers = lib.build_headers(session)
            endpoint = ((session.get("auth") or {}).get("endpoint")
                        or lib.DEFAULT_ENDPOINT).rstrip("/")
        except Exception as e:
            out["error"] = "auth"
            out["report"] = "凭据不可用：%s" % str(e)[:140]
            return out
        try:
            rows, truncated = _fetch_ledger_rows(lib, endpoint, headers)
        except Exception as e:
            msg = str(e)
            out["error"] = "auth" if ("401" in msg or "403" in msg) else "network"
            out["report"] = "明细查询失败：%s" % msg[:140]
            return out
        if not rows:
            # 接口通、确实没有批次 → 合法空态（不是错误）
            out.update(build_ledger([], ledger_config(),
                                    uid=acc["uid"], label=acc["label"]))
            out["truncated"] = truncated
            out["ok"] = True
            return out
        plan = None
        is_paid = False
        try:
            code, body = lib.post(endpoint + BALANCE_PATH, headers, {}, retry=False)
            parsed = parse_balance(body)
            if parsed:
                plan, is_paid = parsed.get("plan"), bool(parsed.get("is_paid"))
        except Exception:
            pass  # 汇总接口只影响「套餐」标注，失败不影响明细
        out.update(build_ledger(rows, ledger_config(), plan=plan, is_paid=is_paid,
                                uid=acc["uid"], label=acc["label"]))
        out["truncated"] = truncated
        out["ok"] = True
        return out
    except Exception as e:  # 兜底：任何意外都不能把面板带崩
        out["error"] = "unknown"
        out["report"] = "%s: %s" % (type(e).__name__, str(e)[:140])
        return out
    finally:
        if cleanup:
            try:
                os.remove(path)
            except OSError:
                pass


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
    ap.add_argument("--balance-json", action="store_true", dest="balance_json",
                    help="查询每个账号的可用积分余额，每账号一行 JSON（供面板消费）")
    ap.add_argument("--ledger-json", action="store_true", dest="ledger_json",
                    help="查询每个账号的积分明细（批次事件流+汇总+预警），每账号一行 JSON（供面板消费）")
    args = ap.parse_args()

    if not os.path.isfile(SIGNIN):
        print("缺少 signin.py：%s" % SIGNIN)
        return 2

    if args.balance_json:
        # 余额查询：与签到完全隔离（不加 .lock、不续期、不写 signin.log）。
        # 每账号一行，立即刷出；失败也照样给行（带 error 字段），面板逐行更新。
        accounts = discover()
        if not accounts:
            print(json.dumps({"event": "balance", "ok": False, "error": "no_accounts",
                              "report": "未发现任何国内站凭据"}, ensure_ascii=False))
            return 2
        ok_all = True
        for acc in accounts:
            out = query_balance(acc)
            out["event"] = "balance"
            print(json.dumps(out, ensure_ascii=False), flush=True)
            ok_all = ok_all and bool(out.get("ok"))
        return 0 if ok_all else 1

    if args.ledger_json:
        # 积分明细：与签到完全隔离（不加 .lock、不续期、不写 signin.log）。
        # 每账号一行 JSON（含事件流），失败也照给行（带 error 字段）。
        accounts = discover()
        if not accounts:
            print(json.dumps({"event": "ledger", "ok": False, "error": "no_accounts",
                              "report": "未发现任何国内站凭据"}, ensure_ascii=False))
            return 2
        ok_all = True
        for acc in accounts:
            out = query_ledger(acc)
            out["event"] = "ledger"
            print(json.dumps(out, ensure_ascii=False), flush=True)
            ok_all = ok_all and bool(out.get("ok"))
        return 0 if ok_all else 1

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
