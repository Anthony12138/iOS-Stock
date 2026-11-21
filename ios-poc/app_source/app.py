#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一夜持股战法 · A股筛选系统
------------------------------------------------
本地运行，自动打开浏览器。数据来源：东方财富公开行情接口。
仅用于研究与复盘，不构成任何投资建议。

结构：
  - 快照层：全市场主板实时行情（涨幅/量比/换手/流通市值/主力净额）
  - 漏斗层：前 4 条硬性条件快速过滤
  - 深度层：日K（量能形态、均线形态）+ 分时（均价线、日内新高）逐只打分
  - 复盘层：保存当次选股，次日自动结算开盘/最高/收盘收益
"""

import os
import re
import sys
import json
import time
import math
import random
import threading
import csv
import io
import webbrowser
import urllib.request
import urllib.parse
import http.client
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _TZ = timezone(timedelta(hours=8))          # 中国无夏令时，固定 UTC+8 等价

APP_TITLE = "一夜持股战法 · A股筛选系统"
VERSION = "1.7.3"
HOST = "127.0.0.1"
BASE_PORT = 8765

# 手机端（iOS 的 ios_boot 启动时）会把 NIGHT_LIGHT 设成 1：并发线程砍到 4，
# 解释执行环境下线程越多越慢，内存也越高（每线程一份栈 + 缓冲）。
LIGHT = bool(os.environ.get("NIGHT_LIGHT"))


def _w(n):
    return min(n, 4) if LIGHT else n


def _wn(n):
    """网络等待型任务的并发上限（拉日K / 分时这类 I/O 等待）。
    等待网络时不占 CPU、不持 GIL，手机端 4 个线程把 200 只日K排成 50 波，
    是 AI 荐股冷扫 68 秒的主因；这类线程提到 8 内存增量只有几 MB，
    实测可以接受。CPU 密集的线程池仍走 _w(4)。"""
    return min(n, 8) if LIGHT else n


def now_cn():
    """统一用北京时间，避免机器时区不是 UTC+8 时全部时段判断错位。"""
    return datetime.now(_TZ)


# ---------------------------------------------------------------- 交易日历
# 周末由 is_holiday() 直接判断，下面只存「工作日中的休市日」。
# 自动获取：每年首次启动联网抓当年安排并缓存，跨年自动重取（见 start_holidays_auto）。
# 离线兜底：内置 2026 年上交所公告（上证公告〔2025〕45 号）里的 19 个工作日休市日。
BUILTIN_HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

HOLIDAYS = set()                 # 当前生效集合；is_holiday / market_status 直接引用
_hol_mtime = [None]              # 文件修改时间，用来做热加载
_hol_lock = threading.RLock()      # 可重入：load_holidays 持锁时会再调 _hol_write
_hol_net_lock = threading.Lock()
_hol_state = {
    "year": 0, "source": "builtin", "updated_at": "", "manual": False,
    "auto": True, "last_error": "", "checking": False,
    "last_fail_ts": 0.0, "note": "",
}


def _hol_clean(days):
    """只保留合法的 YYYY-MM-DD，避免坏数据把整年判断带偏。"""
    out = set()
    for x in days or []:
        v = str(x)[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            continue
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except Exception:
            continue
        out.add(v)
    return out


def _hol_parse_text(v):
    """手动编辑框：一行一个日期，也兼容逗号/空格/中文年月日。"""
    raw = "\n".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "")
    out = set()
    for m in re.finditer(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", raw):
        try:
            out.add(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    .date().strftime("%Y-%m-%d"))
        except Exception:
            continue
    return out


def _hol_read():
    try:
        with open(HOLIDAYS_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if isinstance(d, list):                      # 兼容旧版纯数组格式，视为手动维护
        return _hol_clean(d), {"year": now_cn().year, "source": "manual",
                               "updated_at": "", "manual": True}
    if not isinstance(d, dict):
        return None
    days = _hol_clean(d.get("holidays") or [])
    try:
        year = int(d.get("year") or 0)
    except Exception:
        year = 0
    return days, {"year": year, "source": str(d.get("source") or "auto"),
                  "updated_at": str(d.get("updated_at") or ""),
                  "manual": bool(d.get("manual")),
                  "note": str(d.get("note") or "")}


def _hol_write(days, meta):
    # 所有调用路径（自动刷新 / 手动保存 / 内置写入）统一在此持锁，
    # 且临时文件名带线程 id，避免两个线程同时写同一个 .tmp 交叉 replace 写坏JSON。
    with _hol_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{HOLIDAYS_FILE}.tmp.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"year": meta.get("year", now_cn().year),
                       "source": meta.get("source", "auto"),
                       "updated_at": meta.get("updated_at", ""),
                       "manual": bool(meta.get("manual")),
                       "note": str(meta.get("note") or ""),
                       "holidays": sorted(days)}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HOLIDAYS_FILE)           # 原子写入


def _hol_ensure_file():
    """文件不存在时落一份可读模板：用户打开就能看到内容，不用猜格式。"""
    if os.path.exists(HOLIDAYS_FILE):
        return
    try:
        _hol_write(set(BUILTIN_HOLIDAYS), {"year": 2026, "source": "builtin",
                                           "updated_at": "", "manual": False,
                                           "note": "首次启动：待后台自动联网获取当年安排"})
    except Exception:
        pass


def _fetch_holiday_lib(year, timeout=8):
    """公开节假日库，返回当年全部日期；holiday_recess==1 表示放假（含调休区间）。
    A 股休市 = 周末 ∪ 放假区间，周末由 is_holiday 自己判断，这里只取工作日部分。
    已核对：2026 年结果与上交所上证公告〔2025〕45 号逐日一致（19 个工作日休市）。
    免费接口偶发限流（code 80006），失败时由 _fetch_trade_days 这条独立通道兜底。"""
    url = ("https://api.apihubs.cn/holiday/get?year=%d"
           "&field=date,holiday_recess&size=400" % year)
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("code") != 0:
                raise ValueError("节假日接口返回 code=%s (%s)" %
                                 (d.get("code"), d.get("msg") or ""))
            rows = (d.get("data") or {}).get("list") or []
            out = set()
            for x in rows:
                if x.get("holiday_recess") != 1:
                    continue
                v = str(x.get("date") or "")
                if len(v) != 8 or not v.isdigit():
                    continue
                try:
                    day = datetime.strptime(v, "%Y%m%d").date()
                except Exception:
                    continue
                if day.year == year and day.weekday() < 5:
                    out.add(day.strftime("%Y-%m-%d"))
            if not (8 <= len(out) <= 30):        # 解析异常时宁可用兜底，不写脏数据
                raise ValueError("节假日接口数据异常：%d 个工作日休市" % len(out))
            return out
        except Exception as e:
            last = e
            if attempt == 0 and "80006" not in str(e):
                time.sleep(1)                    # 仅网络抖动重试，限流不硬撞
                continue
            break
    raise last


def _fetch_trade_days(year, min_rows=60):
    """独立自动通道：复用程序本来就要拉的上证指数日 K，反推真实交易日。
    不依赖任何第三方节假日库，还能自动捕捉临时休市与节假日库的错误。
    返回全部 K 线日期（含跨年部分），反推时才能覆盖年初 1/1 这类休市日。"""
    try:
        bars = fetch_kline("000001", 1, 300)
    except Exception:
        return set(), None
    all_days = set()
    for b in bars or []:
        v = str(b.get("date") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            all_days.add(v)
    own = {d for d in all_days if d.startswith("%d-" % year)}
    if len(own) < min_rows:                      # 数据残缺时宁可不校正
        return set(), None
    return all_days, max(all_days)


def _auto_holidays(year):
    """两条自动通道合并：节假日库覆盖全年（含未来），真实交易日校正已发生区间。"""
    lib, lib_err = None, ""
    try:
        lib = _fetch_holiday_lib(year)
    except Exception as e:
        lib_err = "%s: %s" % (type(e).__name__, e)
    trade_days, last = _fetch_trade_days(year)
    if lib is None and not trade_days:
        raise ValueError("节假日库不可用（%s），行情交易日也取不到" % lib_err)
    out = set(lib or ())
    if trade_days:
        d = datetime.strptime(min(trade_days), "%Y-%m-%d").date()
        end = datetime.strptime(last, "%Y-%m-%d").date()
        while d <= end:                          # 只校正 K 线已覆盖的区间
            iso = d.isoformat()
            if d.year == year and d.weekday() < 5:
                if iso in trade_days:
                    out.discard(iso)             # 有成交 = 一定是交易日
                else:
                    out.add(iso)                 # 没成交 = 休市（含临时休市）
            d += timedelta(days=1)
    if lib is not None:
        return out, "auto", ""
    return out, "auto-kline", "节假日库暂不可用，已按真实交易日反推；未来节假日待下次更新补齐"


def load_holidays(force=False):
    """热加载：文件 mtime 一变就重读，所以直接改 data/holidays.json 也无需重启。"""
    global HOLIDAYS
    try:
        mt = os.stat(HOLIDAYS_FILE).st_mtime_ns
    except OSError:
        mt = None
    if not force and mt == _hol_mtime[0]:
        return HOLIDAYS
    with _hol_lock:
        try:
            mt = os.stat(HOLIDAYS_FILE).st_mtime_ns
        except OSError:
            mt = None
        if not force and mt == _hol_mtime[0]:
            return HOLIDAYS
        got = _hol_read()
        if got is None or not got[0]:
            days, meta = set(BUILTIN_HOLIDAYS), {"year": 2026, "source": "builtin",
                                                "updated_at": "", "manual": False}
        else:
            days, meta = got
        # 原子整体替换（而非 clear+update）：clear 与 update 之间读者会看到空集合，
        # 把交易日误判成交易日之外的日子。整体换新集合对读者要么旧要么新，无空窗。
        HOLIDAYS = days
        for k in ("year", "source", "updated_at", "manual", "note"):
            _hol_state[k] = meta.get(k, _hol_state[k])
        _hol_mtime[0] = mt
    return HOLIDAYS


def refresh_holidays(year=None, force=False):
    """联网刷新当年休市安排并落盘，成功后立即生效。失败保留上次结果。
    自动任务在失败后 6 小时内不重试，避免撞免费接口限流；手动点按钮可强制重试。"""
    try:
        y = int(year or now_cn().year)
    except (TypeError, ValueError):
        return {"ok": False, "error": "年份不合法"}
    if not force and _hol_state["last_fail_ts"] and \
            time.time() - _hol_state["last_fail_ts"] < 6 * 3600:
        return {"ok": False, "error": "上次更新失败，6 小时内不自动重试；可点「立即更新」强制重试"}
    if not _hol_net_lock.acquire(blocking=False):
        return {"ok": False, "error": "正在更新中，请稍候"}
    try:
        _hol_state["checking"] = True
        days, src, note = _auto_holidays(y)
        meta = {"year": y, "source": src,
                "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
                "manual": False, "note": note}
        _hol_write(days, meta)
        load_holidays(force=True)
        _hol_state["last_error"] = ""
        _hol_state["last_fail_ts"] = 0.0
        _hol_state["note"] = note
        return {"ok": True, "count": len(days), "year": y,
                "source": src, "note": note, "updated_at": meta["updated_at"]}
    except Exception as e:
        _hol_state["last_error"] = "%s: %s" % (type(e).__name__, e)
        _hol_state["last_fail_ts"] = time.time()
        return {"ok": False, "error": _hol_state["last_error"]}
    finally:
        _hol_state["checking"] = False
        _hol_net_lock.release()


def save_holidays_manual(raw):
    """手动维护：保存后同年不再被自动覆盖，直到点「恢复自动」。"""
    days = _hol_parse_text(raw)
    note = "手动维护中，自动更新已暂停；点「立即更新」即恢复自动"
    meta = {"year": now_cn().year, "source": "manual",
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "manual": True, "note": note}
    _hol_write(days, meta)
    load_holidays(force=True)
    _hol_state["last_fail_ts"] = 0.0
    _hol_state["note"] = note
    return {"ok": True, "count": len(days), "manual": True}


def save_holidays_builtin():
    days = set(BUILTIN_HOLIDAYS)
    note = "已恢复内置 2026 官方表（离线）；点「立即更新」重新启用联网获取"
    meta = {"year": 2026, "source": "builtin",
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "manual": True, "note": note}
    _hol_write(days, meta)
    load_holidays(force=True)
    _hol_state["last_fail_ts"] = 0.0
    _hol_state["note"] = note
    return {"ok": True, "count": len(days), "builtin": True}


def holidays_info():
    load_holidays()
    st = _hol_state
    return {"ok": True, "year": st["year"], "current_year": now_cn().year,
            "stale": st["year"] != now_cn().year,
            "list": sorted(HOLIDAYS), "count": len(HOLIDAYS),
            "source": st["source"], "updated_at": st["updated_at"],
            "manual": st["manual"], "auto": st["auto"],
            "checking": st["checking"], "last_error": st["last_error"],
            "note": st["note"],
            "file": HOLIDAYS_FILE, "builtin": sorted(BUILTIN_HOLIDAYS),
            "market": market_status()}


def _hol_need_refresh():
    st = _hol_state
    if st["manual"]:                              # 手动改过的同年不自动覆盖
        return False
    y = now_cn().year
    if st["year"] != y:
        return True
    try:
        last = datetime.strptime(st["updated_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    # 11 月起次年公告陆续发布，每周复核；平时每月复核一次。
    # 只靠行情反推时没有未来节假日，每 6 小时再试一次让节假日库尽快补上。
    gap = 0.25 if st["source"] == "auto-kline" else (7 if now_cn().month >= 11 else 30)
    return (now_cn().replace(tzinfo=None) - last).total_seconds() >= gap * 86400


def _holidays_auto_loop():
    time.sleep(3)
    while True:
        try:
            if _hol_need_refresh():
                refresh_holidays()
        except Exception:
            pass
        time.sleep(3600)                          # 每小时检查一次是否需要重取


def start_holidays_auto():
    t = threading.Thread(target=_holidays_auto_loop, daemon=True, name="holiday-auto")
    t.start()
    return t


def is_holiday(d=None):
    # 跨年头一小时内后台线程还没唤醒（每小时一次）时，节假日仍是上一年的，
    # 会把元旦等假日误判为交易日。检测到年份变化就触发一次刷新；
    # v1.6.5：改后台线程执行——is_holiday 被每个响应/卡片高频调用，同步联网会
    # 阻塞首批请求数秒~数十秒。非阻塞锁自保，重复触发不会叠加网络请求。
    try:
        if _hol_state.get("year") != now_cn().year:
            t = threading.Thread(target=refresh_holidays, daemon=True, name="holiday-crossyear")
            t.start()
    except Exception:
        pass
    load_holidays()
    d = d or now_cn().date()
    return d.weekday() >= 5 or d.strftime("%Y-%m-%d") in HOLIDAYS


def last_trade_date():
    """最近一个（含今天）非周末、非节假日的交易日，用于标注快照实际数据日期。"""
    d = now_cn().date()
    for _ in range(20):
        if not is_holiday(d):
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)
    return now_cn().strftime("%Y-%m-%d")


def bad_symbol(name):
    """排除新股/次新/除权除息/未盈利/退市等简称标识，这些不适用 10% 隔夜战法。"""
    n = str(name or "").upper().replace(" ", "")
    return any(n.startswith(x) for x in ("N", "C", "XD", "XR", "DR", "U"))


def list_days_ok(list_date, min_days=60):
    """上市满 N 个自然日才纳入（新浪兜底源没有该字段，缺失时不拦）。
    东财 f26 给的是 8 位 YYYYMMDD，早期版本按 10 位带横杠解析会直接 return True，
    等于主数据源的次新股过滤从未生效；这里统一抽数字按 8 位解析。"""
    digits = re.sub(r"\D", "", str(list_date or ""))[:8]
    if len(digits) != 8:
        return True
    try:
        d = datetime.strptime(digits, "%Y%m%d").date()
    except Exception:
        return True
    return (now_cn().date() - d).days >= min_days


def limit_pct(code, name=""):
    """按板块/风险状态返回涨跌停幅度（%）：
       创业板/科创板（含 ST）20、北交所 30、主板 ST 5、主板 10。

       判断顺序必须先板块后 ST：2020 年注册制后创业板/科创板即使戴帽仍是 20%。
       旧写法先判 ST 一律给 5%，会让 _align_bars 把 20% 的正常涨停误判成除权、
       把此前全部历史 K 线按比例缩放，该股 MA/MACD/RSI/ATR/筹码随之全线失真。
       退市整理期（简称带「退」）常态仍是 10%，不再按 ST 的 5% 处理。"""
    c = str(code or "")
    if c.startswith(("300", "301", "688", "689")):
        return 20.0
    if c.startswith(("8", "4", "92")):
        return 30.0
    n = str(name or "").upper()
    if "ST" in n:
        return 5.0
    return 10.0


def r2(x):
    """交易所口径：四舍五入到分（Python round 是银行家舍入，会算错涨跌停价）。"""
    try:
        return float(Decimal(str(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def limit_price(prev_close, up, code="", name=""):
    """由真实前收算涨跌停价，按板块幅度 + 四舍五入到分。"""
    pct = limit_pct(code, name) / 100.0
    return r2(float(prev_close) * (1 + pct if up else 1 - pct))


def _num(v, default=0.0):
    """东财对无值字段会返回字符串 '-'，直接 float() 会抛异常并丢掉整只股票。"""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else default
    t = str(v).strip().replace(",", "")
    if t in ("", "-", "--", "null", "None"):
        return default
    try:
        f = float(t)
        return f if math.isfinite(f) else default
    except Exception:
        return default


MIN_SNAPSHOT_ROWS = 2000          # 沪深主板约 3000 只，低于此值视为分页被限流/截断

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

FS_MAIN = "m:0+t:6,m:1+t:2"          # 深市主板(000/001/002/003) + 沪市主板(60xxxx)
SNAP_FIELDS = ("f2,f3,f4,f5,f6,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f26,f62,f100,f115")

# ---------------------------------------------------------------- 路径处理
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = app_dir()
WEB_DIR = os.path.join(res_dir(), "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
HIST_DIR = os.path.join(DATA_DIR, "history")
HOLIDAYS_FILE = os.path.join(DATA_DIR, "holidays.json")
WATCH_FILE = os.path.join(DATA_DIR, "watchlist.json")

for d in (DATA_DIR, HIST_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass

# 交易日历：先落模板并载入，再起后台线程自动获取当年安排
_hol_ensure_file()
load_holidays(force=True)
start_holidays_auto()


# ---------------------------------------------------------------- HTTP 工具
_cache = {}
_cache_lock = threading.Lock()


# ---- 长连接快路径：urllib 每次请求都要完整 TCP+TLS 握手，手机网络一次 0.3~0.5 秒，
#      一轮 AI 荐股 200 只日K累计白白浪费 15~20 秒。这里按线程复用 HTTPS 连接，
#      同一线程对同一主机只握一次手。任何异常（连接被服务端断开 / 非 200 / 超时）
#      都返回 None 并回落到下面的 urllib 老路径，行为与旧版完全一致，只快不慢。
#      快照(push2)、调研(datacenter-web)、量比补齐(qt.gtimg) 也在白名单内：
#      冷扫快照约 54 页，以前每页都重新握手。连接必须按主机名分开存——
#      单连接跨主机复用会把 GET 发到错误的服务器，白跑一趟再回落。
_KA_HOSTS = {"push2his.eastmoney.com", "web.ifzq.gtimg.cn", "money.finance.sina.com.cn",
             "push2.eastmoney.com", "datacenter-web.eastmoney.com", "qt.gtimg.cn"}
_tls = threading.local()


def _ka_fetch(url, timeout, headers):
    """长连接 GET：成功返回 body(bytes)，失败返回 None（由调用方走 urllib 回落）。
    连接坏掉时自动重建一次——服务端 keep-alive 空闲超时断开是常态，不能让用户重试。"""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.netloc not in _KA_HOSTS:
        return None
    path = u.path + ("?" + u.query if u.query else "")
    conns = getattr(_tls, "conns", None)
    if conns is None:
        conns = {}
        _tls.conns = conns
    for attempt in (0, 1):            # 第一次用现成连接；失败重建连接再试一次
        fresh = u.netloc not in conns   # 刚新建的连接没人复用，非 200 时要关掉防 socket 泄漏
        conn = conns.get(u.netloc)
        if conn is None:
            try:
                conn = http.client.HTTPSConnection(u.netloc, timeout=timeout)
            except Exception:
                return None
        try:
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                r.close() if hasattr(r, "close") else None
                if fresh:               # 新建连接既不缓存复用也不留着等 GC，直接关掉
                    try:
                        conn.close()
                    except Exception:
                        pass
                return None           # 非 200 交给 urllib 路径，保持原有报错/重试语义
            conns[u.netloc] = conn    # 读完整 body 才能复用连接
            return body
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conns.pop(u.netloc, None)
            conn = None
            if attempt:               # 重建后仍失败：回落 urllib
                return None
    return None


def http_json(url, timeout=8, retries=3):
    """带退避重试的请求；东财限流时由上层自动切到腾讯/新浪"""
    body = _ka_fetch(url, timeout, HEADERS)
    if body is not None:
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            pass                      # 响应体异常也走 urllib 路径重试
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            return json.loads(body.decode("utf-8", "replace"))
        except Exception as e:          # 网络抖动 / 限流，退避重试
            last = e
            time.sleep(0.35 * (i + 1) + random.random() * 0.2)
    raise last


def http_text(url, encoding="utf-8", timeout=8, retries=3, headers=None):
    body = _ka_fetch(url, timeout, headers or HEADERS)
    if body is not None:
        try:
            return body.decode(encoding, "replace")
        except Exception:
            pass
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(encoding, "replace")
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1) + random.random() * 0.2)
    raise last


# 东财被限流后的冷却时间（秒），期间直接用备用源，避免每次等待超时
_EM_BLOCK = {"until": 0.0}
_EM_LOCK = threading.Lock()


def em_ok():
    with _EM_LOCK:
        return time.time() >= _EM_BLOCK["until"]


def em_mark_fail(seconds=120):
    with _EM_LOCK:
        _EM_BLOCK["until"] = time.time() + seconds


class EmptyData(Exception):
    """源可用，但这只票没有数据（停牌/代码不存在），不触发数据源拉黑"""


# 各数据源健康状态：某个源被限流后短暂拉黑，避免每只股票都等一次超时
_SRC_BLOCK = {}
_SRC_LOCK = threading.Lock()


def src_ok(name):
    with _SRC_LOCK:
        return time.time() >= _SRC_BLOCK.get(name, 0.0)


def src_fail(name, seconds=90):
    with _SRC_LOCK:
        _SRC_BLOCK[name] = time.time() + seconds



_inflight = {}
_CACHE_MAX = 3000


def cached(key, ttl, producer):
    """同一 key 只允许一个线程真正去请求（单飞），其余等待并共享同一份结果；
    TTL 从「请求完成」起算，长时间分页不会白吃掉有效期；缓存最多 3000 条，超出淘汰最旧。

    owner 无论成功还是失败，都把结果（含异常）发布到 Event 上供等待者共享，
    避免上游故障时 N 个等待者各自重放 producer 形成请求风暴（v1.6.5）。
    owner 取数失败时，等待者仍可用未过期的旧值；旧值也过期则共享抛出 owner 的异常。"""
    while True:
        now = time.time()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            ev = _inflight.get(key)
            owner = ev is None
            if owner:
                ev = threading.Event()
                ev.result = None          # owner 完成后填 (ok, val_or_exc)
                _inflight[key] = ev
        if owner:
            ok = False
            try:
                val = producer()
                with _cache_lock:
                    _cache[key] = (time.time(), val)
                    if len(_cache) > _CACHE_MAX:
                        for k in sorted(_cache, key=lambda k: _cache[k][0])[:_CACHE_MAX // 5]:
                            _cache.pop(k, None)
                ok = True
                ev.result = (True, val)
                return val
            except BaseException as ex:
                with _cache_lock:
                    if not ok:
                        stale = _cache.get(key)
                        if stale and time.time() - stale[0] >= ttl:
                            _cache.pop(key, None)
                ev.result = (False, ex)
                raise
            finally:
                # 必须在 finally 里唤醒等待者，且只摘掉「自己注册的那个」Event，
                # 避免极端超时被接管后误删新 owner 的单飞标记。
                with _cache_lock:
                    if _inflight.get(key) is ev:
                        _inflight.pop(key, None)
                ev.set()
        # 等待者：owner 完成会 set 唤醒；卡死时每 45s 复核一次，若 owner 已消失则接管
        ev.wait(45)
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        r = getattr(ev, "result", None)
        if r is not None:
            ok, val = r
            if ok:
                return val
            raise val
    # while True：只有拿到结果或共享异常才会退出，绝不出现 N 个线程各自直连上游


def cache_peek(key, max_age):
    """只读缓存、绝不触发取数。给「用已拉取的数据快速换算」这类场景用：
       没有缓存或超过 max_age 就返回 None，由调用方降级，热路径上不产生任何请求。"""
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts > max_age:
            return None
        return val


# ---------------------------------------------------------------- 行情抓取
def _parse_row(d):
    """逐字段安全解析：'-' / None 只记缺失，不让整只股票从快照消失。"""
    try:
        code = str(d.get("f12", ""))
        name = str(d.get("f14", ""))
        price = _num(d.get("f2"), 0.0)
        if price <= 0:
            return None
        miss = []
        net_raw = d.get("f62")
        if not isinstance(net_raw, (int, float)) or str(net_raw).strip() in ("-", "", "--"):
            miss.append("main_net")
        pe_ttm = _num(d.get("f115"), _num(d.get("f9"), 0.0))
        if pe_ttm <= 0:
            miss.append("pe_ttm")
        if not _num(d.get("f10"), 0.0):
            miss.append("volratio")
        return {
            "code": code,
            "name": name,
            "market": d.get("f13"),                 # 1=沪 0=深
            "price": price,
            "prev_close": _num(d.get("f18"), 0.0),
            "pct": _num(d.get("f3"), 0.0),
            "volume": _num(d.get("f5"), 0.0),      # 手
            "amount": _num(d.get("f6"), 0.0),      # 元
            "turnover": _num(d.get("f8"), 0.0),    # %
            "volratio": _num(d.get("f10"), 0.0),
            "high": _num(d.get("f15"), 0.0),
            "low": _num(d.get("f16"), 0.0),
            "open": _num(d.get("f17"), 0.0),
            "mcap": _num(d.get("f20"), 0.0),
            "fcap": _num(d.get("f21"), 0.0),       # 流通市值 元
            "list_date": str(d.get("f26") or ""),
            "main_net": None if "main_net" in miss else _num(net_raw, 0.0),
            "industry": str(d.get("f100") or ""),
            "pe": _num(d.get("f9"), 0.0),
            "pb": _num(d.get("f23"), 0.0),
            "pe_ttm": pe_ttm,
            "_miss": miss,
        }
    except Exception:
        return None


def _tencent_symbol(code, market):
    return ("sh" if market == 1 else "sz") + code


def _em_snapshot():
    """东方财富：一次拿全全部字段（含量比、主力净额、行业）"""
    base = ("https://push2.eastmoney.com/api/qt/clist/get?"
            "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
            f"&fs={FS_MAIN}&fields={SNAP_FIELDS}")
    first = http_json(base.format(pn=1), retries=2)
    data = first.get("data") or {}
    rows = list(data.get("diff") or [])
    if not rows:
        raise RuntimeError("eastmoney empty")
    total = int(data.get("total") or 0)
    pages = math.ceil(total / 100)

    if pages > 1:
        def get_page(pn):
            for _ in range(3):
                try:
                    j = http_json(base.format(pn=pn), retries=1)
                    d = (j.get("data") or {}).get("diff")
                    if d is not None:
                        return d
                except Exception:
                    pass
                time.sleep(0.3 + random.random() * 0.3)
            return None                      # 明确标记失败，别当成"这一页没有股票"

        with ThreadPoolExecutor(max_workers=_wn(8)) as ex:
            for chunk in ex.map(get_page, range(2, pages + 1)):
                if chunk:
                    rows.extend(chunk)

    out, seen = [], set()
    for d in rows:
        r = _parse_row(d)
        if r and r["code"] not in seen and is_main_board(r["code"]):
            seen.add(r["code"])
            out.append(r)
    if total and len(rows) < total * 0.95:
        raise RuntimeError("eastmoney incomplete: %d/%d pages" % (len(rows), total))
    if len(out) < MIN_SNAPSHOT_ROWS:
        raise RuntimeError("eastmoney incomplete: only %d main-board rows" % len(out))
    return out


def _sina_snapshot():
    """新浪：全市场分页（无「量比」，后面用腾讯批量补）"""
    H = dict(HEADERS)
    H["Referer"] = "https://finance.sina.com.cn/"
    base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={p}&num=100&sort=changepercent&asc=0"
            "&node=hs_a&symbol=&_s_r_a=page")

    def page(p):
        for _ in range(3):
            try:
                txt = http_text(base.format(p=p), headers=H, timeout=9, retries=2)
                txt = txt.strip()
                if not txt or txt in ("null", "[]"):
                    return []
                return json.loads(txt)
            except Exception:
                time.sleep(0.4 + random.random() * 0.3)
        return []

    first = page(1)
    if not first:
        raise RuntimeError("sina empty")
    rows = list(first)
    # 动态翻页：每轮并发拉 10 页，整轮全空算一记空轮；连续 2 记空轮认定翻到底。
    # 空页可能只是被限流，所以不能见空就停；也不再写死 69 页（约 6900 行）——
    # 主板扩容/页大小变化时不会静默截断，200 页硬上限防失控。
    empty_rounds, p0 = 0, 1
    with ThreadPoolExecutor(max_workers=_wn(8)) as ex:
        while p0 < 200 and empty_rounds < 2:
            window = list(ex.map(page, range(p0 + 1, p0 + 11)))
            got = 0
            for chunk in window:
                if chunk:
                    rows.extend(chunk)      # 空页可能只是被限流，后面还有数据，不能停
                    got += 1
            empty_rounds = 0 if got else empty_rounds + 1
            p0 += 10

    out, seen = [], set()
    for d in rows:
        try:
            code = str(d.get("code") or "")
            if not is_main_board(code):
                continue
            price = float(d.get("trade") or 0)
            if price <= 0 or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "name": d.get("name") or "",
                "market": 1 if code.startswith("6") else 0,
                "price": price,
                "prev_close": float(d.get("settlement") or 0),
                "pct": float(d.get("changepercent") or 0),
                "volume": float(d.get("volume") or 0) / 100.0,     # 股 → 手
                "amount": float(d.get("amount") or 0),
                "turnover": float(d.get("turnoverratio") or 0),
                "volratio": 0.0,                                    # 待补
                "high": float(d.get("high") or 0),
                "low": float(d.get("low") or 0),
                "open": float(d.get("open") or 0),
                "mcap": float(d.get("mktcap") or 0) * 1e4,
                "fcap": float(d.get("nmc") or 0) * 1e4,
                "list_date": "",
                "main_net": None,                   # 新浪源无主力净额：缺失，不是"零流入"
                "industry": "",
                "pe": float(d.get("per") or 0),
                "pb": float(d.get("pb") or 0),
                "pe_ttm": float(d.get("per") or 0),  # 动态 PE 冒充 TTM，标记降级
                "_miss": ["main_net", "volratio", "industry", "list_date", "pe_ttm"],
            })
        except Exception:
            continue
    if len(out) < MIN_SNAPSHOT_ROWS:
        raise RuntimeError("sina incomplete: only %d main-board rows" % len(out))
    return out


def is_main_board(code):
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def _norm_rows(rows):
    """下游评分有大量算术，统一把缺失的 main_net 折成 0；缺失事实保留在 _miss 里，
    评分函数据此对该维度降权，输出层据此把前端数值显示为「—」。"""
    if not rows:
        # 源返回 None / 空列表时明确报错，让 fetch_snapshot 走降级分支，
        # 而不是把「空快照」当成功结果缓存 25 秒
        raise RuntimeError("snapshot rows empty")
    for r in rows:
        if r.get("main_net") is None:
            r["main_net"] = 0.0
        if "_miss" not in r:
            r["_miss"] = []
    return rows


def _last_snapshot():
    with _cache_lock:
        old = _cache.get("snapshot")
        return old[1] if old else None


def snap_meta(pack):
    """五板块响应统一的「数据可信度」元信息，前端顶栏据此显示来源 / 降级 / 数据日期。"""
    pack = pack or {}
    return {
        "stale": bool(pack.get("stale")),
        "degraded_fields": list(pack.get("degraded_fields") or []),
        "data_date": pack.get("data_date") or last_trade_date(),
    }


def fetch_snapshot(force=False):
    """全市场主板快照，东财优先，失败自动切新浪；两源都挂时返回上一份快照 + stale。"""
    def build():
        if em_ok():
            try:
                rows = _norm_rows(_em_snapshot())
                return {"source": "eastmoney", "rows": rows, "stale": False,
                        "degraded_fields": [], "data_date": last_trade_date()}
            except Exception:
                em_mark_fail(180)
        try:
            rows = _norm_rows(_sina_snapshot())
            return {"source": "sina", "rows": rows, "stale": False,
                    "degraded_fields": ["main_net", "volratio", "industry", "list_date", "pe_ttm"],
                    "data_date": last_trade_date()}
        except Exception:
            pass
        old = _last_snapshot()
        if old and old.get("rows"):
            return {**old, "stale": True,
                    "degraded_fields": ["main_net", "volratio", "industry", "list_date", "pe_ttm"],
                    "data_date": old.get("data_date") or last_trade_date()}
        return {"source": "none", "rows": [], "stale": True,
                "degraded_fields": ["price", "pct", "volratio", "turnover", "main_net"],
                "data_date": ""}

    if force:
        # 先等正在进行的单飞 owner 结束，再清缓存，避免 force 请求搭 owner 的
        # 旧结果（v1.6.5）：否则手动刷新与自动重扫重叠时 force 等于无效。
        with _cache_lock:
            _ev = _inflight.get("snapshot")
        if _ev is not None:
            _ev.wait(60)
        with _cache_lock:
            for k in list(_cache):
                if k.startswith("snapshot"):
                    _cache.pop(k, None)
    return cached("snapshot", 25, build)


def enrich_volratio(rows):
    """用腾讯批量行情补「量比」（新浪源缺失时）
    注意：rows 来自 fetch_snapshot() 的共享缓存，必须先拷副本再写，
    否则补进的量比会污染 25 秒内所有命中该缓存的请求，且与 source 元信息脱节。"""
    rows = [dict(r) for r in rows]
    need = [r for r in rows if not r.get("volratio")]
    if not need:
        return rows, 0
    H = dict(HEADERS)
    H["Referer"] = "https://gu.qq.com/"
    filled = 0
    chunks = [need[i:i + 50] for i in range(0, len(need), 50)]

    def one(batch):
        q = ",".join(_tencent_symbol(r["code"], r["market"]) for r in batch)
        try:
            txt = http_text("https://qt.gtimg.cn/q=" + q, encoding="gbk", headers=H, retries=2)
        except Exception:
            return 0
        got = 0
        for line in txt.splitlines():
            if '="' not in line:
                continue
            try:
                f = line.split('="', 1)[1].rstrip('";').split("~")
                if len(f) < 50:
                    continue
                code = f[2]
                rec = next((r for r in batch if r["code"] == code), None)
                if not rec:
                    continue
                rec["volratio"] = float(f[49] or 0)
                rec["_miss"] = [k for k in rec.get("_miss", []) if k != "volratio"]
                got += 1
            except Exception:
                continue
        return got

    with ThreadPoolExecutor(max_workers=_w(6)) as ex:
        for n in ex.map(one, chunks):
            filled += n
    return rows, filled


def _tx_quote(code, market):
    """腾讯实时快照：带当日日期，用来判断「这份行情是不是今天的」。
    东财日K/分时接口被限流时，日K兜底源常滞后一个交易日；这份快照能补出
    当日那根K，并给出正确的今日昨收，让分时百分比与顶部涨幅同一基准。"""
    sym = _tencent_symbol(code, market)
    txt = http_text("https://qt.gtimg.cn/q=" + sym, encoding="gbk", retries=2,
                    headers={**HEADERS, "Referer": "https://gu.qq.com/"})
    parts = txt.split('"')
    p = (parts[1] if len(parts) > 1 else "").split("~")
    if len(p) < 39:
        raise EmptyData("tx quote short")
    price = float(p[3] or 0)
    prev = float(p[4] or 0)
    if not (price > 0 and prev > 0):
        raise EmptyData("tx quote no price")
    stamp = (p[30] or "").strip()
    day = "%s-%s-%s" % (stamp[0:4], stamp[4:6], stamp[6:8]) if len(stamp) >= 8 else ""

    def fnum(i, dflt=0.0):
        try:
            return float(p[i] or 0) or dflt
        except Exception:
            return dflt

    amt = 0.0
    try:
        amt = float((p[35].split("/") + ["0", "0", "0"])[2] or 0)
    except Exception:
        pass
    return {"date": day, "time": stamp[8:14] if len(stamp) >= 14 else "",
            "name": (p[1] or "").strip(),
            "price": price, "prev_close": prev,
            "open": fnum(5, price), "high": fnum(33, price), "low": fnum(34, price),
            "volume": fnum(6), "amount": amt,
            "pct": fnum(32) or round((price / prev - 1) * 100, 3),
            "turnover": fnum(38), "source": "tx"}


def _sina_quote(code, market):
    """新浪实时快照：腾讯不可用时的兜底，日期直接写在返回串里。"""
    sym = _tencent_symbol(code, market)
    txt = http_text("https://hq.sinajs.cn/list=" + sym, encoding="gbk", retries=2,
                    headers={**HEADERS, "Referer": "https://finance.sina.com.cn"})
    parts = txt.split('"')
    p = (parts[1] if len(parts) > 1 else "").split(",")
    if len(p) < 10:
        raise EmptyData("sina quote short")
    price = float(p[3] or 0)
    prev = float(p[2] or 0)
    if not (price > 0 and prev > 0):
        raise EmptyData("sina quote no price")
    md = re.search(r"\d{4}-\d{2}-\d{2}", txt)
    mt = re.search(r"\d{2}:\d{2}:\d{2}", txt)

    def fnum(i, dflt=0.0):
        try:
            return float(p[i] or 0) or dflt
        except Exception:
            return dflt

    return {"date": md.group(0) if md else "", "time": mt.group(0) if mt else "",
            "name": (p[0] or "").strip(),
            "price": price, "prev_close": prev,
            "open": fnum(1, price), "high": fnum(4, price), "low": fnum(5, price),
            "volume": fnum(8) / 100.0, "amount": fnum(9),
            "pct": round((price / prev - 1) * 100, 3),
            "turnover": 0.0, "source": "sina"}


def fetch_realtime_quote(code, market, ttl=8):
    """单股实时快照短缓存；只给 /api/stock 用，不进扫描热路径。"""
    def build():
        for src, fn in (("qtx", _tx_quote), ("qsina", _sina_quote)):
            if not src_ok(src):
                continue
            try:
                q = fn(code, market)
                if q and q.get("price", 0) > 0 and q.get("prev_close", 0) > 0:
                    return q
            except EmptyData:
                continue
            except Exception:
                src_fail(src, 60)
        raise EmptyData("all quote sources failed")

    return cached("q:%s:%s" % (market, code), ttl, build)


def fetch_detail_extra(code, market, ttl=60):
    """详情专用单股补齐「量比 / 主力净额」，只给 /api/stock 用。

    列表快照走 push2 批量接口，被限流后回退新浪（新浪没有主力净额），
    详情若只信列表项快照，这两格就会一直停在「—」。单股接口与批量接口
    限流互不影响，这里实测可用：
      主力净额 = 东财 push2 单股资金流日K末条 f52（元），即当日/最近交易日；
      量比     = 东财单股 stock/get 的 f50 ÷ 100，失败退腾讯 qt.gtimg 的 p[49]。
    多域名兜底，任一项失败留 None，由前端显示「—」而不是伪造 0。
    """
    secid = f"{1 if market == 1 else 0}.{code}"

    def build():
        out = {"volratio": None, "main_net": None}
        # ① 主力净额：东财单股资金流日K（f52 主力净流入，单位元）
        for host in ("push2.eastmoney.com", "push2delay.eastmoney.com", "82.push2.eastmoney.com"):
            try:
                url = (f"https://{host}/api/qt/stock/fflow/kline/get?lmt=3&klt=101&secid={secid}"
                       "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56")
                txt = http_text(url, timeout=6, retries=1,
                                headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"})
                klines = ((json.loads(txt).get("data") or {}).get("klines") or [])
                if not klines:
                    continue
                parts = str(klines[-1]).split(",")
                if len(parts) >= 2 and parts[1] not in ("", "-", "--"):
                    # 元 → 亿，与列表 main_net 同单位；0 是真实结果，不算缺失
                    out["main_net"] = round(float(parts[1] or 0) / 1e8, 2)
                    break
            except Exception:
                continue
        # ② 量比：东财单股 f50（放大 100 倍）；再退腾讯 p[49]
        for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
            try:
                url = f"https://{host}/api/qt/stock/get?secid={secid}&fields=f50&invt=2"
                txt = http_text(url, timeout=6, retries=1,
                                headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"})
                v = ((json.loads(txt).get("data") or {}).get("f50"))
                if isinstance(v, (int, float)) and float(v) > 0:
                    out["volratio"] = round(float(v) / 100.0, 2)
                    break
            except Exception:
                continue
        if out["volratio"] is None:
            try:
                txt = http_text("https://qt.gtimg.cn/q=" + _tencent_symbol(code, market),
                                encoding="gbk", timeout=6, retries=1,
                                headers={**HEADERS, "Referer": "https://gu.qq.com/"})
                p = (txt.split('"')[1] if '"' in txt else "").split("~")
                if len(p) >= 50 and float(p[49] or 0) > 0:
                    out["volratio"] = round(float(p[49]), 2)
            except Exception:
                pass
        return out

    return cached("dextra:%s:%s" % (market, code), ttl, build)


def _today_bar(q, today, prev):
    """把实时快照折成一根日K；pct 用今日昨收，与分时图刻度同口径。"""
    px = float(q.get("price") or 0)
    op = float(q.get("open") or 0) or px
    hi = float(q.get("high") or 0) or px
    lo = float(q.get("low") or 0) or px
    hi = max(hi, px, op)
    lo = min(lo, px, op) if lo > 0 else px
    return {"date": today, "open": round(op, 4), "close": round(px, 4),
            "high": round(hi, 4), "low": round(lo, 4),
            "volume": round(float(q.get("volume") or 0), 2),
            "amount": round(float(q.get("amount") or 0), 2),
            "pct": round((px / prev - 1) * 100, 3) if prev > 0 else float(q.get("pct") or 0),
            "turnover": round(float(q.get("turnover") or 0), 3)}


def _today_bar_from_trends(tr, today, prev):
    """快照也取不到时，用今日分时合成一根日K，至少不让日K停在上一交易日。"""
    pts = [pt for pt in (tr.get("points") or []) if (pt.get("price") or 0) > 0]
    if len(pts) < 2:
        return None
    prices = [float(pt["price"]) for pt in pts]
    his = [float(pt.get("high") or pt["price"]) for pt in pts] + prices
    los = [float(pt.get("low") or pt["price"]) for pt in pts
           if (pt.get("low") or pt["price"]) > 0] + prices
    px = prices[-1]
    return {"date": today, "open": prices[0], "close": px,
            "high": max(his), "low": min(los),
            "volume": sum(float(pt.get("volume") or 0) for pt in pts),
            "amount": sum(float(pt.get("amount") or 0) for pt in pts),
            "pct": round((px / prev - 1) * 100, 3) if prev > 0 else 0.0,
            "turnover": 0.0}


def _em_kline(code, market, limit):
    secid = f"{1 if market == 1 else 0}.{code}"
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&klt=101&fqt=1&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&end=20500101&lmt={limit}")
    j = http_json(url, retries=2)
    data = j.get("data") or {}
    bars = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 11:
            continue
        try:
            b = {"date": p[0], "open": float(p[1]), "close": float(p[2]),
                 "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                 "amount": float(p[6]), "pct": float(p[8]), "turnover": float(p[10])}
        except (ValueError, TypeError, IndexError):
            continue
        if not _finite_bar(b):
            continue
        bars.append(b)
    if not bars:
        raise EmptyData("em kline empty")
    return bars


def _tx_kline(code, market, limit):
    sym = _tencent_symbol(code, market)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sym},day,,,{limit},qfq")
    j = json.loads(http_text(url, headers={**HEADERS, "Referer": "https://gu.qq.com/"}, retries=2))
    node = (j.get("data") or {}).get(sym) or {}
    raw = node.get("qfqday") or node.get("day") or []
    bars = []
    for row in raw:
        if len(row) < 6:
            continue
        try:
            b = {"date": row[0], "open": float(row[1]), "close": float(row[2]),
                 "high": float(row[3]), "low": float(row[4]),
                 "volume": float(row[5]), "amount": 0.0,
                 "pct": 0.0, "turnover": 0.0}
            if _finite_bar(b):
                bars.append(b)
        except Exception:
            continue
    if not bars:
        raise EmptyData("tx kline empty")
    return bars


def _sina_kline(code, market, limit):
    """新浪日K：东财/腾讯同时被限流时的兜底（成交量单位为股，按手折算）"""
    sym = _tencent_symbol(code, market)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={limit}")
    rows = json.loads(http_text(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn"},
                                retries=2))
    bars = []
    for row in rows or []:
        try:
            b = {"date": row["day"], "open": float(row["open"]), "close": float(row["close"]),
                 "high": float(row["high"]), "low": float(row["low"]),
                 "volume": float(row["volume"]) / 100.0, "amount": 0.0,
                 "pct": 0.0, "turnover": 0.0}
            if _finite_bar(b):
                bars.append(b)
        except Exception:
            continue
    if len(bars) < 20:
        raise EmptyData("sina kline empty")
    return bars


def _finite_bar(b):
    """OHLC 必须是有限正数；NaN / inf 会让所有指标污染并产出非法 JSON。"""
    for k in ("open", "close", "high", "low", "volume", "amount", "pct", "turnover"):
        v = b.get(k)
        if v is not None and not math.isfinite(v):
            return False
    return all((b.get(k) or 0) > 0 for k in ("open", "close", "high", "low"))


def _align_bars(bars, code="", name="", allow_adjust=True):
    """三个数据源复权口径不一致（新浪不复权）。这里做两件事：
       1) 备用源没给 pct，用 close/prev_close 现算，避免形态识别把 0 当"平盘"；
       2) 检测超过涨跌幅限制的跳空（除权除息），把更早的 OHLC 按比例折算成近似前复权。
       allow_adjust=False：指数（如上证 000001）无涨跌停/除权，跳过缩放，只排序补 pct。
    """
    if not bars:
        return bars
    # 数据源若返回乱序 / 重复日期，prev_close 关系会整体错位（末根也不再是最新）。
    _uniq = {}
    for _b in bars:
        _d = str(_b.get("date") or "")
        if _d:
            _uniq[_d] = _b                 # 同日重复取最后一条
    bars = [_uniq[d] for d in sorted(_uniq)] if _uniq else sorted(
        bars, key=lambda x: str(x.get("date") or ""))
    for i, b in enumerate(bars):
        if not b.get("pct") and i > 0:
            pv = bars[i - 1].get("close") or 0
            if pv > 0:
                b["pct"] = round((b["close"] / pv - 1) * 100, 3)
    if not allow_adjust:               # 指数：只排序 + 补 pct，不做除权缩放
        return bars
    for i in range(len(bars) - 1, 0, -1):
        pv = bars[i - 1].get("close") or 0
        cc = bars[i].get("close") or 0
        if pv <= 0 or cc <= 0:
            continue
        chg = cc / pv - 1
        # 阈值按该股实际涨跌停幅度动态给（创业板/科创板 20%、ST 5%）；
        # 写死 11.5% 会把 20cm 股票的正常涨停误判成除权并把历史 K 线全部缩放。
        # 防御：无涨跌幅限制期（新股前5日、退市整理期）单日可跌 30%~90%，
        # 纯价格跳空会被误判成除权、污染全部历史 K 线。这类票简称常带
        # 退/N/C/XD/XR/DR 前缀，且单日跌幅 >50% 在正常 A 股根本不可能——直接跳过缩放。
        _nm = str(name or "")
        _special = any(t in _nm for t in ("退", "XD", "XR", "DR")) or _nm.startswith(("N", "C"))
        _implausible = chg < -0.5          # 单日跌超 50%：不是正常除权，是不设涨跌幅的暴跌
        if (not _special and not _implausible
                and abs(chg) > (limit_pct(code, name) / 100.0 + 0.015) and i >= 5):
            f = cc / pv
            for j in range(i):
                for k in ("open", "high", "low", "close"):
                    if bars[j].get(k):
                        bars[j][k] = round(bars[j][k] * f, 4)
            bars[i]["pct"] = 0.0
    return bars


# 统一缓存长度：AI/详情用 160 根、多日持股要算周线用 400 根。
# 之前缓存键带 limit，导致「AI ↔ 多日持股」一切换就对同一批票全部重新联网拉 K 线，
# 这是扫描忽快忽慢（58s / 101s / 118s）的主因。现在始终拉满 400 根存一份，
# 返回时按调用方 limit 截尾部，评分口径完全不变，但切换算法/自动重扫直接命中缓存。
_KL_STORE = 400


def fetch_kline(code, market, limit=160, name="", ttl=None):
    # 扫描热路径不传 ttl：电脑端 60 秒（指标更新更跟手），手机/模拟器 300 秒
    # （并发低、拉日K慢，缓存保久一点才不会每次重扫都冷拉一遍）。
    # 详情页显式传 ttl=15(盘中)/300(休市)，走自己的节奏，不受这里影响。
    if ttl is None:
        ttl = 300 if LIGHT else 60
    def build():
        # 依次尝试：东财 → 腾讯 → 新浪；失败的源进入冷却，避免每次都等超时
        # 循环变量必须叫 src：外层 name 是股票名，遮蔽后 _align_bars 拿到的是
        # 数据源名，ST 5% 动态涨跌停阈值会失效。
        for src, fn in (("em", _em_kline), ("tx", _tx_kline), ("sina", _sina_kline)):
            if not src_ok(src):
                continue
            try:
                bars = fn(code, market, _KL_STORE)
                if bars:
                    # 上证指数（000001/沪）无涨跌停/除权概念，跳过复权缩放，
                    # 否则极端单边日会被误判除权、污染 market_env/节假日反推。
                    if str(code) == "000001" and int(market) == 1:
                        return _align_bars(bars, code, name, allow_adjust=False)
                    return _align_bars(bars, code, name)
            except EmptyData:
                continue
            except Exception:
                src_fail(src, 90 if src != "em" else 120)
        raise EmptyData("all kline sources failed")

    # 缓存键不含 name 也不含 limit：同一只票在所有板块/算法/详情共用一份，
    # 谁先建缓存谁定对齐口径。数量差异在返回处用尾部切片解决，不影响任何指标。
    bars = cached(f"k:{market}:{code}", ttl, build)
    if not bars or not limit:
        return bars
    return bars[-limit:] if len(bars) > limit else bars


def _em_trends(code, market):
    secid = f"{1 if market == 1 else 0}.{code}"
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1")
    j = http_json(url, retries=2)
    data = j.get("data") or {}
    pts = []
    day = ""
    for line in data.get("trends") or []:
        p = line.split(",")
        if len(p) < 8:
            continue
        # 东财每行形如 "2026-11-16 09:30,...)"，日期不能丢：
        # 只有拿到日期才能判断这份分时到底是今天还是上一交易日。
        day = day or (p[0][:10] if len(p[0]) >= 10 else "")
        pts.append({"time": p[0][-5:], "price": float(p[2]), "high": float(p[3]),
                    "low": float(p[4]), "volume": float(p[5]),
                    "amount": float(p[6]), "avg": float(p[7])})
    if not pts:
        raise EmptyData("em trends empty")
    return {"prev_close": float(data.get("preClose") or 0),
            "points": pts, "date": day}


def _tx_trends(code, market):
    sym = _tencent_symbol(code, market)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    j = json.loads(http_text(url, headers={**HEADERS, "Referer": "https://gu.qq.com/"}, retries=2))
    node = (j.get("data") or {}).get(sym) or {}
    raw = (node.get("data") or {}).get("data") or []
    prev = float(node.get("prec") or 0) or float((j.get("data") or {}).get("prec") or 0)
    # 腾讯接口第 3/4 字段是「当日累计」成交量(手)/成交额(元)，不是每分钟增量。
    # 旧写法把累计量当增量直接喂给分时量柱和 tail_strength，量柱成递增斜坡、
    # 尾盘量占比近似 100%；均价也用「累计量的累加」去重算，口径错误。
    # 这里差分回每分钟增量，均价直接用当行 累计额/(累计量*100)。
    pts, prev_cv, prev_ca = [], 0.0, 0.0
    for row in raw:
        p = row.split()
        if len(p) < 4:
            continue
        t = p[0]
        if not (("0925" <= t <= "1130") or ("1300" <= t <= "1505")):
            continue
        price = float(p[1]); cum_v = float(p[2]); cum_a = float(p[3])
        vol = max(0.0, cum_v - prev_cv)     # 差分成每分钟增量
        amt = max(0.0, cum_a - prev_ca)
        prev_cv, prev_ca = cum_v, cum_a
        avg = (cum_a / cum_v / 100.0) if cum_v > 0 else price
        pts.append({"time": t[:2] + ":" + t[2:], "price": price, "high": price,
                    "low": price, "volume": vol, "amount": amt, "avg": avg})
    if not pts:
        raise EmptyData("tx trends empty")
    return {"prev_close": prev, "points": pts}


def _sina_trends(code, market):
    """新浪分时：腾讯/东财被限流时兜底"""
    sym = _tencent_symbol(code, market)
    url = f"https://quotes.sina.cn/cn/api/openapi.php/CN_MinlineService.getMinlineData?symbol={sym}"
    j = json.loads(http_text(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn"},
                             retries=2))
    rows = ((j.get("result") or {}).get("data")) or []
    pts = []
    for r in rows:
        try:
            t = str(r.get("m") or "")
            if len(t) < 5:
                continue
            # 新浪兜底源会带 15:00 之后的盘后快照点（15:19/15:23/15:25…），
            # 画到 240 分钟轴上会全挤在收盘位置、截止标注还会显示「截至 15:25」。
            hhmm = t[0:2] + t[3:5]
            if not (("0925" <= hhmm <= "1130") or ("1300" <= hhmm <= "1500")):
                continue
            av = float(r.get("avg_p") or 0)     # 缺均价线时留 0，不能拿现价冒充
            # v 单位为股（实测与日K成交量 1:1），与 _sina_kline 同口径按手折算；
            # 东财/腾讯分时是手，不折算会让今日量比虚高 100 倍
            pts.append({"time": t[:5], "price": float(r["p"]), "high": float(r["p"]),
                        "low": float(r["p"]), "volume": float(r.get("v") or 0) / 100.0,
                        "amount": 0.0, "avg": av})
        except Exception:
            continue
    if not pts:
        raise EmptyData("sina trends empty")
    return {"prev_close": 0.0, "points": pts,
            "degraded_avg": not any(p["avg"] > 0 for p in pts)}


def fetch_trends(code, market, ttl=60):
    def build():
        for name, fn in (("em", _em_trends), ("tx", _tx_trends), ("sina", _sina_trends)):
            if not src_ok(name):
                continue
            try:
                d = fn(code, market)
                if d and d.get("points"):
                    return d
            except EmptyData:
                continue
            except Exception:
                src_fail(name, 90 if name != "em" else 120)
        raise EmptyData("all trend sources failed")

    return cached(f"t:{market}:{code}", ttl, build)


def _trend_minute(t):
    """把 HH:MM 映射成 A 股交易分钟序号：09:30=0、11:30/13:00=120、15:00=240。"""
    try:
        hh, mm = str(t)[-5:].split(":")
        v = int(hh) * 60 + int(mm)
    except Exception:
        return None
    if v <= 11 * 60 + 30:
        return max(0, min(120, v - 9 * 60 - 30))
    return max(120, min(240, 120 + v - 13 * 60))


def _annotate_trend(d):
    """详情用：标注分时数据的新鲜度，别把上一交易日的全天图伪装成今日实时。
    只在 /api/stock 调用，不进扫描热路径；返回副本，不污染 cached() 里的共享 dict。"""
    pts = d.get("points") or []
    if not pts:
        return dict(d)
    out = dict(d)
    out["as_of"] = str(pts[-1].get("time") or "")
    out["count"] = len(pts)
    day = out.get("date") or ""
    today = now_cn().strftime("%Y-%m-%d")
    st = market_status()
    state, text = st.get("state"), st.get("text") or ""
    if day and day != today:
        out["live"] = False
        out["fresh_note"] = "上一交易日全天 · 非实时"
    elif state == "open":
        m, nm = _trend_minute(out["as_of"]), _trend_minute(now_cn().strftime("%H:%M"))
        lag = (nm - m) if (m is not None and nm is not None) else 999
        # lag 明显为负 = 数据时点比现在还晚，例如盘中拿到上一交易日 15:00 的全天数据。
        # 腾讯/新浪兜底不带日期，只有靠这个判断挡住「历史全天图伪装成实时」。
        out["live"] = -3 <= lag <= 20
        if lag < -3 and not day:
            out["fresh_note"] = "疑似上一交易日全天 · 非实时"
        else:
            out["fresh_note"] = ("实时 · 截至 " + out["as_of"]) if out["live"] \
                else ("数据滞后 · 截至 " + out["as_of"])
    elif state == "break":
        out["live"] = False
        out["fresh_note"] = "午间休市 · 截至 " + out["as_of"]
    elif state == "pre":
        out["live"] = False
        out["fresh_note"] = "未开盘 · 最近交易日全天"
    elif not day:
        # 腾讯/新浪兜底不带日期，休市/周末拿到的其实是最近一个完整交易日
        out["live"] = False
        out["fresh_note"] = "最近交易日全天 · 截至 " + out["as_of"]
    else:
        out["live"] = False
        out["fresh_note"] = text + " · 截至 " + out["as_of"]
    return out


# ---------------------------------------------------------------- 指标计算
def ma(seq, n, i=None):
    """i 为索引用点；默认取最后一根"""
    if i is None:
        i = len(seq) - 1
    if i + 1 < n:
        return None
    return sum(seq[i - n + 1:i + 1]) / n


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def evaluate(q, bars, trend, p, idx_trend=None):
    """一夜持股专用：深度评估，返回 9 条规则的实测值与得分。

    v1.6.4：第⑦条分时站均价线、第⑨条跑赢上证是「硬门槛」——不达标或
    数据缺失都不入选（gate.block 给出原因），不再中性计/标注保留。
    evaluate 仅被 scan 调用，故这些口径不影响 AI/多日/涨停/跌停任何板块。
    idx_trend 为上证指数分时 fetch_trends("000001", 1) 的结果。
    """
    q = dict(q)                       # 快照 dict 由缓存共享，别原地改写
    rules = {}
    score_sum = 0.0
    weight_sum = 0.0

    def add(key, name, passed, value, score, detail, weight):
        nonlocal score_sum, weight_sum
        score = clamp(score)
        rules[key] = {
            "name": name, "passed": bool(passed), "value": value,
            "score": round(score, 1), "detail": detail, "weight": weight,
        }
        score_sum += score * weight
        weight_sum += weight

    # ---- 1 涨幅区间
    lo, hi = p["gain_min"], p["gain_max"]
    mid = (lo + hi) / 2
    half = max((hi - lo) / 2, 0.3)
    s1 = 100 - abs(q["pct"] - mid) / half * 45
    add("gain", "① 涨幅区间", lo <= q["pct"] <= hi, f'{q["pct"]:+.2f}%', s1,
        f"目标 {lo}%~{hi}%，越靠中间越稳", 12)

    # ---- 2 量比
    vr = q["volratio"]
    if vr <= 0 and len(bars) >= 6 and bars[-1]["volume"] > 0:
        # 备用源没有量比时，用「今日每分钟均量 / 近5日每分钟均量」估算
        pts0 = (trend or {}).get("points") or []
        mins = min(max(len(pts0), 5), 240)
        # 分时逐分钟成交量累计才是「今日量」；日K最后一根在盘中常还是昨天的
        today_vol = sum(float(x.get("volume") or 0) for x in pts0)
        if today_vol <= 0 or today_vol > bars[-1]["volume"] * 5:
            # 回退的是完整日量，必须按 240 分钟折算；再除实际分钟数会把量比虚高数倍
            today_vol = bars[-1]["volume"]
            mins = 240
        today_pm = today_vol / mins
        avg5 = statistics.fmean([b["volume"] for b in bars[-6:-1]]) / 240
        vr = round(today_pm / avg5, 2) if avg5 > 0 else 0
    s2 = min(100, vr / 2.0 * 100)
    q["volratio"] = round(vr, 2)
    add("vr", "② 量比", vr >= p["vr_min"], f'{vr:.2f}', s2,
        "量比≥1 说明今日资金比过去几天活跃", 12)

    # ---- 3 换手率
    tlo, thi = p["turn_min"], p["turn_max"]
    if tlo <= q["turnover"] <= thi:
        # 上下限可相等（用户精确筛某一换手率），半宽为 0 会除零；与涨幅区间同口径兜底
        s3 = 100 - abs(q["turnover"] - (tlo + thi) / 2) / max((thi - tlo) / 2, 0.3) * 25
    else:
        s3 = 25
    add("turnover", "③ 换手率", tlo <= q["turnover"] <= thi, f'{q["turnover"]:.2f}%', s3,
        f"目标 {tlo}%~{thi}%，太低没人管、太高分歧大", 12)

    # ---- 4 流通市值
    flo, fhi = p["fcap_min"], p["fcap_max"]
    fcap = q["fcap"] / 1e8
    if flo <= fcap <= fhi:
        # 同上：精确筛某一流通市值时上下限相等，半宽兜底避免除零
        s4 = 100 - abs(fcap - (flo + fhi) / 2) / max((fhi - flo) / 2, 1) * 20
    else:
        s4 = 20
    add("fcap", "④ 流通市值", flo <= fcap <= fhi, f"{fcap:.0f}亿", s4,
        f"目标 {flo}~{fhi} 亿，盘子适中才好拉", 10)

    # ---- 5 量能形态（近 9 日成交量平稳度 + 今日是否异常放巨量）
    vols = [b["volume"] for b in bars]
    pass5, s5, v5txt = False, 30, "数据不足"
    if len(vols) >= 10:
        prior = vols[-10:-1]
        mu = statistics.fmean(prior) if prior else 0
        cv = (statistics.pstdev(prior) / mu) if mu > 0 else 9
        pts0 = (trend or {}).get("points") or []
        # 与规则②统一：分时逐分钟累计量才是「今日量」。日K末根在盘中语义不确定，
        # 只有分时不可用时才回退；回退的是完整日量，绝不能再按分钟二次折算。
        today_vol = sum(float(x.get("volume") or 0) for x in pts0)
        intraday = 0 < len(pts0) < 240 and today_vol > 0
        if not intraday:
            today_vol = vols[-1]
        ratio = (today_vol * 240 / max(len(pts0), 1) * 0.92 / mu
                 if intraday and mu > 0 else today_vol / mu if mu > 0 else 0)
        pass5 = cv <= p["vol_cv_max"] and 0.6 <= ratio <= 2.6
        s5 = clamp(100 - cv * 90 - (25 if ratio > 2.6 else 0) - (12 if ratio < 0.6 else 0))
        v5txt = f"波动系数 {cv:.2f}，今日量 {ratio:.1f}×9日均量"
        _ts = tail_strength(trend)
        if _ts and _ts.get("tail_vol_share") is not None:
            v5txt += f"，尾盘30分钟量占 {_ts['tail_vol_share']:.0f}%"
            if _ts.get("last15_slope") is not None:
                v5txt += f"、末15分钟 {_ts['last15_slope']:+.2f}%"
    add("volume", "⑤ 成交量形态", pass5, v5txt, s5,
        "量能忽大忽小说明资金无序，平稳温和放量最佳", 14)

    # ---- 6 均线与K线形态
    closes = [b["close"] for b in bars]
    pass6, s6, v6txt = False, 25, "数据不足"
    if len(closes) >= 25:
        c = closes[-1]
        m5, m10, m20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
        m20_prev = ma(closes, 20, len(closes) - 6)
        slope = (m20 / m20_prev - 1) if (m20 and m20_prev) else 0
        bull = c > m5 > m10
        if bull and slope >= 0:
            s6, pass6 = 100, True
            v6txt = "多头排列，价>MA5>MA10"
        elif c > m10 and c > m20 and slope >= -0.004:
            s6, pass6 = 82, True
            v6txt = "站上MA10/MA20，均线走平向上"
        elif c > m5:
            s6 = 58
            v6txt = "仅站上MA5，力度偏弱"
        else:
            s6 = 18
            v6txt = "跌破MA5，均线压制"
        # 20 日跌幅过深，扣分
        if len(closes) >= 21:
            # 「高点」要用真实最高价 high，不能用最高收盘价，两者可差好几个点
            hi20 = max(b.get("high") or b["close"] for b in bars[-21:])
            drop = c / hi20 - 1 if hi20 else 0
            if drop < -0.15:
                s6 *= 0.7
                v6txt += f"，距20日最高价 {drop*100:.0f}%"
    add("ma", "⑥ K线与均线", pass6, v6txt, s6,
        "长期压在均线下方=上方套牢盘重，反弹易被砸", 16)

    # ---- 7 分时结构（均价线之上、日内新高、午后向上）
    pts = (trend or {}).get("points") or []
    pass7, s7, v7txt = False, 20, "分时数据不足"
    intraday_ok = len(pts) >= 20      # 分时数据是否完整（缺失即硬门槛拦截，不再中性计）
    if intraday_ok:
        last = pts[-1]["price"]
        real_avg = [x for x in pts if (x.get("avg") or 0) > 0]
        vwap = (real_avg[-1]["avg"] if real_avg else
                sum(x["price"] for x in pts) / len(pts))
        avg_ok = len(real_avg) >= len(pts) * 0.8
        # v1.6.5 严格口径：分母用「全部分时点」len(pts)，均价线缺失的点按未站上计。
        # 旧口径分母用 len(real_avg)（仅有均价线的点），80% 覆盖时 0.85 实际仅约
        # 68% 全天时间，偏松；且缺失均价线按 0.5 中性计会让降级源白送分。
        # 严格口径下「分时数据缺失」自然 above 偏低 → 硬门槛不通过，符合用户口径。
        above = sum(1 for x in pts
                    if (x.get("avg") or 0) > 0 and x["price"] >= x["avg"] * 0.999) / len(pts)
        day_high = max(x["high"] for x in pts)
        near_high = last / day_high if day_high else 0
        base = next((x["price"] for x in pts if x["time"] >= "13:00"), pts[0]["price"])
        slope = (last / base - 1) if base else 0
        # 逐点用「当时的均价线」判回踩；用终值 VWAP 比历史价会随均价升降误判
        hold = avg_ok and all(x["price"] >= x["avg"] * 0.993 for x in real_avg[-25:])
        s7 = clamp(above * 45 + near_high * 100 * 0.35 + clamp(slope * 900, 0, 20))
        # v1.6.4 硬门槛：现价≥均价线 + ≥85% 时间站在均价线上（原仅 60% 且只扣分）
        pass7 = avg_ok and last >= vwap and above >= 0.85
        v7txt = (f"均价线上方 {above*100:.0f}% 时间，"
                 f"距日内高点 {(1-near_high)*100:.1f}%，"
                 f"午后 {slope*100:+.2f}%")
        if not avg_ok:
            v7txt += "（均价线大面积缺失，已按未站上计）"
            s7 = clamp(s7 * 0.8)
        if hold:
            s7 = clamp(s7 + 6)
    add("intraday", "⑦ 分时强弱", pass7, v7txt, s7,
        "全程压在均价线上方，回踩不破，才是强势", 14)

    # ---- 8 尾盘时机
    now = now_cn()
    hm = now.strftime("%H:%M")
    ms = market_status()
    st = ms["state"]
    live = st == "open"
    # 交易日收盘后分时是「今天」的完整走势，可以复盘尾盘；周末/节假日则是上一交易日，不参与
    weekend_off = "休市 ·" in str(ms.get("text") or "")
    closed = st == "closed" and not weekend_off and hm >= "15:00"
    in_window = (live and hm >= p["entry_time"]) or closed
    off_txt = ("午间休市，分时为今日上午数据，13:00 后再做买入决策" if st == "break"
               else "未开盘 / 集合竞价，分时数据为上一交易日，仅供参考" if st == "pre"
               else "当日已收盘，以下按今天完整分时复盘" if closed
               else "休市，分时数据为最近一个交易日，仅供参考")
    pass8, s8, v8txt = False, 40, off_txt
    if len(pts) >= 20:
        last = pts[-1]["price"]
        day_high = max(x["high"] for x in pts)
        near_high = last / day_high if day_high else 0
        if in_window:
            # 盘后取「尾盘窗口那段」的最高价复盘，而不是拿最后一个点硬套当前时间
            tail = [x for x in pts if x["time"] >= p["entry_time"]] if closed else []
            ref = max((x["high"] for x in tail), default=last) if tail else last
            nh = ref / day_high if day_high else 0
            s8 = clamp(55 + (nh - 0.96) * 1200)
            pass8 = nh >= 0.975
            v8txt = (("当日尾盘接近日内新高（盘后复盘）" if pass8 else "当日尾盘离日内高点偏远（盘后复盘）")
                     if closed else ("尾盘窗口，接近日内高点" if pass8 else "尾盘窗口，但离日内高点偏远"))
        else:
            s8 = 55
            v8txt = (f"当前 {hm}，{p['entry_time']} 后再做买入决策" if live else off_txt)
    add("timing", "⑧ 尾盘入场", pass8, v8txt, s8,
        "14:30 后创新高、回踩均价线不破才进场，第二天冲高就走", 10)

    # ---- 9 跑赢上证（分时逐分钟对比，v1.6.4 新增，仅 evaluate/一夜持股调用）
    # 硬门槛维度：weight=0 显示判定但不改既有评级分数分布；进入列表的票
    # 已全部满足「≥85% 时间跑赢上证」，票间排序仍按原 8 维度加权分。
    pass9, s9, v9txt, rs_ok = False, 50, "上证分时数据不足", False
    idx_pts = ((idx_trend or {}).get("points") or [])
    if intraday_ok and len(idx_pts) >= 20:
        idx_prev = (idx_trend or {}).get("prev_close") or (idx_pts[0].get("price") or 0)
        idx_by_min = {}
        for xp in idx_pts:
            mm = _trend_minute(xp.get("time"))
            if mm is not None and (xp.get("price") or 0) > 0:
                idx_by_min[mm] = float(xp["price"])
        stock_prev = (trend or {}).get("prev_close") or (pts[0].get("price") or 0)
        pairs = []
        for sp in pts:
            mm = _trend_minute(sp.get("time"))
            ip = idx_by_min.get(mm)
            if mm is None or not ip or not (sp.get("price") or 0) or not stock_prev or not idx_prev:
                continue
            pairs.append(sp["price"] / stock_prev - ip / idx_prev)   # 个股涨幅 − 上证涨幅
        if len(pairs) >= 20:
            rs_ok = True
            win_rate = sum(1 for ex in pairs if ex > 0) / len(pairs)
            avg_ex = sum(pairs) / len(pairs)
            pass9 = win_rate >= 0.85
            s9 = clamp(50 + (win_rate - 0.5) * 100)
            v9txt = (f"跑赢上证 {win_rate*100:.0f}% 时间（{len(pairs)} 分钟），"
                     f"平均超额 {avg_ex*100:+.2f}%")
    add("rs_index", "⑨ 跑赢上证", pass9, v9txt, s9,
        "分时须 85% 时间强于上证，弱于大盘的票隔夜风险大", 0)

    # 硬门槛汇总：数据缺失或明确不达标都不入选（整批空由 scan 层判上证后处理）
    if not intraday_ok:
        block = "分时数据缺失"
    elif not pass7:
        block = "分时未站上均价线（需现价在上方且≥85%时间站上）"
    elif not rs_ok:
        block = "上证分时数据缺失"
    elif not pass9:
        block = "分时未跑赢上证（需≥85%时间强于上证）"
    else:
        block = None

    total = score_sum / weight_sum if weight_sum else 0
    tags = []
    if rules["ma"]["score"] >= 80:
        tags.append("均线多头")
    if q["volratio"] >= 1.5:
        tags.append("放量")
    if rules["volume"]["passed"]:
        tags.append("量能平稳")
    if rules["intraday"]["score"] >= 78:
        tags.append("分时强势")
    if q["main_net"] > 0:
        tags.append("主力净流入")
    if rules["timing"]["passed"]:
        tags.append("尾盘新高")
    if q["main_net"] < 0:
        tags.append("主力流出")

    return {
        "score": round(total, 1),
        "grade": _grade_of("night", total),
        "rules": rules,
        "tags": tags,
        # 评分实际采用的量比（备用源缺失时是估算值）。必须透出让 scan 的
        # {**q, **ev} 覆盖原始快照值，否则卡片显示 0.00、评分却按估算值通过。
        "volratio": q["volratio"],
        "gate": {"block": block, "intraday_ok": intraday_ok, "pass7": pass7,
                 "rs_ok": rs_ok, "pass9": pass9},
        "pass_deep": sum(1 for k in ("volume", "ma", "intraday", "timing") if rules[k]["passed"]),
        # 详情页技术指标：与多日持股同口径（同一份 K 线、同一套算法），
        # 之前只有 swing 板块透出，scan 详情页的技术指标区是空的。
        "metrics": detail_metrics(bars),
    }


# ---------------------------------------------------------------- AI 多因子选股
# 思路：把同花顺「六维诊断」里可量化的部分（技术/形态/资金/估值/财务）
# 拆成可计算的信号，加权成一个总分，并把命中的信号写成中文理由。
# 消息面需要付费资讯源，这里不假装有，界面上会说明。

def coerce_param(default, v):
    """把 URL/JSON 里的字符串参数安全转成默认值同类型。
       bool('0') 恒为 True 是个经典坑，会让「排除 ST」开关永远关不掉；
       NaN/inf 参与比较恒为 False，会让筛选结果全空且没有任何提示。"""
    if isinstance(default, bool):
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(default, float):
            f = float(v)
            # 数值上限防御：universe/deep_limit/top 等若被放大到数十万，
            # 单请求会对全市场逐只拉 K 线、拖垮本地服务并触发行情限流。
            # 合法参数都远小于 1e5，超界一律回退默认值。
            return f if (math.isfinite(f) and abs(f) < 1e5) else default
        if isinstance(default, int):
            n = int(float(v))
            return n if abs(n) < 1e5 else default
        return type(default)(v)
    except Exception:
        return default


def rsi_series(closes, n=14):
    """Wilder RSI(14)：首值取前 n 个涨跌的简单均值，之后按 alpha=1/n 平滑。
       这是 Welles Wilder 原始定义，不是简单移动平均版的 RSI。"""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = [], []
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag, al = statistics.fmean(gains), statistics.fmean(losses)

    def _r(g, l):
        if l <= 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = _r(ag, al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        out[i] = _r(ag, al)
    return out


def obv_series(bars):
    """OBV 能量潮：收涨加当日量、收跌减当日量、平盘不动（Granville 原始定义）。"""
    out = [0.0]
    for i in range(1, len(bars)):
        d = bars[i]["close"] - bars[i - 1]["close"]
        v = bars[i].get("volume") or 0
        out.append(out[-1] + (v if d > 0 else -v if d < 0 else 0.0))
    return out


def obv_signal(bars, n=10):
    """近 n 日 OBV 净变化 / 同期均量，正数表示资金持续吸筹、负数表示量能流出。"""
    o = obv_series(bars)
    if len(o) < n + 1 or len(bars) < n:
        return None, None
    chg = o[-1] - o[-1 - n]
    av = statistics.fmean([b.get("volume") or 0 for b in bars[-n:]]) or 1.0
    return o[-1], round(chg / (av * n), 2)


def ema(vals, n):
    """标准 EMA：前 n 根用 SMA 播种（TradingView 口径，主流通行做法）。
       通达信 EMA 以首值播种，长序列(>60根)两者差异可忽略；此处用 SMA 播种让
       短序列更稳定。旧写法从 vals[0] 起递推，DIF/DEA 在短序列上会系统性偏移。"""
    if not vals:
        return []
    k = 2.0 / (n + 1)
    if len(vals) <= n:
        seed = statistics.fmean(vals)
        out = [seed]
        for v in vals[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out
    seed = statistics.fmean(vals[:n])
    out = list(vals[:n - 1])          # 播种点之前填原值，保证与输入等长
    out.append(seed)
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd_series(closes, fast=12, slow=26, sig=9):
    e1, e2 = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(e1, e2)]
    dea = ema(dif, sig)
    hist = [(d - s) * 2 for d, s in zip(dif, dea)]
    return dif, dea, hist


def kdj_series(bars, n=9):
    K, D, J = [], [], []
    k = d = 50.0
    for i in range(len(bars)):
        if i < n - 1:
            rsv = 50.0              # 窗口不足 n 根按中性，标准 KDJ 不输出前 n-1 根
        else:
            seg = bars[i - n + 1:i + 1]
            hi = max(b["high"] for b in seg)
            lo = min(b["low"] for b in seg)
            rsv = 50.0 if hi == lo else (bars[i]["close"] - lo) / (hi - lo) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        K.append(k); D.append(d); J.append(3 * k - 2 * d)
    return K, D, J


def boll_series(closes, n=20, k=2.0):
    mid = [None] * len(closes)
    up = [None] * len(closes)
    low = [None] * len(closes)
    width = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        m = sum(seg) / n
        sd = (sum((x - m) ** 2 for x in seg) / max(n - 1, 1)) ** 0.5  # 通达信 STD 口径
        mid[i] = m
        up[i] = m + k * sd
        low[i] = m - k * sd
        width[i] = (up[i] - low[i]) / m if m else None
    return mid, up, low, width

# ---------------------------------------------------------------- v1.5.12 技术分析补充
def atr_wilder_series(bars, n=14):
    """标准 Wilder ATR：首值取前 n 根 TR 的简单均值，之后 alpha=1/n 递推。
       旧实现用第一根 TR 作初值，K 线不足 60 根时偏差可达 2.5%。"""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1]["close"]
        trs.append(max(bars[i]["high"] - bars[i]["low"],
                       abs(bars[i]["high"] - pc), abs(bars[i]["low"] - pc)))
    a = statistics.fmean(trs[:n])
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def atr_wilder_series_all(bars, n=14):
    """与 bars 等长的 Wilder ATR 序列（前 n 根为 None）。
       用于把当前 ATR% 放进「各历史时点的 ATR%」分布算分位，避免拿平滑值
       去比逐日 TR% 分布（波动放大时分位被系统性压低，风控信号永不触发）。"""
    L = len(bars)
    out = [None] * L
    if L < n + 1:
        return out
    trs = [None]
    for i in range(1, L):
        pc = bars[i - 1]["close"]
        t = max(bars[i]["high"] - bars[i]["low"],
                abs(bars[i]["high"] - pc), abs(bars[i]["low"] - pc))
        trs.append(t if math.isfinite(t) else 0.0)
    a = statistics.fmean(trs[1:n + 1])
    out[n] = a
    for i in range(n + 1, L):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def adx_series(bars, n=14):
    """Wilder DMI/ADX：+DI/-DI 判方向，ADX 判趋势强度（>25 趋势 / <20 震荡）。"""
    if len(bars) < n + 2:
        return None, None, None
    trs, pdm, mdm = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        dn = bars[i - 1]["low"] - bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(bars[i]["high"] - bars[i]["low"],
                       abs(bars[i]["high"] - pc), abs(bars[i]["low"] - pc)))
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)

    def _smooth(seq):
        a = sum(seq[:n])
        out = [a]
        for v in seq[n:]:
            a = a - a / n + v
            out.append(a)
        return out

    st, sp, sm = _smooth(trs), _smooth(pdm), _smooth(mdm)
    dx = []
    for t, p, m in zip(st, sp, sm):
        if t <= 0:
            dx.append(0.0)
            continue
        pdi, mdi = p / t * 100, m / t * 100
        dx.append(0.0 if pdi + mdi == 0 else abs(pdi - mdi) / (pdi + mdi) * 100)
    adx = statistics.fmean(dx[:n]) if len(dx) >= n else (statistics.fmean(dx) if dx else 0.0)
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
    pdi = sp[-1] / st[-1] * 100 if st[-1] else 0.0
    mdi = sm[-1] / st[-1] * 100 if st[-1] else 0.0
    return round(adx, 1), round(pdi, 1), round(mdi, 1)


def mfi_series(bars, n=14):
    """资金流量指标 MFI：典型价格 × 成交量，n 日正负资金流之比。"""
    if len(bars) < n + 1:
        return None
    pos = neg = 0.0
    for i in range(len(bars) - n, len(bars)):
        tp = (bars[i]["high"] + bars[i]["low"] + bars[i]["close"]) / 3
        ptp = (bars[i - 1]["high"] + bars[i - 1]["low"] + bars[i - 1]["close"]) / 3
        flow = tp * (bars[i].get("volume") or 0)
        if tp > ptp:
            pos += flow
        elif tp < ptp:
            neg += flow
    if neg <= 0:
        return 100.0 if pos > 0 else 50.0
    return round(100 - 100 / (1 + pos / neg), 1)


def hist_pct_rank(seq, v):
    """v 在历史序列中的分位（0=最低 / 100=最高）；样本不足返回 None。"""
    vals = [x for x in (seq or []) if x is not None and math.isfinite(x)]
    if len(vals) < 20 or v is None or not math.isfinite(v):
        return None
    # 用 (小于 + 0.5*等于)/n：常被调用方传入 vals 自身的最新值，
    # 全用 <= 会把高位重复值抬到 100，端点系统性偏高。
    lo = sum(1 for x in vals if x < v)
    eq = sum(1 for x in vals if x == v)
    return round((lo + 0.5 * eq) / len(vals) * 100, 1)


def _signed_extreme(seq, trough=False):
    """只在同符号区间取极值：顶背离比正柱峰，底背离比负柱谷。
       直接对负柱做 `*0.85` 会因乘法符号反转把「动能修复」误判成顶背离。"""
    vals = [x for x in (seq or [])
            if x is not None and math.isfinite(x) and (x < 0 if trough else x > 0)]
    if not vals:
        return None
    return min(vals) if trough else max(vals)


def macd_divergence(closes, hist, look=20):
    """价格显著创新高/新低、且同符号区间的柱峰/柱谷明显未跟随 → 顶/底背离。
       三重防线压横盘误报（严格 `>` 在横盘微波动下几乎必然成立）：
       ① 新高须超出 2×典型单根波动；② 整个比较窗口（40 根）须有 ≥1% 净方向；
       ③ 柱峰/柱谷衰减 ≥40%——温和衰减本身就是误报大头，动量衰竭应有明显幅度。
       实测随机横盘误报合计 ~40% → 9~10%，双峰/平台型真实背离不受影响。"""
    if not hist or len(closes) < look * 2 or len(hist) < look * 2:
        return None
    a, b = closes[-look:], closes[-look * 2:-look]
    ha, hb = hist[-look:], hist[-look * 2:-look]
    noise = sum(abs(a[i] - a[i - 1]) for i in range(1, look)) / max(1, look - 1)
    net = a[-1] / b[0] - 1 if b[0] else 0.0
    if max(a) > max(b) + 2 * noise and net > 0.01:
        pa, pb = _signed_extreme(ha), _signed_extreme(hb)
        if pa is not None and pb is not None and pa < pb * 0.60:
            return "top"
    if min(a) < min(b) - 2 * noise and net < -0.01:
        ta, tb = _signed_extreme(ha, True), _signed_extreme(hb, True)
        if ta is not None and tb is not None and abs(ta) < abs(tb) * 0.60:
            return "bottom"
    return None


def rsi_divergence(closes, rsi, look=20):
    """RSI 背离：按与 closes 对齐的尾部切片比较。与 macd_divergence 同三重防线
       （2×噪声新高门槛 + 40 根窗口 ≥1% 净方向 + RSI 极值回落 ≥4 点），
       横盘微波动不再误报。"""
    if not rsi or len(rsi) < look * 2 or len(closes) < look * 2:
        return None
    a, b = closes[-look:], closes[-look * 2:-look]
    ra, rb = list(rsi[-look:]), list(rsi[-look * 2:-look])
    if any(x is None or not math.isfinite(x) for x in ra + rb):
        return None
    noise = sum(abs(a[i] - a[i - 1]) for i in range(1, look)) / max(1, look - 1)
    net = a[-1] / b[0] - 1 if b[0] else 0.0
    if max(a) > max(b) + 2 * noise and net > 0.01 and max(ra) < max(rb) - 4:
        return "top"
    if min(a) < min(b) - 2 * noise and net < -0.01 and min(ra) > min(rb) + 4:
        return "bottom"
    return None


def boll_bw_signal(bw):
    """布林带宽近 60 日分位：收缩=变盘临近、极端放大=波动过热。"""
    if not bw or bw[-1] is None:
        return None
    rank = hist_pct_rank(bw[-60:], bw[-1])
    if rank is None:
        return None
    if rank <= 20:
        return ("波动收缩", 6, f"布林带宽处近 60 日 {rank:.0f}% 分位，变盘临近")
    if rank >= 90:
        return ("波动过热", -6, f"布林带宽处近 60 日 {rank:.0f}% 分位，波动风险偏高")
    return None


def candle_patterns(bars):
    """K 线组合：锤子线 / 看涨吞没 / 十字星 / 向上跳空缺口。"""
    if len(bars) < 3:
        return []
    out = []
    l, p = bars[-1], bars[-2]
    o, c, h, lo = l["open"], l["close"], l["high"], l["low"]
    body = abs(c - o)
    rng = h - lo
    if rng > 0 and body > 0 and (min(c, o) - lo) >= body * 2 and (h - max(c, o)) <= body * 0.6:
        out.append(("锤子线", 62, "长下影探底回升，下方承接明显"))
    if p["close"] < p["open"] and c > o and c >= p["open"] and o <= p["close"]:
        out.append(("看涨吞没", 72, "阳线实体包住前一根阴线，短线反转信号"))
    if rng > 0 and body / rng < 0.1:
        out.append(("十字星", 45, "多空平衡，方向待确认"))
    if lo > p["high"] * 1.005:
        out.append(("向上缺口", 66, "跳空高开未回补，属强势缺口"))
    return out


def chip_levels(bars, c, bins=48):
    """简化筹码分布：近 60 日成交量摊到价格格，找上方压力峰 / 下方支撑峰。"""
    win = bars[-60:]
    if len(win) < 20:
        return None, None
    lo = min(b["low"] for b in win)
    hi = max(b["high"] for b in win)
    if hi <= lo:
        return None, None
    step = (hi - lo) / bins
    chips = [0.0] * bins
    for _i, b in enumerate(win):
        v = b.get("volume") or 0
        if v <= 0:
            continue
        # 时间衰减：越久远的成交对当前筹码结构影响越小（旧实现 60 日等量平摊）
        w = v * (0.965 ** (len(win) - 1 - _i))
        a = max(0, min(bins - 1, int((b["low"] - lo) / step)))
        z = max(0, min(bins - 1, int((b["high"] - lo) / step)))
        if z == a:
            chips[a] += w
        else:
            for k in range(a, z + 1):
                chips[k] += w / (z - a + 1)
    mx = max(chips) or 1
    heavy = [lo + (i + 0.5) * step for i, v in enumerate(chips) if v >= mx * 0.5]
    up = [x for x in heavy if x > c * 1.005]
    dn = [x for x in heavy if x < c * 0.995]
    return (min(up) if up else None), (max(dn) if dn else None)


# ---------------------------------------------------------------- v1.5.13 技术面增强
def gap_stats(bars, look=60):
    """隔夜跳空风险：近 look 根日K的 open/prev_close 分布。
       隔夜战法最大风险是次日低开，而日K ATR 只覆盖日内波动，不含这段跳空。"""
    n = len(bars)
    if n < 25:
        return None
    gaps = []
    for i in range(max(1, n - look), n):
        pc = bars[i - 1]["close"]
        op, lo, hi = bars[i]["open"], bars[i]["low"], bars[i]["high"]
        if not pc or pc <= 0 or not op:
            continue
        gaps.append(((op / pc - 1) * 100, lo <= pc <= hi))   # 当日是否回补到前收
    if len(gaps) < 20:
        return None
    down = [g for g, _ in gaps if g < -0.5]
    big = [f for g, f in gaps if g < -1.0]
    return {
        "down_prob": round(len(down) / len(gaps) * 100, 1),
        "avg_down": round(statistics.fmean([abs(g) for g in down]), 2) if down else 0.0,
        "fill_rate": round(sum(1 for f in big if f) / len(big) * 100, 1) if big else None,
        "samples": len(gaps),
    }


def chip_overhead(bars, c, decay=0.965):
    """时间衰减 + 量能加权的筹码分布：现价上方套牢盘占比。
       权重必须含成交量：兜底源换手恒 0，只按天数衰减会让巨量日与微量日等权。"""
    win = bars[-60:]
    if len(win) < 20 or not c:
        return None
    tot, above = 0.0, 0.0
    L = len(win)
    for i, b in enumerate(win):
        v = b.get("volume") or 0
        if v <= 0:
            continue
        w = (decay ** (L - 1 - i)) * v * (1 + min(b.get("turnover") or 0, 10) / 10)
        px = (b["high"] + b["low"] + b["close"]) / 3
        tot += w
        if px > c * 1.002:
            above += w
    return round(above / tot * 100, 1) if tot else None


def sar_series(bars, af=0.02, af_step=0.02, af_max=0.2):
    """Wilder 抛物线转向 (Parabolic SAR)。返回与 bars 等长的 SAR 列表，
       只有第 0 根无法判方向填 None，第 1 根起即可用 highs[1]/highs[0] 定初始趋势。
       SAR 位于价格下方=多头(跟踪止损)，位于上方=空头(压力)。
       af 从 0.02 起，每创一次极值加 0.02，上限 0.2。"""
    n = len(bars)
    out = [None] * n
    if n < 3:
        return out
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    bull = highs[1] >= highs[0]
    sar = lows[0] if bull else highs[0]
    ep = highs[1] if bull else lows[1]
    step = af
    for i in range(1, n):
        sar = sar + step * (ep - sar)          # 逐步向极值靠拢
        if bull:
            # 只用已完成的 K 线夹逼；若把当根 lows[i] 也算进来，sar 会被压到
            # <= lows[i]，后面的 lows[i] < sar 永远为假，SAR 永远不会翻转。
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if highs[i] > ep:                  # 创新高：加速
                ep, step = highs[i], min(step + af_step, af_max)
            if lows[i] < sar:                  # 翻转为空
                bull, sar, ep, step = False, ep, lows[i], af
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if lows[i] < ep:
                ep, step = lows[i], min(step + af_step, af_max)
            if highs[i] > sar:                 # 翻转为多
                bull, sar, ep, step = True, ep, highs[i], af
        out[i] = round(sar, 3)
    return out


def _weekly_ohlc(bars):
    """自然周(周一为起点)聚合周K，供 weekly_trend / weekly_macd_kdj 共享，
       同一只票不必解析两遍日期。日期异常返回 None，由调用方走各自兜底。"""
    import datetime as _dt
    try:
        weeks = {}
        for b in bars:
            d = _dt.date.fromisoformat(str(b.get("date") or "")[:10])
            monday = d - _dt.timedelta(days=d.weekday())
            w = weeks.setdefault(monday, {"o": b["open"], "h": b["high"],
                                          "l": b["low"], "c": b["close"]})
            w["h"] = max(w["h"], b["high"])
            w["l"] = min(w["l"], b["low"])
            w["c"] = b["close"]                 # 收盘取周内最后一根
        return [dict(open=weeks[k]["o"], high=weeks[k]["h"],
                     low=weeks[k]["l"], close=weeks[k]["c"]) for k in sorted(weeks)]
    except Exception:
        return None


def weekly_macd_kdj(bars, wb=None):
    """按自然周把日K聚合成周K(开高低收)，再算周线 MACD / KDJ，给多周期共振信号。
       返回 {"bull"/"bear"/None, "detail": str}；样本不足返回 None。
       wb 可传入已聚合好的周K（_weekly_ohlc），避免重复解析日期。"""
    if wb is None:
        wb = _weekly_ohlc(bars)
    if not wb or len(wb) < 60:
        # 周线 MACD 的 EMA26 需要足够历史才收敛：26 周起算时 DIF/DEA 仍带明显偏差
        # （实测相对误差 20~30%，个别票 DEA 符号都反），宁可不给共振信号。
        return None
    wc = [b["close"] for b in wb]
    dif, dea, hist = macd_series(wc)
    k, d, j = kdj_series(wb)
    gold = len(wc) >= 2 and dif[-2] <= dea[-2] and dif[-1] > dea[-1]
    dead = len(wc) >= 2 and dif[-2] >= dea[-2] and dif[-1] < dea[-1]
    macd_bull = dif[-1] > dea[-1] and hist[-1] > 0
    kdj_bull = k[-1] > d[-1] and k[-1] < 80
    detail = ("周线 MACD DIF %.3f / DEA %.3f、KDJ K %.0f / D %.0f" %
              (dif[-1], dea[-1], k[-1], d[-1]))
    if (gold or macd_bull) and kdj_bull:
        return {"sig": "bull", "detail": detail + "，周线金叉/红柱 + KDJ 向上共振"}
    if (dead or (dif[-1] < dea[-1] and hist[-1] < 0)) and k[-1] < d[-1]:
        return {"sig": "bear", "detail": detail + "，周线死叉/绿柱 + KDJ 向下共振"}
    return None


def weekly_trend(bars, wb=None, adx=None):
    """多周期一致性：5 根日K合成周线，周线 MA 方向与日线 ADX/±DI 是否同向。
       震荡市里只靠日K金叉容易反复，周线确认能过滤掉大部分假信号。
       wb/adx 可传入已算好的周K与日线 ADX 三元组，避免重复计算。"""
    if len(bars) < 60:
        return None
    # 按自然周(周一为起点)聚合每周最后一根收盘价：
    # 旧写法从最旧端每 5 根一组，既丢最新 1~4 根、分组相位又逐日漂移。
    if wb is None:
        wb = _weekly_ohlc(bars)
    if wb is not None:
        wk = [b["close"] for b in wb]           # 按时间升序
    else:
        # date 缺失/异常时退回从最新端往回对齐，确保最新K一定入组；
        # 从最旧端分组会丢掉最新 1~4 根（含最新收盘），周线 MA 用的是旧数据
        wk = [bars[i]["close"] for i in range(len(bars) - 1, 3, -5)][::-1]
    if len(wk) < 10:
        return None
    w5, w10 = _ma_at(wk, 5), _ma_at(wk, 10)
    if w5 is None or w10 is None:
        return None
    if adx is None:
        adx, pdi, mdi = adx_series(bars)
    else:
        adx, pdi, mdi = adx
    up, di_ok = w5 > w10, (pdi or 0) > (mdi or 0)
    strong = (adx or 0) >= 22
    detail = ("周线 MA5 %.2f / MA10 %.2f，日线 ADX %.0f、+DI %.0f / -DI %.0f" %
              (w5, w10, adx or 0, pdi or 0, mdi or 0))
    if up and di_ok and strong:
        return {"state": "周线 / 日线一致向上", "score": 4, "detail": detail}
    if not up and not di_ok and strong:
        return {"state": "周线 / 日线一致向下", "score": -5, "detail": detail}
    return {"state": "多周期分歧或震荡", "score": -2, "detail": detail}


def tail_strength(trend):
    """尾盘 30 分钟资金强度：量占全日比 + 最后 15 分钟价格斜率。
       分时 volume 是逐分钟增量量，可直接累加。"""
    pts = (trend or {}).get("points") or []
    if len(pts) < 30:
        return None
    vt = sum(float(p.get("volume") or 0) for p in pts[-30:])
    va = sum(float(p.get("volume") or 0) for p in pts)
    p0 = pts[-16].get("price")
    p1 = pts[-1].get("price")
    return {
        "tail_vol_share": round(vt / va * 100, 1) if va else None,
        "last15_slope": round((p1 / p0 - 1) * 100, 2) if p0 and p1 else None,
    }


def rs_rank_map(rows, ind_gain):
    """行业 RS 分位化：把「个股涨幅 − 行业均涨幅」放进当日横截面取分位，
       避免 ±2% 固定阈值在小盘普涨时把整批票都判成「跑赢行业」。"""
    vals = []
    for q in rows or []:
        ind = str(q.get("industry") or "").strip()
        if ind and ind in (ind_gain or {}):
            vals.append((str(q.get("code")), float(q.get("pct") or 0) - ind_gain[ind]))
    if len(vals) < 10:
        return {}
    s = sorted(v for _, v in vals)
    # 与 pct_rank / hist_pct_rank 同口径：等于自身的样本只算半权，
    # 否则横盘/涨跌停潮时大量同值会把整批票的分位系统性推高。
    return {code: round((sum(1 for x in s if x < v) +
                         0.5 * sum(1 for x in s if x == v)) / len(s) * 100, 1)
            for code, v in vals}


def prescore_snapshot(q, p):
    """候选池预排序：涨幅/换手/量比/主力净额/成交额多因子，
       不再让量比单项截断掉形态与分时更好的票。"""
    lo, hi = p["gain_min"], p["gain_max"]
    gain = clamp(100 - abs(q["pct"] - (lo + hi) / 2) / max((hi - lo) / 2, 0.3) * 40)
    tlo, thi = p["turn_min"], p["turn_max"]
    turn = clamp(100 - abs(q["turnover"] - (tlo + thi) / 2) / max((thi - tlo) / 2, 1) * 30)
    # 量比到 2.2 即饱和：异常放量常是出货，不该靠这一项把票顶进候选池
    vr = clamp(min(q["volratio"], 2.2) / 2.0 * 100)
    net = clamp(50 + (q.get("main_net") or 0) / max(q.get("fcap") or 1, 1) * 100 * 12)
    liq = clamp(min(q["amount"] / 1e8, 10) / 5 * 100)
    return gain * 0.30 + turn * 0.20 + vr * 0.22 + net * 0.18 + liq * 0.10


def market_env(snap=None):
    """大盘环境：上证 20 日趋势 + 全市场涨跌家数宽度 + 指数是否站上 MA20。
       弱市里同样的技术信号胜率明显下降，用它做温和调节而不是硬闸门。"""
    try:
        rows = snap if snap is not None else (fetch_snapshot().get("rows") or [])
    except Exception:
        rows = []
    up = sum(1 for q in rows if (q.get("pct") or 0) > 0)
    dn = sum(1 for q in rows if (q.get("pct") or 0) < 0)
    breadth = round(up / max(up + dn, 1) * 100, 1)
    idx = index_gain20()
    ic, ima = 0, None
    try:
        ib = fetch_kline("000001", 1, 40)
        ic = ib[-1]["close"] if ib else 0
        ima = ma([b["close"] for b in ib], 20) if ib else None
    except Exception:
        pass
    above = bool(ic and ima and ic > ima)
    score = clamp(50 + (breadth - 50) * 0.6 + (idx or 0) * 1.5 + (8 if above else -8))
    state = "偏强" if score >= 62 else "偏弱" if score <= 38 else "中性"
    return {"score": round(score, 1), "state": state, "breadth": breadth,
            "up": up, "down": dn,
            "index_gain20": (round(idx, 2) if idx is not None else None),
            "index_above_ma20": above}


ANN_RISK_WORDS = ("减持", "解禁", "预亏", "亏损", "立案", "处罚", "退市", "质押",
                  "冻结", "诉讼", "违规", "商誉减值", "业绩下滑", "被查", "问询函")


def ann_risk_flags(code, rows=None):
    """硬利空公告：返回去重后的标题，供评分扣分与前端提示。
    rows 可选：批量调用方预先取好的该股公告，避免每个代码重复加锁/联网。"""
    if rows is None:
        fetch_announcements()
        with _ann_lock:
            rows = list((_ann["by_code"] or {}).get(str(code), []))[:5]
    out = []
    for a in rows:
        t = str(a.get("title") or "")
        if any(w in t for w in ANN_RISK_WORDS) and t[:24] not in out:
            out.append(t[:24])
    return out[:3]


def industry_gain(rows):
    """行业当日平均涨幅，用于个股相对行业强度（RS 的横截面基准）。"""
    d = {}
    for q in rows or []:
        ind = str(q.get("industry") or "").strip()
        if not ind:
            continue
        g = d.setdefault(ind, [0.0, 0])
        g[0] += q.get("pct") or 0
        g[1] += 1
    return {k: round(v[0] / v[1], 2) for k, v in d.items() if v[1] >= 3}


def _industry_pe(rows):
    """行业内 PE 分位：银行 PE 5 不该和半导体 PE 60 放在同一条分布里比。"""
    d = {}
    for q in rows:
        ind = q.get("industry") or ""
        pe = q.get("pe_ttm") or 0
        if ind and 1 < pe < 200:
            d.setdefault(ind, []).append(pe)
    return {k: sorted(v) for k, v in d.items() if len(v) >= 8}


def pct_rank(sorted_vals, v):
    """v 在样本中的百分位（0=最低）"""
    if not sorted_vals:
        return 50.0
    import bisect
    n = len(sorted_vals)
    if n == 1:
        return 50.0          # 单样本既是最低也是最高，取中位而非 0
    # 样本中含自身：用 (小于个数 + 0.5*等于个数) / n，端点不再失真
    lo = bisect.bisect_left(sorted_vals, v)
    hi = bisect.bisect_right(sorted_vals, v)
    return max(0.0, min(100.0, (lo + 0.5 * (hi - lo)) / n * 100))


def _ma_at(seq, n, idx=None):
    if idx is None:
        idx = len(seq) - 1
    if idx + 1 < n:
        return None
    return sum(seq[idx - n + 1:idx + 1]) / n


def detect_patterns(bars):
    """经典形态识别，返回 [(分数, 名称, 说明)]"""
    out = []
    n = len(bars)
    if n < 40:
        return out
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    last = bars[-1]
    c = last["close"]

    ma5 = _ma_at(closes, 5); ma10 = _ma_at(closes, 10)
    ma20 = _ma_at(closes, 20); ma60 = _ma_at(closes, 60)
    ma20_prev = _ma_at(closes, 20, n - 11) if n >= 31 else None
    avg5v = statistics.fmean(vols[-6:-1]) if n >= 6 else vols[-1]

    # 平台突破
    if n >= 62:
        hi60 = max(highs[-61:-1])
        if c >= hi60 * 0.995 and vols[-1] > avg5v * 1.15:
            out.append((90, "平台突破", f"放量突破近 60 日高点 {hi60:.2f}"))
        elif c >= hi60 * 0.97 and c > (ma20 or 0):
            out.append((66, "逼近平台高点", f"距 60 日高点 {hi60:.2f} 仅 {(c/hi60-1)*100:+.1f}%"))

    # 回踩支撑
    if ma20 and ma20_prev and ma20 > ma20_prev and ma20 * 0.982 <= c <= ma20 * 1.025:
        out.append((82, "回踩支撑", f"回踩 MA20（{ma20:.2f}）获支撑，MA20 仍在上行"))

    # W 底
    if n >= 50:
        seg = closes[-50:]
        mid1 = min(seg[:20]); mid2 = min(seg[20:40])
        neck = max(seg[15:40])
        if mid1 > 0 and abs(mid1 - mid2) / mid1 <= 0.05 and neck / min(mid1, mid2) - 1 >= 0.06 \
                and c >= neck * 0.985:
            out.append((88, "W 底突破", f"双底 {min(mid1, mid2):.2f} 附近，已站上颈线 {neck:.2f}"))

    # 老鸭头：MA5 上穿 MA10 后回调不破 MA10，再度上攻
    if n >= 45 and ma5 and ma10:
        crossed = False
        for i in range(n - 26, n - 3):
            if i < 10:
                continue
            a5, a5p = _ma_at(closes, 5, i), _ma_at(closes, 5, i - 1)
            a10, a10p = _ma_at(closes, 10, i), _ma_at(closes, 10, i - 1)
            if None in (a5, a5p, a10, a10p):
                continue
            if a5p <= a10p and a5 > a10:
                crossed = True
                break
        if crossed:
            trough = min(closes[-9:])
            shrink = statistics.fmean(vols[-9:-1])
            # 文案承诺「缩量回调」，就必须真的检查回调段量能不放大
            if (ma10 and trough >= ma10 * 0.96 and c > ma5 > ma10
                    and shrink > 0 and (vols[-9] / shrink) <= 1.15):
                out.append((84, "老鸭头", "MA5 上穿 MA10 后缩量回调不破，二次上攻"))

    # 长上影试盘 / 仙人指路
    body = abs(c - last["open"])
    upper = last["high"] - max(c, last["open"])
    if n >= 25 and c >= last["open"] and upper > max(body * 2, c * 0.018) and ma20 and c > ma20:
        if last.get("pct", 0) >= -0.5:
            out.append((78, "长上影试盘", f"上影 {upper:.2f} 元测试上方抛压，收盘守住红盘"))

    # 均线粘合后发散
    if ma5 and ma10 and ma20 and ma60:
        spread = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20
        if spread < 0.03 and c > max(ma5, ma10, ma20) and ma20 > ma60 * 0.99:
            out.append((80, "均线粘合发散", "MA5/10/20 粘合后向上发散，变盘向上"))

    # 上升通道
    if ma20 and ma60 and ma20 > ma60 and c > ma20 and n >= 21:
        up_days = sum(1 for i in range(n - 20, n) if closes[i] > closes[i - 1])
        if up_days >= 11:
            out.append((58, "上升通道", f"近 20 日 {up_days} 天收阳，MA20 位于 MA60 上方"))

    return out


def fetch_survey(days=30):
    """近 N 日机构调研记录（真实公开数据），用作「消息面」维度的可量化信号"""
    def build():
        since = (now_cn() - timedelta(days=days)).strftime("%Y-%m-%d")
        flt = urllib.parse.quote(f"(RECEIVE_START_DATE>='{since}')")
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               "reportName=RPT_ORG_SURVEYNEW&columns=SECURITY_CODE,SECURITY_NAME_ABBR,"
               "RECEIVE_START_DATE,NUMBERNEW"
               f"&filter={flt}&pageNumber=1&pageSize=1000"
               "&sortColumns=RECEIVE_START_DATE&sortTypes=-1")
        j = http_json(url, retries=2, timeout=12)
        out = {}
        for r in (j.get("result") or {}).get("data") or []:
            code = str(r.get("SECURITY_CODE") or "")
            if not code:
                continue
            try:
                orgs = float(r.get("NUMBERNEW") or 1)
            except Exception:
                orgs = 1.0
            cur = out.get(code, {"times": 0, "orgs": 0.0, "date": ""})
            cur["times"] += 1
            cur["orgs"] += orgs
            cur["date"] = max(cur["date"], str(r.get("RECEIVE_START_DATE") or "")[:10])
            out[code] = cur
        return out

    try:
        return cached("survey", 3600, build)
    except Exception:
        return {}


AI_DEFAULTS = {
    "universe": 200,        # 深度分析候选数
    "top": 42,              # 返回卡片数
    "min_score": 55,
    "min_ind": 2,           # 至少要命中几个核心指标
    "min_amount": 0.8,      # 成交额下限（亿）
    "fcap_min": 20,
    "fcap_max": 2000,
    "pct_min": -6.0,
    "pct_max": 9.8,         # 排除涨停（买不进）
}


TAG_MAP = [
    ("MACD 金叉", "MACD金叉"), ("MACD 多头", "MACD多头"),
    ("KDJ 低位金叉", "KDJ低位金叉"), ("KDJ 金叉", "KDJ金叉"),
    ("BOLL 上轨", "BOLL突破"), ("BOLL 中轨", "BOLL中轨"),
    ("均线多头排列", "均线多头"), ("短中期均线多头", "均线多头"),
    ("放量上涨", "量价齐升"),
    ("主力净流入", "主力流入"), ("量比", "量能放大"),
    ("W 底突破", "W底"), ("老鸭头", "老鸭头"), ("平台突破", "平台突破"),
    ("逼近平台高点", "逼近前高"), ("回踩支撑", "回踩支撑"),
    ("长上影试盘", "长上影试盘"), ("均线粘合发散", "均线粘合"),
    ("上升通道", "上升通道"),
    ("PE(TTM)", "低估值"), ("ROE", "ROE良好"),
]

# 计入「命中几个指标」的核心指标（不含量比这类弱信号）
CORE_LABELS = {"MACD金叉", "MACD多头", "KDJ金叉", "KDJ低位金叉", "BOLL突破", "BOLL中轨",
               "均线多头", "W底", "老鸭头", "平台突破", "逼近前高", "回踩支撑",
               "长上影试盘", "均线粘合", "上升通道", "量价齐升", "主力流入"}


def sig_tag(text):
    for k, v in TAG_MAP:
        if k in text:
            return v
    return ""


def ai_evaluate(q, bars, pe_sorted, pb_sorted, p, survey=None, pe_by_ind=None,
                 env=None, sm=None, defer_metrics=False):
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    n = len(bars)
    c = closes[-1]
    ma5, ma10 = _ma_at(closes, 5), _ma_at(closes, 10)
    ma20, ma60 = _ma_at(closes, 20), _ma_at(closes, 60)
    dif, dea, hist = macd_series(closes)
    K, D, J = kdj_series(bars)
    bmid, bup, blow, bw = sm["boll"] if sm is not None else boll_series(closes)
    t = q["pct"]

    sig = []          # (强度, 维度, 文本)；维度=技术/形态/资金/估值/财务
    tech = 0.0

    # ---- MACD
    gold = any(dif[i - 1] <= dea[i - 1] and dif[i] > dea[i] for i in range(max(1, n - 3), n))
    if gold:
        tech += 30
        sig.append((96, "技术", f"MACD 金叉（DIF {dif[-1]:+.2f} 上穿 DEA {dea[-1]:+.2f}）"))
    elif dif[-1] > dea[-1] > 0:
        tech += 18
        sig.append((70, "技术", "MACD 多头排列，红柱延续"))

    # ---- KDJ
    kdj_gold = any(K[i - 1] <= D[i - 1] and K[i] > D[i] for i in range(max(1, n - 2), n))
    if kdj_gold:
        tech += 16 if K[-1] >= 35 else 24
        tip = "低位" if K[-1] < 35 else ""
        sig.append((88 if K[-1] < 35 else 80, "技术",
                    f"KDJ {tip}金叉（K={K[-1]:.1f}，D={D[-1]:.1f}）"))

    # ---- BOLL
    if bup[-1] and c > bup[-1] * 0.995:
        tech += 22
        _opening = bool(bw[-1] and bw[-6] and bw[-1] > bw[-6] * 1.1)
        _av5 = statistics.fmean(vols[-6:-1]) if n >= 6 else 0
        _vol_ok = bool(_av5 > 0 and vols[-1] > _av5 * 1.1)
        _desc = ("放量突破 BOLL 上轨" if _vol_ok else "突破 BOLL 上轨")
        _desc += (f"（{bup[-1]:.2f}），布林开口" if _opening else f"（{bup[-1]:.2f}）")
        sig.append((90, "技术", _desc))
    elif bmid[-1] and c > bmid[-1] and n >= 2 and closes[-2] < (bmid[-2] or bmid[-1]):
        tech += 14
        sig.append((74, "技术", f"站上 BOLL 中轨 {bmid[-1]:.2f}，弱势转强"))

    # ---- 均线
    if ma5 and ma10 and ma20 and ma60 and ma5 > ma10 > ma20 > ma60:
        tech += 26
        sig.append((92, "技术", "均线多头排列：MA5>MA10>MA20>MA60"))
    elif ma5 and ma10 and ma20 and c > ma5 > ma10 > ma20:
        tech += 20
        sig.append((82, "技术", "短中期均线多头，价站 MA5 上方"))

    # ---- 量价
    avg5v = statistics.fmean(vols[-6:-1]) if n >= 6 else vols[-1]
    if avg5v > 0 and vols[-1] > avg5v * 1.3 and t > 0:
        tech += 14
        sig.append((76, "技术", f"放量上涨：成交量为 5 日均量 {vols[-1]/avg5v:.1f} 倍"))
    # ---- RSI(14) / OBV：补上经典体系里两个最常用、原来缺失的指标
    _rsi_seq = rsi_series(closes)      # 序列只算一次，下面 RSI 背离复用
    rsi14 = _rsi_seq[-1]
    _obv_last, obv_s = obv_signal(bars)
    if rsi14 is not None:
        if rsi14 >= 80:
            tech -= 8
            sig.append((28, "技术", f"RSI(14) {rsi14:.0f} 超买，短线追高赔率下降"))
        elif 45 <= rsi14 <= 68:
            tech += 8
            sig.append((72, "技术", f"RSI(14) {rsi14:.0f}，强弱适中、未过热"))
        elif rsi14 < 25:
            tech += 6
            sig.append((66, "技术", f"RSI(14) {rsi14:.0f} 超卖，存在技术反抽机会"))
    if obv_s is not None:
        if obv_s > 0.35:
            tech += 8
            sig.append((78, "技术", f"OBV 近 10 日净流入 {obv_s:+.2f} 倍均量，资金持续吸筹"))
        elif obv_s < -0.35:
            tech -= 8
            sig.append((32, "技术", f"OBV 近 10 日净流出 {obv_s:+.2f} 倍均量，量能背离"))

    # ---- 新增技术分析维度：趋势强度 / 资金流量 / 波动率 / 背离 / K线组合 / 筹码 / 行业RS
    extra = 0.0
    adx, pdi, mdi = ((sm["adx"], sm["pdi"], sm["mdi"]) if sm is not None
                     else adx_series(bars))
    if adx is not None:
        if adx >= 25 and pdi > mdi:
            extra += 8
            sig.append((80, "技术", f"ADX {adx:.0f} 趋势成立且 +DI 领先，顺势信号更可靠"))
        elif adx < 20:
            extra -= 5
            sig.append((42, "技术", f"ADX {adx:.0f} 处震荡格局，金叉类信号易反复"))
    mfi = sm["mfi"] if sm is not None else mfi_series(bars)
    if mfi is not None:
        if mfi >= 75:
            extra -= 5
            sig.append((36, "技术", f"MFI(14) {mfi:.0f} 资金流过热"))
        elif 40 <= mfi <= 65:
            extra += 5
            sig.append((70, "技术", f"MFI(14) {mfi:.0f}，量价资金流健康"))
        elif mfi < 25:
            extra += 4
            sig.append((64, "技术", f"MFI(14) {mfi:.0f} 超卖，资金流有望回补"))
    # bw 已在函数开头算过（2347），复用即可，扫描热路径不再重复算两遍布林带
    bws = boll_bw_signal(bw)
    if bws:
        extra += bws[1]
        sig.append((72 if bws[1] > 0 else 38, "技术", f"{bws[0]}：{bws[2]}"))
    _dv = macd_divergence(closes, hist)
    if _dv == "top":
        extra -= 8
        sig.append((30, "技术", "MACD 柱顶背离：价创新高而柱峰走低"))
    elif _dv == "bottom":
        extra += 7
        sig.append((76, "技术", "MACD 柱底背离：价创新低而柱谷抬高"))
    _rdv = rsi_divergence(closes, _rsi_seq)
    if _rdv == "top":
        extra -= 6
        sig.append((34, "技术", "RSI 顶背离，追高需谨慎"))
    elif _rdv == "bottom":
        extra += 5
        sig.append((70, "技术", "RSI 底背离，反弹动能积累"))
    for _nm, _sc, _desc in candle_patterns(bars):
        if _sc >= 62:
            extra += 4
            sig.append((_sc, "形态", f"{_nm}：{_desc}"))
    _res, _sup = ((sm["chip_res"], sm["chip_sup"]) if sm is not None
                  else chip_levels(bars, c))
    if _res and (_res / c - 1) < 0.025:
        extra -= 5
        sig.append((40, "技术", f"上方 {_res:.2f} 有筹码峰压力，空间不足 2.5%"))
    elif _sup and (1 - _sup / c) < 0.03:
        extra += 4
        sig.append((70, "技术", f"下方 {_sup:.2f} 有筹码峰支撑"))
    # ---- v1.5.13 新增技术面：只作用于 extra，不动原有评分基线
    _g = gap_stats(bars)
    if _g and _g["down_prob"] >= 60:
        extra -= 3
        sig.append((42, "技术", f"隔夜低开概率 {_g['down_prob']:.0f}%，跳空风险偏高"))
    elif _g and _g["down_prob"] <= 30:
        extra += 2
        sig.append((64, "技术", f"隔夜低开概率仅 {_g['down_prob']:.0f}%，开盘风险较小"))
    _ov = chip_overhead(bars, c)
    if _ov is not None and _ov >= 65:
        extra -= 3
        sig.append((40, "技术", f"现价上方套牢盘约 {_ov:.0f}%，冲高抛压重"))
    elif _ov is not None and _ov <= 30:
        extra += 2
        sig.append((66, "技术", f"现价上方套牢盘仅 {_ov:.0f}%，抛压较轻"))
    _wt = weekly_trend(bars, adx=((sm["adx"], sm["pdi"], sm["mdi"]) if sm is not None else None))
    if _wt:
        extra += _wt["score"]
        sig.append((60 if _wt["score"] > 0 else 42, "技术", _wt["state"]))
    _rr = q.get("_rs_rank")
    if _rr is not None:
        if _rr >= 80:
            extra += 3
            sig.append((70, "技术", f"相对行业强度处当日 {_rr:.0f}% 分位"))
        elif _rr <= 20:
            extra -= 2
            sig.append((44, "技术", f"相对行业强度仅 {_rr:.0f}% 分位"))
    # v1.5.17：硬利空公告统一由 message_score() 计入「消息面」维度，
    # 不再在这里偷偷扣基础分——否则关掉消息面开关后扣分仍在，且开关开启时会双重扣分。
    # 大盘环境只调节「新增信号」的强度：弱市削弱正向信号、放大风险信号，
    # 不动原有评分基线，避免市场状态一变就把历史分数全部推翻。
    _ef = 1.0
    if env and env.get("score") is not None:
        _ef = 0.7 if env["score"] <= 38 else 1.1 if env["score"] >= 62 else 1.0
    extra = extra * _ef if extra > 0 else extra / _ef
    tech = clamp(tech * 0.86 + extra)
    if tech <= 0 and extra < 0:
        tech = 20.0                 # 明确利空给低分，但不归零淹没其余维度


    # ---- 形态
    pats = detect_patterns(bars)
    pats.sort(key=lambda x: -x[0])
    pattern = pats[0][0] if pats else 22     # 无形态不再白送 38 分
    for sc, name, desc in pats[:2]:
        sig.append((sc, "形态", f"{name}：{desc}"))

    # ---- 资金
    fcap = q["fcap"] or 1
    net_ratio = (q.get("main_net") or 0) / fcap * 100
    has_net = "main_net" not in (q.get("_miss") or [])
    if has_net:
        fund = clamp(42 + min(q["volratio"], 3) * 8 + min(q["turnover"], 12) * 1.2 + net_ratio * 9)
    else:
        fund = clamp(18 + min(q["volratio"], 3) * 6 + min(q["turnover"], 12) * 0.8)
        sig.append((30, "资金", "主力净额未采集，资金维度已降权"))
    if has_net and (q.get("main_net") or 0) > 0:
        sig.append((min(92, 62 + net_ratio * 8), "资金",
                    f"主力净流入 {q['main_net']/1e8:.2f} 亿（占流通市值 {net_ratio:.2f}%）"))
    if q["volratio"] >= 1.5:
        sig.append((72, "资金", f"量比 {q['volratio']:.2f}，换手 {q['turnover']:.1f}%，资金活跃"))
    elif q["volratio"] >= 1:
        sig.append((58, "资金", f"量比 {q['volratio']:.2f}，成交较近期温和放大"))

    # ---- 估值
    pe, pb = q.get("pe_ttm") or 0, q.get("pb") or 0
    val = 50.0
    if 0 < pe < 200:
        pool = (pe_by_ind or {}).get(q.get("industry") or "") or pe_sorted
        pr = pct_rank(pool, pe)
        val = clamp(100 - pr)
        lvl = "偏低" if pr < 35 else ("居中" if pr < 70 else "偏高")
        scope = "同行业" if len(pool) < len(pe_sorted) else "主板"
        sig.append((72 if pr < 35 else 52, "估值",
                    f"PE(TTM) {pe:.1f}，处{scope} {pr:.0f}% 分位，估值{lvl}"))
    elif pe <= 0:
        val = 28.0
        sig.append((35, "估值", "PE(TTM) 为负或缺失，亏损股估值项扣分"))
    elif pe >= 200:
        val = 26.0
        sig.append((30, "估值", f"PE(TTM) {pe:.0f} 倍，估值极高，盈利安全垫不足"))
    if 0 < pb < 15:
        pbr = pct_rank(pb_sorted, pb)
        val = clamp(val * 0.7 + (100 - pbr) * 0.3)

    # ---- 财务（ROE ≈ PB / PE，同口径估算）
    fin = 45.0
    if pe <= 0:
        fin = 24.0                       # 亏损股排雷，不再拿中性 45 分
    elif 0 < pe < 300 and 0 < pb < 30:
        roe = pb / pe * 100
        fin = clamp(30 + roe * 1.6)     # 原来 ROE≈19% 就打满，改为更平缓
        if roe >= 10:
            sig.append((66, "财务", f"ROE 约 {roe:.1f}%（PB/PE 估算），盈利质量较好"))
        elif roe < 3:
            sig.append((40, "财务", f"ROE 约 {roe:.1f}%，盈利能力偏弱"))

    # ---- 消息面（机构调研关注度，真实公开数据）
    news = 50.0
    sv = (survey or {}).get(q["code"])
    if sv:
        news = clamp(58 + min(sv["times"], 4) * 8 + min(sv["orgs"], 60) * 0.45)
        sig.append((min(90, 60 + sv["times"] * 8), "消息",
                    f"近 30 日 {sv['times']} 次机构调研（约 {int(sv['orgs'])} 家机构），最近 {sv['date']}"))

    # 六维加权：技术 32 + 形态 18 + 资金 22 + 消息 12 + 估值 8 + 财务 8
    # AI 荐股走「短线动能 + 资金 + 消息催化」口径，估值/财务只做排雷
    total = (tech * 0.32 + pattern * 0.18 + fund * 0.22
             + news * 0.12 + val * 0.08 + fin * 0.08)

    # 卡片只展示「指标类」信号（技术/形态/资金），估值财务留到详情里
    sig.sort(key=lambda x: -x[0])
    ind = [s for s in sig if s[1] in ("技术", "形态", "资金")]
    picked, used = [], {}
    for s in ind:
        if used.get(s[1], 0) >= (2 if s[1] in ("技术", "形态") else 1):
            continue
        picked.append(s)
        used[s[1]] = used.get(s[1], 0) + 1
        if len(picked) >= 3:
            break
    if len(picked) < 3:
        for s in ind:
            if s not in picked:
                picked.append(s)
            if len(picked) >= 3:
                break
    # 兜底：一个指标都没命中时，用估值/财务说明为什么入选
    if len(picked) < 3:
        for s in sig:
            if s not in picked:
                picked.append(s)
            if len(picked) >= 3:
                break

    rules = {
        "tech": {"name": "① 技术指标（MACD / KDJ / BOLL / 均线 / 量价）", "passed": round(tech) >= 60,
                 "value": f"{tech:.0f} 分", "score": round(tech, 1),
                 "detail": "金叉、突破、均线多头、放量上涨累计", "weight": 32},
        "pattern": {"name": "② 形态（突破 / 反包 / 加速）", "passed": round(pattern) >= 60,
                    "value": f"{pattern:.0f} 分", "score": round(pattern, 1),
                    "detail": (pats[0][1] + "：" + pats[0][2]) if pats else "未识别到经典形态", "weight": 18},
        "fund": {"name": "③ 资金（主力净额 / 量比 / 换手）", "passed": round(fund) >= 60,
                 "value": f"{fund:.0f} 分", "score": round(fund, 1),
                 "detail": ((f"主力净额 {q['main_net']/1e8:+.2f} 亿，量比 {q['volratio']:.2f}")
                            if has_net else
                            f"主力净额缺失（备用源），按量比 {q['volratio']:.2f} / 换手 {q['turnover']:.1f}% 计"),
                 "weight": 22},
        "news": {"name": "④ 消息（机构调研催化）", "passed": round(news) >= 58,
                 "value": f"{news:.0f} 分", "score": round(news, 1),
                 "detail": (f"近 30 日 {sv['times']} 次调研" if sv else "近 30 日无公开调研记录，按中性计"), "weight": 12},
        "val": {"name": "⑤ 估值（PE / PB 分位）", "passed": round(val) >= 55,
                "value": f"{val:.0f} 分", "score": round(val, 1),
                "detail": (f"PE {pe:.1f} / PB {pb:.2f}" if pe > 0 else "PE 为负或缺失"), "weight": 8},
        "fin": {"name": "⑥ 财务（ROE 估算）", "passed": round(fin) >= 55,
                "value": f"{fin:.0f} 分", "score": round(fin, 1),
                "detail": "ROE ≈ PB/PE，仅作粗略衡量", "weight": 8},
    }

    grade = _grade_of("ai", total)
    labels = []
    for s in ind:
        t2 = sig_tag(s[2])
        if t2 and t2 not in labels:
            labels.append(t2)

    res = {
        "code": q["code"], "name": q["name"], "market": q["market"], "mode": "ai",
        "price": q["price"], "prev_close": q["prev_close"], "pct": round(t, 2),
        "volratio": round(q["volratio"], 2), "turnover": round(q["turnover"], 2),
        "fcap": round(fcap / 1e8, 1), "amount": round(q["amount"] / 1e8, 2),
        "main_net": (None if "main_net" in (q.get("_miss") or []) else round((q.get("main_net") or 0) / 1e8, 2)),
        "industry": q["industry"],
        "pe": round(pe, 1), "pb": round(pb, 2),
        "high": q["high"], "open": q["open"],
        "score": round(total, 1), "grade": grade,
        "reasons": [x[2] for x in picked[:3]],
        "labels": labels[:8],
        "ind_count": sum(1 for l in labels if l in CORE_LABELS),
        "dims": {"tech": round(tech), "pattern": round(pattern), "fund": round(fund),
                 "news": round(news), "val": round(val), "fin": round(fin)},
        "rules": rules, "tags": labels[:8],
    }
    # 与 scan / swing 同一份指标口径。defer_metrics 时这里不算——AI 深扫 200 只
    # 只有入选的几十只会展示，全算等于白算一百多只；把 sm 和评分时已算好的
    # RSI/OBV/跳空/套牢盘挂在结果上，由调用方给入选票补齐（口径完全一致）。
    pre = {"rsi14": rsi14, "obv_slope": obv_s, "gap": _g, "chip_overhead": _ov}
    if defer_metrics:
        res["_sm"], res["_pre"] = sm, pre
    else:
        res["metrics"] = detail_metrics(bars, sm=sm, pre=pre)
    return res


def _deep_scan(cand, work, workers, min_b, min_eval):
    """深度分析统一出口。
    电脑端：一次性全量评估（与历史版本行为完全一致）。
    手机端：按预筛分排序分波评估——每波 workers 只；已评过 min_eval 只且
    凑够 min_b 只 B 档（score≥66）后提前收工。
    保底逻辑：预筛分越靠前越先评；收工前手里已有 min_b 只 B 档以上，
    最终榜单的截断线必然 ≥ B 档——后面没评的票要挤进榜单得先赢过一只 B 档，
    C/D 档票被漏掉不影响榜单质量。弱势行情凑不够 min_b 时自动全量扫完，
    结果与不提速完全一致，只是白等。"""
    if not LIGHT:
        results, errors = [], 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(work, cand):
                if r is None:
                    errors += 1
                else:
                    results.append(r)
        return results, errors
    results, errors = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i in range(0, len(cand), workers):
            wave = cand[i:i + workers]
            for r in ex.map(work, wave):
                if r is None:
                    errors += 1
                else:
                    results.append(r)
            done = i + len(wave)
            nb = sum(1 for r in results if (r.get("score") or 0) >= 66)
            if done >= min_eval and nb >= min_b:
                break
    return results, errors


def ai_pick(params):
    t0 = time.time()
    # AI 板块内的「多日持股算法」开关：仅这一次请求切换到趋势波段口径。
    # 复用 swing_pick 的完整算法与返回结构（total/base/deep/list/params 等字段一致），
    # 不写任何全局设置、不改动 swing_pick 本身、不影响其他任何板块。
    if (params or {}).get("algo") == "swing":
        sp = {}
        for k in ("universe", "min_score", "top"):
            if k in params and params[k] not in (None, ""):
                sp[k] = params[k]
        # fast=1：开关刚打开时的即时换算，只用已缓存的日K，不额外联网。
        # 前端拿到 insufficient 会立刻改走完整扫描，不需要用户再点按钮。
        if str((params or {}).get("fast") or "") in ("1", "true", "yes"):
            r = swing_pick_fast(sp)
            r["note"] = "已按「多日持股」趋势波段口径换算（仅对 AI 荐股生效）。" + (r.get("note") or "")
            return r
        r = swing_pick(sp)
        r["algo"] = "swing"
        r["note"] = "已按「多日持股」趋势波段口径筛选（本页开关，仅对 AI 荐股生效）。" + (r.get("note") or "")
        return r
    p = dict(AI_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v not in (None, ""):
            p[k] = coerce_param(AI_DEFAULTS[k], v)

    # 手机/模拟器候选池封顶：解释执行 + GIL 串行 CPU，评满 200 只近一分钟；
    # 预筛排序已把资金/量能活跃的票排前，截尾对榜单影响有限（桌面端不受影响）。
    _cap_note = ""
    if LIGHT and int(p["universe"]) > 100:
        p["universe"] = 100
        _cap_note = "手机轻量模式：候选池封顶 100 只。"

    # 快照与调研互不依赖：冷扫时串行拉取白白多等一段网络往返，并行取
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_pack = ex.submit(fetch_snapshot)
        f_survey = ex.submit(fetch_survey, 30)
        pack = f_pack.result()
        survey = f_survey.result()
    rows, source = pack["rows"], pack["source"]

    pe_sorted = sorted([q["pe_ttm"] for q in rows if 1 < q.get("pe_ttm", 0) < 200])
    pb_sorted = sorted([q["pb"] for q in rows if 0.3 < q.get("pb", 0) < 20])
    pe_by_ind = _industry_pe(rows)
    ind_gain = industry_gain(rows)
    env = market_env(rows)

    base = []
    for q in rows:
        if q["volume"] <= 0 or is_bad_name(q["name"], True):
            continue
        if bad_symbol(q["name"]) or not list_days_ok(q.get("list_date")):
            continue
        if q["amount"] / 1e8 < p["min_amount"]:
            continue
        if not (p["fcap_min"] <= q["fcap"] / 1e8 <= p["fcap_max"]):
            continue
        if not (p["pct_min"] <= q["pct"] <= p["pct_max"]):
            continue
        base.append(q)

    # 备用源(新浪)把 main_net 折成 0，资金因子会全体同分；此时改用换手+量比代理资金热度，
    # 只在资金数据整体缺失时生效，正常东财源排序完全不变。
    fund_degraded = bool(base) and all(not q.get("main_net") for q in base)

    def prescore(q):
        nr = q["main_net"] / q["fcap"] * 100 if q["fcap"] else 0
        fund = (min(q["turnover"], 20) * 1.2 + min(q["volratio"], 4) * 2) if fund_degraded else (nr * 6)
        return fund + min(q["volratio"], 4) * 3 + q["pct"] * 1.2 + min(q["turnover"], 20) * 0.5

    # 备用源缺量比时，先用腾讯批量补齐，否则资金维度会全线失真
    if any(not q["volratio"] for q in base):
        base, filled = enrich_volratio(base)
        if filled:
            source += "+腾讯"

    base.sort(key=lambda q: -prescore(q))
    # 用副本构造候选：下面会给每只写 _rs_rank 派生字段，绝不能原地改 fetch_snapshot()
    # 的共享 rows（缓存 25 秒内被所有板块复用），否则会污染下一个板块的评分口径。
    cand = [dict(q) for q in base[: int(p["universe"])]]
    _rs = rs_rank_map(rows, ind_gain)
    for _q in cand:
        _q["_rs_rank"] = _rs.get(str(_q["code"]))

    def work(q):
        try:
            bars = fetch_kline(q["code"], q["market"], 160, q.get("name", ""))
            if len(bars) < 120:
                return None
            # 每只票 swing_metrics 只算一次：评分复用其中的 ADX/MFI/筹码/布林，
            # 入选票的详情指标与交易计划也复用它，不再一只票算三遍全套
            sm = swing_metrics(bars)
            return ai_evaluate(q, bars, pe_sorted, pb_sorted, p, survey, pe_by_ind, env,
                               sm=sm, defer_metrics=True)
        except Exception:
            return None

    # 手机端分波深扫：凑够 top+6 只 B 档（≥66 分）且至少评过 top+18 只就提前收工，
    # 高分票（S/A）在预筛分前排优先评到；电脑端一次全量，行为不变。
    results, errors = _deep_scan(cand, work, _wn(12),
                                 int(p["top"]) + 6, int(p["top"]) + 18)

    results.sort(key=lambda x: (-x["score"], -x["ind_count"]))
    passed = [x for x in results if x["score"] >= p["min_score"] and x["ind_count"] >= p["min_ind"]]
    top = passed[: int(p["top"])]
    # 详情指标 / 交易计划只给入选票算（以前 200 只全算，落选的一百多只白算）。
    # sm/pre 全部复用评分时的成果，不需要 bars，也不必把落选票的 K 线留在内存里。
    for x in top:
        sm, pre = x.pop("_sm", None), x.pop("_pre", None)
        x["metrics"] = detail_metrics(None, sm=sm, pre=pre)
        x["trade"] = trade_plan("ai", None, x.get("price"), m=sm, pre=pre)

    return {
        "ok": True, "source": source, "env": env, **snap_meta(pack),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "total": len(rows), "base": len(base), "deep": len(cand),
        "hit": len(passed),
        "errors": errors, "elapsed": round(time.time() - t0, 1),
        "list": top,
        "params": p,
        "note": _cap_note + "消息面仅统计交易所披露的机构调研（近30日），不含公告与研报评级：免费接口没有可靠数据源，不编造指标。",
    }


# ---------------------------------------------------------------- 可多日持股（趋势波段）
SWING_DEFAULTS = {
    "universe": 180,        # 深度分析候选数
    "top": 36,              # 返回卡片数
    "min_score": 58,
    "min_ind": 2,           # 至少命中几个核心指标
    "min_amount": 0.8,      # 成交额下限（亿）
    "fcap_min": 30,         # 多日持股放宽市值，中盘更稳
    "fcap_max": 800,
    "pct_min": -4.0,        # 不要求当天大涨，回调中的趋势股同样可选
    "pct_max": 7.0,         # 当天已接近涨停的不要（次日接力风险）
}

# 多日持股的核心指标（计入「命中几项」，弱信号不算）
SWING_CORE_LABELS = {
    "趋势向上", "趋势偏多", "站上MA20", "位置合适", "均线多头",
    "MACD零上金叉", "MACD多头", "KDJ金叉", "KDJ低位金叉",
    # v1.6.5：原写「箱体突破」，但 detect_patterns/sig_tag 实际产出的是
    # 「平台突破」「回踩支撑」——旧名永不命中，导致形态票 ind_count 漏计被错杀。
    "平台突破", "回踩支撑", "W底", "老鸭头", "上升通道", "均线粘合",
    "缩量回踩", "量能温和", "主力流入",
}

# 多日持股的形态偏好：回踩 / 通道 / 底部结构加分，短线试盘类减分
SWING_PATTERN_ADJ = {
    "回踩支撑": 10, "上升通道": 8, "W 底突破": 6, "平台突破": 4,
    "老鸭头": 4, "均线粘合发散": 4, "逼近平台高点": 0, "长上影试盘": -16,
}


def index_gain20():
    """上证指数近 20 个交易日涨幅，作为多日持股「相对强度」的基准。
       拿不到就返回 None —— 该维度不加分也不扣分，不会因为接口抖动误杀。"""
    def _calc():
        try:
            bars = fetch_kline("000001", 1, 40)
            if len(bars) >= 21:
                return (bars[-1]["close"] / bars[-21]["close"] - 1) * 100
        except Exception:
            pass
        return None
    return cached("index_gain20", 300, _calc)


def swing_metrics(bars):
    """多日持股需要的趋势 / 波动 / 位置指标，全部由日K算出"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    n, c = len(bars), closes[-1]
    ma5, ma10 = _ma_at(closes, 5), _ma_at(closes, 10)
    ma20, ma60 = _ma_at(closes, 20), _ma_at(closes, 60)
    ma20_prev = _ma_at(closes, 20, n - 6) if n >= 26 else None
    slope = (ma20 / ma20_prev - 1) * 100 if ma20 and ma20_prev else 0.0
    ma120 = _ma_at(closes, 120)
    ma60_prev = _ma_at(closes, 60, n - 6) if n >= 66 else None
    slope60 = (ma60 / ma60_prev - 1) * 100 if ma60 and ma60_prev else 0.0

    trs = []
    for i in range(max(1, n - 14), n):
        pc = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    atr = statistics.fmean(trs) if trs else c * 0.02
    # 标准 Wilder ATR：首值取前 14 根 TR 的简单均值，之后 alpha=1/14 递推。
    # 旧写法以第一根 TR 作初值，40 根 K 线时偏差约 -2.5%，会直接带偏止损位。
    # 下面 ATR 分位本来就要全量序列，这里只递推一次、取尾部当当前值，
    # 省掉 atr_wilder_series 的重复递推（解释执行环境下这是纯 CPU 浪费）。
    _aseq = atr_wilder_series_all(bars)
    atr_w = (_aseq[-1] if _aseq and _aseq[-1] is not None else None) or atr
    # 真实最大回撤（原 dd60 只是"距 60 日最高"）
    peak, mdd60 = 0.0, 0.0          # 峰值必须在 60 日窗口内从 0 起算
    for v in closes[-60:]:
        peak = max(peak, v)
        if peak > 0:
            mdd60 = min(mdd60, (v / peak - 1) * 100)
    # 换手率历史分位 + 量价背离（价涨量缩 / 价跌量增）
    turns = [b.get("turnover") or 0 for b in bars[-60:]]
    # 备用源 K 线不带换手率，全 0 时算出的 100% 分位是假信号（会误报情绪过热）
    # 与 pct_rank / hist_pct_rank 同口径：自身只算半权，
    # 换手率全等值时不该把所有股票都判成 100% 高分位。
    turn_rank = ((sum(1 for t in turns if t < turns[-1]) +
                  0.5 * sum(1 for t in turns if t == turns[-1])) / len(turns) * 100
                 if turns and any(turns) else None)
    up5 = (c / closes[-6] - 1) * 100 if n >= 6 and closes[-6] else 0.0
    vol5 = statistics.fmean(vols[-5:]) if n >= 5 else vols[-1]
    vol20 = statistics.fmean(vols[-20:]) if n >= 20 else vol5 or 1
    divergence = (up5 > 3 and vol5 < vol20 * 0.8) or (up5 < -3 and vol5 > vol20 * 1.2)

    win = bars[-60:] if n >= 60 else bars
    hi60 = max(b["high"] for b in win)
    lo60 = min(b["low"] for b in win)

    # --- 新增技术面字段：评分、详情与图表共用，避免同一只票重复计算
    _bmid, _bup, _blow, bw = boll_series(closes)
    bw_rank = hist_pct_rank(bw[-60:], bw[-1]) if bw else None
    tr_pct = []
    for i in range(max(0, n - 60), n):
        if _aseq[i] and closes[i] and math.isfinite(_aseq[i]):
            tr_pct.append(_aseq[i] / closes[i] * 100)
    atr_rank = hist_pct_rank(tr_pct, atr_w / c * 100 if c else 0.0)
    adx, pdi, mdi = adx_series(bars)
    mfi = mfi_series(bars)
    chip_res, chip_sup = chip_levels(bars, c)
    sar_seq = sar_series(bars)

    return {
        "bw_rank": bw_rank, "atr_rank": atr_rank, "sar_seq": sar_seq,
        "adx": adx, "pdi": pdi, "mdi": mdi, "mfi": mfi,
        "chip_res": chip_res, "chip_sup": chip_sup,
        "boll": (_bmid, _bup, _blow, bw),
        "c": c, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
        "slope": slope, "slope60": slope60, "atr": atr, "atr_wilder": atr_w,
        "mdd60": round(mdd60, 2),
        "turn_rank60": (round(turn_rank, 1) if turn_rank is not None else None),
        "vol_price_divergence": divergence,
        "vol_today": (vols[-1] / vol5) if vol5 else 1.0,
        "atr_pct": (atr_w / c * 100) if c else 0.0,   # 与 swing_plan 止损/止盈口径一致，都用 Wilder ATR
        "vr_v": (vol5 / vol20) if vol20 else 1.0,
        "gain20": ((c / closes[-21] - 1) * 100) if n >= 21 and closes[-21] else 0.0,
        "hi60": hi60, "lo60": lo60,
        "pos60": ((c - lo60) / (hi60 - lo60) * 100) if hi60 > lo60 else 50.0,
        "dd60": ((c / hi60 - 1) * 100) if hi60 else 0.0,
        "dist20": ((c / ma20 - 1) * 100) if ma20 else 0.0,
    }


# 详情页技术指标的完整字段清单：scan / ai / swing 三个板块共用，前端 swingRiskCells
# 只按「字段是否存在」渲染格子，所以缺一个字段就少一个格，字段名必须严格对齐。
DETAIL_METRIC_KEYS = ("rsi14", "obv_slope", "mfi14", "adx", "bw_rank", "chip_res", "chip_sup",
                      "chip_overhead", "gap", "sar", "sar_bull", "atr_wilder", "atr_pct",
                      "mdd60", "turn_rank60", "vol_price_divergence")


def detail_metrics(bars, sm=None, pre=None):
    """详情页技术指标（打新 / AI / 多日持股 三个板块同一份口径）。

    以前只有多日持股板块透出这些指标，scan 与 ai 的详情页是空的——
    同一套 K 线、同一套算法，只是没往返回里放，属于遗漏而非设计。
    sm 传入已算好的 swing_metrics(bars) 可省一次重复计算（多日持股走这条）。
    pre 传入评分时已算好的 RSI/OBV/跳空/套牢盘，直接复用不再重算；
    sm+pre 齐全时 bars 可为 None（AI 板块只给最终入选票算这份指标）。
    样本不足（< 25 根）时返回空 dict，前端整块技术指标不渲染，不会显示一排「—」。
    """
    if sm is None:
        if not bars or len(bars) < 25:
            return {}
        sm = swing_metrics(bars)
    m = sm
    pre = pre or {}
    closes = [b["close"] for b in bars] if bars else []
    c = m.get("c") or (closes[-1] if closes else 0)
    rsi14 = pre["rsi14"] if "rsi14" in pre else (rsi_series(closes)[-1] if len(closes) >= 15 else None)
    obv_s = pre["obv_slope"] if "obv_slope" in pre else (obv_signal(bars)[1] if bars else None)
    _sar = m.get("sar_seq") or []
    return {
        "atr_wilder": round(m.get("atr_wilder") or 0, 2),
        "atr_pct": round(m.get("atr_pct") or 0, 2),
        "mdd60": round(m.get("mdd60") or 0, 2),
        "turn_rank60": m.get("turn_rank60"),
        "vol_price_divergence": bool(m.get("vol_price_divergence")),
        "rsi14": (round(rsi14, 1) if rsi14 is not None else None),
        "obv_slope": obv_s,
        "mfi14": (round(m["mfi"], 1) if m.get("mfi") is not None else None),
        "adx": m.get("adx"),
        "bw_rank": m.get("bw_rank"),
        "chip_res": (round(m["chip_res"], 2) if m.get("chip_res") is not None else None),
        "chip_sup": (round(m["chip_sup"], 2) if m.get("chip_sup") is not None else None),
        "gap": pre["gap"] if "gap" in pre else (gap_stats(bars) if bars else None),
        "chip_overhead": pre["chip_overhead"] if "chip_overhead" in pre else (chip_overhead(bars, c) if bars else None),
        "sar": (round(_sar[-1], 2) if (_sar and _sar[-1] is not None) else None),
        "sar_bull": (bool(_sar[-1] < c) if (_sar and _sar[-1] is not None) else None),
    }


def swing_plan(m, c, price=None):
    """持股计划：建议持有天数 / 止损位 / 目标位（ATR + 均线口径）。
       price 为实时价，优先作基准；日K给出的 ATR/MA20/60日高点按比例同步缩放，
       避免盘中或数据延迟时止损位停在昨天的价位上。"""
    c0 = c or m.get("c") or 0
    if price and c0 and 0.5 * c0 < price < 1.5 * c0:
        k = price / c0
        c = price
    else:
        k = 1.0
    # 统一用 Wilder ATR，避免「卡片显示 ATR-W、止损却用简单 ATR」两套口径
    atr = ((m.get("atr_wilder") or m["atr"]) or c0 * 0.02) * k
    _ar = m.get("atr_rank")
    _sl = 2.2 * (1.15 if (_ar is not None and _ar >= 80) else
                 0.9 if (_ar is not None and _ar <= 20) else 1.0)
    # MA20 只有落在现价下方才是支撑；下跌趋势里 MA20 在上方是压力，
    # 若仍拿它当支撑会把止损夹到贴脸 -0.5%、盈亏比虚高到 9.9（trade_plan 同款修正）。
    # MA20 与 ATR/60日高点一样按实时价比例缩放，否则盘中支撑位停在旧价位。
    _ma20 = (m.get("ma20") or 0) * k
    support = _ma20 * 0.985 if (0 < _ma20 < c) else 0
    stop = max(c - _sl * atr, support)                    # 二者取更紧的一个
    stop = max(stop, c * 0.92)                            # 单笔亏损上限约 8%
    stop = min(stop, c * 0.995)
    # SAR 动态跟踪止损：多头趋势里 SAR 在价格下方，随创新高持续上移。
    # 只在 SAR 高于现有止损(更紧)且仍低于现价时才采用，避免空头 SAR 在上方
    # 把止损抬到买入价之上；同时守住 8% 单笔亏损上限。
    sar_val, sar_bull, sar_used = None, None, False
    sar_seq = m.get("sar_seq") or []
    if sar_seq:
        sv = sar_seq[-1]
        if sv is not None:
            sar_val = round(sv, 2)
            sar_bull = bool(sv < c)
            if sar_bull and sv > stop and sv < c * 0.995:
                stop = max(sv, c * 0.92)
                sar_used = True
    tgt_far = min((m["hi60"] or 0) * k, c * 1.15) if m["hi60"] else c * 1.08
    target = max(c + 3.0 * atr, tgt_far, c * 1.05)
    # 止损被夹到现价下方 0.5% 时，分母极小会让盈亏比失真（出现几十倍）
    rr = min((target - c) / max(c - stop, c * 0.005), 9.9)
    atr_pct = (atr / c * 100) if c else 0.0
    strong = bool(m["ma20"] and m["ma60"] and m["ma20"] > m["ma60"]
                  and m["slope"] > 0 and atr_pct < 4.5 and m["gain20"] < 20)
    return {
        "px": round(c, 2), "price_basis": ("实时价" if k != 1.0 else "日K收盘"),
        "days": "5~10" if strong else "3~6",
        "stop": round(stop, 2), "target": round(target, 2),
        "stop_pct": round((stop / c - 1) * 100, 2) if c else None,
        "target_pct": round((target / c - 1) * 100, 2) if c else None,
        "rr": round(rr, 1), "atr_pct": round(atr_pct, 1),
        "atr_wilder": round(atr, 2),
        "basis": "真实日K · Wilder ATR",
        "atr_rank": m.get("atr_rank"), "sl_coef": round(_sl, 2),
        "sar": sar_val, "sar_bull": sar_bull,
        "stop_basis": ("SAR 跟踪" if sar_used else ("ATR/MA20" if support else "ATR")),
    }


# 各板块列表卡片统一的「止损 / 止盈 / 盈亏比」口径（v1.5.5）。
# 关键：五个板块在深度分析时**本来就已抓过日K**，所以这里直接复用 bars，
# 不额外发一次网络请求；实测单只计算约 0.08ms，全量 500 只约 40ms。
# 多日持股沿用上面的 swing_plan（3~10 天波段），其余板块按持仓周期收紧系数，
# 避免把波段口径硬套到隔夜 / 涨跌停博弈上。
TRADE_COEF = {
    "night": dict(sl=1.2, tp=1.5, maxloss=0.03, maxtp=0.05, days="1"),        # 隔夜 1 天
    "ai":    dict(sl=1.8, tp=2.5, maxloss=0.06, maxtp=0.10, days="1~3"),     # 短线 1~3 天
    "zt":    dict(sl=2.0, tp=2.8, maxloss=0.08, maxtp=0.12, days="1~2"),     # 涨停博弈
    "dt":    dict(sl=2.0, tp=2.8, maxloss=0.08, maxtp=0.12, days="1~2"),     # 跌停博弈
}


def trade_plan(mode, bars, price=None, m=None, pre=None):
    """用已抓到的真实日K算一份止盈止损：Wilder ATR + MA20 支撑 + 60 日高点压力。
    返回结构刻意与 swing_plan 对齐，前端所有板块卡片共用同一段渲染。
    m/pre 传入已算好的 swing_metrics / 跳空统计时直接复用（AI 深扫每只票
    已经算过一遍，这里再算就是第三遍）；齐全时 bars 可为 None。"""
    if m is None:
        try:
            m = swing_metrics(bars)
        except Exception:
            return None
    c0 = m.get("c") or 0
    if not c0 or c0 <= 0:
        return None
    # 实时价优先：日K的 ATR / MA20 / 60日高点是绝对价位，按同一比例缩放后再用，
    # 否则盘中或日K延迟时，止盈止损会整体停在旧价格上（旧实现 price 参数完全没生效）。
    c = price if (price and 0.5 * c0 < price < 1.5 * c0) else c0
    k = c / c0
    cfg = TRADE_COEF.get(mode, TRADE_COEF["ai"])
    atr = ((m.get("atr_wilder") or m.get("atr")) or c0 * 0.02) * k
    ma20 = (m.get("ma20") or 0) * k
    hi60 = (m.get("hi60") or c0 * 1.08) * k
    # 止损：ATR 下沿与 MA20 支撑取更近的一个，再用单笔最大亏损兜底。
    # MA20 在现价上方时它已经是压力不是支撑，别把止损夹到贴脸 -1%。
    _ar = m.get("atr_rank")
    _sl = cfg["sl"] * (1.15 if (_ar is not None and _ar >= 80) else
                       0.9 if (_ar is not None and _ar <= 20) else 1.0)
    support = (ma20 * 0.985) if (ma20 and ma20 < c) else 0
    atr_stop, floor_stop = c - _sl * atr, c * (1 - cfg["maxloss"])
    stop = max(atr_stop, support, floor_stop)
    stop_basis = ("ATR/MA20" if support > 0 and support >= max(atr_stop, floor_stop)
                  else "最大亏损兜底" if floor_stop >= atr_stop else "ATR")
    stop = min(stop, c * 0.99)
    # 止盈：ATR 上沿起步；上方 60 日高点只有留出 3% 以上空间时才算压力位，
    # 否则股票贴近新高时会被压到「止盈 +1%」这种没有意义的位置。
    target = max(c + cfg["tp"] * atr, c * 1.02)
    if hi60 > c * 1.03:
        target = min(target, hi60 * 1.01)
    target = max(min(target, c * (1 + cfg["maxtp"])), c * 1.01)
    rr = min((target - c) / max(c - stop, c * 0.005), 9.9)
    return {
        "px": round(c, 2), "price_basis": ("实时价" if k != 1.0 else "日K收盘"),
        "days": cfg["days"],
        "stop": round(stop, 2), "target": round(target, 2),
        "stop_pct": round((stop / c - 1) * 100, 2),
        "target_pct": round((target / c - 1) * 100, 2),
        "rr": round(rr, 1), "atr_pct": round(atr / c * 100, 1),
        "basis": "真实日K · Wilder ATR",
        "atr_rank": m.get("atr_rank"), "sl_coef": round(_sl, 2),
        "stop_basis": stop_basis,
        "gap": pre["gap"] if (pre is not None and "gap" in pre) else (gap_stats(bars) if bars else None),
    }


def night_reasons(r):
    """一夜持股卡片化用的三条理由：直接取 8 条规则的实测值，不编造指标。"""
    rules = r.get("rules") or {}
    order = [("gain", "涨幅"), ("vr", "量比"), ("turnover", "换手"),
             ("volume", "量能"), ("ma", "K线均线"), ("intraday", "分时")]
    out = []
    for k, lab in order:
        v = rules.get(k)
        if not v:
            continue
        txt = str(v.get("value") or "").strip()
        if txt and txt not in ("—", "-"):
            out.append(f"{lab} {txt}")
        if len(out) >= 3:
            break
    return out or list(r.get("tags") or [])[:3]


def _swing_detail(m, wk, pre):
    """多日持股详情指标：detail_metrics 统一口径 + 波段独有的仓位/周线字段。
       评分时算好的 m/pre/_wk 直接复用，入选后才调用，落选票不必算。"""
    return {**detail_metrics(None, sm=m, pre=pre),
            "slope": round(m["slope"], 2), "dist20": round(m["dist20"], 1),
            "vr_v": round(m["vr_v"], 2), "gain20": round(m["gain20"], 1),
            "pos60": round(m["pos60"]), "dd60": round(m["dd60"], 1),
            "weekly_resonance": ((wk or {}).get("sig") if wk else None),
            "weekly_detail": ((wk or {}).get("detail") if wk else None)}


def swing_evaluate(q, bars, pe_sorted, pb_sorted, survey=None, idx_gain=None,
                   pe_by_ind=None, env=None, defer_metrics=False):
    """多日持股（波段 5~10 天）打分，六维权重与 AI 荐股刻意不同：
       趋势 32 / 形态 12 / 资金 16 / 估值 16 / 财务 16 / 消息 8。
       口径来自 20/60/120 三线趋势跟随（60 日线定方向、20 日线找买点、回踩缩量低吸），
       偏「趋势是否成立 + 估值与盈利有没有安全垫」，不看当日爆发力，也不看打板情绪。
       defer_metrics=True 时不算详情指标与持股计划，把 _sm/_pre/_wk 留给入选后复用。"""
    # 日线指标只用尾部 160 根（与 AI 同窗口）：MA120/60 日窗口都够；递归类指标
    # (MACD/RSI/ATR/ADX) 的种子影响经 140+ 步衰减后 <0.001%，窗口类严格不变。
    # 周线共振仍用完整 bars（400 根≈86 周）聚合，EMA26 收敛口径不受影响。
    daily = bars[-160:] if len(bars) > 160 else bars
    m = swing_metrics(daily)
    closes = [b["close"] for b in daily]
    c = closes[-1]
    dif, dea, hist = macd_series(closes)
    K, D, J = kdj_series(daily)
    _rsi_seq = rsi_series(closes)
    ma5, ma10, ma20, ma60 = m["ma5"], m["ma10"], m["ma20"], m["ma60"]
    ma120 = m.get("ma120")
    t = q["pct"]

    sig = []          # (强度, 维度, 标签, 说明)

    # ---- 技术（权重 32）：趋势 26 + MA60 方向 10 + 均线 10 + MACD 14 + KDJ 8
    #      + 量能 8 + 缩量回踩 8 + 位置 4 + 相对强度 8 + 长期趋势 4 = 100
    tech = 0.0
    if ma20 and ma60:
        if ma20 > ma60 and m["slope"] > 0:
            tech += 26
            sig.append((95, "技术", "趋势向上",
                        f"MA20（{ma20:.2f}）站在 MA60（{ma60:.2f}）上方，且 5 日继续抬升 {m['slope']:+.2f}%"))
        elif ma20 > ma60:
            tech += 16
            sig.append((74, "技术", "趋势偏多",
                        f"MA20（{ma20:.2f}）位于 MA60（{ma60:.2f}）上方，趋势未破坏"))
        elif c > ma20:
            tech += 7
            sig.append((56, "技术", "站上MA20",
                        f"股价站上 MA20（{ma20:.2f}），但 MA20 仍在 MA60 下方，属反弹初期"))

    # 60 日线是中期生命线：必须走平或向上，否则最多只是反弹
    if ma60:
        if m["slope60"] > 0:
            tech += 10
            sig.append((88, "技术", "MA60向上", f"60 日均线 5 日抬升 {m['slope60']:+.2f}%，中期方向向上"))
        elif m["slope60"] > -0.8:
            tech += 6
            sig.append((62, "技术", "MA60走平", f"60 日均线走平（{m['slope60']:+.2f}%），属震荡格局"))
        else:
            tech -= 8
            sig.append((30, "技术", "MA60向下", f"60 日均线仍在下行（{m['slope60']:+.2f}%），反弹级别宜轻仓"))
    if ma120 and c > ma120:
        tech += 4
        sig.append((70, "技术", "站上半年线", f"股价站上 MA120（{ma120:.2f}），长期趋势转暖"))

    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        tech += 10
        sig.append((86, "技术", "均线多头", "MA5>MA10>MA20 多头排列，短中期成本一致向上"))
    elif ma5 and ma10 and ma20 and c > ma5 > ma10:
        tech += 6
        sig.append((70, "技术", "均线多头", "价格站上 MA5，短中期均线呈多头状态"))

    gold5 = any(dif[i - 1] <= dea[i - 1] and dif[i] > dea[i] for i in range(max(1, len(closes) - 5), len(closes)))
    if gold5 and dif[-1] > 0:
        tech += 14
        sig.append((90, "技术", "MACD零上金叉", f"DIF {dif[-1]:+.2f} 上穿 DEA {dea[-1]:+.2f}，且都在零轴上方，趋势二次启动"))
    elif dif[-1] > dea[-1] > 0:
        tech += 11
        sig.append((76, "技术", "MACD多头", f"DIF {dif[-1]:+.2f} / DEA {dea[-1]:+.2f} 零轴上方红柱延续"))
    elif dif[-1] > dea[-1]:
        tech += 6
        sig.append((58, "技术", "MACD金叉", "金叉发生在零轴下方，属反弹级别，需站上零轴确认"))

    kdj_gold = any(K[i - 1] <= D[i - 1] and K[i] > D[i] for i in range(max(1, len(closes) - 4), len(closes)))
    if kdj_gold and K[-1] < 50:
        tech += 8
        sig.append((82, "技术", "KDJ低位金叉", f"K={K[-1]:.1f}，D={D[-1]:.1f}，回调动能衰竭"))
    elif kdj_gold:
        tech += 5
        sig.append((64, "技术", "KDJ金叉", f"K={K[-1]:.1f}，短线转强"))
    if J[-1] > 110:
        tech -= 8
        sig.append((36, "技术", "KDJ超买", f"J 值 {J[-1]:.1f} 偏高，短线过热"))

    if 1.0 <= m["vr_v"] <= 2.2:
        tech += 8
        sig.append((80, "技术", "量能温和", f"5 日均量是 20 日均量的 {m['vr_v']:.2f} 倍，温和放大而非暴量"))
    elif 0.8 <= m["vr_v"] < 1.0:
        tech += 5
        sig.append((60, "技术", "量能温和", f"5 日均量 / 20 日均量 = {m['vr_v']:.2f}，缩量整理"))
    elif m["vr_v"] > 2.8:
        tech -= 6
        sig.append((38, "技术", "量能暴增", f"5 日均量已达 20 日均量的 {m['vr_v']:.2f} 倍，容易买在情绪高点"))

    # 缩量回踩 MA20：波段最舒服的低吸位置（通用「回踩不破 + 缩量」买点）
    if ma20 and -2.5 <= m["dist20"] <= 4 and m["vol_today"] <= 0.95 and c >= ma20 * 0.985:
        tech += 8
        sig.append((86, "技术", "缩量回踩", f"回踩 MA20（偏离 {m['dist20']:+.1f}%）且当日量仅为 5 日均量 {m['vol_today']:.2f} 倍，抛压有限"))

    if ma20:
        if -1.5 <= m["dist20"] <= 6:
            tech += 4
            sig.append((84, "技术", "位置合适", f"股价贴近 MA20（偏离 {m['dist20']:+.1f}%），回撤空间小、介入位置好"))
        elif m["dist20"] > 12:
            tech -= 10
            sig.append((34, "技术", "偏离过大", f"股价已高于 MA20 {m['dist20']:.1f}%，此时介入属于追高"))

    # 相对强度：20 日超额收益（个股 - 上证指数），波段只做跑赢大盘的
    if idx_gain is not None:
        excess = m["gain20"] - idx_gain
        if excess >= 8:
            tech += 8
            sig.append((88, "技术", "跑赢大盘", f"近 20 日涨 {m['gain20']:.1f}%，跑赢上证 {excess:+.1f} 个百分点"))
        elif excess >= 0:
            tech += 4
            sig.append((66, "技术", "略强于大盘", f"近 20 日跑赢上证 {excess:+.1f} 个百分点"))
        else:
            tech -= 5
            sig.append((40, "技术", "弱于大盘", f"近 20 日落后上证 {excess:.1f} 个百分点，资金关注度不足"))

    if m["gain20"] > 30:
        tech -= 10
        sig.append((32, "技术", "涨幅透支", f"近 20 日累计涨幅 {m['gain20']:.1f}%，多日持股的赔率已经变差"))
    # ---- RSI(14) / OBV：经典超买超卖与量能累积视角，补 KDJ / 单点量比之不足
    rsi14 = _rsi_seq[-1]
    _obv_last, obv_s = obv_signal(daily)
    if rsi14 is not None:
        if rsi14 >= 80:
            tech -= 8
            sig.append((28, "技术", "RSI超买", f"RSI(14) {rsi14:.0f}，短线追高赔率下降"))
        elif 45 <= rsi14 <= 68:
            tech += 8
            sig.append((72, "技术", "RSI健康", f"RSI(14) {rsi14:.0f}，强弱适中、未过热"))
        elif rsi14 < 25:
            tech += 6
            sig.append((66, "技术", "RSI超卖", f"RSI(14) {rsi14:.0f}，存在技术反抽机会"))
    if obv_s is not None:
        if obv_s > 0.35:
            tech += 8
            sig.append((78, "技术", "OBV净流入", f"近 10 日 OBV 净流入 {obv_s:+.2f} 倍均量，资金持续吸筹"))
        elif obv_s < -0.35:
            tech -= 8
            sig.append((32, "技术", "OBV净流出", f"近 10 日 OBV 净流出 {obv_s:+.2f} 倍均量，量能背离"))

    # ---- 新增技术分析维度（复用 swing_metrics 已算好的字段，不重复抓数）
    extra = 0.0
    adx, pdi, mdi = m.get("adx"), m.get("pdi"), m.get("mdi")
    if adx is not None:
        if adx >= 25 and pdi > mdi:
            extra += 8
            sig.append((80, "技术", "ADX强趋势", f"ADX {adx:.0f} 趋势强度足够，波段顺势更稳"))
        elif adx < 20:
            extra -= 6
            sig.append((40, "技术", "ADX震荡", f"ADX {adx:.0f} 震荡市，趋势跟随信号易失效"))
    mfi = m.get("mfi")
    if mfi is not None:
        if mfi >= 75:
            extra -= 5
            sig.append((36, "技术", "MFI过热", f"MFI(14) {mfi:.0f} 资金流过热，追高不宜"))
        elif 40 <= mfi <= 65:
            extra += 5
            sig.append((70, "技术", "MFI健康", f"MFI(14) {mfi:.0f}，资金流量价配合健康"))
        elif mfi < 25:
            extra += 4
            sig.append((64, "技术", "MFI超卖", f"MFI(14) {mfi:.0f} 超卖，低吸窗口"))
    if m.get("bw_rank") is not None:
        if m["bw_rank"] <= 20:
            extra += 6
            sig.append((74, "技术", "布林收口", f"布林带宽处近 60 日 {m['bw_rank']:.0f}% 分位，波动收缩待变盘"))
        elif m["bw_rank"] >= 90:
            extra -= 6
            sig.append((38, "技术", "布林过热", f"布林带宽处近 60 日 {m['bw_rank']:.0f}% 分位，波动过热"))
    if m.get("atr_rank") is not None and m["atr_rank"] >= 90:
        extra -= 4
        sig.append((40, "技术", "ATR分位高", f"ATR% 处近 60 日 {m['atr_rank']:.0f}% 分位，止损需放宽"))
    _dv = macd_divergence(closes, hist)
    if _dv == "top":
        extra -= 8
        sig.append((30, "技术", "MACD顶背离", "MACD 柱顶背离，波段上沿别追"))
    elif _dv == "bottom":
        extra += 7
        sig.append((76, "技术", "MACD底背离", "MACD 柱底背离，底部动能积累"))
    _rdv = rsi_divergence(closes, _rsi_seq)
    if _rdv == "top":
        extra -= 6
        sig.append((34, "技术", "RSI顶背离", "RSI 顶背离，注意回撤"))
    elif _rdv == "bottom":
        extra += 5
        sig.append((70, "技术", "RSI底背离", "RSI 底背离，反弹动能积累"))
    for _nm, _sc, _desc in candle_patterns(daily):
        if _sc >= 62:
            extra += 4
            sig.append((_sc, "形态", _nm, _desc))
    if m.get("chip_res") and (m["chip_res"] / c - 1) < 0.03:
        extra -= 5
        sig.append((40, "技术", "筹码峰压制", f"上方 {m['chip_res']:.2f} 筹码峰压制，波段空间有限"))
    elif m.get("chip_sup") and (1 - m["chip_sup"] / c) < 0.03:
        extra += 4
        sig.append((70, "技术", "筹码峰支撑", f"下方 {m['chip_sup']:.2f} 筹码峰支撑，回撤有底"))
    # ---- v1.5.13 新增技术面：只作用于 extra，不动原有评分基线
    _g = gap_stats(daily)
    if _g and _g["down_prob"] >= 60:
        extra -= 3
        sig.append((42, "技术", "跳空风险", f"隔夜低开概率 {_g['down_prob']:.0f}%，跳空风险偏高"))
    elif _g and _g["down_prob"] <= 30:
        extra += 2
        sig.append((64, "技术", "跳空风险低", f"隔夜低开概率仅 {_g['down_prob']:.0f}%，开盘风险较小"))
    _ov = chip_overhead(daily, c)
    if _ov is not None and _ov >= 65:
        extra -= 3
        sig.append((40, "技术", "套牢盘重", f"现价上方套牢盘约 {_ov:.0f}%，冲高抛压重"))
    elif _ov is not None and _ov <= 30:
        extra += 2
        sig.append((66, "技术", "套牢盘轻", f"现价上方套牢盘仅 {_ov:.0f}%，抛压较轻"))
    # 周线聚合只做一次，weekly_trend 与 weekly_macd_kdj 共享；ADX 复用 swing_metrics 的成果
    _wb = _weekly_ohlc(bars)
    _wt = weekly_trend(bars, wb=_wb, adx=(m["adx"], m["pdi"], m["mdi"]))
    if _wt:
        extra += _wt["score"]
        sig.append((60 if _wt["score"] > 0 else 42, "技术", _wt["state"], _wt.get("detail") or _wt["state"]))
    # v1.5.16 SAR 抛物线转向：多头(价格站上 SAR)=趋势跟踪有效，空头=下降趋势未扭转
    _sar = m.get("sar_seq")
    if _sar and _sar[-1] is not None:
        _bull = _sar[-1] < c
        if _bull:
            extra += 3
            sig.append((68, "技术", "SAR多头", "SAR 处于价格下方，趋势跟踪止损有效"))
        else:
            extra -= 3
            sig.append((40, "技术", "SAR空头", "SAR 仍在价格上方，下降趋势尚未扭转"))
    # v1.5.16 真·周线 MACD/KDJ 共振（自然周聚合，非伪周线）
    _wk = weekly_macd_kdj(bars, wb=_wb)
    if _wk:
        if _wk["sig"] == "bull":
            extra += 4
            sig.append((80, "技术", "周线共振向上", _wk["detail"]))
        elif _wk["sig"] == "bear":
            extra -= 4
            sig.append((38, "技术", "周线共振向下", _wk["detail"]))
    _rr = q.get("_rs_rank")
    if _rr is not None:
        if _rr >= 80:
            extra += 3
            sig.append((70, "技术", "RS强度高", f"相对行业强度处当日 {_rr:.0f}% 分位"))
        elif _rr <= 20:
            extra -= 2
            sig.append((44, "技术", "RS强度低", f"相对行业强度仅 {_rr:.0f}% 分位"))
    # v1.5.17：硬利空公告统一由 message_score() 计入「消息面」维度（见 evaluate 同处说明），
    # 基础分不再重复扣，也不能绕过消息面开关。
    # 大盘环境与 AI 同口径：只调节新增信号强度，弱市削弱正向、放大风险
    _ef = 1.0
    if env and env.get("score") is not None:
        _ef = 0.7 if env["score"] <= 38 else 1.1 if env["score"] >= 62 else 1.0
    extra = extra * _ef if extra > 0 else extra / _ef
    tech = clamp(tech * 0.86 + extra)
    if tech <= 0 and extra < 0:
        tech = 20.0

    # ---- 形态（12）：按多日偏好调整，回踩/通道/底部结构优先
    pats = detect_patterns(daily)
    scored = [(clamp(30 + (sc - 50) * 1.1 + SWING_PATTERN_ADJ.get(name, 0)), name, desc)
              for sc, name, desc in pats]
    scored.sort(key=lambda x: -x[0])
    pattern = scored[0][0] if scored else 22
    for sc, name, desc in scored[:2]:
        if sc >= 52:
            sig.append((sc, "形态", sig_tag(name) or name.replace(" ", ""), desc))

    # ---- 资金（16）：多日持股看「温和持续」，不看单日爆量
    fcap = q["fcap"] or 1
    net_ratio = (q.get("main_net") or 0) / fcap * 100
    has_net = "main_net" not in (q.get("_miss") or [])
    fund = ((40 if has_net else 16) + min(q["volratio"], 2.5) * (7 if has_net else 5)
            + min(q["turnover"], 10) * (1.6 if has_net else 1.0)
            + (net_ratio * 10 if has_net else 0))
    if not has_net:
        sig.append((30, "资金", "主力净额缺失", "主力净额未采集，资金维度已降权"))
    if 1.0 <= m["vr_v"] <= 2.2 and has_net:
        fund += 8
    if has_net and (q.get("main_net") or 0) > 0:
        sig.append((min(92, 62 + net_ratio * 8), "资金", "主力流入",
                    f"当日主力净流入 {q['main_net']/1e8:.2f} 亿（占流通市值 {net_ratio:.2f}%），资金面配合"))
    if q["volratio"] >= 1.2:
        sig.append((70, "资金", "量能放大", f"当日量比 {q['volratio']:.2f}，换手 {q['turnover']:.1f}%，交投温和活跃"))
    fund = clamp(fund)

    # ---- 估值（16）
    pe, pb = q.get("pe_ttm") or 0, q.get("pb") or 0
    val = 50.0
    if 0 < pe < 200:
        pool = (pe_by_ind or {}).get(q.get("industry") or "") or pe_sorted
        pr = pct_rank(pool, pe)
        val = clamp(100 - pr)
        lvl = "偏低" if pr < 35 else ("居中" if pr < 70 else "偏高")
        scope = "同行业" if len(pool) < len(pe_sorted) else "主板"
        sig.append((72 if pr < 35 else 50, "估值", "低估值",
                    f"PE(TTM) {pe:.1f}，处{scope} {pr:.0f}% 分位，估值{lvl}"))
    elif pe <= 0:
        val = 28.0
        sig.append((35, "估值", "亏损排雷", "PE(TTM) 为负或缺失，亏损股估值项扣分"))
    elif pe >= 200:
        val = 26.0
        sig.append((28, "估值", "高估值排雷", f"PE(TTM) {pe:.0f} 倍，估值极高，盈利安全垫不足"))
    if 0 < pb < 15:
        pbr = pct_rank(pb_sorted, pb)
        val = clamp(val * 0.7 + (100 - pbr) * 0.3)

    # ---- 财务（16）
    fin = 45.0
    if pe <= 0:
        fin = 24.0                       # 亏损股排雷，不再拿中性 45 分
    elif 0 < pe < 300 and 0 < pb < 30:
        roe = pb / pe * 100
        fin = clamp(30 + roe * 1.6)     # 原来 ROE≈19% 就打满，改为更平缓
        if roe >= 10:
            sig.append((66, "财务", "ROE良好", f"ROE 约 {roe:.1f}%（PB/PE 估算），盈利质量较好"))
        elif roe < 3:
            sig.append((40, "财务", "ROE偏弱", f"ROE 约 {roe:.1f}%，盈利能力偏弱"))

    # 风险指标：量价背离 / 最大回撤 / 换手分位（用日K算出，写进卡片理由）
    if m.get("vol_price_divergence"):
        sig.append((38, "风险", "量价背离", "近 5 日价量方向背离，追高需谨慎"))
    if (m.get("mdd60") or 0) <= -25:
        sig.append((40, "风险", "回撤偏大", f"近 60 日最大回撤 {m['mdd60']:.1f}%，仓位要压低"))
    if (m.get("turn_rank60") or 0) >= 90:
        sig.append((52, "资金", "换手高分位", f"换手率处近 60 日 {m['turn_rank60']:.0f}% 分位，情绪偏热"))

    # ---- 消息（8）：机构调研
    news = 50.0
    sv = (survey or {}).get(q["code"])
    if sv:
        news = clamp(58 + min(sv["times"], 4) * 8 + min(sv["orgs"], 60) * 0.45)
        sig.append((min(90, 60 + sv["times"] * 8), "消息", "机构调研",
                    f"近 30 日 {sv['times']} 次机构调研（约 {int(sv['orgs'])} 家机构），最近 {sv['date']}"))

    # 趋势与基本面并重，消息面只作辅助（AI 荐股则相反：资金与消息更重）
    total = (tech * 0.32 + pattern * 0.12 + fund * 0.16
             + val * 0.16 + fin * 0.16 + news * 0.08)

    # 卡片三条理由：技术最多 2 条，形态 / 资金各 1 条，保证既有趋势也有触发点
    sig.sort(key=lambda x: -x[0])
    ind = [s for s in sig if s[1] in ("技术", "形态", "资金")]
    picked, used = [], {}
    for s in ind:
        cap = 2 if s[1] == "技术" else 1
        if used.get(s[1], 0) >= cap:
            continue
        picked.append(s)
        used[s[1]] = used.get(s[1], 0) + 1
        if len(picked) >= 3:
            break
    if len(picked) < 3:
        for s in ind:
            if s not in picked:
                picked.append(s)
            if len(picked) >= 3:
                break
    if len(picked) < 3:
        for s in sig:
            if s not in picked:
                picked.append(s)
            if len(picked) >= 3:
                break

    labels = []
    for s in sig:
        if s[2] and s[2] not in labels:
            labels.append(s[2])

    rules = {
        "tech": {"name": "① 趋势（MA20/MA60/MA120 · 斜率 · 位置）", "passed": round(tech) >= 60,
                 "value": f"{tech:.0f} 分", "score": round(tech, 1),
                 "detail": (f"MA20 {'>' if (ma20 and ma60 and ma20 > ma60) else '<'} MA60，"
                            f"5 日斜率 {m['slope']:+.2f}%，距 MA20 {m['dist20']:+.1f}%，"
                            f"5/20 日量能比 {m['vr_v']:.2f}"), "weight": 32},
        "pattern": {"name": "② 形态（回踩 / 通道 / 突破 / 底部）", "passed": round(pattern) >= 60,
                    "value": f"{pattern:.0f} 分", "score": round(pattern, 1),
                    "detail": (scored[0][1] + "：" + scored[0][2]) if scored else "未识别到经典形态", "weight": 12},
        "fund": {"name": "③ 资金（主力净额 / 量能温和）", "passed": round(fund) >= 60,
                 "value": f"{fund:.0f} 分", "score": round(fund, 1),
                 "detail": ((f"主力净额 {q['main_net']/1e8:+.2f} 亿，量比 {q['volratio']:.2f}，换手 {q['turnover']:.1f}%")
                            if has_net else
                            f"主力净额缺失（备用源），按量比 {q['volratio']:.2f} / 换手 {q['turnover']:.1f}% 计"),
                 "weight": 16},
        "news": {"name": "④ 消息（机构调研，仅辅助）", "passed": round(news) >= 58,
                 "value": f"{news:.0f} 分", "score": round(news, 1),
                 "detail": (f"近 30 日 {sv['times']} 次调研" if sv else "近 30 日无公开调研记录，按中性计"), "weight": 8},
        "val": {"name": "⑤ 估值（PE / PB 分位）", "passed": round(val) >= 55,
                "value": f"{val:.0f} 分", "score": round(val, 1),
                "detail": (f"PE {pe:.1f} / PB {pb:.2f}" if pe > 0 else "PE 为负或缺失"), "weight": 16},
        "fin": {"name": "⑥ 财务（ROE 估算）", "passed": round(fin) >= 55,
                "value": f"{fin:.0f} 分", "score": round(fin, 1),
                "detail": "ROE ≈ PB/PE，仅作粗略衡量", "weight": 16},
    }

    grade = _grade_of("swing", total)
    res = {
        "code": q["code"], "name": q["name"], "market": q["market"], "mode": "swing",
        "price": q["price"], "prev_close": q["prev_close"], "pct": round(t, 2),
        "volratio": round(q["volratio"], 2), "turnover": round(q["turnover"], 2),
        "fcap": round(fcap / 1e8, 1), "amount": round(q["amount"] / 1e8, 2),
        "main_net": (None if "main_net" in (q.get("_miss") or []) else round((q.get("main_net") or 0) / 1e8, 2)),
        "industry": q["industry"],
        "pe": round(pe, 1), "pb": round(pb, 2),
        "high": q["high"], "open": q["open"],
        "score": round(total, 1), "grade": grade,
        "reasons": [f"{s[2]}：{s[3]}" for s in picked[:3]],
        "labels": labels[:8],
        "ind_count": sum(1 for l in labels if l in SWING_CORE_LABELS),
        "dims": {"tech": round(tech), "pattern": round(pattern), "fund": round(fund),
                 "news": round(news), "val": round(val), "fin": round(fin)},
        "rules": rules, "tags": labels[:8],
    }
    # 详情指标 / 持股计划只给入选票算（以前 200 只全算，落选的一百多只白算）。
    # m/pre/_wk 全部复用评分时的成果，_wk 来自完整 K 线的周线聚合，与日线窗口无关。
    pre = {"rsi14": rsi14, "obv_slope": obv_s, "gap": _g, "chip_overhead": _ov}
    if defer_metrics:
        res["_sm"], res["_pre"], res["_wk"] = m, pre, _wk
    else:
        res["plan"] = swing_plan(m, c, q.get("price"))
        res["metrics"] = _swing_detail(m, _wk, pre)
    return res


def _swing_base(rows, p):
    """多日持股的初筛：只做内存过滤，不产生任何请求。
       swing_pick（完整重扫）与 swing_pick_fast（缓存换算）共用同一份口径，
       避免两处过滤条件漂移、导致快速版与完整版榜单口径不一致。"""
    base = []
    for q in rows:
        if q["volume"] <= 0 or is_bad_name(q["name"], True):
            continue
        if bad_symbol(q["name"]) or not list_days_ok(q.get("list_date")):
            continue
        if q["amount"] / 1e8 < p["min_amount"]:
            continue
        if not (p["fcap_min"] <= q["fcap"] / 1e8 <= p["fcap_max"]):
            continue
        if not (p["pct_min"] <= q["pct"] <= p["pct_max"]):
            continue
        if q["turnover"] < 0.8:      # 太冷清的票多日也拉不动
            continue
        base.append(q)
    return base


def _swing_prescore(q, fund_degraded):
    """多日持股的预排序：偏好温和放量 + 主力净流入，不奖励当天大涨。"""
    nr = q["main_net"] / q["fcap"] * 100 if q["fcap"] else 0
    heat = 1.6 if 0 <= q["pct"] <= 4 else 0.6
    fund = (min(q["turnover"], 12) * 1.0 + min(q["volratio"], 3) * 2) if fund_degraded else (nr * 5)
    return fund + min(q["volratio"], 3) * 4 + q["pct"] * heat + min(q["turnover"], 12) * 0.8


def swing_pick(params):
    t0 = time.time()
    p = dict(SWING_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v not in (None, ""):
            p[k] = coerce_param(SWING_DEFAULTS[k], v)

    # 手机/模拟器候选池封顶：解释执行 + GIL 串行 CPU，评满 200 只要一分多钟；
    # 预筛排序已把资金/量能活跃的票排前，截尾对榜单影响有限（桌面端不受影响）。
    _cap_note = ""
    if LIGHT and int(p["universe"]) > 100:
        p["universe"] = 100
        _cap_note = "手机轻量模式：候选池封顶 100 只。"

    # 快照 / 调研 / 大盘指数互不依赖：冷扫时串行拉取白白多等网络往返，并行取
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_pack = ex.submit(fetch_snapshot)
        f_survey = ex.submit(fetch_survey, 30)
        f_idx = ex.submit(index_gain20)      # 相对强度基准（上证 20 日涨幅）
        pack = f_pack.result()
        survey = f_survey.result()
        idx_gain = f_idx.result()
    rows, source = pack["rows"], pack["source"]
    ind_gain = industry_gain(rows)
    env = market_env(rows)

    pe_sorted = sorted([q["pe_ttm"] for q in rows if 1 < q.get("pe_ttm", 0) < 200])
    pb_sorted = sorted([q["pb"] for q in rows if 0.3 < q.get("pb", 0) < 20])
    pe_by_ind = _industry_pe(rows)

    base = _swing_base(rows, p)
    fund_degraded = bool(base) and all(not q.get("main_net") for q in base)

    if any(not q["volratio"] for q in base):
        base, filled = enrich_volratio(base)
        if filled:
            source += "+腾讯"

    base.sort(key=lambda q: -_swing_prescore(q, fund_degraded))
    # 用副本构造候选：下面会给每只写 _rs_rank 派生字段，绝不能原地改 fetch_snapshot()
    # 的共享 rows（缓存 25 秒内被所有板块复用），否则会污染下一个板块的评分口径。
    cand = [dict(q) for q in base[: int(p["universe"])]]
    _rs = rs_rank_map(rows, ind_gain)
    for _q in cand:
        _q["_rs_rank"] = _rs.get(str(_q["code"]))

    def work(q):
        try:
            # 多日持股要算周线 MACD/KDJ：160 根日K 只有约 35 周，EMA26 远未收敛，
            # 实测 DIF 相对偏差 20~30%、个别票 DEA 连符号都反。拉 400 根（约 86 周）
            # 让周线指标具备参考价值；日K 指标仍取尾部窗口，不受影响。
            bars = fetch_kline(q["code"], q["market"], 400, q.get("name", ""))
            if len(bars) < 130:      # MA120 + EMA26 收敛，至少 130 根
                return None
            # 详情指标/持股计划延迟到入选后算（defer_metrics），落选票省掉这部分 CPU
            return swing_evaluate(q, bars, pe_sorted, pb_sorted, survey, idx_gain,
                                  pe_by_ind, env, defer_metrics=True)
        except Exception:
            return None

    # 手机端分波深扫（同 AI 荐股口径，按多日持股的 top 换算）；电脑端全量不变。
    results, errors = _deep_scan(cand, work, _wn(12),
                                 int(p["top"]) + 6, int(p["top"]) + 18)

    results.sort(key=lambda x: (-x["score"], -x["ind_count"]))
    passed = [x for x in results if x["score"] >= p["min_score"] and x["ind_count"] >= p["min_ind"]]

    # 同行业最多 3 只：多日持股最怕一篮子全是同一板块（比如清一色银行），分散一下。
    # 备用源（新浪/腾讯）不带行业字段时不启用这条，避免把结果误砍成 3 只。
    top, seen = [], {}
    for x in passed:
        ind = (x.get("industry") or "").strip()
        if ind:
            if seen.get(ind, 0) >= 3:
                continue
            seen[ind] = seen.get(ind, 0) + 1
        top.append(x)
        if len(top) >= int(p["top"]):
            break

    # 详情指标 / 持股计划只给入选票算，复用评分时的 m/pre/周线成果，不必再碰 K 线。
    for x in top:
        sm, pre, wk = x.pop("_sm", None), x.pop("_pre", None), x.pop("_wk", None)
        x["plan"] = swing_plan(sm, sm["c"], x.get("price"))
        x["trade"] = x["plan"]
        x["metrics"] = _swing_detail(sm, wk, pre)

    return {
        "ok": True, "mode": "swing", "source": source, "env": env, **snap_meta(pack),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "total": len(rows), "base": len(base), "deep": len(cand),
        "hit": len(passed),
        "errors": errors, "elapsed": round(time.time() - t0, 1),
        "list": top,
        "params": p,
        "note": (_cap_note + "多日持股口径：不看当天涨了多少，看中期趋势（MA20/MA60）、均线结构、量能是否温和、"
                 "位置是否贴近 MA20。持仓天数、止损位、目标位由 ATR 与均线算出。"),
    }


# 快速换算只认 30 分钟内的日K缓存：日线级别指标对几分钟的延迟不敏感，
# 但也不能无限期用旧数据，否则停牌/除权的票会拿错价格。
FAST_KLINE_MAX_AGE = 1800


def swing_pick_fast(params):
    """「用已拉取的数据」按多日持股口径快速换算一版，全程不拉日K、不联网。

    场景：AI 荐股面板打开「多日持股算法」开关的那一刻。上次 AI 扫描已经把
    候选股的 400 根日K 存进缓存，这里直接拿缓存跑同一个 swing_evaluate，
    1~3 秒就能出榜单，不必等完整重扫的 30~60 秒。

    与 swing_pick 的差别只有一个：候选集是「本次快照初筛 ∩ 已有缓存」，
    所以结果是完整版的子集（可能少几只）。前端会标注「缓存换算」，
    并紧接着自动跑一次完整口径复核来替换，不会让子集冒充最终榜单。

    命中缓存太少（刚启动 / 缓存过期）时返回 insufficient=True，
    由前端直接降级为完整扫描，不返回半截榜单误导用户。"""
    t0 = time.time()
    p = dict(SWING_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v not in (None, ""):
            p[k] = coerce_param(SWING_DEFAULTS[k], v)

    _cap_note = ""
    if LIGHT and int(p["universe"]) > 100:
        p["universe"] = 100
        _cap_note = "手机轻量模式：候选池封顶 100 只。"

    # 快照本身有 25 秒缓存，刚扫完 AI 必命中；万一没命中也只是多一次快照请求，
    # 比完整重扫（几十只日K全线拉取）便宜得多。
    pack = fetch_snapshot()
    rows, source = pack["rows"], pack["source"]
    pe_sorted = sorted([q["pe_ttm"] for q in rows if 1 < q.get("pe_ttm", 0) < 200])
    pb_sorted = sorted([q["pb"] for q in rows if 0.3 < q.get("pb", 0) < 20])
    pe_by_ind = _industry_pe(rows)
    ind_gain = industry_gain(rows)
    env = market_env(rows)

    base = _swing_base(rows, p)
    fund_degraded = bool(base) and all(not q.get("main_net") for q in base)
    base.sort(key=lambda q: -_swing_prescore(q, fund_degraded))

    pool = base[: int(p["universe"])]
    cand = []
    for q in pool:
        bars = cache_peek(f"k:{q['market']}:{q['code']}", FAST_KLINE_MAX_AGE)
        if bars and len(bars) >= 130:      # 与 swing_pick 的 K 线长度门槛一致
            cand.append((q, bars))

    if len(cand) < 6:
        return {
            "ok": True, "mode": "swing", "algo": "swing", "fast": True,
            "insufficient": True, "cache_hit": len(cand), "cache_pool": len(pool),
            "source": source, "total": len(rows), "base": len(base), "deep": 0,
            "hit": 0, "list": [], "params": p,
            "elapsed": round(time.time() - t0, 2),
            "note": "缓存不足，直接按完整口径扫描。",
        }

    # 上证 20 日涨幅也只用缓存：拿不到就传 None（该维度不加分不扣分），
    # 这样快速换算真的做到零网络请求。
    idx_gain = cache_peek("index_gain20", 3600)
    try:
        survey = fetch_survey(30)          # 与 AI 扫描共用缓存，正常不会产生请求
    except Exception:
        survey = {}

    def work(pair):
        q, bars = pair
        try:
            return swing_evaluate(q, bars, pe_sorted, pb_sorted, survey, idx_gain,
                                  pe_by_ind, env, defer_metrics=True)
        except Exception:
            return None

    results, errors = [], 0
    with ThreadPoolExecutor(max_workers=_wn(8)) as ex:
        for r in ex.map(work, cand):
            if r is None:
                errors += 1
            else:
                results.append(r)

    results.sort(key=lambda x: (-x["score"], -x["ind_count"]))
    passed = [x for x in results if x["score"] >= p["min_score"] and x["ind_count"] >= p["min_ind"]]

    top, seen = [], {}
    for x in passed:
        ind = (x.get("industry") or "").strip()
        if ind:
            if seen.get(ind, 0) >= 3:
                continue
            seen[ind] = seen.get(ind, 0) + 1
        top.append(x)
        if len(top) >= int(p["top"]):
            break

    for x in top:
        sm, pre, wk = x.pop("_sm", None), x.pop("_pre", None), x.pop("_wk", None)
        x["plan"] = swing_plan(sm, sm["c"], x.get("price"))
        x["trade"] = x["plan"]
        x["metrics"] = _swing_detail(sm, wk, pre)

    return {
        "ok": True, "mode": "swing", "algo": "swing", "fast": True, "partial": True,
        "cache_hit": len(cand), "cache_pool": len(pool),
        "source": source, "env": env, **snap_meta(pack),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "total": len(rows), "base": len(base), "deep": len(cand),
        "hit": len(passed), "errors": errors, "elapsed": round(time.time() - t0, 1),
        "list": top, "params": p,
        "note": (_cap_note + "快速换算：用上次扫描已缓存的日K（命中 %d/%d 只）按多日持股口径重算，"
                 "未额外联网，完整口径复核随后自动进行。" % (len(cand), len(pool))),
    }


# ---------------------------------------------------------------- 股票搜索
def stock_search(params):
    """按代码（精确/前缀）或名称（模糊）在主板快照里找股票，对命中的每只
       用 AI 多因子口径实时打分，返回与其他板块完全同构的卡片，供「股票搜索」板块渲染。
       只查主板快照里的票，不做全市场扫描；单次最多 12 只，避免极端关键词拖慢接口。"""
    t0 = time.time()
    kw = str((params or {}).get("q", "") or "").strip()
    # 关键词长度上限：防止超长串打接口 / 畸形输入；只保留代码数字与常见名称字符
    kw = kw[:32]
    if not kw:
        return {"ok": True, "list": [], "total": 0, "matched": 0, "q": kw,
                "elapsed": round(time.time() - t0, 2), "note": "请输入股票代码或名称"}

    pack = fetch_snapshot()
    rows = pack["rows"]
    # 只认主板（快照本身已是主板，这里再兜一层）；代码精确/前缀，名称子串
    low = kw.lower()
    hits = []
    for q in rows:
        code = str(q.get("code") or "")
        name = str(q.get("name") or "")
        if not is_main_board(code):
            continue
        if code == kw or code.startswith(kw) or low in name.lower():
            hits.append(q)
    # 代码精确匹配的排前面，其次代码前缀，再名称匹配；最多 12 只
    def _rank(q):
        code = str(q.get("code") or "")
        if code == kw:
            return (0, code)
        if code.startswith(kw):
            return (1, code)
        return (2, code)
    hits.sort(key=_rank)
    hits = hits[:12]

    if not hits:
        return {"ok": True, "list": [], "total": len(rows), "matched": 0, "q": kw,
                "source": pack.get("source"), **snap_meta(pack),
                "elapsed": round(time.time() - t0, 2),
                "note": "未找到匹配的主板股票，试试完整代码或名称关键字"}

    p = dict(AI_DEFAULTS)
    survey = fetch_survey(30)
    pe_sorted = sorted([q["pe_ttm"] for q in rows if 1 < q.get("pe_ttm", 0) < 200])
    pb_sorted = sorted([q["pb"] for q in rows if 0.3 < q.get("pb", 0) < 20])
    pe_by_ind = _industry_pe(rows)
    ind_gain = industry_gain(rows)
    env = market_env(rows)

    def work(q):
        try:
            bars = fetch_kline(q["code"], q["market"], 160, q.get("name", ""))
            if len(bars) < 60:
                return None
            q2 = dict(q)
            r = ai_evaluate(q2, bars, pe_sorted, pb_sorted, p, survey, pe_by_ind, env)
            r["trade"] = trade_plan("ai", bars, q.get("price"))
            return r
        except Exception:
            return None

    results = []
    with ThreadPoolExecutor(max_workers=_w(min(12, len(hits)))) as ex:
        for r in ex.map(work, hits):
            if r is not None:
                results.append(r)
    results.sort(key=lambda x: (-x["score"], -x["ind_count"]))

    return {
        "ok": True, "source": pack.get("source"), **snap_meta(pack),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "total": len(rows), "matched": len(hits),
        "q": kw, "elapsed": round(time.time() - t0, 1),
        "list": results,
        "note": "股票搜索用 AI 多因子口径实时打分，只查主板；评分仅用于排序，不代表上涨概率。",
    }


# ---------------------------------------------------------------- 涨停分析
LIMIT_DEFAULTS = {
    "top": 40,
    "min_score": 0,      # 涨停股本身不多，默认全展示，按分数排序
}

# 同花顺涨停池字段：涨停原因 / 封板类型 / 首末封时间 / 开板次数 / 封单额 / 连板
_THS_ZT_FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,"
                  "133971,133970,1968584,3475914,9003,9004")


def parse_boards(tag):
    """同花顺「3天2板」= 2 连板；东财「3连板」= 3；首板 = 1。"""
    t = str(tag or "")
    m = re.search(r"(\d+)\s*连板", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*天\s*(\d+)\s*板", t)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+)\s*板", t)
    if m:
        return int(m.group(1))
    return 1 if "首板" in t else 0


def _norm_pool_row(x):
    """两个池子的封单额/市值单位不一致，先做量级校验再统一到「元 / 亿」；
    顺带算出封成比（封单额 ÷ 当日成交额），判断封板结实程度的关键指标。
    外部池字段可能整列缺失，这里统一补齐评分函数硬取的字段，避免单只票 KeyError 拖垮整板块。"""
    defaults = {
        "code": "", "name": "", "price": 0.0, "pct": 0.0, "turnover": 0.0,
        "fcap": 0.0, "amount": 0.0, "seal_amount": 0.0, "open_num": 0,
        "last": "", "first": "", "days": 1, "industry": "", "ztype": "",
        "reason": "", "high_days": "", "src": "",
    }
    for k, v in defaults.items():
        if x.get(k) is None:
            x[k] = v
    fcap_yuan = (x.get("fcap") or 0) * 1e8
    amt_yuan = (x.get("amount") or 0) * 1e8
    seal = x.get("seal_amount") or 0
    # 只用流通市值做参照：一字板封单可达成交额百倍，
    # 拿成交额倍数判单位会把真元封单误折万倍
    if seal > 0 and fcap_yuan and seal > fcap_yuan * 0.8:
        x["seal_amount"] = seal / 1e4        # 源以「万元」给出封单额
        seal = x["seal_amount"]
    x["seal_amt_ratio"] = round(seal / amt_yuan, 3) if amt_yuan else None
    # 量纲校验结果显式回传：单位没对齐时评分应降权而不是静默按 0 处理
    _ratio = (seal / fcap_yuan) if fcap_yuan else 0
    x["seal_unit_ok"] = bool(_ratio <= 0.8)
    deg = []
    if not amt_yuan:
        deg.append("amount")
    if seal > 0 and not x["seal_unit_ok"]:
        deg.append("seal_amount")
    x["degraded"] = deg
    return x


def _pool_degraded(pool, rows):
    """把逐只票的量纲/缺失标注汇总成接口级 degraded_fields。"""
    if not pool:
        return ["pool"]
    out = set()
    for x in rows or []:
        for k in (x.get("degraded") or []):
            out.add(k)
    return sorted(out)


def _em_zb_pool(date_str):
    """真实炸板池：炸板率 = 炸板 / (涨停 + 炸板)，不能用「开板后回封」冒充。"""
    d8 = (date_str or "").replace("-", "")
    if len(d8) != 8:
        return None
    url = ("https://push2ex.eastmoney.com/getTopicZBPool?ut=7eea3edcaed734bea9cbfc24409ed989"
           f"&dpt=wz.ztzt&Pageindex=0&pagesize=1000&sort=fbt%3Aasc&date={d8}")
    try:
        j = http_json(url, retries=1, timeout=8)
        pool = (j.get("data") or {}).get("pool") or []
        if len(pool) >= 1000:          # 疑似分页截断，宁可标未知也不低估炸板率
            return None
        return len(pool)
    except Exception:
        return None                    # 接口失败 ≠ 今天没有炸板


def _hm(ts):
    """同花顺给的是 unix 时间戳字符串，转 HH:MM"""
    try:
        v = int(float(str(ts)))
    except Exception:
        return ""
    if v <= 0:
        return ""
    return datetime.fromtimestamp(v, _TZ).strftime("%H:%M")


def _hm_date(ts):
    """同花顺 unix 时间戳 → YYYY-MM-DD，用于校验返回数据是不是请求的交易日。"""
    try:
        v = int(float(str(ts)))
    except Exception:
        return ""
    if v <= 0:
        return ""
    try:
        return datetime.fromtimestamp(v, _TZ).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ths_zt_pool(date_str):
    d8 = date_str.replace("-", "")
    url = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?"
           f"page=1&limit=200&field={_THS_ZT_FIELDS}"
           f"&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={d8}")
    j = http_json(url, retries=2, timeout=10)
    info = ((j.get("data") or {}).get("info")) or []
    out = []
    for r in info:
        try:
            fcap = float(r.get("currency_value") or 0)
            out.append({
                "code": str(r.get("code") or ""),
                "name": str(r.get("name") or ""),
                "price": float(r.get("latest") or 0),
                "pct": float(r.get("change_rate") or 0),
                "turnover": float(r.get("turnover_rate") or 0),
                "fcap": fcap / 1e8,
                "amount": float(r.get("amount") or 0) / 1e8,
                "seal_amount": float(r.get("order_amount") or 0),   # 封单额（元）
                "seal_vol": float(r.get("order_volume") or 0),
                "open_num": int(float(r.get("open_num") or 0)),
                "first": _hm(r.get("first_limit_up_time")),
                "last": _hm(r.get("last_limit_up_time")),
                "ztype": str(r.get("limit_up_type") or ""),
                "high_days": str(r.get("high_days") or ""),
                "reason": str(r.get("reason_type") or "").strip(),
                "again": int(float(r.get("is_again_limit") or 0)),
                "src": "ths",
                # 用首封时间戳反推真实交易日：同花顺响应体不带 date 字段，
                # 但 first_limit_up_time 是 unix 秒，含真实日期。这样 fetch_zt_pool
                # 的「回吐旧数据」校验才有可靠依据（v1.6.4 的 stamped 恒为空）。
                "date": (_hm_date(r.get("first_limit_up_time")) or
                         _hm_date(r.get("last_limit_up_time")) or ""),
            })
        except Exception:
            continue
    return out


def _em_zt_pool(date_str, with_date=False):
    """备用：东财涨停池（无涨停原因，字段口径不同）。
    注意：东财不认非法日期参数，会直接返回最近一个交易日的数据，
    所以从 qdate 取真实日期，避免休市日把日期标错。"""
    d8 = date_str.replace("-", "")
    url = ("https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
           f"&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={d8}")
    j = http_json(url, retries=2, timeout=10)
    data = j.get("data") or {}
    qd = str(data.get("qdate") or "")
    real_date = f"{qd[:4]}-{qd[4:6]}-{qd[6:]}" if len(qd) == 8 else date_str
    out = []
    for r in ((j.get("data") or {}).get("pool") or []):
        try:
            fbt = str(int(r.get("fbt") or 0)).zfill(6)
            out.append({
                "code": str(r.get("c") or ""),
                "name": str(r.get("n") or ""),
                "price": float(r.get("p") or 0) / 1000,
                "pct": float(r.get("zdp") or 0),
                "turnover": float(r.get("hs") or 0),
                "fcap": float(r.get("ltsz") or 0) / 1e8,
                "amount": float(r.get("amount") or 0) / 1e8,
                "seal_amount": float(r.get("fund") or 0),
                "seal_vol": 0.0,
                "open_num": int(r.get("zbc") or 0),
                "first": f"{fbt[:2]}:{fbt[2:4]}" if fbt != "000000" else "",
                "last": "",
                "ztype": "",
                "high_days": f"{int(r.get('lbc') or 1)}连板" if int(r.get("lbc") or 1) > 1 else "首板",
                "reason": "",
                "again": 0,
                "industry": str(r.get("hybk") or "").strip(),
                "src": "em",
            })
        except Exception:
            continue
    return (out, real_date) if with_date else out


def fetch_zt_pool():
    """当日涨停池。非交易日自动往前找最近一个有数据的交易日。"""
    def build():
        for back in range(0, 15):
            d = (now_cn() - timedelta(days=back)).strftime("%Y-%m-%d")
            # 同花顺不认非法日期且返回项不带 date：休市日直接跳过，
            # 让东财的 qdate 给出真实交易日，避免旧数据被标成请求日
            if is_holiday(datetime.strptime(d, "%Y-%m-%d").date()):
                pool = []
            else:
                try:
                    pool = _ths_zt_pool(d)
                except Exception:
                    pool = []
            # 同花顺偶尔忽略 date 参数回吐最近一个交易日的数据；若返回项自带
            # date 且与请求日不符，就不能把它标成请求日（炸板率会跟着取错日）。
            if pool:
                stamped = [str(x.get("date") or "")[:10] for x in pool]
                stamped = [v for v in stamped if v]
                if stamped and all(v != d for v in stamped):
                    pool = []
            if pool:
                return {"date": d, "pool": pool, "source": "同花顺"}
            try:
                pack = _em_zt_pool(d, with_date=True)
            except Exception:
                pack = ([], d)
            if pack[0]:
                return {"date": pack[1] or d, "pool": pack[0], "source": "东财"}
        return {"date": "", "pool": [], "source": ""}

    return cached("zt_pool", 60, build)


def limit_analysis(params):
    t0 = time.time()
    p = dict(LIMIT_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v not in (None, ""):
            p[k] = coerce_param(LIMIT_DEFAULTS[k], v)

    pack = fetch_zt_pool()
    pool, day = pack["pool"], pack["date"]
    total_all = len(pool)

    # 题材热度（同标签涨停家数）与市场情绪（涨停总数 / 炸板率），供板块联动打分
    theme_cnt = {}
    for x in pool:
        for t in (x.get("reason") or "").replace("＋", "+").split("+"):
            t = t.strip()
            if t:
                theme_cnt[t] = theme_cnt.get(t, 0) + 1
    reopen = sum(1 for x in pool if (x.get("open_num") or 0) > 0)
    zb_raw = _em_zb_pool(day)
    zb_known = zb_raw is not None      # 未知与 0 家必须区分，否则情绪折价闸门被悄悄关掉
    zb = zb_raw or 0
    broken_rate = (round(zb / (total_all + zb) * 100, 1)
                   if zb_known and (total_all + zb) else 0.0)
    max_boards = max([parse_boards(x.get("high_days")) for x in pool] or [1])
    ctx = {
        "theme_cnt": theme_cnt,
        "market_total": total_all,
        "reopen_rate": round(reopen / total_all * 100, 1) if total_all else 0.0,
        "broken_rate": broken_rate,
        "zb_count": zb, "zb_known": zb_known,
        "market_max_boards": max_boards,
    }

    rows = []
    for x in pool:
        if not is_main_board(x["code"]):      # 只看主板（10% 涨停口径）
            continue
        if is_bad_name(x["name"], True) or bad_symbol(x["name"]):
            continue
        rows.append(_norm_pool_row(dict(x)))   # 不改 cached() 里的共享池对象

    results, errors = [], 0

    def work(x):
        try:
            bars = fetch_kline(x["code"], 1 if x["code"].startswith("6") else 0, 160,
                               x.get("name", ""))
            if len(bars) < 40:
                return None
            r = zt_evaluate(x, bars, ctx)
            if r:
                r["trade"] = trade_plan("zt", bars, x.get("price"))
            return r
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=_w(10)) as ex:
        for r in ex.map(work, rows):
            if r is None:
                errors += 1
            else:
                results.append(r)

    # 先按「能不能做」分组，再按分数：可挂隔夜单 → 盘中观察 → 不参与
    prio = {"overnight": 0, "intraday": 1, "avoid": 2}
    results.sort(key=lambda x: (prio.get(x["action"], 3), -x["score"], -x["dims"]["seal"]))
    hit = [x for x in results if x["score"] >= p["min_score"]]
    top = hit[: int(p["top"])]

    buckets = {"overnight": 0, "intraday": 0, "avoid": 0}
    for x in top:
        buckets[x["action"]] = buckets.get(x["action"], 0) + 1

    return {
        "ok": True, "mode": "zt", "date": day, "source": pack["source"],
        "env": market_env(),
        # stale = 拿不到任何有效交易日（数据源全挂），而非「今日恰好 0 家涨停」
        "stale": not pack.get("source"), "degraded_fields": _pool_degraded(pool, rows),
        "data_date": day or last_trade_date(),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "total": total_all, "main": len(rows), "hit": len(hit),
        "errors": errors, "elapsed": round(time.time() - t0, 1),
        "list": top, "params": p, "buckets": buckets,
        "emotion": {"broken_rate": broken_rate, "zb_count": zb, "zb_known": zb_known,
                    "reopen_rate": ctx["reopen_rate"], "max_boards": max_boards,
                    "market_total": total_all},
        "note": ("涨停原因取自同花顺涨停池披露的题材标签；封板时间、开板次数、封单额均为交易所盘后公开数据。"
                 "「隔夜单」＝当晚挂次日涨停价排队，博次日溢价，风险是次日高开回落；"
                 "「盘中追」＝次日不挂单，等竞价与分时走稳再决定。"),
    }


def zt_evaluate(x, bars, ctx=None):
    """涨停接力打分（与其余三个板块口径不交叉）：
       封板质量 26 / 连板与位置 20 / 板块题材 16 / 量能换手 14 / 封单资金 12 / 涨停形态 12。
       依据来自游资「二板定龙头」与情绪周期：看封单结构、封板时间、开板次数、
       板块内涨停家数（主线共振）、换手与量能是否健康；不套用波段/均线的趋势打分。"""
    ctx = ctx or {}
    theme_cnt = ctx.get("theme_cnt") or {}
    market_total = ctx.get("market_total") or 0
    broken_rate = ctx.get("broken_rate") or 0.0
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    n, c = len(bars), closes[-1]
    last = bars[-1]

    # 连板数：按该股实际涨跌停幅度（ST 5% / 主板 10%）反推，回溯 20 根
    lim = limit_pct(x["code"], x.get("name", ""))
    boards = 0
    for i in range(n - 1, max(0, n - 20), -1):
        pv = closes[i - 1] if i >= 1 else 0
        if pv and (closes[i] / pv - 1) * 100 >= lim * 0.96:
            boards += 1
        else:
            break
    # 盘中日K常未收录当天：池子里这条就是今日涨停，先把今天补算进去
    _last_date = str(bars[-1].get("date") or "")
    if (market_status()["state"] in ("open", "break")
            and _last_date and _last_date < now_cn().strftime("%Y-%m-%d")):
        boards += 1
    # 日K滞后或价格对不上时，退回接口给的连板标签兜底
    if (c and x["price"] and abs(c - x["price"]) / max(x["price"], 0.01) > 0.02) or boards <= 1:
        boards = max(boards, parse_boards(x.get("high_days")) or 1)

    ma5, ma10 = _ma_at(closes, 5), _ma_at(closes, 10)
    ma20, ma60 = _ma_at(closes, 20), _ma_at(closes, 60)
    vol5 = statistics.fmean(vols[-5:]) if n >= 5 else vols[-1]
    vol20 = statistics.fmean(vols[-20:]) if n >= 20 else vol5
    win = bars[-60:] if n >= 60 else bars
    hi60 = max(b["high"] for b in win)
    lo60 = min(b["low"] for b in win)
    pos60 = ((c - lo60) / (hi60 - lo60) * 100) if hi60 > lo60 else 50.0
    gain20 = ((c / closes[-21] - 1) * 100) if n >= 21 else 0.0

    # 数据源偶发负流通市值 / 负封单额，seal_pct 为负会让下面的 **0.5 得复数而崩；
    # 非正值一律按「无有效封单占比」处理，与上游 seal_unit_ok 降级思路一致。
    _fcap_yuan = (x.get("fcap") or 0) * 1e8
    seal_pct = (max(0.0, x["seal_amount"]) / _fcap_yuan * 100) if _fcap_yuan > 0 else 0.0
    first = x.get("first") or ""

    sig = []   # (强度, 维度, 标签, 说明)

    # ---- ① 封板质量（26）
    seal = 55.0
    if x["open_num"] == 0:
        seal += 20
        sig.append((94, "封板", "未开板", f"{first or '开盘'} 封板后全天未再打开，封板意愿强"))
    elif x["open_num"] <= 2:
        seal -= 8 * x["open_num"]
        sig.append((70, "封板", "轻微开板", f"盘中开板 {x['open_num']} 次后回封"))
    else:
        seal -= 9 * min(x["open_num"], 5)
        sig.append((42, "封板", "反复开板", f"开板 {x['open_num']} 次，封板意愿不坚决"))
    if first:
        if first <= "10:00":
            seal += 12
            sig.append((90, "封板", "早盘封板", f"{first} 首次封板，属于资金抢筹型"))
        elif first >= "14:30":
            seal -= 10
            sig.append((38, "封板", "尾盘封板", f"{first} 才封板，尾盘偷袭，次日承接存疑"))
    seal += min(13.0, (seal_pct ** 0.5) * 9)
    seal = clamp(seal)

    # ---- ② 连板与位置（20）
    if boards <= 1:
        pos = 92.0 if pos60 < 55 else 76.0
    elif boards == 2:
        pos = 70.0
    elif boards == 3:
        pos = 50.0
    else:
        pos = 28.0
    if pos60 > 88:
        pos -= 12
    if gain20 > 60:
        pos -= 15
        sig.append((34, "位置", "涨幅透支", f"近 20 日已涨 {gain20:.0f}%，此位置接力赔率很差"))
    if hi60 and c >= hi60 * 0.995:
        pos += 6
        sig.append((80, "位置", "创60日新高", f"涨停收在 60 日新高 {hi60:.2f} 附近，上方无套牢盘"))
    elif pos60 < 40:
        sig.append((82, "位置", "低位首板", f"处于 60 日区间 {pos60:.0f}% 分位，属于低位启动"))
    if gain20 > 40:
        pos -= 10
    pos = clamp(pos)

    # ---- ③ 量能换手（14）
    tr = x["turnover"]
    if tr < 2:
        vol_s = 68.0
        sig.append((58, "量能", "缩量板", f"换手仅 {tr:.1f}%，多是惜售一字，买不进也卖不出"))
    elif tr <= 15:
        vol_s = 90.0
        sig.append((84, "量能", "换手健康", f"换手 {tr:.1f}%，筹码交换充分且未失控"))
    elif tr <= 25:
        vol_s = 70.0
        sig.append((60, "量能", "换手偏高", f"换手 {tr:.1f}%，分歧开始加大"))
    else:
        vol_s = 44.0
        sig.append((40, "量能", "巨量换手", f"换手 {tr:.1f}%，获利盘出逃压力大"))
    vr_v = vol5 / vol20 if vol20 else 1
    if 1.0 <= vr_v <= 2.6:
        vol_s += 6
    # 量能节奏：首板要放量、二板温和放大 1.2~2 倍、三板后缩量加速才健康
    vr_prev = (vols[-1] / vols[-2]) if (n >= 2 and vols[-2]) else 1.0
    if boards <= 1:
        if 1.3 <= vr_prev <= 3.2:
            vol_s += 6
            sig.append((78, "量能", "首板放量", f"今日成交量为昨日 {vr_prev:.1f} 倍，属于放量突破型首板"))
        elif vr_prev < 0.8:
            vol_s -= 4
            sig.append((46, "量能", "首板缩量", f"今日量仅为昨日 {vr_prev:.1f} 倍，缩量涨停多为惜售或买不进"))
    elif boards == 2:
        if 1.2 <= vr_prev <= 2.2:
            vol_s += 8
            sig.append((84, "量能", "二板量能健康", f"二板量为首板 {vr_prev:.1f} 倍，接力资金在进场而非出货"))
        elif vr_prev > 3:
            vol_s -= 6
            sig.append((42, "量能", "二板暴量", f"二板量已达首板 {vr_prev:.1f} 倍，分歧过大容易烂板"))
    elif vr_prev <= 0.75:
        vol_s += 6
        sig.append((74, "量能", "缩量加速", f"量为昨日 {vr_prev:.1f} 倍，缩量加速属于最强形态，但随时可能见顶"))
    vol_s = clamp(vol_s)

    # ---- ④ 涨停形态（12）：只看涨停自身的位置结构，不套用波段形态库
    tech = 40.0
    amp20 = 99.0
    if n >= 20:
        lo20 = min(lows[-20:])
        hi20 = max(highs[-20:])
        amp20 = ((hi20 - lo20) / lo20 * 100) if lo20 else 99.0
    prev_chg = 0.0
    if n >= 3 and closes[-3]:
        prev_chg = (closes[-2] / closes[-3] - 1) * 100
    if hi60 and c >= hi60 * 0.995:
        tech += 22
        sig.append((92, "形态", "60日新高板", f"涨停收在 60 日新高 {hi60:.2f} 附近，上方无套牢盘"))
    elif amp20 <= 15:
        tech += 16
        sig.append((82, "形态", "平台突破板", f"近 20 日振幅仅 {amp20:.0f}%，横盘后首个涨停，筹码断层"))
    if prev_chg < -2 and last["close"] >= (bars[-2]["open"] * 1.02):
        tech += 14
        sig.append((86, "形态", "反包板", "前一日下跌、今日涨停反包，短线资金重新聚力"))
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        tech += 8
        sig.append((70, "形态", "均线多头", "MA5>MA10>MA20，涨停前趋势已走顺"))
    tech = clamp(tech)

    # ---- ⑤ 封单资金（12）
    fund = 34 + min(40, seal_pct * 22)
    if x["amount"] and x["fcap"]:
        fund += min(18, x["amount"] / x["fcap"] * 100 * 1.2)
    fund = clamp(fund)
    if seal_pct >= 0.3:
        sig.append((78, "资金", "封单充足", f"封单 {x['seal_amount']/1e8:.2f} 亿，占流通市值 {seal_pct:.2f}%"))

    # ---- ⑥ 板块与题材（16）：主线共振远好于孤立涨停
    reason = x.get("reason") or ""
    topics = [t for t in reason.replace("＋", "+").split("+") if t.strip()]
    peers = max([theme_cnt.get(t, 0) for t in topics], default=0) if topics else 0
    if not topics or not theme_cnt:
        heat = 62.0
        sig.append((58, "板块", "板块数据缺失",
                    "当前涨停池来源（东财备用源）未提供涨停原因，板块联动按中性计"))
    elif peers >= 8:
        heat = 96.0
        sig.append((95, "板块", "主线共振", f"同题材今日涨停 {peers} 家，是当日最强主线"))
    elif peers >= 5:
        heat = 84.0
        sig.append((86, "板块", "板块联动", f"同题材涨停 {peers} 家，板块效应明确"))
    elif peers >= 3:
        heat = 68.0
        sig.append((68, "板块", "板块联动", f"同题材涨停 {peers} 家，有一定跟风盘"))
    elif peers >= 1:
        heat = 52.0
        sig.append((50, "板块", "孤立涨停", f"同题材仅 {peers} 家涨停，缺乏板块合力"))
    else:
        heat = 46.0
    heat = clamp(heat + min(len(topics), 3) * 3)

    total = (seal * 0.26 + pos * 0.20 + heat * 0.16
             + vol_s * 0.14 + fund * 0.12 + tech * 0.12)
    # 市场情绪闸门：炸板率高 / 涨停家数少的日子，同样的封板质量也该打折
    mood = 1.0
    if not ctx.get("zb_known", True):
        sig.append((45, "板块", "情绪数据缺失", "炸板池接口未返回，本次不启用情绪折价/溢价"))
    elif broken_rate >= 45:
        mood = 0.88
    elif broken_rate >= 30:
        mood = 0.94
    elif broken_rate <= 15 and market_total >= 40:
        mood = 1.04
    if market_total < 15:
        mood *= 0.95
    total = clamp(total * mood)
    if mood != 1.0:
        sig.append((60, "板块", "情绪折价", f"全市场炸板率 {broken_rate:.0f}%（炸板 {ctx.get('zb_count', 0)} 家），"
                    f"涨停 {market_total} 家，最高 {ctx.get('market_max_boards', 1)} 板，评分按情绪 ×{mood:.2f}"))
    grade = _grade_of("zt", total)

    # 一字板（无开板、封单厚、换手极低）实际上买不进，不能给「可挂隔夜单」
    one_word = x["open_num"] == 0 and seal_pct >= 0.8 and tr < 2
    # ---- 结论：能不能挂隔夜单 / 盘中追
    if gain20 >= 70:
        action, action_txt = "avoid", "涨幅透支，不接力"
    elif boards >= 4:
        action, action_txt = "avoid", "高位连板，不接力"
    elif total >= 72 and boards <= 2 and tr <= 22 and seal >= 60:
        action, action_txt = "overnight", "可挂隔夜单"
    elif total >= 58:
        action, action_txt = "intraday", "盘中观察再追"
    else:
        action, action_txt = "avoid", "不参与"
    if one_word and action == "overnight":
        action, action_txt = "intraday", "一字板买不进，仅观察"

    sig.sort(key=lambda t: -t[0])
    picked, used = [], {}
    for s in sig:
        if s[1] == "板块" and reason:
            continue                      # 涨停原因已经单独展示，不重复占一条理由
        cap = 2 if s[1] in ("封板", "形态") else 1
        if used.get(s[1], 0) >= cap:
            continue
        picked.append(s)
        used[s[1]] = used.get(s[1], 0) + 1
        if len(picked) >= 3:
            break
    for s in sig:
        if len(picked) >= 3:
            break
        if s not in picked and not (s[1] == "板块" and reason):
            picked.append(s)

    # 日K含今日时前收=bars[-2]；盘中日K滞后一天时 bars[-1] 才是昨收
    if n >= 2 and str(bars[-1].get("date") or "") == now_cn().strftime("%Y-%m-%d"):
        prev_close = bars[-2]["close"]
    elif bars:
        prev_close = bars[-1]["close"]
    else:
        prev_close = x["price"] / (1 + lim / 100)
    zt_price = limit_price(prev_close, True, x["code"], x.get("name", ""))
    rules = {
        "seal": {"name": "① 封板质量（开板次数 / 首封时间 / 封单）", "passed": round(seal) >= 60,
                 "value": f"{seal:.0f} 分", "score": round(seal, 1),
                 "detail": f"{first or '—'} 首封，开板 {x['open_num']} 次，封单 {x['seal_amount']/1e8:.2f} 亿", "weight": 26},
        "pos": {"name": "② 连板与位置（几板 / 60 日分位 / 20 日涨幅）", "passed": round(pos) >= 60,
                "value": f"{pos:.0f} 分", "score": round(pos, 1),
                "detail": f"{boards} 连板，60 日区间 {pos60:.0f}% 分位，20 日涨幅 {gain20:+.1f}%", "weight": 20},
        "heat": {"name": "③ 板块与题材（同题材涨停家数 / 标签）", "passed": round(heat) >= 60,
                 "value": f"{heat:.0f} 分", "score": round(heat, 1),
                 "detail": f"同题材涨停 {peers} 家；{reason or '接口未披露涨停原因'}", "weight": 16},
        "vol": {"name": "④ 量能换手（换手率 / 首板二板量能节奏）", "passed": round(vol_s) >= 60,
                "value": f"{vol_s:.0f} 分", "score": round(vol_s, 1),
                "detail": f"换手 {tr:.1f}%，今日/昨日量 {vr_prev:.2f}，5/20 日量能比 {vr_v:.2f}", "weight": 14},
        "fund": {"name": "⑤ 封单资金（封单额 / 流通市值占比）", "passed": round(fund) >= 55,
                 "value": f"{fund:.0f} 分", "score": round(fund, 1),
                 "detail": f"封单额 {x['seal_amount']/1e8:.2f} 亿，占流通市值 {seal_pct:.2f}%", "weight": 12},
        "tech": {"name": "⑥ 涨停形态（新高板 / 平台突破 / 反包）", "passed": round(tech) >= 60,
                 "value": f"{tech:.0f} 分", "score": round(tech, 1),
                 "detail": f"近 20 日振幅 {amp20:.0f}%，60 日位置 {pos60:.0f}% 分位", "weight": 12},
    }

    return {
        "code": x["code"], "name": x["name"], "mode": "zt",
        "market": 1 if x["code"].startswith("6") else 0,
        "price": round(x["price"], 2), "prev_close": round(prev_close, 2),
        "pct": round(x["pct"], 2), "turnover": round(tr, 2),
        "volratio": 0, "fcap": round(x["fcap"], 1), "amount": round(x["amount"], 2),
        "main_net": None, "industry": "", "pe": 0, "pb": 0,
        "high": last["high"], "low": last["low"], "open": last["open"],
        "score": round(total, 1), "grade": grade,
        "boards": boards, "reason": reason, "ztype": x.get("ztype") or "",
        "seal_amount": round(x["seal_amount"] / 1e8, 2), "seal_pct": round(seal_pct, 2),
        "first": first, "open_num": x["open_num"],
        "action": action, "action_txt": action_txt,
        "reasons": [f"{s[2]}：{s[3]}" for s in picked[:3]],
        "labels": [s[2] for s in sig[:8]],
        "ind_count": sum(1 for s in sig if s[0] >= 70),
        "dims": {"seal": round(seal), "pos": round(pos), "vol": round(vol_s),
                 "tech": round(tech), "fund": round(fund), "heat": round(heat)},
        "peers": peers, "market_total": market_total, "broken_rate": broken_rate,
        "rules": rules, "tags": [s[2] for s in sig[:8]],
        "metrics": {"pos60": round(pos60), "gain20": round(gain20, 1),
                    "vr_v": round(vr_v, 2), "hi60": round(hi60, 2)},
        "buy_ok": (not one_word) and action == "overnight",
        "seal_amt_ratio": x.get("seal_amt_ratio"),
        "plan": {"zt_price": zt_price,
                 "next_limit": limit_price(x["price"], True, x["code"], x.get("name", "")),
                 "support": r2(max(ma5 or 0, x["price"] * (1 - lim / 100 * 0.6)))},
    }


# ---------------------------------------------------------------- 跌停分析
DT_DEFAULTS = {
    "top": 40,
    "min_score": 0,
}


def _em_dt_pool(date_str):
    """东财跌停池。字段：p 现价×1000 / zdp 涨跌幅 / hs 换手 / ltsz 流通市值 /
    fund 跌停封单额（元）/ oc 开板次数 / lbt 最后封板时间 /
    days 连续跌停天数 / hybk 所属行业。
    注：接口另有 fba 字段，但与当日成交额量级不自洽（个别是成交额的 27 倍），不采用。"""
    d8 = date_str.replace("-", "")
    url = ("https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
           f"&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fund%3Aasc&date={d8}")
    j = http_json(url, retries=2, timeout=10)
    data = j.get("data") or {}
    qd = str(data.get("qdate") or "")
    real_date = f"{qd[:4]}-{qd[4:6]}-{qd[6:]}" if len(qd) == 8 else date_str
    out = []
    for r in (data.get("pool") or []):
        try:
            lbt = str(int(float(r.get("lbt") or 0))).zfill(6)
            out.append({
                "code": str(r.get("c") or ""),
                "name": str(r.get("n") or ""),
                "price": float(r.get("p") or 0) / 1000,
                "pct": float(r.get("zdp") or 0),
                "turnover": float(r.get("hs") or 0),
                "fcap": float(r.get("ltsz") or 0) / 1e8,
                "amount": float(r.get("amount") or 0) / 1e8,
                "seal_amount": float(r.get("fund") or 0),
                "open_num": int(float(r.get("oc") or 0)),
                "last": f"{lbt[:2]}:{lbt[2:4]}" if lbt != "000000" else "",
                "days": int(float(r.get("days") or 1)),
                "industry": str(r.get("hybk") or "").strip(),
                "src": "em",
            })
        except Exception:
            continue
    return {"pool": out, "total": int(data.get("tc") or len(out)), "date": real_date}


def fetch_dt_pool():
    """当日跌停池。非交易日自动往前找最近一个有数据的交易日。"""
    def build():
        for back in range(0, 15):
            d = (now_cn() - timedelta(days=back)).strftime("%Y-%m-%d")
            try:
                pack = _em_dt_pool(d)
            except Exception:
                pack = {"pool": [], "total": 0, "date": ""}
            if pack["pool"]:
                return {"date": pack.get("date") or d, "pool": pack["pool"],
                        "total": pack["total"], "source": "东财"}
        return {"date": "", "pool": [], "total": 0, "source": ""}

    return cached("dt_pool", 60, build)


def limit_down_analysis(params):
    t0 = time.time()
    p = dict(DT_DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v not in (None, ""):
            p[k] = coerce_param(DT_DEFAULTS[k], v)

    pack = fetch_dt_pool()
    pool, day = pack["pool"], pack["date"]
    total_all = pack["total"] or len(pool)

    # 行业维度：同行业跌停家数（板块性崩塌）与同行业涨停家数（错杀线索）
    ind_dt, ind_zt, market_zt = {}, {}, 0
    for x in pool:
        ind = x.get("industry") or ""
        if ind:
            ind_dt[ind] = ind_dt.get(ind, 0) + 1
    try:
        zpool = _em_zt_pool(day) if day else []
        market_zt = len(zpool)
        for x in zpool:
            ind = x.get("industry") or ""
            if ind:
                ind_zt[ind] = ind_zt.get(ind, 0) + 1
    except Exception:
        pass

    # 盘中才有「当日地天板」可谈；收盘后一律按次日反核 / 次日低吸处理
    hm = now_cn().strftime("%H:%M")
    session = "intraday" if (market_status()["state"] == "open" and hm < "14:30") else "closed"
    ctx = {"ind_dt": ind_dt, "ind_zt": ind_zt, "market_dt": total_all,
           "market_zt": market_zt, "session": session}

    rows = []
    for x in pool:
        if not is_main_board(x["code"]):        # 只看主板（10% 跌停口径）
            continue
        if is_bad_name(x["name"], True) or bad_symbol(x["name"]):
            continue
        rows.append(_norm_pool_row(dict(x)))   # 不改 cached() 里的共享池对象

    results, errors = [], 0

    def work(x):
        try:
            bars = fetch_kline(x["code"], 1 if x["code"].startswith("6") else 0, 160,
                               x.get("name", ""))
            if len(bars) < 40:
                return None
            r = dt_evaluate(x, bars, ctx)
            if r:
                r["trade"] = trade_plan("dt", bars, x.get("price"))
            return r
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=_w(10)) as ex:
        for r in ex.map(work, rows):
            if r is None:
                errors += 1
            else:
                results.append(r)

    # 分组顺序：当日盘中买入 → 可博地天板 → 尾盘买入博弈次日 → 不参与
    prio = {"intraday": 0, "ditian": 1, "close": 2, "avoid": 3}
    results.sort(key=lambda x: (prio.get(x.get("grp") or x["action"], 4),
                                -x["score"], -x["dims"]["break"]))
    hit = [x for x in results if x["score"] >= p["min_score"]]
    top = hit[: int(p["top"])]

    buckets = {"intraday": 0, "ditian": 0, "close": 0, "avoid": 0}
    by_action = {"ditian": 0, "rebound": 0, "avoid": 0}
    for x in top:
        g = x.get("grp") or x["action"]
        buckets[g] = buckets.get(g, 0) + 1
        by_action[x["action"]] = by_action.get(x["action"], 0) + 1

    return {
        "ok": True, "mode": "dt", "date": day, "source": pack["source"],
        "env": market_env(),
        # stale = 数据源全挂（无 source），而非「今日恰好 0 家跌停」
        "stale": not pack.get("source"), "degraded_fields": _pool_degraded(pool, rows),
        "data_date": day or last_trade_date(),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(), "session": session,
        "total": total_all, "main": len(rows), "hit": len(hit),
        "market_zt": market_zt,
        "errors": errors, "elapsed": round(time.time() - t0, 1),
        "list": top, "params": p, "buckets": buckets, "by_action": by_action,
        "note": ("跌停封单额、开板次数、连续跌停天数均取自交易所盘后公开数据。"
                 "「地天板」＝跌停价被撬开并翻红封板，要求封单薄、反复开板、换手充分；"
                 "「当日盘中买入」＝现价已离开跌停价、盘中就能动手的反抽；"
                 "「尾盘买入博弈次日」＝仍然压在跌停价上，等尾盘看封单被吃掉的迹象再博次日反核。"
                 "封死一字跌停（0 次开板 + 大封单）一律不参与：买不进，且次日大概率惯性低开。"),
    }


def dt_evaluate(x, bars, ctx=None):
    """跌停博弈打分（两个出口，与涨停 / 波段口径都不交叉）：
       撬板强度 26 / 超跌位置 20 / 量能换手 18 / 封单压力 14 / 板块环境 12 / 资金承接 10。
       依据：跌停封单被反复撬开、封单额占流通市值低、跌停价上换手充分 → 有资金在承接，
       是「反核 / 地天板」的前兆（约投顾《复盘跌停板上的封单变化》、淘股吧《地天板的 4 个关键时点》）；
       跌幅够深 + 远离 MA20 + 同行业跌停家数少而仍有涨停 → 属情绪错杀，反抽概率高
       （三七财经《短线如何做超跌反弹》）。本板块不看封板质量，也不套 MA60 趋势。"""
    ctx = ctx or {}
    ind_dt = ctx.get("ind_dt") or {}
    ind_zt = ctx.get("ind_zt") or {}
    market_dt = ctx.get("market_dt") or 0
    market_zt = ctx.get("market_zt") or 0
    session = ctx.get("session") or "closed"

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    n, c = len(bars), closes[-1]
    last = bars[-1]
    px = x["price"] if x["price"] > 0 else c

    lim = limit_pct(x["code"], x.get("name", ""))
    # 用日K的真实前收，别用「现价 ÷ (1+涨跌幅)」反推（涨跌幅本身已四舍五入）；
    # 日K含今日时前收=bars[-2]，盘中滞后一天时 bars[-1] 才是昨收
    _today = now_cn().strftime("%Y-%m-%d")
    if n >= 2 and str(bars[-1].get("date") or "") == _today and bars[-2]["close"] > 0:
        prev_close = bars[-2]["close"]
    elif bars and bars[-1]["close"] > 0:
        prev_close = bars[-1]["close"]
    else:
        prev_close = px / (1 + x["pct"] / 100) if x["pct"] else px
    dp_price = limit_price(prev_close, False, x["code"], x.get("name", ""))
    off = (px / dp_price - 1) * 100 if dp_price else 0.0  # >0 表示已脱离跌停价（被撬开）
    # 数据源偶发负流通市值 / 负封单额：非正值一律按「无有效封单占比」处理，
    # 与 zt_evaluate 同口径（旧写法只判 truthy，fcap 为负时会算出负占比，
    # 让「封单极薄」一类的判断在脏数据上误触发）。
    _fcap_yuan = (x.get("fcap") or 0) * 1e8
    seal_pct = (max(0.0, x["seal_amount"]) / _fcap_yuan * 100) if _fcap_yuan > 0 else 0.0
    # 封单相对当日成交额的倍数：倍数越小，撬开它需要的资金越少
    seal_cover = (max(0.0, x["seal_amount"]) / (x["amount"] * 1e8)) if (x["amount"] or 0) > 0 else 0.0
    tr = x["turnover"]

    ma5, ma10 = _ma_at(closes, 5), _ma_at(closes, 10)
    ma20, ma60 = _ma_at(closes, 20), _ma_at(closes, 60)
    win = bars[-60:] if n >= 60 else bars
    hi60 = max(b["high"] for b in win)
    lo60 = min(b["low"] for b in win)
    pos60 = ((px - lo60) / (hi60 - lo60) * 100) if hi60 > lo60 else 50.0
    gain20 = ((px / closes[-21] - 1) * 100) if n >= 21 else 0.0
    dist20 = ((px / ma20 - 1) * 100) if ma20 else 0.0
    vol5 = statistics.fmean(vols[-5:]) if n >= 5 else vols[-1]
    vol20 = statistics.fmean(vols[-20:]) if n >= 20 else vol5
    vr_today = (vols[-1] / vols[-2]) if (n >= 2 and vols[-2]) else 1.0

    # 连续跌停天数：按该股实际跌幅限制反推（ST 5%），回溯 20 根
    ddays = 0
    for i in range(n - 1, max(0, n - 20), -1):
        pv = closes[i - 1] if i >= 1 else 0
        if pv and (closes[i] / pv - 1) * 100 <= -lim * 0.96:
            ddays += 1
        else:
            break
    # 盘中日K常未收录当天：池子里这条就是今日跌停，先把今天补算进去
    _last_date = str(bars[-1].get("date") or "")
    if (market_status()["state"] in ("open", "break")
            and _last_date and _last_date < now_cn().strftime("%Y-%m-%d")):
        ddays += 1
    ddays = max(ddays, 1, x.get("days") or 1)

    sig = []   # (强度, 维度, 标签, 说明)

    # ---- ① 撬板强度（26）：开板 3~8 次是健康撬板；几十次还收在跌停＝板上抛压根本吃不完
    oc = x["open_num"]
    brk = 40.0
    if oc == 0:
        if seal_pct <= 0.5:
            brk = 62.0
            sig.append((62, "撬板", "封单将尽",
                        f"全天没开板，但封单只剩流通市值 {seal_pct:.2f}%，收盘前已被吃光，次日易高开"))
        else:
            brk = 34.0
            sig.append((40, "撬板", "封死未开板", "全天 0 次开板、封单还压着，典型核按钮死封，买不进也卖不出"))
    elif oc <= 2:
        brk = 72.0
        sig.append((74, "撬板", "试探性撬板", f"盘中开板 {oc} 次，有资金在跌停价试探，但力度还不强"))
    elif oc <= 8:
        brk = 88.0
        sig.append((88, "撬板", "撬板坚决", f"开板 {oc} 次，跌停封单被反复吃掉，是教科书式的撬板形态"))
    elif oc <= 15:
        brk = 72.0
        sig.append((68, "撬板", "反复拉锯", f"开板 {oc} 次，撬得开也守不住，多空在跌停价反复拉锯"))
    else:
        brk = 56.0
        sig.append((46, "撬板", "撬不动", f"开板 {oc} 次仍收在跌停，每次都被砸回，抛压还没吃完"))
    if oc > 0 and seal_pct <= 0.15:
        brk += 10
        if x["seal_amount"] < 1e6:
            sig.append((90, "撬板", "封单吃尽",
                        f"收盘时跌停封单只剩 {x['seal_amount'] / 1e4:.0f} 万元，几乎没人再压单，次日易高开"))
        else:
            sig.append((88, "撬板", "封单极薄", f"跌停封单仅占流通市值 {seal_pct:.2f}%，几笔大单就能撬开"))
    elif oc > 0 and seal_pct <= 0.5:
        brk += 5
    elif seal_pct >= 1.5:
        brk -= 14
        sig.append((34, "撬板", "死封", f"封单占流通市值 {seal_pct:.2f}%，属于核按钮死封"))
    if seal_cover and seal_cover <= 0.3:
        brk += 8                     # 与「封单极薄」高度相关，只加分不重复占用理由位
    elif seal_cover >= 2:
        brk -= 6
        sig.append((44, "撬板", "封单难吃",
                    f"跌停封单是当日成交额的 {seal_cover:.1f} 倍，要吃下它需要的资金量很大"))
    if off >= 0.3:
        brk += 10
        sig.append((86, "撬板", "已撬开", f"现价高于跌停价 {off:.1f}%，跌停板已经被打开"))
    brk = clamp(brk)

    # ---- ② 超跌位置（20）：跌得够深才有反抽空间，高位崩塌不算超跌
    if pos60 < 20:
        pos = 90.0
    elif pos60 < 35:
        pos = 82.0
    elif pos60 < 50:
        pos = 68.0
    elif pos60 < 65:
        pos = 52.0
    else:
        pos = 34.0
    if gain20 <= -25:
        pos += 6
        sig.append((84, "位置", "深度超跌", f"近 20 日跌幅 {gain20:.0f}%，跌透了才有反抽空间"))
    elif gain20 <= -12:
        pos += 3
    if dist20 <= -12:
        pos += 5
        sig.append((76, "位置", "远离MA20", f"现价低于 MA20 {abs(dist20):.0f}%，乖离过大有回归需求"))
    if gain20 >= 8 and pos60 >= 70:
        pos = 24.0
        sig.append((30, "位置", "高位崩塌",
                    f"近 20 日仍涨 {gain20:+.0f}% 且处在 60 日高位，这是高位杀跌，反抽多是逃命波"))
    elif gain20 >= 25:
        pos = min(pos, 58.0)
        sig.append((62, "位置", "强势回撤",
                    f"近 20 日仍涨 {gain20:+.0f}%，属于前期强势股被砸，反核赌的是情绪修复而不是超跌"))
    if ddays >= 3:
        pos -= 8
    pos = clamp(pos)

    # ---- ③ 量能换手（18）：跌停要换手才算把恐慌盘换出去
    if tr < 1.5:
        vol_s = 42.0
        sig.append((46, "量能", "缩量一字", f"换手仅 {tr:.1f}%，卖不出去，抛压没释放"))
    elif tr < 4:
        vol_s = 58.0
    elif tr <= 12:
        vol_s = 82.0
        sig.append((80, "量能", "换手健康", f"换手 {tr:.1f}%，恐慌盘在跌停价完成交换"))
    elif tr <= 25:
        vol_s = 90.0
        sig.append((86, "量能", "充分换手", f"换手 {tr:.1f}%，套牢盘充分交换，地天板的必要前提"))
    else:
        vol_s = 60.0
        sig.append((44, "量能", "天量出逃", f"换手 {tr:.1f}%，资金在夺路而逃，接盘风险大"))
    if vr_today >= 1.5:
        vol_s += 4
    elif vr_today <= 0.6:
        vol_s -= 4
    vol_s = clamp(vol_s)

    # ---- ④ 封单压力（14）：分数越高＝上方压单越轻，越容易被撬
    if seal_pct <= 0.02:
        seal = 90.0
    elif seal_pct <= 0.2:
        seal = 86.0
    elif seal_pct <= 0.5:
        seal = 74.0
    elif seal_pct <= 1.0:
        seal = 56.0
    elif seal_pct <= 2.0:
        seal = 38.0
    else:
        seal = 24.0
    if x["seal_amount"] >= 3e8:
        seal -= 6
    seal = clamp(seal)

    # ---- ⑤ 板块环境（12）：同行业集体跌停＝板块崩塌；同行业还有涨停＝错杀
    ind = x.get("industry") or ""
    peers_dt = ind_dt.get(ind, 0) if ind else 0
    peers_zt = ind_zt.get(ind, 0) if ind else 0
    if not ind:
        heat = 60.0
        sig.append((56, "板块", "行业未知", "接口未给所属行业，板块环境按中性计"))
    elif peers_dt >= 5:
        heat = 34.0
        sig.append((40, "板块", "板块崩塌", f"同行业今日 {peers_dt} 家跌停，是板块性出货，反弹不可靠"))
    elif peers_dt >= 3:
        heat = 52.0
    elif peers_dt == 2:
        heat = 66.0
    else:
        heat = 74.0
        sig.append((72, "板块", "个股跌停", "同行业没有第二家跌停，更像个股事件而非板块崩塌"))
    if peers_zt >= 2:
        heat += 12
        sig.append((88, "板块", "同行业仍涨停", f"同行业今天还有 {peers_zt} 家涨停，这个跌停更像错杀"))
    elif peers_zt == 1:
        heat += 6
    if 50 <= market_dt <= 150:
        heat += 5
        sig.append((74, "板块", "情绪冰点", f"当日跌停池 {market_dt} 家，系统性恐慌里错杀占比高"))
    if market_dt > 200:
        heat -= 6
    heat = clamp(heat)

    # ---- ⑥ 资金承接（10）：成交额占流通市值 + 跌停价上的换手厚度 + 盘子大小
    amt_ratio = (x["amount"] / x["fcap"] * 100) if x["fcap"] else 0.0
    if amt_ratio < 2:
        fund = 40.0
    elif amt_ratio < 4:
        fund = 56.0
    elif amt_ratio < 8:
        fund = 70.0
    elif amt_ratio < 15:
        fund = 82.0
    else:
        fund = 86.0
    if tr >= 8 and amt_ratio >= 6:
        fund += 6
        sig.append((78, "资金", "承接有厚度", f"换手 {tr:.1f}%、成交额占流通市值 {amt_ratio:.0f}%，跌停价上有人真金白银在接"))
    if 20 <= x["fcap"] <= 50:
        fund += 8
        sig.append((74, "资金", "盘子好撬", f"流通市值 {x['fcap']:.0f} 亿，四两拨千斤的体量"))
    elif 50 < x["fcap"] <= 100:
        fund += 4
    elif x["fcap"] > 300:
        fund -= 6
        sig.append((42, "资金", "盘子太重", f"流通市值 {x['fcap']:.0f} 亿，撬板需要的资金量太大"))
    fund = clamp(fund)

    total = (brk * 0.26 + pos * 0.20 + vol_s * 0.18
             + seal * 0.14 + heat * 0.12 + fund * 0.10)
    grade = _grade_of("dt", total)

    # ---- 结论：地天板 / 当日盘中买入 / 尾盘买入博弈次日 / 不参与
    # grp 是前端分组键，action 保留原始出口（评分详情、徽章色仍按 action 走）
    window, grp = "", "avoid"
    if (x["open_num"] == 0 and seal_pct >= 1.0) or ddays >= 4:
        action, action_txt = "avoid", "封死跌停，不接"
    elif gain20 >= 15 and pos60 >= 70:
        action, action_txt = "avoid", "高位崩塌，不接"
    elif (total >= 72 and brk >= 78 and seal_pct <= 0.5
          and tr >= 5 and peers_dt <= 4 and gain20 > -45):
        action, action_txt = "ditian", "可博地天板"
        grp = "ditian"
        window = "当日撬板" if session == "intraday" else "次日反核"
    elif total >= 58:
        action = "rebound"
        if off >= 0.3:
            # 跌停板已经被撬开 → 当天盘中可动手（T+1，当日买入次日才能卖）
            grp, window, action_txt = "intraday", "当日盘中", "当日盘中买入（T+1 次日才能卖）"
        else:
            # 还压在跌停价上 → 白天买不到，只能尾盘看封单变化，买的是次日反弹
            grp, window, action_txt = "close", "尾盘买入", "尾盘博次日"
    else:
        action, action_txt = "avoid", "不参与"
    if action == "avoid" and ddays >= 3:
        action_txt = f"{ddays} 连跌停，不接"

    sig.sort(key=lambda t: -t[0])
    picked, used = [], {}
    for s in sig:
        if s[1] == "板块" and not ind:
            continue
        cap = 2 if s[1] in ("撬板", "板块") else 1
        if used.get(s[1], 0) >= cap:
            continue
        picked.append(s)
        used[s[1]] = used.get(s[1], 0) + 1
        if len(picked) >= 3:
            break
    for s in sig:
        if len(picked) >= 3:
            break
        if s not in picked:
            picked.append(s)

    target = limit_price(prev_close, True, x["code"], x.get("name", "")) if action == "ditian" \
        else r2(min(prev_close, ma5 or prev_close))
    rules = {
        "break": {"name": "① 撬板强度（开板次数 / 封单占比 / 封单能否吃下）", "passed": round(brk) >= 62,
                  "value": f"{brk:.0f} 分", "score": round(brk, 1),
                  "detail": f"开板 {x['open_num']} 次，封单占流通市值 {seal_pct:.2f}%，封成比（封单/成交额）{(x.get('seal_amt_ratio') if x.get('seal_amt_ratio') is not None else seal_cover):.2f}",
                  "weight": 26},
        "pos": {"name": "② 超跌位置（60 日分位 / 20 日跌幅 / 偏离 MA20）", "passed": round(pos) >= 60,
                "value": f"{pos:.0f} 分", "score": round(pos, 1),
                "detail": f"60 日区间 {pos60:.0f}% 分位，20 日涨幅 {gain20:+.1f}%，距 MA20 {dist20:+.1f}%",
                "weight": 20},
        "vol": {"name": "③ 量能换手（换手率 / 放量出清 / 缩量一字）", "passed": round(vol_s) >= 60,
                "value": f"{vol_s:.0f} 分", "score": round(vol_s, 1),
                "detail": f"换手 {tr:.1f}%，今日/昨日量 {vr_today:.2f}，5/20 日量能比 {(vol5 / vol20 if vol20 else 1):.2f}",
                "weight": 18},
        "seal": {"name": "④ 封单压力（封单额占流通市值，越低越好撬）", "passed": round(seal) >= 60,
                 "value": f"{seal:.0f} 分", "score": round(seal, 1),
                 "detail": f"跌停封单 {x['seal_amount'] / 1e8:.2f} 亿，占流通市值 {seal_pct:.2f}%",
                 "weight": 14},
        "heat": {"name": "⑤ 板块环境（同行业跌停家数 / 同行业涨停）", "passed": round(heat) >= 60,
                 "value": f"{heat:.0f} 分", "score": round(heat, 1),
                 "detail": f"{ind or '行业未知'}：同行业跌停 {peers_dt} 家、涨停 {peers_zt} 家；当日跌停池 {market_dt} 家",
                 "weight": 12},
        "fund": {"name": "⑥ 资金承接（成交额占流通市值 / 换手厚度 / 流通盘）", "passed": round(fund) >= 55,
                 "value": f"{fund:.0f} 分", "score": round(fund, 1),
                 "detail": f"成交额占流通市值 {amt_ratio:.1f}%，换手 {tr:.1f}%，流通市值 {x['fcap']:.0f} 亿",
                 "weight": 10},
    }

    return {
        "code": x["code"], "name": x["name"], "mode": "dt",
        "market": 1 if x["code"].startswith("6") else 0,
        "price": round(px, 2), "prev_close": round(prev_close, 2),
        "pct": round(x["pct"], 2), "turnover": round(tr, 2),
        "volratio": 0, "fcap": round(x["fcap"], 1), "amount": round(x["amount"], 2),
        "main_net": None, "industry": ind, "pe": 0, "pb": 0,
        "high": last["high"], "low": last["low"], "open": last["open"],
        "score": round(total, 1), "grade": grade,
        "open_num": x["open_num"], "last": x.get("last", ""), "days": ddays,
        "seal_amount": round(x["seal_amount"] / 1e8, 4), "seal_pct": round(seal_pct, 2),
        "seal_cover": round(seal_cover, 2), "off": round(off, 2),
        "peers_dt": peers_dt, "peers_zt": peers_zt, "market_dt": market_dt, "market_zt": market_zt,
        "action": action, "action_txt": action_txt, "window": window, "grp": grp,
        "reasons": [f"{s[2]}：{s[3]}" for s in picked[:3]],
        "labels": [s[2] for s in sig[:8]],
        "ind_count": sum(1 for s in sig if s[0] >= 70),
        "buy_ok": action != "avoid" and not (x["open_num"] == 0 and seal_pct >= 1.0),
        "seal_amt_ratio": x.get("seal_amt_ratio"),
        "dims": {"break": round(brk), "pos": round(pos), "vol": round(vol_s),
                 "seal": round(seal), "heat": round(heat), "fund": round(fund)},
        "rules": rules, "tags": [s[2] for s in sig[:8]],
        "metrics": {"pos60": round(pos60), "gain20": round(gain20, 1),
                    "dist20": round(dist20, 1), "ma5": round(ma5 or 0, 2),
                    "ma20": round(ma20 or 0, 2), "hi60": round(hi60, 2)},
        "plan": {"dp_price": dp_price,
                 "next_dp": limit_price(px, False, x["code"], x.get("name", "")),
                 "target": target,
                 "stop": r2(dp_price * 0.97),
                 "stop_pct": round((r2(dp_price * 0.97) / px - 1) * 100, 2) if px else None,
                 "target_pct": round((target / px - 1) * 100, 2) if px else None},
    }


# ---------------------------------------------------------------- 筛选主流程
DEFAULTS = {
    "gain_min": 3.0, "gain_max": 5.0,
    "vr_min": 1.0,
    "turn_min": 5.0, "turn_max": 10.0,
    "fcap_min": 50.0, "fcap_max": 200.0,
    "vol_cv_max": 0.65,
    "entry_time": "14:30",
    "exclude_st": True,
    "min_amount": 0.8,          # 成交额下限（亿）
    "deep_limit": 90,           # 深度分析上限，防止接口打爆
    "score_min": 60.0,
}


def market_status():
    load_holidays()
    now = now_cn()
    if now.strftime("%Y-%m-%d") in HOLIDAYS:
        return {"state": "closed", "text": "休市 · 节假日"}
    if now.weekday() >= 5:
        return {"state": "closed", "text": "休市 · 周末"}
    hm = now.strftime("%H:%M")
    if "09:15" <= hm < "09:30":
        return {"state": "pre", "text": "集合竞价"}
    # 11:30 / 15:00 是收盘时点本身，不再算「交易中」；11:30 整点归入午间休市
    if "09:30" <= hm < "11:30" or "13:00" <= hm < "15:00":
        return {"state": "open", "text": "交易中"}
    if "11:30" <= hm < "13:00":
        return {"state": "break", "text": "午间休市"}
    if hm < "09:15":
        return {"state": "pre", "text": "未开盘"}
    return {"state": "closed", "text": "已收盘"}


def is_bad_name(name, exclude_st):
    if not exclude_st:
        return False
    up = name.upper()
    if "ST" in up or "退" in name:
        return True
    return False


def scan(params):
    t0 = time.time()
    p = dict(DEFAULTS)
    for k, v in (params or {}).items():
        if k in p and v is not None and v != "":
            p[k] = coerce_param(DEFAULTS[k], v)

    snap_pack = fetch_snapshot()
    snap = snap_pack["rows"]
    source = snap_pack["source"]
    env = market_env(snap)
    if not snap:
        return {"ok": False, "mode": "scan", "error": "行情源暂不可用，且没有可用的历史快照，请稍后重试",
                "source": "none", "stale": True,
                "degraded_fields": snap_pack.get("degraded_fields", []),
                "data_date": snap_pack.get("data_date") or "",
                "market": market_status(), "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
    # v1.6.4 一夜持股硬门槛基线：上证指数分时。缓存 60 秒全票复用，不额外增加
    # 每只票的联网开销。取不到时不立即整批空——交给 work/gate 逐票判定后，与
    # 个股分时缺失一起走「过半→整批空 + 明确提醒」的统一口径（单次抖动不误杀）。
    idx_tr = None
    try:
        _idx = fetch_trends("000001", 1, ttl=60)
        if _idx and len(_idx.get("points") or []) >= 20:
            idx_tr = _idx
    except Exception:
        idx_tr = None

    funnel = []

    def stage(name, items, note):
        funnel.append({"name": name, "count": len(items), "note": note})
        return items

    stage("全市场主板", snap, "沪深主板全部个股")

    # ① 涨幅 + 基础剔除
    s1 = []
    for q in snap:
        if q["volume"] <= 0:
            continue
        if is_bad_name(q["name"], p["exclude_st"]):
            continue
        if bad_symbol(q["name"]) or not list_days_ok(q.get("list_date")):
            continue
        if not (p["gain_min"] <= q["pct"] <= p["gain_max"]):
            continue
        s1.append(q)
    stage(f"① 涨幅 {p['gain_min']}~{p['gain_max']}%", s1,
          f"{p['gain_min']}% ~ {p['gain_max']}%，剔除ST/停牌")

    # ② 量比（备用源缺该字段时，用腾讯批量行情补齐）
    if any(not q["volratio"] for q in s1):
        s1, filled = enrich_volratio(s1)
        if filled:
            source += "+腾讯"
    s2 = [q for q in s1 if q["volratio"] >= p["vr_min"]]
    stage(f"② 量比 ≥ {p['vr_min']}", s2, "资金活跃度达标")

    # ③ 换手率
    s3 = [q for q in s2 if p["turn_min"] <= q["turnover"] <= p["turn_max"]]
    stage(f"③ 换手 {p['turn_min']}~{p['turn_max']}%", s3, "有资金关注但不过热")

    # ④ 流通市值
    s4 = [q for q in s3 if p["fcap_min"] <= q["fcap"] / 1e8 <= p["fcap_max"]]
    stage(f"④ 流通市值 {p['fcap_min']}~{p['fcap_max']}亿", s4, "盘子适中的弹性标的")

    # 成交额下限（可选，提升可执行性）
    s5 = [q for q in s4 if q["amount"] / 1e8 >= p["min_amount"]]
    if p["min_amount"] > 0:
        stage("⑤ 成交额达标", s5, f"≥ {p['min_amount']} 亿，避免冲击成本")

    # 多因子预排序：不再让量比单项截断掉涨幅/换手/资金更均衡的票
    cand = sorted(s5, key=lambda x: -prescore_snapshot(x, p))[: int(p["deep_limit"])]

    # 深度分析
    results, errors = [], 0
    gate_stats = {"分时数据缺失": 0, "分时未站上均价线（需现价在上方且≥85%时间站上）": 0,
                  "上证分时数据缺失": 0, "分时未跑赢上证（需≥85%时间强于上证）": 0}

    def work(q):
        try:
            bars = fetch_kline(q["code"], q["market"], 160, q.get("name", ""))
            if len(bars) < 25:
                return None
            # v1.6.4：分时数据缺失不再降级中性分——与跑赢上证一样属硬门槛，
            # 缺失/不达标都不入选（gate.block 给出原因）。
            try:
                tr = fetch_trends(q["code"], q["market"])
            except Exception:
                tr = None
            ev = evaluate(q, bars, tr, p, idx_tr)
            block = (ev.get("gate") or {}).get("block")
            if block:                       # 硬门槛拦截，不进 final
                return ("__gate__", block)
            # 复用刚抓到的日K算真实止盈止损，不新增请求
            return {**q, **ev, "trade": trade_plan("night", bars, q.get("price"))}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=_wn(10)) as ex:
        for r in ex.map(work, cand):
            if isinstance(r, tuple) and r and r[0] == "__gate__":
                gate_stats[r[1]] = gate_stats.get(r[1], 0) + 1
            elif r is None:
                errors += 1
            else:
                results.append(r)

    # 数据大面积缺失（过半候选因分时/上证缺失被拦）：数据源故障，整批空 + 明确提醒，
    # 而不是给一个空榜让人以为是「今天没票」。单次抖动只影响个别票，不触发整批。
    miss_stock = gate_stats.get("分时数据缺失", 0)
    miss_idx = gate_stats.get("上证分时数据缺失", 0)
    if cand and (miss_stock + miss_idx) > len(cand) / 2:
        detail = []
        if miss_idx:
            detail.append(f"上证分时缺失影响 {miss_idx}/{len(cand)} 只")
        if miss_stock:
            detail.append(f"个股分时缺失 {miss_stock}/{len(cand)} 只")
        return {"ok": False, "mode": "scan",
                "error": "分时数据源大面积不可用（" + "、".join(detail) +
                         "），无法执行「分时站均价线 / 跑赢上证」硬门槛，本次不入选任何票，请稍后重试",
                "source": source, "stale": True,
                "degraded_fields": snap_pack.get("degraded_fields", []),
                "data_date": snap_pack.get("data_date") or "",
                "market": market_status(), "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
                "funnel": funnel, "list": [], "total_scanned": len(snap),
                "deep_count": len(cand), "errors": errors,
                "gate_stats": gate_stats, "elapsed": round(time.time() - t0, 1)}

    results.sort(key=lambda x: -x["score"])
    final = [r for r in results if r["score"] >= p["score_min"]]
    stage("⑥⑦⑧ 深度筛选", final,
          f"量能 + 均线 + 分时 + 跑赢上证 + 尾盘；硬门槛拦下 "
          f"{sum(gate_stats.values())} 只（分时弱势 {gate_stats['分时未站上均价线（需现价在上方且≥85%时间站上）']} / "
          f"未跑赢上证 {gate_stats['分时未跑赢上证（需≥85%时间强于上证）']} / 数据缺失 {gate_stats['分时数据缺失'] + gate_stats['上证分时数据缺失']}）")

    # 精简返回字段（v1.6.5：统一用 final，list 与 funnel/评级统计口径一致，
    # 不再返回 score<score_min 的未达标票；前端 filtered() 二次过滤仍保留作防御）
    slim = []
    for r in final:
        slim.append({
            "code": r["code"], "name": r["name"], "market": r["market"], "mode": "scan",
            "price": r["price"], "pct": round(r["pct"], 2),
            "volratio": round(r["volratio"], 2), "turnover": round(r["turnover"], 2),
            "fcap": round(r["fcap"] / 1e8, 1), "amount": round(r["amount"] / 1e8, 2),
            "main_net": (None if "main_net" in (r.get("_miss") or []) else round((r.get("main_net") or 0) / 1e8, 2)),
            "industry": r["industry"], "high": r["high"], "open": r["open"],
            "prev_close": r["prev_close"],
            "score": r["score"], "grade": r["grade"], "tags": r["tags"],
            "rules": r["rules"],
            "metrics": r.get("metrics"),          # 详情页技术指标区（与多日持股同口径）
            "trade": r.get("trade"), "reasons": night_reasons(r),
            "buy_ok": not (r.get("high") == r.get("open") and r.get("pct", 0) >= 9.8),
        })

    return {
        "ok": True,
        "source": source, **snap_meta(snap_pack),
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market_status(),
        "env": env,
        "params": p,
        "funnel": funnel,
        "gate_stats": gate_stats,
        "list": slim,
        "total_scanned": len(snap),
        "deep_count": len(cand),
        "errors": errors,
        "elapsed": round(time.time() - t0, 1),
        "grade_s": sum(1 for x in slim if x["grade"] == "S" and x["score"] >= p["score_min"]),
        "grade_a": sum(1 for x in slim if x["grade"] == "A" and x["score"] >= p["score_min"]),
        "grade_b": sum(1 for x in slim if x["grade"] == "B" and x["score"] >= p["score_min"]),
        "grade_c": sum(1 for x in slim if x["grade"] == "C" and x["score"] >= p["score_min"]),
        "grade_d": sum(1 for x in slim if x["grade"] == "D" and x["score"] >= p["score_min"]),
    }


# ---------------------------------------------------------------- 历史 / 自选
# 复盘清单同一天共用一份文件（尤其 add_pick），读-改-写必须串行 + 原子替换，
# 否则自动重扫期间连点「＋」或多页面同时操作会互相覆盖，只剩最后一次写入。
_hist_lock = threading.Lock()
_settings_lock = threading.Lock()


def _write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)      # 原子替换，避免中途断电 / 并发读到半截文件


def _save_min(payload):
    """保存门槛跟用户当前设置走，不再硬编码 60 分。"""
    try:
        v = float(payload.get("score_min", (payload.get("params") or {}).get("score_min", 60)))
        return max(0, min(100, v))
    except Exception:
        return 60


def _gate_score(x):
    """入选门槛一律按原基础分判断，避免与「综合分」口径混用。
    卡片展示的 score 是叠加消息面 / 情绪面后的折算分，通常低于基础分；
    若用它跟基础分门槛比较，开启评分维度后保存 / 复盘会少股票甚至空列表。"""
    try:
        v = x.get("base_score")
        return float(x.get("score") if v is None else v)
    except Exception:
        return 0.0


def save_scan(payload):
    raw = payload.get("list")
    if not isinstance(raw, list) or not raw:
        # 行情全失败 / 请求体为空时，别在复盘页留下一堆 0 只的空记录
        return {"ok": False, "error": "本次没有可保存的标的"}
    sm = _save_min(payload)
    items = [{
        "code": x.get("code"), "name": x.get("name"), "market": x.get("market"),
        "buy": x.get("price"), "score": x.get("score"), "grade": x.get("grade"),
        "pct": x.get("pct"), "tags": x.get("tags"),
        "buy_ok": x.get("buy_ok", True),
        "mode": x.get("mode") or "scan",   # 复盘按来源板块分组要用，不能丢
        "stop": (x.get("trade") or {}).get("stop"),
        "target": (x.get("trade") or {}).get("target"),
    } for x in raw if isinstance(x, dict) and _gate_score(x) >= sm]
    if not items:
        return {"ok": False, "error": "没有达到评分门槛的标的，未生成空记录"}
    ts = now_cn()
    body = {
        "saved_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": payload.get("ts"),
        "market": payload.get("market"),
        "params": payload.get("params"),
        "items": items,
    }
    with _hist_lock:
        os.makedirs(HIST_DIR, exist_ok=True)
        fn = f"{ts.strftime('%Y-%m-%d_%H%M%S')}.json"
        path = os.path.join(HIST_DIR, fn)
        if os.path.exists(path):          # 同秒连点两次会互相覆盖，补毫秒后缀
            fn = f"{ts.strftime('%Y-%m-%d_%H%M%S')}_{ts.microsecond // 1000:03d}.json"
            path = os.path.join(HIST_DIR, fn)
        _write_json_atomic(path, body)
    return {"ok": True, "file": fn, "count": len(items)}


def add_pick(payload):
    """详情页「＋」：把单只个股加进当天的复盘清单。

    与「保存本次选股」的区别：不设评分门槛、一只也能建单，同一天共用一份文件，
    重复加入只更新价格与评分，不会出现两条一样的票。
    整段读-改-写用写锁串行 + 原子替换，避免连点 / 多页面并发只剩最后一只。"""
    ts = now_cn()
    day = ts.strftime("%Y-%m-%d")
    code = str(payload.get("code") or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "error": "股票代码不合法"}

    try:
        price = float(payload.get("price") or 0)
    except Exception:
        price = 0.0
    try:
        score = float(payload.get("score") or 0)
    except Exception:
        score = 0.0
    try:
        market = int(payload.get("market"))
    except (TypeError, ValueError):
        market = 1 if code.startswith("6") else 0
    item = {
        "code": code,
        "name": str(payload.get("name") or code),
        "market": market,
        "buy": round(price, 2),
        "score": round(score, 1),
        "grade": str(payload.get("grade") or ""),
        "pct": payload.get("pct") or 0,
        "tags": [t for t in (payload.get("tags") or []) if isinstance(t, str)][:4],
        "mode": str(payload.get("mode") or "scan"),
        "buy_ok": bool(payload.get("buy_ok", True)),
        "stop": (payload.get("trade") or {}).get("stop"),
        "target": (payload.get("trade") or {}).get("target"),
    }
    with _hist_lock:
        os.makedirs(HIST_DIR, exist_ok=True)
        fn = f"manual_{day}.json"
        path = os.path.join(HIST_DIR, fn)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    body = json.load(f)
            except Exception:
                body = {}
        else:
            body = {}
        if (not isinstance(body, dict) or not isinstance(body.get("items"), list)
                or not body["items"]):
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
            body = {"saved_at": stamp, "ts": stamp, "market": market,
                    "params": {"src": "手动加入"}, "items": []}

        idx = next((i for i, x in enumerate(body["items"])
                    if isinstance(x, dict) and x.get("code") == code), None)
        dup = idx is not None
        if dup:
            body["items"][idx] = {**body["items"][idx], **item}
        else:
            body["items"].append(item)
        body["updated_at"] = ts.strftime("%Y-%m-%d %H:%M:%S")
        _write_json_atomic(path, body)
        count = len(body["items"])
    return {"ok": True, "file": fn, "count": count, "dup": dup}


def delete_scans(files):
    """多选删除整条选股记录；与 add_pick / save_scan 共用 _hist_lock，
       否则并发的 add_pick 会把刚删掉的文件重新写回来（删除被静默撤销）。"""
    removed = []
    with _hist_lock:
        for fn in files or []:
            name = os.path.basename(str(fn))
            if not name.endswith(".json"):
                continue
            p = os.path.join(HIST_DIR, name)
            try:
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(name)
            except Exception:
                continue
    return {"ok": True, "removed": removed, "count": len(removed)}


def remove_scan_items(fn, codes):
    """多选删除某条记录里的个股；删空则整条记录一起删掉。
       整段读-改-写与删除都在 _hist_lock 内：避免与 add_pick 丢失更新，
       也避开 Windows 上读句柄未关时 os.replace 抛 WinError 5。"""
    name = os.path.basename(str(fn))
    p = os.path.join(HIST_DIR, name)
    with _hist_lock:
        if not name.endswith(".json") or not os.path.exists(p):
            return {"ok": False, "error": "记录不存在"}
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("items"), list):
                raise ValueError("复盘记录格式不正确")
        except Exception as e:
            return {"ok": False, "error": f"读取失败：{e}"}
        drop = {str(c) for c in (codes or [])}
        before = d["items"]
        left = [x for x in before if isinstance(x, dict) and str(x.get("code")) not in drop]
        if not left:
            try:
                os.remove(p)
            except Exception:
                pass
            return {"ok": True, "removed": len(before) - len(left), "left": 0, "file": None}
        d["items"] = left
        _write_json_atomic(p, d)
        return {"ok": True, "removed": len(before) - len(left), "left": len(left), "file": name}


def list_scans():
    out = []
    if not os.path.isdir(HIST_DIR):
        return out
    for fn in sorted(os.listdir(HIST_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(HIST_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                continue
            items = [i for i in (d.get("items") or []) if isinstance(i, dict)]
            scores = []
            for i in items:
                # 旧版本数据 / 手工编辑过的文件可能缺 score，跳过坏的 item，
                # 但不能让一条缺字段就把整份记录从列表里吞掉。
                try:
                    v = float(i.get("score"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    scores.append(v)
            p = d.get("params")
            src = (p.get("src") or "") if isinstance(p, dict) else ""
            out.append({"file": fn, "saved_at": d.get("saved_at"), "src": src,
                        "count": len(items),
                        "avg_score": round(statistics.fmean(scores), 1) if scores else 0})
        except Exception:
            continue
    # 按「保存时间」倒序，手动加入的清单和整批保存的清单才会排在一起
    out.sort(key=lambda x: (x.get("saved_at") or "", x["file"]), reverse=True)
    return out


def review_scan(fn):
    """用日K结算：扫描日之后第一个交易日的开盘/最高/收盘相对买入价的表现"""
    path = os.path.join(HIST_DIR, os.path.basename(str(fn)))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise ValueError("复盘记录格式不正确")
    except Exception as e:
        return {"ok": False, "error": f"读取复盘记录失败：{e}"}
    items = d["items"]
    scan_day = (d.get("ts") or d.get("saved_at") or "")[:10]

    rows = []

    def work(it):
        try:
            bars = fetch_kline(it["code"], it["market"], 160, it.get("name", ""))
            idx = next((i for i, b in enumerate(bars) if b["date"] == scan_day), None)
            if idx is None:
                # 周末 / 节假日 / 盘前手动加入的清单，扫描日当天没有K线：
                # 退到扫描日之前最后一根，结算它的下一根，否则这些记录永远停在「待次日数据」
                cand = [i for i, b in enumerate(bars) if b["date"] <= scan_day]
                idx = cand[-1] if cand else None
            if idx is None or idx + 1 >= len(bars):
                return {**it, "status": "待次日数据", "open_pct": None,
                        "high_pct": None, "low_pct": None, "close_pct": None}
            nxt = bars[idx + 1]
            buy_ok = it.get("buy_ok", True)
            # 涨停/跌停价是挂单价，实际买不到；改用扫描日收盘价估算可实现收益
            buy = (it["buy"] if buy_ok else bars[idx]["close"]) or bars[idx]["close"]
            price_note = "" if buy_ok else "挂单价不可成交，按当日收盘价估算"
            gap = 0
            try:
                gap = (datetime.strptime(nxt["date"], "%Y-%m-%d").date()
                       - datetime.strptime(bars[idx]["date"], "%Y-%m-%d").date()).days
            except Exception:
                pass
            low_pct = round((nxt["low"] / buy - 1) * 100, 2)
            # 用卡片给出的真实 ATR 止损位判断，不能再统一按 -3% 硬编码
            _stop = it.get("stop") or (it.get("trade") or {}).get("stop")
            stop_line = round((_stop / buy - 1) * 100, 2) if (_stop and buy) else -3.0
            return {
                **it,
                "status": "已结算" + ("（跨休市/停牌）" if gap > 4 else ""),
                "next_date": nxt["date"],
                "buy_ref": round(buy, 2),
                "buy_ok": buy_ok,
                "price_note": price_note,
                "open_pct": round((nxt["open"] / buy - 1) * 100, 2),
                "high_pct": round((nxt["high"] / buy - 1) * 100, 2),
                "low_pct": low_pct,
                "close_pct": round((nxt["close"] / buy - 1) * 100, 2),
                "stop_hit": low_pct <= stop_line,
                "stop_line": stop_line,           # 实际止损线（缺失才回退 -3% 参考值）
            }
        except Exception:
            return {**it, "status": "获取失败", "open_pct": None,
                    "high_pct": None, "low_pct": None, "close_pct": None}

    with ThreadPoolExecutor(max_workers=_w(8)) as ex:
        rows = list(ex.map(work, items))

    done = [r for r in rows if str(r.get("status") or "").startswith("已结算")]
    summary = {
        "count": len(rows),
        "settled": len(done),
        "win": sum(1 for r in done if (r.get("close_pct") or 0) > 0),
        "avg_close": round(statistics.fmean([r["close_pct"] for r in done]), 2) if done else 0,
        "avg_high": round(statistics.fmean([r["high_pct"] for r in done]), 2) if done else 0,
        "avg_low": round(statistics.fmean([r["low_pct"] for r in done]), 2) if done else 0,
        "avg_open": round(statistics.fmean([r["open_pct"] for r in done]), 2) if done else 0,
    }
    summary["stop_hits"] = sum(1 for r in done if r.get("stop_hit"))
    # 一夜持股纪律是「次日 9:30–10:00 冲高即走」，补一个次日开盘口径，
    # 收盘口径保留作保守参考，两种都明示，不再混为一谈。
    early = [r for r in done if r.get("open_pct") is not None]
    summary["early_win"] = sum(1 for r in early if (r.get("open_pct") or 0) > 0)
    summary["early_win_rate"] = (round(summary["early_win"] / len(early) * 100, 1)
                                 if early else 0)
    summary["early_avg_open"] = (round(statistics.fmean([r["open_pct"] for r in early]), 2)
                                 if early else 0)
    summary["win_rate"] = round(summary["win"] / summary["settled"] * 100, 1) if summary["settled"] else 0
    summary["note"] = ("收盘胜率 = 次日收盘价高于买入价（保守口径）；早盘胜率 = 次日开盘价高于买入价，"
                       "更贴近『次日 9:30–10:00 冲高即走』的纪律；次日最高/最低为盘中理论极值，"
                       "不是可成交价；stop_hit 按卡片给出的真实 ATR 止损位判断，缺失才回退 -3%。")
    # 隔夜 / 打板 / 反核的胜率差异极大，混在一起的胜率参考意义有限
    by_mode = {}
    for r in done:
        k = str(r.get("mode") or "scan")
        g = by_mode.setdefault(k, {"count": 0, "win": 0, "_sum": 0.0})
        g["count"] += 1
        g["win"] += 1 if (r.get("close_pct") or 0) > 0 else 0
        g["_sum"] += r.get("close_pct") or 0
    for g in by_mode.values():
        g["win_rate"] = round(g["win"] / g["count"] * 100, 1) if g["count"] else 0
        g["avg_close"] = round(g.pop("_sum") / g["count"], 2) if g["count"] else 0
    summary["by_mode"] = by_mode
    return {"ok": True, "file": fn, "scan_day": scan_day, "rows": rows, "summary": summary}


def load_watch():
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_watch(items):
    """与复盘清单共用一个写锁 + 原子替换；直接 open("w") 在并发下会互相覆盖，
       Windows 上还会抛 PermissionError 变成 500，写入中途失败更会截成半截 JSON。"""
    items = items if isinstance(items, list) else []
    with _hist_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_json_atomic(WATCH_FILE, items)
    return {"ok": True, "count": len(items)}


# ---------------------------------------------------------------- v1.5.8 消息面 / 情绪面
# 两套作用域：「选项 → 筛选」里的开关(common)只管除一夜持股外的 3 个板块（显示层）；
# 一夜持股板块内的开关(night)只管一夜持股。作用域表保证每个板块只读一个开关。
DIM_SCOPE = {"night": "night", "swing": "common", "ai": "common",
             "zt": "common", "dt": "common"}
# (基础分占比, 消息面权重, 情绪面权重) —— 逐板块差异化，三项合计 100
DIM_WEIGHTS = {
    "night": (84, 6, 10),
    "swing": (86, 10, 4),
    "ai":    (84, 8, 8),
    "zt":    (84, 5, 11),
    "dt":    (84, 9, 7),
}
DIM_DEFAULT = {"common": True, "night": False}
# v1.5.22 五档评级：(S线, A线, B线, C线)，低于 C 线为 D。
# 用户口径：S≥85（金）、A≥75（红，推荐买入）、B≥66（绿）、C≥58（蓝）、D<58（灰）；
# 一夜持股按自身更严尺度设线，涨停/跌停 C 线沿用原 56。S/A 均判「推荐买入」。
DIM_GRADE = {"night": (85, 76, 68, 60), "ai": (85, 75, 66, 58),
             "swing": (85, 75, 66, 58), "zt": (85, 75, 66, 56), "dt": (85, 75, 66, 56)}
GRADE_BOARD_DEFAULT = (85, 75, 66, 58)
API_BOARD = {"/api/scan": "night", "/api/ai_pick": "ai",
             "/api/swing_pick": "swing", "/api/limit_up": "zt",
             "/api/limit_down": "dt"}

# 公告：60 秒增量轮询；五板块共用一份原始数据，但各自按自己的开关与权重折算。
ANN_TTL = 60
_ann_lock = threading.Lock()
_ann_fetch_lock = threading.Lock()   # 单飞：同一时刻只允许一个线程真正去抓公告
_ann = {"ts": 0.0, "ver": 0, "ids": set(), "by_code": {}, "total": 0,
        "new": 0, "src": "东财公告", "error": "", "updated": ""}
_ANN_RULES = (
    (("中标通知书", "中标", "重大合同", "重大订单", "签订"), 18, "利好·订单"),
    (("回购", "增持", "业绩预增", "预增", "重组", "收购"), 12, "利好·资本运作"),
    (("获批", "获得注册", "专利", "现金分红", "纳入医保", "医保目录", "纳入目录"), 7, "利好·经营"),
    (("立案", "问询函", "监管函", "警示函", "处罚", "刑事拘留", "被调查"), -16, "利空·监管"),
    (("减持", "质押", "业绩预亏", "预亏", "亏损", "诉讼", "退市风险", "终止"), -11, "利空·经营"),
)
_STOP_CH = set("股份有限公司中国")
_HOT_WORDS = ("算力", "机器人", "固态电池", "人工智能", "芯片", "光伏", "军工",
              "影视", "传媒", "数据", "储能", "低空", "华为", "小米", "医药")


def _cjk(ch):
    return '\u4e00' <= ch <= '\u9fff'


def _ann_kind(title):
    t = str(title or "")
    for kws, delta, kind in _ANN_RULES:
        if any(k in t for k in kws):
            return delta, kind
    return 0, "中性·例行"


def fetch_announcements(force=False):
    """全市场最近公告，60s 增量缓存；失败保留上一份并标注，绝不抛异常拖垮扫描。

    TTL 判断不能带 "缓存非空" 条件：接口返回空列表或首次抓取失败时，
    否则一次扫描里每只股票都会各自重试一次网络请求（20 只 = 20 次 8 秒超时），
    把整个扫描拖死。再加单飞锁，避免并发评分同时打同一个接口。"""
    with _ann_lock:
        if not force and time.time() - _ann["ts"] < ANN_TTL:
            return
    with _ann_fetch_lock:
        # 排队等锁的线程要重新判断，别重复抓
        with _ann_lock:
            if not force and time.time() - _ann["ts"] < ANN_TTL:
                return
        now = time.time()
        # 分页抓取最近 5 页（约 500 条）：A 股每日公告常达数百上千条，
        # 只取第 1 页 100 条会让绝大多数个股永远拿到「暂无公告」，消息面形同虚设。
        # 最多 5 页、遇空页即停，控制耗时（单页超时 6s，全失败也只 30s 且 60s 内不重试）。
        raw = []
        ann_err = ""
        for _page in range(1, 6):
            url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
                   f"sr=-1&page_size=100&page_index={_page}&ann_type=A&client_source=web&"
                   "stock_list=&f_node=0&s_node=0")
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    d = json.loads(r.read().decode("utf-8"))
                page_rows = ((d.get("data") or {}).get("list") or [])
                if not page_rows:
                    break
                raw.extend(page_rows)
            except Exception as _pe:
                ann_err = type(_pe).__name__
                if not raw:
                    break        # 第一页就失败：网络不可用，不再逐页重试浪费时间
                break            # 已抓到的页保留，不因后续页失败全盘丢弃
        try:
            by_code, ids = {}, set()
            for it in raw:
                aid = str(it.get("art_code") or "")
                if not aid:
                    continue
                ids.add(aid)
                title = str(it.get("title") or "")
                for sep in (":", "："):
                    if sep in title:
                        title = title.split(sep, 1)[1]
                        break
                date = str(it.get("notice_date") or "")[:10]
                delta, kind = _ann_kind(title)
                for c in (it.get("codes") or []):
                    code = str(c.get("stock_code") or "")
                    if code:
                        by_code.setdefault(code, []).append(
                            {"id": aid, "title": title, "date": date,
                             "kind": kind, "delta": delta})
            with _ann_lock:
                new = len(ids - _ann["ids"]) if _ann["ids"] else 0
                _ann.update({"ts": now, "ids": ids, "by_code": by_code,
                             "total": len(ids), "new": new, "error": ann_err,
                             "updated": now_cn().strftime("%Y-%m-%d %H:%M:%S")})
                if new:
                    _ann["ver"] += 1
                elif not _ann["ver"]:
                    _ann["ver"] = 1
        except Exception as e:
            with _ann_lock:
                _ann["ts"] = now      # 空结果 / 失败也推进时间戳，60s 内不重试
                _ann["error"] = type(e).__name__


def message_score(code, rows=None):
    """以公告原文为依据：基线 50 + Σ 事件分 × 时效系数，裁剪到 0~100。
    rows 可选：批量调用方预先取好的该股公告，避免每个代码重复加锁/联网。"""
    if rows is None:
        fetch_announcements()
        with _ann_lock:
            rows = list((_ann["by_code"] or {}).get(str(code), []))[:5]
    if not rows:
        return 50.0, "暂无近期公告 · 中性", []
    today = now_cn().date()
    total, used = 0.0, []
    for a in rows:
        try:
            dd = (today - datetime.strptime(a["date"], "%Y-%m-%d").date()).days
        except Exception:
            dd = 1
        k = 1.0 if dd <= 0 else 0.7 if dd <= 3 else 0.4 if dd <= 7 else 0.15
        delta = a.get("delta", 0)          # 缺字段的异常公告按中性处理，不让整批评分降级
        total += delta * k
        used.append({**a, "delta": delta, "eff": round(delta * k, 1)})
    score = clamp(50 + total)
    risks = ann_risk_flags(code, rows=rows)   # 复用已取到的公告，不再二次加锁
    if risks:
        score = clamp(score - 12)
    top = max(used, key=lambda u: abs(u["eff"]))
    if top.get("delta"):
        why = "%s《%s》%s" % (top["kind"], top["title"][:20], top["date"][5:])
    else:
        why = "近期均为例行公告 · 中性"
    if risks:
        why = "硬利空公告：" + "、".join(risks[:2])
    return round(score, 1), why, used[:5]


def _name_base():
    """全市场股票名称用字基线，一天缓存一次（名称变化极慢）。"""
    out = {"_n": 3000}
    try:
        rows = (fetch_snapshot() or {}).get("rows") or []
        for r in rows:
            for ch in set(str(r.get("name") or "")):
                if _cjk(ch) and ch not in _STOP_CH:
                    out[ch] = out.get(ch, 0) + 1
        out["_n"] = max(1, len(rows))
    except Exception:
        pass
    return out


# ------------------------------------------------------------ v1.5.14 情绪面谐音
# 「当炒字」不再要求字面相同：同音字（羊/阳/洋）算完整命中，
# 近音字（平翘舌、前后鼻音）算半分命中。读音表在构建时用 pypinyin 生成后固化，
# 运行期不依赖任何第三方拼音库，PC 的 PyInstaller 包与 iOS 内嵌解释器都能直接用。
# 音节 -> 同音字串（忽略声调，每字只按主读音归组）
_SYLLABLE_CHARS = {
    'a': '啊嗄锕阿',
    'ai': '哀哎唉嗌嗳埃嫒挨捱暧爱瑷癌皑矮砹碍艾蔼锿隘霭',
    'an': '俺埯安岸庵按揞暗案桉氨犴胺谙铵鞍鹌黯',
    'ang': '昂盎肮',
    'ao': '傲凹嗷坳奥媪岙廒懊拗敖澳熬獒翱聱螯袄遨鏊鏖骜鳌',
    'ba': '八叭吧坝岜巴扒把拔捌灞爸疤笆粑罢耙芭茇菝跋钯霸靶魃鲅',
    'bai': '佰拜捭掰摆擘柏白百稗败',
    'ban': '伴办半坂扮扳拌搬斑板版班瓣瘢癍绊舨般钣阪颁',
    'bang': '傍帮梆棒榜浜磅绑膀蒡蚌谤邦镑',
    'bao': '保勹包堡孢宝报抱暴煲爆胞苞葆薄褒褓豹趵雹饱鲍鸨龅',
    'bei': '倍北卑呗备孛悖悲惫杯焙狈碑碚背蓓被褙贝辈邶鐾钡陂鞴鹎',
    'ben': '坌奔本畚笨苯贲锛',
    'beng': '嘣崩泵甏甭绷蹦迸',
    'bi': '俾匕吡哔壁妣婢嬖币庇庳弊弼彼必愎敝比毕毖毙滗濞狴璧畀痹碧秕笔筚箅篦臂舭荜荸萆蓖蔽薜裨襞跸逼避鄙铋闭陛髀鼻',
    'bian': '便匾卞变弁忭扁汴煸砭碥窆笾缏编苄蝙褊贬辨辩辫边遍鞭鳊',
    'biao': '婊彪杓标灬瘭膘表裱镖镳飑飙飚骠髟鳔',
    'bie': '别憋瘪蹩鳖',
    'bin': '傧宾彬摈斌槟殡滨濒玢缤膑豳镔髌鬓',
    'bing': '丙兵冫冰并摒柄炳病禀秉邴饼',
    'bo': '亳伯剥勃博卜啵帛拨搏播檗波渤玻礴箔簸脖膊舶菠跛踣钵钹铂饽驳鹁',
    'bu': '不卟哺埠布怖捕晡步瓿簿补逋部醭钚钸',
    'ca': '嚓擦礤',
    'cai': '彩才材猜睬菜蔡裁财踩采',
    'can': '参孱惨惭掺残灿璨粲蚕餐骖黪',
    'cang': '仓伧沧舱苍藏',
    'cao': '嘈操曹槽漕糙艚艹草螬',
    'ce': '侧册厕恻测策',
    'cen': '岑涔',
    'ceng': '噌层曾蹭',
    'cha': '叉姹察岔差插搽杈查槎檫汊猹碴茬茶衩诧锸镲馇',
    'chai': '侪拆柴瘥虿豺钗',
    'chan': '产冁婵廛忏搀潺澶禅缠羼蒇蝉蟾觇谄谗躔铲镡阐颤馋骣',
    'chang': '伥倡偿厂唱场娼嫦尝常徜怅惝敞昌昶氅猖畅肠苌菖阊鬯鲳',
    'chao': '吵嘲巢怊抄晁朝潮炒焯耖超钞',
    'che': '坼屮彻扯掣撤澈砗车',
    'chen': '嗔宸尘忱抻晨榇沉琛碜臣衬谌谶趁辰郴陈龀',
    'cheng': '丞乘呈城埕塍惩成承撑晟枨柽橙澄瞠秤称程蛏裎诚逞酲铖骋',
    'chi': '侈傺叱吃哧啻嗤坻墀媸尺弛彳持敕斥池炽痴瘛眵笞篪翅耻茌蚩螭褫赤踟迟饬驰魑鸱齿',
    'chong': '充冲宠崇忡憧舂艟茺虫铳',
    'chou': '丑仇俦帱惆愁抽畴瘳瞅稠筹绸臭踌酬雠',
    'chu': '亍储出刍初厨处怵憷搐杵楚楮樗橱滁畜矗础绌蜍褚触蹰躇锄除雏黜',
    'chuai': '啜嘬揣搋膪踹',
    'chuan': '串传喘巛川椽氚穿舛舡船遄钏',
    'chuang': '创幢床怆疮窗闯',
    'chui': '吹垂捶棰椎槌炊锤陲',
    'chun': '唇春椿淳纯莼蝽蠢醇鹑',
    'chuo': '戳绰踔辍辶龊',
    'ci': '伺刺呲慈次此瓷疵磁祠糍茈茨词赐辞雌鹚',
    'cong': '丛从匆囱枞淙琮璁聪苁葱骢',
    'cou': '凑腠辏',
    'cu': '促徂殂猝簇粗蔟蹙蹴酢醋',
    'cuan': '撺汆爨窜篡蹿镩',
    'cui': '催啐崔悴摧榱毳淬璀瘁粹翠脆萃',
    'cun': '存寸忖村皴',
    'cuo': '厝嵯挫措搓撮痤矬磋脞蹉锉错鹾',
    'da': '哒嗒大妲怛打搭沓瘩笪答耷褡达靼鞑',
    'dai': '代傣呆呔埭岱带待怠戴歹殆玳甙绐袋贷迨逮骀黛',
    'dan': '丹但儋单啖弹惮担掸旦殚氮淡澹疸瘅眈箪耽聃胆萏蛋诞赕郸',
    'dang': '党凼宕当挡档砀荡菪裆谠铛',
    'dao': '倒刀刂到叨导岛忉悼捣氘焘盗祷稻纛蹈道',
    'de': '得德的锝',
    'deng': '凳噔嶝戥灯登瞪磴等簦蹬邓镫',
    'di': '低嘀地堤娣嫡帝底弟抵敌柢棣氐涤滴狄睇砥碲笛第籴缔羝翟荻蒂觌诋谛迪递邸镝骶',
    'dian': '佃典坫垫奠巅店惦掂殿淀滇点玷电甸癜癫碘簟踮钿阽靛颠',
    'diao': '凋刁叼吊掉碉调貂钓铞铫雕鲷',
    'die': '叠喋嗲垤堞揲爹牒瓞碟耋蝶谍跌蹀迭鲽',
    'ding': '丁仃叮啶定玎疔盯碇耵腚订酊钉铤锭顶鼎',
    'diu': '丢铥',
    'dong': '东侗冬冻动咚垌岽峒恫懂栋氡洞硐胨胴董鸫',
    'dou': '兜抖斗痘窦篼蔸蚪豆逗都陡',
    'du': '嘟堵妒度杜椟毒渎渡牍犊独督睹碡笃肚芏蠹读赌镀髑黩',
    'duan': '断椴段煅短端簖缎锻',
    'dui': '兑堆对怼憝碓镦队',
    'dun': '吨囤墩敦沌炖盹盾砘礅趸蹲遁钝顿',
    'duo': '剁咄哆哚垛堕多夺惰掇朵柁缍舵裰跺踱躲铎',
    'e': '俄厄呃噩垩娥婀屙峨恶愕扼腭苊莪萼蛾讹谔轭遏鄂锇锷阏颚额饿鳄鹅鹗',
    'ei': '诶',
    'en': '恩摁蒽',
    'er': '二佴儿尔洱珥而耳贰迩铒饵鲕鸸',
    'fa': '乏伐发垡法珐砝筏罚阀',
    'fan': '凡反帆幡梵樊泛烦燔犯畈番矾繁翻范蕃藩蘩贩蹯返钒饭',
    'fang': '仿匚坊妨房放方枋纺肪舫芳访邡钫防鲂',
    'fei': '匪吠啡妃废悱扉斐榧沸淝狒痱篚绯翡肥肺腓芾菲蜚诽费镄霏非飞鲱',
    'fen': '份偾分吩坟奋忿愤棼氛汾瀵焚粉粪纷芬酚鲼鼢',
    'feng': '丰俸冯凤唪奉封峰枫沣烽疯砜缝葑蜂讽逢酆锋风',
    'fou': '否缶',
    'fu': '付伏佛俘俯傅凫副匐呋咐复夫妇孚孵富幅幞府弗怫扶抚拂拊敷斧服桴氟浮涪滏父甫砩祓福稃符绂绋缚罘肤腐腑腹艴芙苻茯莩菔蚨蜉蝠蝮袱覆讣负赋赙赴趺跗辅辐郛釜阜阝附馥驸鲋鳆麸黻黼',
    'ga': '伽呷嘎噶尕尜尬旮钆',
    'gai': '丐垓戤改概溉盖该赅钙陔',
    'gan': '坩尴干感擀敢旰杆柑橄泔淦澉甘疳矸秆竿绀肝苷赣赶酐',
    'gang': '冈刚岗戆杠港筻纲缸罡肛钢',
    'gao': '告搞杲槁槔皋睾稿篙糕缟羔膏藁诰郜锆镐高',
    'ge': '个仡割各咯哥哿嗝圪塥戈搁搿格歌疙硌纥胳膈舸葛虼袼铬镉阁隔革骼鬲鸽',
    'gei': '给',
    'gen': '亘哏根艮茛跟',
    'geng': '哽埂庚更梗绠羹耕耿赓鲠',
    'gong': '供公共功宫工巩廾弓恭拱攻汞珙肱蚣觥贡躬龚',
    'gou': '佝勾垢够媾岣彀构枸沟狗笱篝缑苟觏诟购遘钩鞲',
    'gu': '估古呱咕嘏固姑孤崮故梏毂汩沽牯牿痼瞽箍罟股臌菇菰蛄蛊觚诂谷轱辜酤钴锢雇顾骨鲴鸪鹄鹘鼓',
    'gua': '刮剐卦寡挂栝瓜聒胍褂诖鸹',
    'guai': '乖怪拐掴',
    'guan': '倌关冠官惯掼棺涫灌盥管罐莞观贯馆鳏鹳',
    'guang': '光咣广桄犷胱逛',
    'gui': '傀刽刿匦圭妫宄庋归晷柜桂桧炔瑰癸皈硅簋规诡贵跪轨闺鬼鲑鳜龟',
    'gun': '丨棍滚磙绲衮辊鲧',
    'guo': '呙国埚崞帼果椁猓虢蜾蝈裹过郭锅馘',
    'ha': '哈蛤铪',
    'hai': '亥嗨孩害氦海胲还醢骇骸',
    'han': '函含喊寒悍憨憾捍撖撼旱晗汉汗涵瀚焊焓罕翰菡蚶邗邯酣阚韩顸颔鼾',
    'hang': '夯杭沆珩绗航颃',
    'hao': '号嗥嚆嚎壕好昊毫浩濠灏皓耗蒿薅蚝豪貉郝颢',
    'he': '何劾合呵和喝嗬壑曷核河涸盍盒禾翮荷菏蚵褐诃贺赫阂阖颌鹤',
    'hei': '嘿黑',
    'hen': '很恨狠痕',
    'heng': '亨哼恒桁横蘅衡',
    'hong': '哄宏弘泓洪烘红荭蕻薨虹訇讧轰闳鸿黉',
    'hou': '侯候厚后吼喉堠後猴瘊篌糇逅骺鲎',
    'hu': '乎互冱呼唬唿囫壶岵弧忽怙惚户戽扈护斛槲沪浒湖滹烀煳狐猢琥瑚瓠祜笏糊胡葫虍虎蝴觳轷醐鹕鹱',
    'hua': '划化华哗桦滑猾画花话铧骅',
    'huai': '坏徊怀槐淮踝',
    'huan': '唤圜奂宦寰幻患换擐桓欢洹浣涣漶焕獾环痪缓缳萑豢逭郇锾鬟鲩',
    'huang': '凰幌徨恍惶慌晃湟潢煌璜癀皇磺篁簧肓荒蝗蟥谎遑隍鳇黄',
    'hui': '会卉咴哕喙回彗徽恚恢悔惠慧挥晖晦毁汇洄浍灰烩珲秽绘缋茴荟蕙虺蛔蟪讳诙诲贿辉隳麾',
    'hun': '婚昏浑混溷荤诨阍馄魂',
    'huo': '伙劐嚯夥惑或攉活火砉祸耠获藿蠖豁货钬锪镬霍',
    'ji': '丌乩亟伎佶偈冀几击剂剞即及叽吉咭哜唧圾基墼妓姬嫉季寂寄屐岌嵇嵴己彐忌急悸戟戢技挤掎既暨机极棘楫殛汲洎济激犄玑畸畿疾瘠矶祭积稷稽笄笈箕籍级纪继绩缉羁肌脊芨芰荠蒺蓟蕺藉虮觊计讥记诘赍跻跽辑迹际集霁饥骥髻鲚鲫鸡麂齑',
    'jia': '价佳假加嘉夹嫁家岬恝戛架枷浃珈甲痂瘕稼笳胛茄荚葭蛱袈袷贾跏迦郏钾铗镓颊驾',
    'jian': '件俭健僭兼减剑剪囝坚奸尖建戋戬拣捡搛枧柬检楗歼毽涧渐湔溅煎牮犍监睑硷碱笕笺简箭缄缣翦肩腱舰艰茧荐菅蒹裥见謇谏谫贱趼践踺蹇鉴锏键间鞯饯鲣鹣',
    'jiang': '僵匠奖姜将桨江洚浆犟疆礓糨绛缰耩茳蒋讲豇酱降',
    'jiao': '交佼侥僬剿叫噍姣娇峤徼挢搅教敫椒浇湫焦狡皎矫礁窖绞缴胶脚艽茭蕉蛟角跤轿较郊酵醮铰饺骄鲛鹪',
    'jie': '介借劫卩喈嗟姐婕孑届戒截拮捷接揭杰桀洁界疖疥皆睫碣秸竭结羯节芥蚧街解讦诫阶颉骱鲒',
    'jin': '仅今劲卺噤堇妗尽巾廑斤晋槿津浸烬瑾矜禁筋紧缙荩衿襟觐谨赆近进金钅锦靳馑',
    'jing': '井京儆兢净刭境婧弪径惊憬敬旌景晶泾獍痉睛竞竟粳精经肼胫腈茎荆菁警迳镜阱靓靖静颈鲸',
    'jiong': '冂扃炅炯窘迥',
    'jiu': '久九僦厩咎啾就揪救旧柩桕灸玖疚究纠臼舅赳酒阄韭鬏鸠鹫',
    'ju': '举俱倨具剧句咀局居屦巨惧拒拘据掬桔椐榉榘橘沮炬犋狙琚疽矩窭聚苣苴莒菊菹裾讵趄距踞踽遽醵钜锔锯雎鞠鞫飓驹龃',
    'juan': '倦卷娟捐桊涓狷眷绢蠲鄄锩镌隽鹃',
    'jue': '倔决劂厥噘噱嚼孓崛抉掘撅攫桷橛爝爵獗珏矍绝蕨觉觖诀谲蹶镢',
    'jun': '俊军君均峻捃浚皲竣菌郡钧骏麇',
    'ka': '佧卡咔咖喀胩',
    'kai': '凯剀垲开忾恺慨揩楷蒈铠锎锴',
    'kan': '侃刊勘坎堪戡槛看瞰砍莰龛',
    'kang': '亢伉康慷扛抗炕糠钪闶',
    'kao': '尻拷栲烤犒考铐靠',
    'ke': '克刻可咳嗑坷壳客岢恪柯棵氪渴溘珂疴瞌磕科稞窠缂苛蝌课轲钶锞颏颗骒髁',
    'ken': '啃垦恳肯裉龈',
    'keng': '吭坑铿',
    'kong': '倥孔崆恐控空箜',
    'kou': '口叩寇扣抠眍筘芤蔻',
    'ku': '刳哭喾堀库枯窟绔苦裤酷骷',
    'kua': '侉垮夸挎胯跨',
    'kuai': '侩哙块快狯筷脍蒯郐',
    'kuan': '宽款髋',
    'kuang': '况匡哐圹夼旷框狂眶矿筐纩诓诳贶邝',
    'kui': '亏匮喟喹夔奎岿悝愦愧揆暌溃盔睽窥篑聩葵蒉蝰跬逵隗馈馗魁',
    'kun': '困坤悃捆昆琨醌锟阃髡鲲',
    'kuo': '廓扩括蛞阔',
    'la': '剌啦喇垃拉旯瘌砬腊蜡辣邋',
    'lai': '崃徕来涞濑癞睐籁莱赉赖铼',
    'lan': '兰婪岚懒拦揽斓栏榄滥漤澜烂篮缆罱蓝褴览谰镧阑',
    'lang': '啷廊朗榔浪狼琅稂莨蒗螂郎锒阆',
    'lao': '佬劳唠姥崂捞栳涝潦烙牢痨老耢酪醪铑铹',
    'le': '乐了仂叻泐肋鳓',
    'lei': '儡勒嘞垒嫘擂檑泪磊类累缧羸耒蕾诔酹镭雷',
    'leng': '冷塄愣棱楞',
    'li': '丽例俐俚俪傈利力励历厉厘吏呖哩唳喱坜娌嫠戾李枥栎栗梨沥溧漓澧犁狸猁理璃疠疬痢砺砾礼离立笠篥篱粒粝缡罹苈荔莅莉蓠藜蛎蜊蠡詈跞轹逦郦醴里锂隶雳骊鲡鲤鳢鹂黎黧',
    'lia': '俩',
    'lian': '奁帘廉怜恋敛楝殓涟潋濂炼琏练联脸臁莲蔹蠊裢裣连链镰鲢',
    'liang': '两亮凉墚晾梁椋粮粱良谅踉辆量魉',
    'liao': '僚嘹寥寮尥廖撂撩料燎獠疗缭聊蓼辽钌镣鹩',
    'lie': '冽列劣咧埒捩洌烈猎裂趔躐鬣',
    'lin': '临凛吝啉嶙廪懔拎林檩淋琳瞵磷粼膦蔺赁躏辚遴邻霖鳞麟',
    'ling': '令伶凌另呤囹岭柃棂泠灵玲瓴绫羚翎聆苓菱蛉酃铃陵零领鲮龄',
    'liu': '六刘旒柳榴流浏溜熘琉留瘤硫绺遛鎏锍镏馏骝鹨',
    'long': '咙垄垅拢栊泷珑癃砻窿笼聋胧茏陇隆龙',
    'lou': '偻喽娄嵝搂楼漏瘘篓耧蒌蝼镂陋髅',
    'lu': '卢卤噜垆庐录戮掳撸栌橹氇泸渌漉潞炉璐碌禄簏胪舻芦虏赂路轳辂辘逯镥陆露颅鲁鲈鸬鹭鹿麓',
    'luan': '乱卵娈孪峦挛栾滦脔銮鸾',
    'lun': '仑伦囵抡沦纶论轮',
    'luo': '倮摞椤泺洛漯猡珞瘰箩络罗脶荦萝落螺蠃裸逻锣镙雒骆骡',
    'lv': '侣吕屡履律捋旅榈氯滤率稆绿缕膂虑褛铝闾驴',
    'lve': '掠略锊',
    'ma': '吗唛嘛妈嬷杩犸玛码蚂蟆马骂麻',
    'mai': '买劢卖埋脉荬迈霾麦',
    'man': '墁幔慢曼满漫熳瞒缦蔓蛮螨谩镘鞔颟馒鳗',
    'mang': '忙氓漭盲硭芒茫莽蟒邙',
    'mao': '冒卯峁帽懋旄昴毛泖牦猫瑁瞀矛耄茂茅茆蝥蟊袤貌贸铆锚髦',
    'me': '么',
    'mei': '妹媒媚寐嵋昧枚梅楣每没浼湄煤猸玫眉美莓袂酶镁镅霉魅鹛',
    'men': '们懑扪焖钔门闷',
    'meng': '勐孟懵朦梦檬猛甍盟瞢礞艋艨萌蒙虻蜢蠓锰',
    'mi': '冖咪嘧宓密幂弥弭敉汨泌猕眯祢秘米糜糸縻脒芈蘼蜜觅谜谧迷醚靡麋',
    'mian': '免冕勉娩宀棉沔渑湎眄眠绵缅腼面',
    'miao': '喵妙庙描杪淼渺眇瞄秒缈苗藐邈鹋',
    'mie': '乜咩灭篾蔑蠛',
    'min': '岷悯愍抿敏民泯珉皿缗苠闵闽鳘黾',
    'ming': '冥名命明暝溟瞑茗螟酩铭鸣',
    'miu': '谬',
    'mo': '墨嫫寞抹摩摸摹末模殁沫漠瘼磨秣耱膜茉莫蓦蘑谟貊貘镆陌馍魔麽默',
    'mou': '侔哞某牟眸缪蛑谋鍪',
    'mu': '亩仫募坶墓姆幕慕拇暮木母毪沐牡牧目睦穆苜钼',
    'n': '嗯',
    'na': '呐哪娜拿捺纳肭衲那钠镎',
    'nai': '乃奈奶柰氖耐艿萘鼐',
    'nan': '南喃囡楠男腩蝻赧难',
    'nang': '囊囔攮曩馕',
    'nao': '呶垴孬恼挠淖猱瑙硇脑蛲铙闹',
    'ne': '呢疒讷',
    'nei': '内馁',
    'nen': '嫩恁',
    'neng': '能',
    'ni': '伲你倪匿坭妮尼怩拟旎昵泥溺猊睨腻逆铌霓鲵',
    'nian': '埝年廿念拈捻撵碾蔫辇辗鲇鲶黏',
    'niang': '娘酿',
    'niao': '嬲尿脲茑袅鸟',
    'nie': '啮嗫孽捏涅聂臬蘖蹑镊镍陧颞',
    'nin': '您',
    'ning': '佞凝咛宁拧柠泞狞甯聍',
    'niu': '妞忸扭牛狃纽钮',
    'nong': '侬农哝弄浓脓',
    'nou': '耨',
    'nu': '努奴孥弩怒胬驽',
    'nuan': '暖',
    'nuo': '傩喏懦挪搦糯诺锘',
    'nv': '女恧衄钕',
    'nve': '疟虐',
    'o': '哦喔噢',
    'ou': '偶呕怄欧殴沤瓯耦藕讴鸥',
    'pa': '啪帕怕杷爬琶筢葩趴',
    'pai': '俳哌徘拍排派湃牌蒎',
    'pan': '判叛拚攀泮潘爿畔盘盼磐蟠袢襻蹒',
    'pang': '乓庞彷旁滂耪胖螃逄',
    'pao': '刨匏咆庖抛泡炮狍疱脬袍跑',
    'pei': '佩呸培帔旆沛胚裴赔辔配醅锫陪霈',
    'pen': '喷湓盆',
    'peng': '嘭堋彭怦抨捧朋棚澎烹砰硼碰篷膨蓬蟛鹏',
    'pi': '丕仳僻劈匹啤噼圮坯埤媲屁庀批披擗枇毗淠琵甓疋疲痞癖皮睥砒纰罴脾芘蚍蜱譬貔辟邳郫铍陴霹鼙',
    'pian': '偏片犏篇翩胼谝蹁骈骗',
    'piao': '剽嘌嫖殍漂瓢瞟票缥螵飘',
    'pie': '丿撇氕瞥苤',
    'pin': '品姘嫔拼榀牝聘贫频颦',
    'ping': '乒俜凭坪娉屏平枰瓶苹萍评鲆',
    'po': '叵坡婆泊泼珀皤破笸粕迫鄱钋钷颇魄',
    'pou': '剖掊裒',
    'pu': '仆匍噗圃埔扑攴攵普曝朴氆浦溥濮瀑璞脯莆菩葡蒲谱蹼铺镤镨',
    'qi': '七乞亓企俟其凄启嘁器圻奇契妻屺岂岐崎弃憩戚旗期杞柒栖桤棋槭欺歧气汔汽沏泣淇漆琦琪畦砌碛祁祈祺綦綮绮耆脐芑芪萁萋葺蕲蛴蜞讫起蹊迄颀骐骑鳍麒齐',
    'qia': '恰掐洽葜髂',
    'qian': '乾仟佥倩凵前千堑岍嵌悭愆慊扦掮搴椠欠歉浅潜牵签箝缱肷芊芡茜虔褰谦谴迁遣钎钤钱钳铅阡骞黔',
    'qiang': '丬呛墙嫱强戕戗抢枪樯炝羌羟腔蔷蜣襁跄锖锵镪',
    'qiao': '乔侨俏劁峭巧悄愀憔撬敲桥樵橇瞧硗窍缲翘荞诮谯跷锹鞒鞘',
    'qie': '且切妾怯惬挈窃箧郄锲',
    'qin': '亲侵勤吣嗪噙寝揿擒檎沁溱琴禽秦芩芹螓衾钦锓',
    'qing': '倾卿圊庆情擎晴檠氢氰清磬箐罄苘蜻謦请轻青顷鲭黥',
    'qiong': '琼穷穹筇芎茕蛩跫邛銎',
    'qiu': '丘俅囚巯楸求泅犰球秋糗虬蚯蝤裘赇逑遒邱酋鳅鼽',
    'qu': '劬区去取娶屈岖曲朐氍渠璩癯瞿磲祛蕖蘧蛆蛐蠼衢觑诎趋趣躯阒驱鸲麴黢龋',
    'quan': '全券劝圈悛拳权泉犬犭畎痊筌绻荃蜷诠辁醛铨颧鬈',
    'que': '却悫榷瘸确缺阕阙雀鹊',
    'qun': '群裙逡',
    'ran': '冉染然燃苒蚺髯',
    'rang': '嚷壤攘瓤禳穰让',
    'rao': '娆扰桡绕荛饶',
    're': '惹热',
    'ren': '人亻仁仞任刃壬妊忍稔纫荏葚衽认轫韧饪',
    'reng': '仍扔',
    'ri': '日',
    'rong': '冗容嵘戎榕溶熔狨绒肜茸荣蓉蝾融',
    'rou': '揉柔糅肉蹂鞣',
    'ru': '乳儒入嚅如孺汝洳溽濡缛茹蓐薷蠕褥襦辱铷颥',
    'ruan': '朊软阮',
    'rui': '枘瑞睿芮蕊蕤蚋锐',
    'run': '润闰',
    'ruo': '偌弱箬若',
    'sa': '仨卅挲撒洒脎萨飒',
    'sai': '噻塞腮赛鳃',
    'san': '三伞叁散毵糁馓',
    'sang': '丧嗓搡桑磉颡',
    'sao': '埽嫂扫搔瘙缫臊骚鳋',
    'se': '啬涩瑟穑色铯',
    'sen': '森',
    'seng': '僧',
    'sha': '傻刹厦唼啥杀歃沙煞痧砂纱莎裟铩霎鲨',
    'shai': '晒筛酾',
    'shan': '删剡善埏姗嬗山彡扇擅杉汕潸煽珊疝缮膳膻舢芟苫蟮衫讪赡跚鄯钐闪陕骟鳝',
    'shang': '上伤商垧墒尚晌殇熵绱裳觞赏',
    'shao': '劭勺哨少捎梢潲烧稍筲绍艄芍苕蛸邵韶',
    'she': '佘厍奢射慑摄歙涉滠猞畲社舌舍蛇设赊赦麝',
    'shen': '什伸呻哂娠婶审慎椹沈深渖渗甚申矧砷神绅肾胂莘蜃诜谂身',
    'sheng': '剩升圣声嵊牲生甥盛省眚笙绳胜',
    'shi': '世事仕似使侍势匙十史嗜噬埘士失始实室尸屎市师式弑恃拭拾施时是柿氏湿炻狮矢石示礻筮舐莳蓍虱蚀螫视誓识试诗谥豉豕贳轼适逝释铈食饣饰驶鲥鲺',
    'shou': '兽受售守寿手扌授收狩瘦绶艏首',
    'shu': '书倏叔塾墅姝孰属庶恕戍抒摅数暑曙术束枢树梳殊殳毹沭淑漱澍熟疏秫竖纾署腧舒菽蔬薯蜀赎输述黍鼠',
    'shua': '刷唰耍',
    'shuai': '帅摔甩蟀衰',
    'shuan': '拴栓涮闩',
    'shuang': '双孀爽霜',
    'shui': '水氵睡税谁',
    'shun': '吮瞬舜顺',
    'shuo': '妁搠朔槊烁硕蒴说铄',
    'si': '丝兕厮厶司咝嗣嘶四姒寺巳思撕斯死汜泗澌祀私笥纟缌耜肆蛳锶饲驷鸶',
    'song': '凇宋崧嵩忪怂悚松淞竦耸菘讼诵送颂',
    'sou': '叟嗖嗽嗾搜擞溲瞍艘薮螋锼飕馊',
    'su': '俗僳嗉塑夙宿愫涑溯稣簌粟素肃苏蔌觫诉谡速酥',
    'suan': '狻算蒜酸',
    'sui': '岁濉燧眭睢碎祟穗绥荽虽谇遂邃隋随隧髓',
    'sun': '孙损榫狲笋荪隼飧',
    'suo': '唆唢嗍嗦娑所桫梭琐睃索缩羧蓑锁',
    'ta': '他塌塔她它挞榻溻獭趿踏蹋遢铊闼鳎',
    'tai': '台太态抬汰泰炱肽胎苔薹跆邰酞钛鲐',
    'tan': '叹坍坛坦忐探摊昙檀毯滩潭炭痰瘫碳袒覃谈谭贪郯钽锬',
    'tang': '倘傥唐堂塘帑搪棠樘汤淌溏烫瑭糖羰耥膛螗螳趟躺醣铴镗饧',
    'tao': '啕套掏桃洮涛淘滔绦萄讨逃陶韬饕鼗',
    'te': '忑忒慝特铽',
    'teng': '滕疼腾藤誊',
    'ti': '体倜剃剔啼嚏屉悌惕提替梯涕绨缇荑裼踢蹄逖醍锑题鹈',
    'tian': '填天忝恬掭殄添甜田畋腆舔阗',
    'tiao': '佻挑条眺祧窕笤粜蜩跳迢髫鲦龆',
    'tie': '帖萜贴铁餮',
    'ting': '亭停厅听婷庭廷挺梃汀烃町艇莛葶蜓霆',
    'tong': '仝佟僮同嗵彤恸捅桐桶潼痛瞳砼童筒统茼通酮铜',
    'tou': '亠偷头投透钭骰',
    'tu': '兔凸吐图土堍屠徒涂秃突荼菟途酴钍',
    'tuan': '团彖抟湍疃',
    'tui': '推煺腿蜕褪退颓',
    'tun': '吞屯暾氽臀豚饨',
    'tuo': '乇佗唾坨妥庹托拓拖柝椭橐沱沲砣箨脱跎酡陀驮驼鸵鼍',
    'wa': '佤哇娃娲挖洼瓦腽蛙袜',
    'wai': '外崴歪',
    'wan': '万丸剜婉完宛弯惋挽晚湾烷玩琬畹皖碗纨绾脘腕芄菀蜿豌顽',
    'wang': '亡妄往忘惘旺望枉汪王网罔辋魍',
    'wei': '为伟伪位偎卫危味唯喂囗围圩委威娓尉尾嵬巍帏帷微惟慰未桅沩洧涠渭潍炜煨猥猬玮畏痿纬维胃艉苇萎葳蔚薇诿谓軎违逶闱隈韦韪魏鲔',
    'wen': '刎吻文汶温玟璺瘟稳紊纹蚊问闻阌雯',
    'weng': '嗡瓮翁蓊蕹',
    'wo': '倭卧幄我挝握斡沃涡渥硪窝肟莴蜗龌',
    'wu': '乌五仵伍侮兀务勿午吴吾呒呜唔圬坞妩婺寤屋巫庑忤怃悟戊捂无晤杌梧武毋污浯焐物牾痦舞芜芴蜈诬误迕邬鋈钨阢雾骛鹉鹜鼯',
    'xi': '习僖兮吸唏喜嘻夕奚媳嬉屣希席徙息悉惜戏昔晰曦析樨檄欷汐洗浠淅溪烯熄熙熹牺犀玺皙矽硒禊禧稀穸粞系细羲翕膝舄舾菥葸蓰蜥螅蟋袭西觋郗醯铣锡阋隙隰饩鼷',
    'xia': '下侠匣吓夏峡暇柙狎狭瑕瞎硖罅虾辖遐霞黠',
    'xian': '仙先冼县咸娴嫌宪岘弦掀显暹氙涎燹猃献现痫祆筅籼纤线羡腺舷苋莶藓蚬衔贤跣跹酰锨闲限险陷霰馅鲜鹇',
    'xiang': '乡享像厢向响巷庠想橡湘相祥箱缃翔芗葙蟓襄详象镶项飨饷香骧鲞',
    'xiao': '哓哮啸嚣孝宵小崤效晓枭枵校消淆潇硝笑筱箫绡肖萧逍销霄骁魈',
    'xie': '些亵偕写勰协卸屑廨懈挟携撷斜械楔榍榭歇泄泻渫瀣燮獬绁缬胁薤蝎蟹谐谢躞邂邪鞋',
    'xin': '信囟心忄忻新昕欣歆芯薪衅辛鑫锌馨',
    'xing': '兴刑型姓幸形性悻惺擤星杏猩硎腥荇荥行邢醒陉',
    'xiong': '兄凶匈汹熊胸雄',
    'xiu': '休修咻嗅岫庥朽溴秀绣羞袖貅锈馐髹鸺',
    'xu': '勖叙吁嘘墟婿序徐恤戌旭栩洫溆煦盱糈絮绪续胥蓄蓿虚许诩酗醑需须顼',
    'xuan': '儇喧宣悬揎旋暄楦泫渲漩炫煊玄璇痃癣眩碹绚萱谖轩选铉镟',
    'xue': '削学泶穴薛血谑踅雪靴鳕',
    'xun': '勋埙寻峋巡巽徇循恂旬曛殉汛洵浔熏獯窨荀荨蕈薰训讯询迅逊醺驯鲟',
    'ya': '丫亚伢压吖呀哑垭娅岈崖押揠桠氩涯牙琊痖睚砑芽蚜衙讶轧迓雅鸦鸭',
    'yan': '严俨偃兖厌厣咽唁堰奄妍嫣宴岩崦延彦恹掩晏檐沿淹湮滟演炎烟焉焰焱燕琰盐眼研砚筵罨胭腌艳芫菸蜒衍言讠谚谳赝郾鄢酽闫阉阎雁颜餍验魇鼹',
    'yang': '仰佯养央徉怏恙扬杨样殃氧泱洋漾炀烊疡痒秧羊蛘阳鞅鸯',
    'yao': '吆咬夭妖姚尧崾幺徭摇曜杳爻珧瑶窈窑繇耀肴腰舀药要谣轺遥邀钥鳐鹞',
    'ye': '业也冶叶噎夜掖揶晔曳椰液烨爷耶腋谒邺野铘靥页',
    'yi': '一义乙亦亿以仪伊佚佾依倚刈劓医呓咦咿噫圯埸壹夷奕姨宜屹峄嶷已异弈弋彝役忆怡怿悒意懿抑挹揖旖易椅欹殪毅沂溢漪熠猗疑疫痍瘗癔益眙矣移绎缢羿翊翌翳翼肄胰臆舣艺苡薏蚁蜴衣衤裔议译诒诣谊贻轶迤逸遗邑酏钇铱镒镱颐饴驿黟',
    'yin': '印吟吲喑因垠堙夤姻寅尹廴引殷氤洇淫狺瘾胤茚茵荫蚓鄞铟银阴隐霪音饮',
    'ying': '嘤婴媵嬴应影撄映楹樱滢潆瀛瑛璎瘿盈硬缨罂膺英茔荧莹莺萤营萦蓥蝇赢迎郢颍颖鹦鹰',
    'yo': '哟唷',
    'yong': '佣俑勇咏喁墉壅庸恿慵拥永泳涌用甬痈臃蛹踊邕镛雍饔鳙',
    'you': '优佑侑卣又友右呦囿宥尢尤幼幽忧悠攸有柚油游牖犹猷由疣莜莠莸蚰蚴蝣诱邮酉釉铀铕鱿黝鼬',
    'yu': '与予于伛余俞俣喻圄圉域妤妪娱宇寓屿峪嵛庾御愈愉愚揄於昱榆欤欲毓浴淤渔渝煜燠狱狳玉瑜瘀瘐盂禹禺窬窳竽纡羽聿肀育腴臾舁舆芋萸蓣虞蜮蝓裕觎誉语谀谕豫迂逾遇郁钰阈隅雨雩预饫馀驭鬻鱼鹆鹬龉',
    'yuan': '元冤原员园圆垣垸塬媛怨愿掾援橼沅渊源爰猿瑗眢箢缘苑螈袁辕远院鸢鸳鼋',
    'yue': '刖岳悦曰月樾瀹粤约越跃钺阅龠',
    'yun': '云允匀孕恽愠昀晕殒氲熨狁筠纭耘芸蕴运郓郧酝陨韫韵',
    'za': '匝咂咋拶杂砸',
    'zai': '仔再哉在宰崽栽灾甾载',
    'zan': '咱攒昝暂瓒簪糌赞趱錾',
    'zang': '奘脏臧葬赃驵',
    'zao': '凿唣噪早枣澡灶燥皂糟藻蚤躁造遭',
    'ze': '仄则啧帻择昃泽笮箦舴责赜迮',
    'zei': '贼',
    'zen': '怎谮',
    'zeng': '增憎甑缯罾赠锃',
    'zha': '乍吒咤哳喳扎揸札柞栅楂榨渣炸痄眨砟蚱诈铡闸齄',
    'zhai': '债宅寨摘斋瘵砦窄',
    'zhan': '占展崭战搌斩旃栈毡沾湛盏瞻站粘绽蘸詹谵',
    'zhang': '丈仉仗嫜嶂帐幛张彰掌杖樟涨漳獐璋瘴章胀蟑账鄣长障',
    'zhao': '兆召啁找招昭棹沼照爪笊罩肇诏赵钊',
    'zhe': '哲折摺柘浙着磔者蔗蛰蜇褶谪赭辄辙这遮锗鹧',
    'zhen': '侦圳帧振斟朕枕桢榛浈珍甄畛疹真砧祯稹箴缜胗臻蓁诊贞赈轸针镇阵震鸩',
    'zheng': '争峥征徵怔拯挣政整正狰症睁筝蒸证诤郑钲铮',
    'zhi': '之侄值制卮只吱咫址埴夂峙帙帜彘志忮执指挚掷摭支旨智枝枳栀栉桎植止殖汁治滞炙痔痣直知祉祗秩稚窒絷纸织置职肢胝脂膣至致芝芷蛭蜘觯豸质贽趾跖踬踯轵轾郅酯陟雉骘鸷黹',
    'zhong': '中仲众冢忠盅种终肿舯螽衷踵重钟锺',
    'zhou': '周咒妯宙州帚昼洲皱籀粥纣绉肘胄舟荮诌轴酎骤',
    'zhu': '丶主伫住侏助嘱拄朱杼柱株槠橥注洙渚潴炷烛煮猪珠疰瘃瞩祝竹竺筑箸翥舳苎茱著蛀蛛诛诸贮躅逐邾铢铸驻麈',
    'zhua': '抓',
    'zhuai': '拽',
    'zhuan': '专啭撰砖篆赚转颛馔',
    'zhuang': '壮妆庄撞桩状装',
    'zhui': '坠惴缀缒赘追锥隹骓',
    'zhun': '准窀肫谆',
    'zhuo': '倬卓啄拙捉擢斫桌浊浞涿濯灼禚茁诼酌镯',
    'zi': '兹咨姊姿子字孜孳嵫恣梓淄渍滋滓眦秭笫籽粢紫缁耔自觜訾谘赀资趑辎锱髭鲻龇',
    'zong': '偬宗总棕粽纵综腙踪鬃',
    'zou': '奏揍楱诹走邹鄹陬驺鲰',
    'zu': '俎卒族祖租组诅足镞阻',
    'zuan': '攥纂缵躜钻',
    'zui': '嘴最罪蕞醉',
    'zun': '尊撙樽遵鳟',
    'zuo': '佐作做唑坐左座怍昨琢祚胙阼',
}

_SOUND_GROUPS = {k: set(v) for k, v in _SYLLABLE_CHARS.items()}
_CHAR_SOUNDS = {}
for _syl, _cs in _SYLLABLE_CHARS.items():
    for _ch in _cs:
        _CHAR_SOUNDS.setdefault(_ch, []).append(_syl)


def _sound_key(syl):
    """近似发音归一：平翘舌 z/zh、c/ch、s/sh 与前后鼻音 in/ing、en/eng、an/ang。
    只用于「近音」档，同音档仍按原音节精确匹配。"""
    s = syl.replace('zh', 'z').replace('ch', 'c').replace('sh', 's')
    return s.replace('ing', 'in').replace('eng', 'en').replace('ang', 'an')


_LOOSE_GROUPS = {}
for _syl, _cs in _SOUND_GROUPS.items():
    _LOOSE_GROUPS.setdefault(_sound_key(_syl), set()).update(_cs)


_HOMO_CACHE = {}


def _homophone_chars(ch, loose=False):
    """返回与 ch 同音（loose=True 时含近音）的常用汉字集合，不含自身。
    查不到读音时返回空集合，行为与加这张表之前的字面匹配完全一致。
    当炒字最多 5 个，缓存结果避免全市场扫描时反复构造集合。"""
    key = (ch, bool(loose))
    hit = _HOMO_CACHE.get(key)
    if hit is not None:
        return hit
    out = set()
    for s in _CHAR_SOUNDS.get(ch, ()):
        out.update(_SOUND_GROUPS.get(s, ()))
        if loose:
            out.update(_LOOSE_GROUPS.get(_sound_key(s), ()))
    out.discard(ch)
    hit = frozenset(out)
    _HOMO_CACHE[key] = hit
    return hit




def _mood_build():
    hot, themes, temp, pool_n = [], {}, 50.0, 0
    try:
        pool = (fetch_zt_pool() or {}).get("pool") or []
        pool_n = len(pool)
        base = cached("name_base", 86400, _name_base)
        cnt = {}
        for x in pool:
            for ch in set(str(x.get("name") or "")):
                if _cjk(ch) and ch not in _STOP_CH:
                    cnt[ch] = cnt.get(ch, 0) + 1
        uni = max(1, base.get("_n", 3000))
        for ch, n in cnt.items():
            if n < 3:            # 必须 ≥3 只涨停股命中，过滤生僻字造成的假热点
                continue
            p0 = base.get(ch, 0) / uni
            p1 = n / max(1, pool_n)
            mult = p1 / p0 if p0 > 0 else 0.0
            if mult > 1.2:
                hot.append((ch, n, round(mult, 1)))
        hot.sort(key=lambda h: -h[2])
        hot = hot[:5]
        for x in pool:
            # 题材按 "+" 拆分（与 zt_evaluate 同口径，含全角＋归一）：
            # 整串如「机器人+算力」当键，股票名/行业几乎不可能子串命中，
            # 会把 "+8 题材共振" 打成永久 miss 并系统性压低评分。
            for t in str(x.get("reason") or "").replace("＋", "+").split("+"):
                t = t.strip()
                if t:
                    themes[t] = themes.get(t, 0) + 1
        temp = clamp(40 + pool_n * 0.6)
    except Exception:
        pass
    return {"hot": hot, "themes": themes, "temp": round(temp, 1), "pool_n": pool_n}


def mood_score(name, industry=""):
    """短线炒作偏好：当炒字（含谐音 / 近音）+ 关键词 / 题材 + 市场温度。

    v1.5.14：当炒字不再要求字面相同。同音字（羊→阳 / 洋 / 杨）按完整权重命中；
    近音字（平翘舌、前后鼻音）按半权重命中，并在 why 里标注，便于详情页解释。
    查不到读音的字仍退回原来的字面匹配，行为完全不变。
    """
    c = cached("mood_ctx", 60, _mood_build)
    nm, ind = str(name or ""), str(industry or "")
    score, why = 50.0, []
    nm_chars = set(nm)
    exact, near = [], []
    for h in c["hot"]:
        ch = h[0]
        if ch in nm_chars:                       # 字面命中：完全不变
            exact.append((h, ch))
            continue
        same = sorted(_homophone_chars(ch) & nm_chars)
        if same:                                 # 同音（忽略声调）：完整权重
            exact.append((h, same[0]))
            continue
        loose = sorted(_homophone_chars(ch, loose=True) & nm_chars)
        if loose:                                # 近音：半权重
            near.append((h, loose[0]))
    if exact or near:
        bonus = sum(min(12, 4 + h[2] * 0.35) for h, _ in exact) \
              + sum(min(12, 4 + h[2] * 0.35) * 0.5 for h, _ in near)
        score += min(28, bonus)
        parts = [h[0] if v == h[0] else "%s（谐音 %s）" % (h[0], v) for h, v in exact]
        parts += ["%s（近音 %s）" % (h[0], v) for h, v in near]
        why.append("命中当炒字「%s」" % "·".join(parts))
    kw = [w for w in _HOT_WORDS if w in nm or w in ind]
    if kw:
        score += min(14, 5 * len(kw))
        why.append("热点关键词 %s" % "/".join(kw[:2]))
    th = [t for t in c["themes"] if t and (t in nm or t in ind)]
    if th:
        score += 8
        why.append("题材「%s」%d 家涨停" % (th[0], c["themes"][th[0]]))
    score += (c["temp"] - 50) * 0.25
    if not why:
        score -= 8
        why.append("未命中当日当炒字 / 题材")
    return round(clamp(score), 1), why


def _grade_of(board, s):
    """五档评级 S/A/B/C/D：S/A 为推荐买入区。所有板块统一出口。"""
    sp, a, b, c = DIM_GRADE.get(board, GRADE_BOARD_DEFAULT)
    return ("S" if s >= sp else "A" if s >= a else
            "B" if s >= b else "C" if s >= c else "D")


def apply_dims(board, obj):
    """唯一评分出口：按板块查作用域 → 读唯一开关 → 取该板块分值行 → 折算。
    始终计算两项维度（开关关闭时也带上），这样前端切换开关 / 公告刷新都能本地重算，无需重新扫描。"""
    st = load_settings()
    scope = DIM_SCOPE.get(board, "common")
    enabled = st.get("dim_" + scope, DIM_DEFAULT[scope])
    if isinstance(enabled, str):
        enabled = enabled.lower() not in ("false", "0", "off", "no")
    bw, mw, ew = DIM_WEIGHTS[board]
    lst = obj.get("list")
    if not isinstance(lst, list) or not lst:
        return obj
    for x in lst:
        if not isinstance(x, dict):
            continue
        try:
            base = float(x.get("base_score") if x.get("base_score") is not None
                         else (x.get("score") or 0))
        except (TypeError, ValueError):
            base = 0.0   # 单只脏数据不拖垮整批 mdims（与 _gate_score 同口径）
        # 原始理由只保存一次：开关切换 / 公告刷新 / 缓存复用都从它派生，
        # 避免「关掉开关后卡片理由里还挂着公告」以及反复重算累积覆盖。
        if "base_reasons" not in x:
            x["base_reasons"] = list(x.get("reasons") or [])[:3]
        msg, msg_why, anns = message_score(x.get("code"))
        mood, mood_why = mood_score(x.get("name"), x.get("industry"))
        x["mdims"] = {"enabled": bool(enabled), "scope": scope, "board": board,
                      "base": round(base, 1), "base_w": bw,
                      "msg": msg, "msg_w": mw, "msg_why": msg_why,
                      "mood": mood, "mood_w": ew, "mood_why": mood_why,
                      "anns": anns[:8]}
        base = round(base, 1)   # 与 mdims.base / 前端 dimFoldItem 统一舍入口径
        x["base_score"] = base
        if enabled:
            x["score"] = round(base * bw / 100 + msg * mw / 100 + mood * ew / 100, 1)
            x["grade"] = _grade_of(board, x["score"])
            ev = msg_why if anns else (mood_why[0] if mood_why else "")
            rs = list(x.get("base_reasons") or [])
            if ev:
                if len(rs) >= 3:
                    rs[2] = ev
                else:
                    rs.append(ev)
            x["reasons"] = rs[:3]
        else:
            x["score"] = round(base, 1)
            x["grade"] = _grade_of(board, x["score"])
            x["reasons"] = list(x.get("base_reasons") or [])[:3]
    # 五个板块统一按折算后的综合分排序：涨停/跌停虽然按 action 分组展示，
    # 但分组顺序由前端 groups 固定，组内必须按卡片上显示的分数排，
    # 否则「显示综合分、排序用基础分」会让用户看到 90 分排在 80 分后面。
    lst.sort(key=lambda z: -(float(z.get("score") or 0)))
    for i, z in enumerate(lst):
        if isinstance(z, dict) and "rank" in z:
            z["rank"] = i + 1
    # 评级统计必须基于折算后的 score/grade（即卡片上显示的值）重算，
    # 否则开启消息/情绪开关后卡片 S/A 数量变了、统计却还是基础分口径，二者对不上。
    # 覆盖 scan() 里用基础分算出的兜底值，并补齐五档 S/A/B/C/D。
    try:
        pri = obj.get("params") or {}
        smin = float(pri.get("score_min", pri.get("min_score", 0)) or 0)
        counted = [z for z in lst if isinstance(z, dict)
                   and float(z.get("score") or 0) >= smin]
        for g in ("S", "A", "B", "C", "D"):
            obj["grade_" + g.lower()] = sum(1 for z in counted if z.get("grade") == g)
    except Exception:
        pass
    with _ann_lock:
        obj["mdims_meta"] = {"enabled": bool(enabled), "scope": scope, "board": board,
                             "base_w": bw, "msg_w": mw, "mood_w": ew,
                             "ann_version": _ann["ver"], "ann_source": _ann["src"],
                             "ann_updated": _ann["updated"], "ann_error": _ann["error"]}
    return obj


def announce_check(codes_str=""):
    """轻量心跳：返回公告版本 + 指定代码的最新消息面分数，不扫描行情。"""
    fetch_announcements()
    with _ann_lock:
        out = {"ok": True, "version": _ann["ver"], "count": _ann["total"],
               "new": _ann["new"], "updated": _ann["updated"],
               "source": _ann["src"], "error": _ann["error"], "msgs": {}}
        by_code = dict(_ann["by_code"] or {})   # 一次性快照：下面 2000 次评分不再重复加锁
    # 前端已按批发送并去重，这里再去重一次；不再硬截 500 —— 旧写法超出部分
    # 既不请求也不提示，那些票的消息面分数会永久停在旧值。
    codes = list(dict.fromkeys(
        c.strip() for c in str(codes_str or "").split(",") if c.strip()))
    out["received"] = len(codes)
    out["truncated"] = len(codes) > 2000
    if out["truncated"]:
        codes = codes[:2000]
    for c in codes:
        rows = list(by_code.get(str(c), []))[:5]
        msg, why, anns = message_score(c, rows=rows)
        out["msgs"][c] = {"msg": msg, "msg_why": why, "anns": anns[:8]}
    return out


# ---------------------------------------------------------------- 用户参数持久化
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SETTINGS_KEYS = (
    "gain_min", "gain_max", "vr_min", "turn_min", "turn_max",
    "fcap_min", "fcap_max", "vol_cv_max", "min_amount", "min_amount_on", "score_min",
    "entry_time", "exclude_st", "switches", "preset", "ai", "swing", "limit",
    "dim_common", "dim_night",
)


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_settings(patch):
    with _settings_lock:
        cur = load_settings()
        for k in SETTINGS_KEYS:
            if k in patch:
                cur[k] = patch[k]
        cur["updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_json_atomic(SETTINGS_FILE, cur)
        return {"ok": True, "settings": cur}


# ---------------------------------------------------------------- HTTP 服务
def _csv_txt(v):
    """只给文本字段做公式转义：以 = + - @ 开头会被 Excel / WPS 当公式执行。
       数值列（涨幅、现价等）保持原样，不能给负数前面加单引号。"""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["代码", "名称", "现价", "涨幅%", "量比", "换手%", "流通市值(亿)",
                "成交额(亿)", "主力净额(亿)", "评分", "基础分", "评级", "标签", "行业"])
    for x in (data.get("list") or []):
        if not isinstance(x, dict):
            continue
        # 代码列前置 apostrophe：Excel/WPS 按文本处理，保住 000001 的前导零
        w.writerow(["'" + str(x.get("code", "")), _csv_txt(x.get("name", "")),
                    x.get("price", ""), x.get("pct", ""),
                    x.get("volratio", ""), x.get("turnover", ""), x.get("fcap", ""),
                    x.get("amount", ""), x.get("main_net", ""), x.get("score", ""),
                    x.get("base_score", x.get("score", "")),
                    _csv_txt(x.get("grade", "")),
                    _csv_txt("/".join(x.get("tags", []) or [])),
                    _csv_txt(x.get("industry", ""))])
    # utf-8-sig 自带 BOM，手写一个 \ufeff 会变成双 BOM，Excel 首列表头显示成乱码
    return buf.getvalue().encode("utf-8-sig"), \
        f"一夜持股_{now_cn().strftime('%Y%m%d_%H%M%S')}.csv"


def _jsonable(o):
    """NaN / Infinity 不是合法 JSON：递归换成 None，避免整份响应让前端 JSON.parse 抛错。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


# ---------------------------------------------------------------- 龙头分析
# 热门板块 → 龙头股 → 跟随股。板块资金与成分股取自东财公开行情，
# 涨停 / 连板 / 涨停原因取同花顺涨停池（与涨停分析共用 fetch_zt_pool，口径一致）。
LD_BF = "f2,f3,f6,f8,f10,f12,f13,f14,f20,f21,f62,f104,f105,f128,f136,f184,f207"
# 统计型 / 指数型板块名：它们不是一条资金主线（「昨日涨停」「融资融券」「基金重仓」
# 「同花顺概念」这类是按条件筛出来的股票集合），混进来会顶掉真正在做的主线。
LD_BLACK = ("昨日", "融资融券", "沪股通", "深股通", "机构重仓", "基金重仓", "社保重仓",
            "QFII", "券商重仓", "保险重仓", "信托重仓", "预盈预增", "预亏预减", "破净",
            "高送转", "次新股", "股权转让", "举牌", "增持回购", "股权激励", "员工持股",
            "MSCI", "标普", "富时", "转债", "B股", "AB股", "低价股", "高价股", "百元股",
            "壳资源", "IPO受益", "参股", "同花顺", "东方财富", "转融通", "深成500",
            "中证", "上证", "创业板", "科创板", "历史新高", "历史新低", "创新高",
            "创新低", "连板", "涨停", "跌停", "换手", "量比", "振幅", "解禁",
            "大宗交易", "龙虎榜", "摘帽", "填权", "贴权", "业绩", "高股息", "红利",
            "低市盈", "市净率", "破发", "增仓", "减仓", "重仓", "基金", "券商", "保险")
LD_DEFAULTS = {"boards": 8, "follow": 6}
LD_MIN_AMT = 40e8        # 板块成交额不到 40 亿：资金不够集中，谈不上主线
LD_MIN_MAIN = 5          # 主板成分股不足 5 只：样本太小，龙头没有代表性
LD_LIMIT_PCT = 9.8       # 主板涨停判定（20cm 不在主板范围）
LD_CAND_MAX = 16         # 单轮最多评估 16 个候选板块，手机端两波并发就能跑完


def _ld_f(v, d=0.0):
    """东财对停牌股和部分板块字段返回 "-"，直接参与算术会抛类型错误。"""
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except (TypeError, ValueError):
        return d


def _ld_clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _ld_pctile(v, arr):
    if not arr:
        return 0.5
    return sum(1 for a in arr if a <= v) / len(arr)


LD_PUSH_HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com")

# v1.7.3：龙头分析多数据源。2026-11 实测 push2 全域名被限流（RemoteDisconnected），
# 但东财 datacenter 板块资金榜、同花顺板块详情仍可用。改为三源轮询 + 缓存兜底，
# 任一源成功即返回，全失败时退回上一次成功的快照，不再直接报「接口暂时无响应」。
_LD_SRC = {"board": "", "members": ""}      # 最近一次成功的来源名（写进响应便于前端标注）
_LD_CACHE = {}                              # key -> (timestamp, value)，接口失败时兜底
_LD_THS_MAP = {"ts": 0, "data": None}       # 同花顺「板块名→代码」映射，缓存 10 分钟


def _ld_cache_get(key, ttl):
    hit = _LD_CACHE.get(key)
    if hit and time.time() - hit[0] <= ttl:
        return hit[1]
    return None


def _ld_cache_put(key, val):
    _LD_CACHE[key] = (time.time(), val)


def _ld_push_get(q, timeout=6):
    """东财 push2 系列接口的多域名兜底（备用源）。"""
    last = None
    for h in LD_PUSH_HOSTS:
        try:
            return http_json("https://" + h + q, timeout=timeout, retries=1) or {}
        except Exception as e:
            last = e
    raise last if last is not None else RuntimeError("push2 unavailable")


def _dc_board_page(t, pz=100):
    """东财 datacenter 板块资金流榜（主源）。t=2 行业 / t=3 概念。
       返回结构对齐 push2 的 page：total + diff[{f12,f14,f3,f62,f6,f21,f104,f105,f8,f10,f128,f136}]。
       datacenter 没有成交额 / 涨跌家数 / 换手 / 量比，用比值列与估算值填充，
       板块筛选主要看 f62 主力净额、f3 涨幅、f14 名称，这些都有真实值。"""
    rn = "RPT_INDUSTRY_FUNDFLOW" if t == 2 else "RPT_CONCEPT_FUNDFLOW"
    # 按最近交易日过滤，避免历史数据按净流入排序把旧日期的高值顶到前面
    td = last_trade_date()
    flt = urllib.parse.quote("(TRADE_DATE='%s')" % td) if td else ""
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=%s"
         "&columns=ALL&pageNumber=1&pageSize=%d&sortColumns=NET_INFLOW&sortTypes=-1"
         "&source=WEB&client=WEB" % (rn, pz))
    if flt:
        u += "&filter=" + flt
    j = http_json(u, timeout=10, retries=1) or {}
    if not j.get("success"):
        raise RuntimeError("datacenter board: %s" % j.get("message"))
    result = j.get("result") or {}
    rows = result.get("data") or []
    # 双保险：即便 filter 未生效，也只保留最近交易日那批
    dates = sorted([str(r.get("TRADE_DATE") or "")[:10] for r in rows if r.get("TRADE_DATE")], reverse=True)
    latest = dates[0] if dates else td
    diff = []
    for r in rows:
        if latest and str(r.get("TRADE_DATE") or "")[:10] != latest:
            continue
        net = _ld_f(r.get("NET_INFLOW"))          # 主力净额（元）
        ratio = _ld_f(r.get("NET_INFLOW_RATIO"))  # 净额占成交额百分比
        amt = (net / (ratio / 100.0)) if ratio > 0.01 else 0.0   # 反推成交额
        diff.append({
            "f12": str(r.get("BOARD_CODE") or ""), "f14": str(r.get("BOARD_NAME") or ""),
            "f3": _ld_f(r.get("CHANGE_RATE")), "f62": net, "f6": amt, "f21": 0.0,
            "f104": 0, "f105": 0, "f8": 0.0, "f10": 0.0,
            "f128": str(r.get("MAX_NETINFLOW_SEC") or ""), "f136": 0.0
        })
    if not diff:
        raise RuntimeError("datacenter board empty")
    return {"total": int(result.get("count") or len(diff)), "diff": diff, "date": latest}


def _ld_board_page(t, pz=100):
    """行业(t=2) / 概念(t=3) 板块资金榜。datacenter 主源 → push2 备源 → 缓存兜底。"""
    key = "board:%d" % t
    errs = []
    try:
        page = _dc_board_page(t, pz)
        if page.get("diff"):
            _LD_SRC["board"] = "东财资金流"
            _ld_cache_put(key, page)
            return page
    except Exception as e:
        errs.append("datacenter:%s" % type(e).__name__)
    try:
        q = ("/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2"
             "&invt=2&fid=f62&fs=m:90+t:%d&fields=%s" % (pz, t, LD_BF))
        page = (_ld_push_get(q) or {}).get("data") or {}
        if page.get("diff"):
            _LD_SRC["board"] = "东财push2"
            _ld_cache_put(key, page)
            return page
    except Exception as e:
        errs.append("push2:%s" % type(e).__name__)
    cached = _ld_cache_get(key, 1800)
    if cached:
        _LD_SRC["board"] = "缓存快照"
        return cached
    raise RuntimeError("board sources failed: " + ",".join(errs))


def _ths_board_map():
    """同花顺「板块名→代码」映射（概念 + 行业），缓存 10 分钟。"""
    if _LD_THS_MAP["data"] is not None and time.time() - _LD_THS_MAP["ts"] < 600:
        return _LD_THS_MAP["data"]
    m = {}   # name -> (code, kind)，kind 决定详情 URL 用 gn 还是 thshy
    for kind in ("gn", "thshy"):
        try:
            txt = http_text("https://q.10jqka.com.cn/%s/" % kind, encoding="gbk",
                            retries=1, timeout=10,
                            headers={**HEADERS, "Referer": "https://q.10jqka.com.cn/"})
            for code, name in re.findall(r"/%s/detail/code/(\d+)/[^>]*>([^<]+)<" % kind, txt):
                nm = re.sub(r"\s+", "", name)
                if nm and nm not in m:
                    m[nm] = (code, kind)
        except Exception:
            continue
    if m:
        _LD_THS_MAP.update(ts=time.time(), data=m)
    return m


def _num_cn(s):
    """同花顺成交额/市值形如 1.63亿 / 8300万 → 返回元。"""
    s = str(s or "").strip()
    if not s or s in ("--", "-"):
        return 0.0
    m = re.match(r"([-\d.]+)\s*([亿万]?)", s)
    if not m:
        return 0.0
    v = _ld_f(m.group(1))
    unit = m.group(2)
    return v * (1e8 if unit == "亿" else 1e4 if unit == "万" else 1.0)


def _ths_norm_name(s):
    """板块名归一化：去空格、罗马数字分级后缀（白酒Ⅱ→白酒）、首尾空白，便于两站匹配。"""
    s = re.sub(r"\s+", "", str(s or ""))
    s = re.sub(r"[ⅠⅡⅢⅣⅤ]+$", "", s)
    return s


def _ths_match(name):
    """名称精确匹配优先，其次归一化后互相包含（东财「白酒Ⅱ」↔同花顺「白酒」）。"""
    m = _ths_board_map()
    key = re.sub(r"\s+", "", str(name or ""))
    if key in m:
        return m[key]
    nk = _ths_norm_name(key)
    for k, v in m.items():
        if _ths_norm_name(k) == nk:
            return v
    for k, v in m.items():
        nk2 = _ths_norm_name(k)
        if len(nk) >= 3 and len(nk2) >= 3 and (nk in nk2 or nk2 in nk):
            return v
    return None


def _ths_members(name, board_net=0.0, board_amt=0.0):
    """同花顺板块详情页成分股（主源）。返回对齐 push2 的 members 结构。
       同花顺无个股主力净额，用「板块主力净额 × 个股成交额占比」近似，保证资金维度仍有效。"""
    hit = _ths_match(name)
    if not hit:
        raise RuntimeError("ths board code not found: %s" % name)
    code, kind = hit
    txt = http_text("https://q.10jqka.com.cn/%s/detail/code/%s/" % (kind, code), encoding="gbk",
                    retries=1, timeout=10,
                    headers={**HEADERS, "Referer": "https://q.10jqka.com.cn/"})
    body = re.search(r"<tbody>(.*?)</tbody>", txt, re.S)
    if not body:
        raise RuntimeError("ths members empty")
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", body.group(1), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 10 or not re.match(r"^\d{6}$", cells[1] if len(cells) > 1 else ""):
            continue
        ccode = cells[1]
        amt = _num_cn(cells[10]) if len(cells) > 10 else 0.0
        net = (board_net * (amt / board_amt)) if board_amt > 0 else 0.0
        out.append({
            "f12": ccode, "f14": cells[2], "f2": _ld_f(cells[3]), "f3": _ld_f(cells[4]),
            "f8": _ld_f(cells[7]), "f10": _ld_f(cells[8]),
            "f6": amt, "f21": _num_cn(cells[11]) if len(cells) > 11 else 0.0,
            "f62": net, "f13": 1 if ccode[0] == "6" else 0
        })
    if not out:
        raise RuntimeError("ths members parse empty")
    return out


def _ld_members(name, board_net=0.0, board_amt=0.0):
    """板块成分股。同花顺主源 → push2 备源（入参是 BK 代码时）→ 缓存兜底。"""
    key = "members:%s" % name
    errs = []
    try:
        rows = _ths_members(name, board_net, board_amt)
        _LD_SRC["members"] = "同花顺"
        _ld_cache_put(key, rows)
        return rows
    except Exception as e:
        errs.append("ths:%s" % type(e).__name__)
    if re.match(r"^BK\d+", str(name)):
        try:
            q = ("/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2"
                 "&invt=2&fid=f62&fs=b:%s&fields=f2,f3,f6,f8,f10,f12,f13,f14,f21,f62"
                 % urllib.parse.quote(str(name)))
            rows = ((_ld_push_get(q) or {}).get("data") or {}).get("diff") or []
            if rows:
                _LD_SRC["members"] = "东财push2"
                _ld_cache_put(key, rows)
                return rows
        except Exception as e:
            errs.append("push2:%s" % type(e).__name__)
    cached = _ld_cache_get(key, 1800)
    if cached:
        _LD_SRC["members"] = "缓存快照"
        return cached
    return []   # 成分股取不到时该板块跳过，不影响其他板块


def _ld_hot(c, b):
    """板块热度：涨停家数 40 / 资金强度 24 / 板块涨幅 22 / 成交额 14。"""
    return (0.40 * min(b["lu"], 6) / 6 * 100
            + 0.24 * _ld_clip(c["strength"] / 1.2) * 100
            + 0.22 * _ld_clip((c["pct"] + 1) / 5) * 100
            + 0.14 * _ld_clip(c["amount"] / 600e8) * 100)


def _ld_build_board(c, zmap):
    """板块成分股打分并排出龙头。个股分数＝涨幅 30 / 资金占成交额 25 / 成交额 20 /
       换手 15 / 量比 10，涨停再直接加分。龙头判定先看涨停、再看连板数、
       最后才比分数——盘面上的「龙头」就是最高板那只，不会出现龙头首板而板块
       另有二板的矛盾。"""
    # v1.7.3：同花顺成分股无个股主力净额，用板块净额×成交额占比近似传入
    ms = _ld_members(c.get("name") or c["code"], c.get("net", 0) * 1e8, c.get("amount", 0) * 1e8)
    main = []
    for m in ms:
        code = str(m.get("f12") or "")
        name = str(m.get("f14") or "")
        if not is_main_board(code) or "ST" in name.upper() or "退" in name:
            continue
        if _ld_f(m.get("f2")) <= 0:                 # 停牌
            continue
        main.append(m)
    amts = sorted(_ld_f(m.get("f6")) for m in main)
    turns = sorted(_ld_f(m.get("f8")) for m in main)
    rows = []
    for m in main:
        code = str(m.get("f12") or "")
        pct, net, amt = _ld_f(m.get("f3")), _ld_f(m.get("f62")), _ld_f(m.get("f6"))
        turn, vr = _ld_f(m.get("f8")), _ld_f(m.get("f10"))
        rel = (net / amt) if amt else 0.0
        s = (0.30 * _ld_clip(pct / 10.0) + 0.25 * _ld_clip(rel / 0.25)
             + 0.20 * _ld_pctile(amt, amts) + 0.15 * _ld_pctile(turn, turns)
             + 0.10 * _ld_clip((vr - 0.8) / 3))
        z = zmap.get(code) or {}
        boards = parse_boards(z.get("high_days")) if z else 0
        lu = pct >= LD_LIMIT_PCT or boards >= 1
        if lu:
            s += 0.12 + 0.02 * min(boards, 4)
        rows.append({"name": str(m.get("f14") or ""), "code": code,
                     "market": 1 if str(m.get("f13")) == "1" else 0,
                     "price": round(_ld_f(m.get("f2")), 2),
                     "pct": round(pct, 2), "net": round(net / 1e8, 2),
                     "amount": round(amt / 1e8, 2), "turn": round(turn, 2),
                     "vr": round(vr, 2), "lu": lu, "boards": boards,
                     "hd": str(z.get("high_days") or ""),
                     "reason": str(z.get("reason") or "").strip(),
                     "score": round(_ld_clip(s) * 100, 1)})
    rows.sort(key=lambda z: (-(1 if z["lu"] else 0), -z["boards"], -z["score"]))
    cats = []
    for z in rows[:8]:
        for part in re.split(r"[+＋]", z.get("reason") or ""):
            part = part.strip()
            if not part or any(b in part for b in LD_BLACK):
                continue
            if part not in cats:
                cats.append(part)
    return {"main_n": len(main), "all": len(ms),
            "lu": sum(1 for z in rows if z["lu"]),
            "maxb": max([z["boards"] for z in rows] or [0]),
            "cat": " · ".join(cats[:3])[:26], "rows": rows}


def leader_analysis(params):
    """龙头分析：先挑出资金最集中的板块，再在板块里分龙头与跟随。
       整包缓存 25 秒（自动重扫最短 30 秒，够用且不会拿到旧行情）。"""
    t0 = time.time()
    p = dict(LD_DEFAULTS)
    for k, v in (params or {}).items():
        if k not in p or v in (None, ""):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        p[k] = max(2, min(n, 12 if k == "boards" else 10))

    def build():
        # 涨停池只拉一次：板块内哪些股在涨停、几板、涨停原因都从这里取
        zmap, zdate, zsrc = {}, "", ""
        try:
            zpack = fetch_zt_pool() or {}
            zdate = str(zpack.get("date") or "")
            zsrc = str(zpack.get("source") or "")
            for x in (zpack.get("pool") or []):
                zmap[str(x.get("code") or "")] = x
        except Exception:
            pass

        scanned, inflow, raw, board_err = 0, 0, [], 0
        for src_name, t in (("行业", 2), ("概念", 3)):
            try:
                page = _ld_board_page(t)
            except Exception:
                board_err += 1
                continue
            scanned += int(page.get("total") or 0)
            for x in (page.get("diff") or []):
                nm = str(x.get("f14") or "")
                if not nm or any(b in nm for b in LD_BLACK):
                    continue
                net, amt, pct = _ld_f(x.get("f62")), _ld_f(x.get("f6")), _ld_f(x.get("f3"))
                if net <= 0:                       # 主力还在净流出的板块不是当下的主线
                    continue
                inflow += 1
                if amt < LD_MIN_AMT or pct < -2.0:
                    continue
                cap = _ld_f(x.get("f21"))
                up, dn = _ld_f(x.get("f104")), _ld_f(x.get("f105"))
                ur = up / (up + dn) if (up + dn) else 0.5
                strength = (net / cap * 100) if cap else 0.0
                hot0 = (0.30 * _ld_clip((pct + 1) / 6) + 0.24 * _ld_clip(strength / 1.2)
                        + 0.22 * ur + 0.24 * _ld_clip(amt / 600e8))
                raw.append({"name": nm, "code": str(x.get("f12") or ""), "src": src_name,
                            "hot0": round(hot0 * 100, 1), "strength": strength,
                            "pct": round(pct, 2), "net": round(net / 1e8, 2),
                            "amount": round(amt / 1e8, 1), "turn": round(_ld_f(x.get("f8")), 2),
                            "vr": round(_ld_f(x.get("f10")), 2),
                            "up": int(up), "down": int(dn),
                            "lead": str(x.get("f128") or ""),
                            "lead_pct": round(_ld_f(x.get("f136")), 2)})

        # 同一条资金主线常被拆成两个名字（印制电路板 / 元件、半导体 / 半导体概念），
        # 名称互相包含的只留热度高的那个，避免同一批票占两张卡
        raw.sort(key=lambda z: -z["hot0"])
        cand, seen = [], []
        for c in raw:
            if any(c["name"] in s or s in c["name"] for s in seen):
                continue
            seen.append(c["name"])
            cand.append(c)
            if len(cand) >= LD_CAND_MAX:
                break

        berr = [0]

        def one(c):
            try:
                return _ld_build_board(c, zmap)
            except Exception:
                berr[0] += 1
                return None

        built = []
        if cand:
            with ThreadPoolExecutor(max_workers=_wn(8)) as ex:
                built = list(ex.map(one, cand))

        picks, used, tops, covered = [], set(), [], 0
        for c, b in zip(cand, built):
            if not b or b["main_n"] < LD_MIN_MAIN:
                continue
            # 行情弱的日子放宽：板块里还有涨停，或者板块本身红盘 / 主力仍在流入就留
            if b["lu"] == 0 and c["pct"] < 0.2 and c["net"] < 5:
                continue
            top5 = set(z["code"] for z in b["rows"][:5])
            if any(len(top5 & t) >= 2 for t in tops):
                continue                       # 与已入选板块的重合度过高
            rest = [z for z in b["rows"] if z["code"] not in used]
            if not rest:
                continue                       # 龙头已经被别的板块用掉
            leader = rest[0]
            followers = [z for z in rest[1:]
                         if z["score"] >= 18 and (z["pct"] > 0 or (z["pct"] > -1.2 and z["net"] > 0))]
            followers = followers[: p["follow"]]
            if len(followers) < 2:
                continue                       # 只有一只票的板块谈不上「跟随」
            used.add(leader["code"])
            tops.append(top5)
            covered += len(rest)
            picks.append({**c, "hot": round(_ld_hot(c, b), 1), "lu": b["lu"],
                          "maxb": b["maxb"], "main_n": b["main_n"], "all": b["all"],
                          "cat": b["cat"], "leader": leader, "followers": followers})
            if len(picks) >= p["boards"]:
                break

        board_src = _LD_SRC.get("board") or "东财资金流"
        mem_src = _LD_SRC.get("members") or "同花顺"
        src_txt = "%s板块 + %s成分股" % (board_src, mem_src)
        if zsrc:
            src_txt += " + 同花顺涨停池"
        cache_used = ("缓存快照" in board_src) or ("缓存快照" in mem_src)
        return {"ok": True, "mode": "leader",
                "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
                "date": zdate or last_trade_date(),
                "source": src_txt, "cache_used": cache_used,
                "market": market_status(), "scanned": scanned, "inflow": inflow,
                "covered": covered, "elapsed": round(time.time() - t0, 1),
                "list": picks, "params": p,
                "empty": "" if picks else (
                    "板块数据源暂时都不可用，已沿用最近一次缓存；稍后会自动重试"
                    if cache_used else (
                        "板块资金榜接口暂时无响应，稍后会自动重试"
                        if board_err >= 2 else "当前没有资金净流入且成交额达标的板块，等放量后再试"))}

    key = "leader:%d:%d" % (p["boards"], p["follow"])
    try:
        return cached(key, 25, build)
    except Exception as e:
        return {"ok": False, "mode": "leader", "error": "龙头分析失败：%s" % e,
                "elapsed": round(time.time() - t0, 1)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        if isinstance(obj, dict) and obj.get("ok") and ("list" in obj or "buckets" in obj):
            board = API_BOARD.get((self.path or "").split("?")[0])
            if board:
                try:
                    obj = apply_dims(board, obj)   # 维度折算失败也保留原始响应，不影响现有功能
                except Exception as e:
                    # 不能静默吞掉：失败时整批股票没有 mdims，卡片既无徽章也无
                    # 「原基础分」，看起来像开关失效。这里显式标注，由前端提示。
                    obj["dim_degraded"] = True
                    obj["dim_error"] = f"{type(e).__name__}: {e}"
                    deg = list(obj.get("degraded_fields") or [])
                    if "评分维度" not in deg:
                        deg.append("评分维度")
                    obj["degraded_fields"] = deg
            obj.setdefault("stale", False)
            obj.setdefault("degraded_fields", [])
            obj.setdefault("data_date", (obj.get("date") or obj.get("ts") or "")[:10])
            obj.setdefault("market", market_status())
        self._send(code, json.dumps(_jsonable(obj), ensure_ascii=False,
                                    default=str, allow_nan=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                fp = os.path.join(WEB_DIR, "index.html")
                if not os.path.exists(fp):
                    return self._send(500, "缺少 web/index.html", "text/plain; charset=utf-8")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if path == "/api/health":
                return self._json({"ok": True, "version": VERSION, "market": market_status(),
                                   "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S")})

            if path == "/api/announce":
                return self._json(announce_check(qs.get("codes", [""])[0]))

            if path == "/api/scan":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(scan(params))

            if path == "/api/ai_pick":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(ai_pick(params))

            if path == "/api/search":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(stock_search(params))

            if path == "/api/swing_pick":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(swing_pick(params))

            if path == "/api/limit_up":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(limit_analysis(params))

            if path == "/api/limit_down":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(limit_down_analysis(params))

            if path == "/api/leader":
                params = {k: v[0] for k, v in qs.items()}
                return self._json(leader_analysis(params))

            if path == "/api/stock":
                code = qs.get("code", [""])[0]
                # 股票代码必须是 6 位数字，防止注入查询参数或拼出畸形行情 URL
                if not re.fullmatch(r"\d{6}", str(code or "")):
                    return self._json({"ok": False, "error": "股票代码格式无效"}, 400)
                try:
                    market = int(qs.get("market", ["0"])[0])
                except (TypeError, ValueError):
                    market = 1 if str(code).startswith("6") else 0
                # 详情页按「图表刷新间隔」10~90 秒轮询。扫描热路径的日K 300s / 分时 60s
                # 缓存会让短间隔拿到的仍是旧图，所以交易中给详情单独用短 TTL；
                # 收盘 / 休市 / 周末沿用长 TTL，避免非交易时段空转打行情接口。
                # cached() 按调用方的 ttl 判定命中，共享条目与单飞锁都不变，扫描不受影响。
                fast = market_status().get("state") == "open"
                today = now_cn().strftime("%Y-%m-%d")
                # 日K源盘中/收盘后常滞后一个交易日，分时源又不给昨收：两者各错一天，
                # 就会出现「日K还停在昨天」且「分时图百分比对不上顶部涨幅」。
                # 用带日期的单股实时快照统一今日K线与今日昨收口径。
                # 快照先行：拿到股票名，日K才能按 ST 5% 阈值做除权对齐
                q = None
                try:
                    q = fetch_realtime_quote(code, market, ttl=8 if fast else 60)
                except Exception:
                    q = None
                # swing 详情与列表同口径：列表评分/周线共振用 400 根，详情也用 400，
                # 否则止损止盈与周线指标两处数值对不上；其余板块 160 根与卡片评分统一。
                mode = (qs.get("mode", [""])[0] or "").strip()
                _kl = 400 if mode == "swing" else 160
                bars = fetch_kline(code, market, _kl, name=(q or {}).get("name") or "",
                                   ttl=15 if fast else 300)   # 缓存只留一份，按需截取
                try:
                    tr = fetch_trends(code, market, ttl=8 if fast else 60)
                except Exception:
                    # 停牌股三个分时源全空会抛 EmptyData：分时缺失不该把
                    # 已成功拿到的日K一起拖成 500，降级为无分时即可。
                    tr = {}
                # v1.7.2：区分「今日实时快照」与「上一交易日定格快照」。
                #   旧逻辑只认 date==today：周末/节假日/盘前接口回吐的是上一交易日
                #   定格值，被直接丢弃 -> quote=None，今开/最高/最低/成交额全变「—」，
                #   昨收又退成 bars[-1] 自身收盘 -> 顶部涨幅误显示 +0.00%。
                #   现在：整日休市(周末/节假日)与盘前/集合竞价改用上一交易日快照填数据，
                #   并由 prev_day 告诉前端弹「已休市，此为上一交易日数据」标注；
                #   午间休市(break)、15:00 后收盘(closed·已收盘)的快照就是今天的，
                #   仍走 fresh_q 正常路径，不弹标注。
                #   注意：无论哪种快照都只用它取昨收/顶部数据，绝不拿旧快照合成今日K，
                #   否则会把整段历史抬高一天。
                mst = market_status()
                mstate = mst.get("state")
                # 整日无交易：周末 / 节假日（休市），或盘前 / 集合竞价（未开盘）
                day_off = (mstate == "pre") or (mstate == "closed" and "休市" in (mst.get("text") or ""))
                q_price = float((q or {}).get("price") or 0)
                q_date = str((q or {}).get("date") or "")
                prev_close = float((q or {}).get("prev_close") or 0)
                fresh_q = bool(q_price > 0 and q_date == today and not day_off)
                # 上一交易日定格快照：整日休市/盘前拿到的非今日快照，用来填顶部数据
                stale_q = bool(q_price > 0 and prev_close > 0 and not fresh_q and day_off)
                tr = dict(tr) if tr else {}          # cached() 共享对象，禁止原地写
                tr_day = tr.get("date") or ""
                tr_today = (not tr_day) or tr_day == today
                # 昨收：今日快照优先；上一交易日定格快照用其自带昨收（那天的真实前收），
                # 让顶部涨幅 / 分时百分比都按上一交易日真实涨跌显示，而不是 0.00%。
                if fresh_q and prev_close > 0:
                    tr["prev_close"] = prev_close
                    if not tr_day:
                        tr["date"] = today
                elif stale_q:
                    tr["prev_close"] = prev_close
                elif not tr.get("prev_close"):
                    if len(bars) >= 2 and bars[-1].get("date") == today:
                        tr["prev_close"] = bars[-2]["close"]
                    elif bars:
                        tr["prev_close"] = bars[-1]["close"]
                tr = _annotate_trend(tr) if tr.get("points") else tr
                # 补当日K：实时快照优先，其次用今日实时分时合成；只在确认是今日数据时补。
                # stale_q 绝不补——旧快照合成今日K会把历史抬高一天。
                tb = None
                if fresh_q:
                    tb = _today_bar(q, today, prev_close)
                elif (tr.get("live") or tr.get("date") == today) and tr.get("points"):
                    tb = _today_bar_from_trends(tr, today, tr.get("prev_close") or 0)
                if tb and bars:
                    bars = [dict(b) for b in bars]   # 同上：绝不改 cached() 里的共享 list
                    if bars[-1].get("date") == today:
                        # 分时合成的 amount/turnover 可能是 0，不能冲掉日K已有的真实值
                        keep = {k: bars[-1][k] for k in ("amount", "turnover") if bars[-1].get(k)}
                        bars[-1].update(tb)
                        bars[-1].update(keep)
                    else:
                        bars.append(tb)
                        if len(bars) > _kl:
                            bars = bars[-_kl:]
                # 持仓计划统一由后端按真实日K算（Wilder ATR + TRADE_COEF），
                # 与列表卡片同一口径；前端不必再回退到自己的估算公式，杜绝双口径。
                trade = None
                # scan（一夜持股）列表项 mode="scan"，卡片计划按 night 系数算；
                # 不补进白名单的话详情刷新时 trade 恒 None，止盈止损线停在扫描时刻。
                if mode in ("night", "scan", "ai", "swing", "zt", "dt"):
                    try:
                        _px = (q or {}).get("price") if fresh_q else None
                        if mode == "swing":
                            _m = swing_metrics(bars)
                            trade = swing_plan(_m, _m.get("c"), _px)
                        else:
                            # scan 与列表卡片同一口径走 night 系数，别落进 ai 的兜底
                            trade = trade_plan("night" if mode == "scan" else mode, bars, _px)
                    except Exception:
                        trade = None
                # v1.7.2：上一交易日定格快照时给出真实交易日日期（MM-DD），
                # 前端据此弹「已休市，此为上一交易日数据（MM-DD）」；午休/收盘后为空。
                prev_day = ""
                if stale_q:
                    pd = q_date if (q_date and q_date != today) else str((bars[-1].get("date") if bars else "") or "")
                    if len(pd) >= 10:
                        prev_day = pd[5:10]
                # v1.7.3：详情量比 / 主力净额用单股接口补齐。列表快照被 push2 批量
                # 限流回退新浪时会缺主力净额，详情不能再拿旧快照顶替；失败留 None，
                # 前端按缺失显示「—」。绝不让补数失败拖垮整张详情。
                try:
                    extra = fetch_detail_extra(code, market, ttl=8 if fast else 60)
                except Exception:
                    extra = {"volratio": None, "main_net": None}
                resp = {"ok": True, "code": code, "kline": bars, "trends": tr,
                        "quote": q if (fresh_q or stale_q) else None,
                        "prev_day": prev_day,
                        "extra": extra,
                        "trade": trade, "metrics": detail_metrics(bars)}
                # v1.6.7：所有板块的详情分时都叠加上证指数（以前只有 scan 取）。
                # 与个股分时同一时刻口径，缓存 60 秒全站复用，多这一次请求基本无感；
                # 失败时留空，由前端跳过叠加，不影响个股图。
                try:
                    _it = fetch_trends("000001", 1, ttl=8 if fast else 60)
                    if _it and len(_it.get("points") or []) >= 20:
                        # 备用源偶尔丢前收：用首个点位兜底，避免前端叠加时起点错位
                        if not _it.get("prev_close"):
                            _it = dict(_it)
                            _it["prev_close"] = _it["points"][0]["price"]
                        resp["idx_trends"] = _it
                except Exception:
                    pass
                return self._json(resp)

            if path == "/api/scans":
                return self._json({"ok": True, "list": list_scans()})

            if path == "/api/settings":
                return self._json({"ok": True, "settings": load_settings()})

            if path == "/api/review":
                return self._json(review_scan(qs.get("file", [""])[0]))

            if path == "/api/watchlist":
                return self._json({"ok": True, "list": load_watch()})

            if path == "/api/holidays":
                return self._json(holidays_info())

            if path == "/api/export":
                try:
                    data = json.loads(qs.get("data", ["{}"])[0])
                    if not isinstance(data, dict):
                        raise ValueError("data 需要是 JSON 对象")
                except Exception:
                    return self._json({"ok": False, "error": "导出参数不是有效 JSON"}, 400)
                out, fname = export_csv(data)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)

            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 4 * 1024 * 1024:
                raise ValueError("长度超限")
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "Content-Length 不合法"}, 400)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("请求体需要是 JSON 对象")
        except Exception:
            # 静默吞成 {} 会让 /api/save_scan 落一条 0 只的空复盘记录
            return self._json({"ok": False, "error": "请求内容不是有效 JSON 对象"}, 400)
        try:
            if u.path == "/api/export":
                out, fname = export_csv(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            if u.path == "/api/save_scan":
                return self._json(save_scan(body))
            if u.path == "/api/add_pick":
                return self._json(add_pick(body))
            if u.path == "/api/scan_delete":
                return self._json(delete_scans(body.get("files") or []))
            if u.path == "/api/scan_remove_items":
                return self._json(remove_scan_items(body.get("file"), body.get("codes") or []))
            if u.path == "/api/watchlist":
                return self._json(save_watch(body.get("list", [])))
            if u.path == "/api/settings":
                return self._json(save_settings(body or {}))
            if u.path == "/api/holidays":
                act = str(body.get("action") or "refresh").lower()
                if act == "save":
                    return self._json(save_holidays_manual(body.get("list") or body.get("text") or ""))
                if act == "builtin":
                    return self._json(save_holidays_builtin())
                # bool("false") 是 True，会把 6 小时冷却误跳过；按文本显式解析。
                _force = str(body.get("force", "")).lower() in ("1", "true", "yes")
                return self._json(refresh_holidays(body.get("year"), force=_force))
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def pick_port():
    import socket
    for port in range(BASE_PORT, BASE_PORT + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
    return BASE_PORT


def find_running():
    """已有实例在跑就直接复用，避免重复开多个端口"""
    for port in range(BASE_PORT, BASE_PORT + 30):
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=0.35) as r:
                d = json.loads(r.read().decode())
            if d.get("ok"):
                return port
        except Exception:
            continue
    return None


def open_browser(url):
    try:
        ok = webbrowser.open(url)
    except Exception:
        ok = False
    if not ok:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"服务已启动，请手动在浏览器打开：\n\n{url}", "一夜持股战法 · 筛选系统", 0x40)
        except Exception:
            print("请手动打开浏览器访问：", url)


def main():
    if os.environ.get("NO_BROWSER"):
        pass
    else:
        port0 = find_running()
        if port0:
            url0 = f"http://{HOST}:{port0}/"
            print("已有实例在运行：", url0)
            open_browser(url0)
            return

    port = pick_port()
    url = f"http://{HOST}:{port}/"
    srv = ThreadingHTTPServer((HOST, port), Handler)
    print("=" * 58)
    print(f"  {APP_TITLE}  v{VERSION}")
    print(f"  服务地址: {url}")
    print("  关闭此窗口即停止服务")
    print("=" * 58)
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: open_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 打包为无控制台 exe 时，把崩溃信息落到 exe 同目录，方便排查
        import traceback
        log = os.path.join(BASE_DIR, "data", "error.log")
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n[{now_cn()}] 启动失败\n{traceback.format_exc()}\n")
        except Exception:
            pass
        raise
