# -*- coding: utf-8 -*-
"""
比赛获奖证书批量下载器 · 本地网页界面
=====================================================================
为什么用网页而不是桌面窗口？
  打包成 exe 要能跑在同事任何一台电脑上。Tkinter 需要 Python 自带 tcl/tk 运行库，
  一旦目标机器（或打包用的 Python）缺了它，整个界面直接起不来。
  改成「本地小服务 + 浏览器」后：界面只依赖浏览器，而浏览器人人都有；
  打包体积还从上百 MB 降到十几 MB。

安全边界：服务只绑定 127.0.0.1（仅本机可访问），并且带一次性随机令牌，
         别的程序/网页无法指挥它干活。
=====================================================================
"""
import atexit
import base64
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = BASE_DIR
sys.path.insert(0, str(BASE_DIR))

import cert_downloader as core   # noqa: E402

TOKEN = "%08x" % random.getrandbits(32)
APP_TAG = "certdl"

# 界面上的"按表头筛选"是在浏览器里现算的（这样点一下就能立刻出结果，
# 不用每次都问一遍后端），所以预览接口会把整张表发过去。
# 几百行毫无压力；但万一有人拖进来几万行，浏览器会卡死。
# 超过这个行数就不发表格数据，界面上直接把筛选面板关掉并说明原因。
MAX_FILTER_ROWS = 5000
# 运行中把「端口 + 令牌 + 界面地址」落一份在这里。为什么需要它？
# 同事双击第二次时，新进程知道端口在哪，却拿不到旧进程的随机令牌 ——
# 没有令牌，打开的就是一个"能看不能点"的空壳页面。所以必须落盘留一份。
RUNFILE = BASE_DIR / "运行中.json"

STATE = {
    "running": False, "stage": "就绪", "done": 0, "total": 0, "extra": "",
    "pct": 0.0,                    # 整条任务的总进度（0~1），不是单个阶段的
    "paused": False,               # 是否处于「暂停」状态
    "logs": [], "result": None, "error": None, "out": "", "xlsx": "", "diag": "",
    "eta": "", "_last_done": 0, "_eta_pts": [],
}
LOCK = threading.Lock()
STOP = threading.Event()
# 暂停开关：set = 放行，clear = 暂停。引擎每处理一个人之前看一次。
PAUSE = threading.Event()
PAUSE.set()
# 暂停期间网页上改好的速度，先存这儿，等按「继续」时交给引擎换挡。
PENDING_SPEED = {"v": None}


def _pause_gate():
    """引擎每处理一个人之前会调这个。

    暂停时就阻塞在这儿（每 0.2 秒醒一次，看有没有按「继续」或「结束」）。
    恢复时返回新速度（没人改过就是 None），引擎据此换挡。
    """
    while not PAUSE.is_set():
        if STOP.is_set():
            return None            # 已经按了「结束」，让引擎自己去走退出流程
        time.sleep(0.2)
    v = PENDING_SPEED["v"]
    PENDING_SPEED["v"] = None
    return v


def guess_fail_kind(msg):
    """把异常原文映射到自救手册的条目。

    顺序有讲究：FileNotFoundError 的文案里也带"找不到名单"字样，
    如果先判「找不到列」，就会把"文件没找到"说成"表头写错了"——
    错的建议比没有建议更糟，同事会照着改表头，越改越远。
    """
    if "找不到名单文件" in msg:
        return "list_file"
    if "找不到「" in msg or "找不到" in msg and "列" in msg:
        return "list_missing"
    if "空" in msg or "没有数据行" in msg:
        return "list_empty"
    low = msg.lower()
    # 后缀被改过的假 Excel（.xls 改名成 .xlsx）：openpyxl 会抛
    # "File is not a zip file"。这不能落到 unknown —— 那是"原因不明"，
    # 而这里原因相当明确，而且有现成的解法（另存为一次）。
    if "not a zip file" in low or "badzip" in low or "not a valid" in low:
        return "bad_xlsx"
    if "proxy" in low:
        return "proxy"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "getaddrinfo" in low or "name resolution" in low or "11001" in low:
        return "dns"
    if "refused" in low or "reset" in low or "10054" in low or "10061" in low:
        return "blocked"
    return "unknown"


def peek_error_text(e):
    """把"这份名单读不了"翻成同事能照着做的事。

    绝不能把 Python 原文甩出去 —— "File is not a zip file" 对同事等于什么
    也没说，而这一步（拖进来先看一眼）的全部意义就是"早点告诉他哪儿不对"。
    所以这里不重复造文案，而是复用故障手册里已经写好的那条。
    """
    detail = "%s: %s" % (type(e).__name__, e)
    kind = guess_fail_kind(detail)
    d = core.EH.DIAG.get(kind) or core.EH.DIAG["unknown"]
    lines = [str(d["name"])]
    for s in (d.get("why") or [])[:2]:
        lines.append("· " + str(s))
    for s in (d.get("fixes") or [])[:3]:
        lines.append(str(s))
    if d.get("warn"):
        lines.append("⚠ " + str(d["warn"]))
    return "\n".join(lines)


def find_latest_report(out_dir):
    """找出刚生成的故障报告文件名，方便提示同事去转发。"""
    try:
        d = Path(out_dir or default_out_dir())
        if not d.exists():
            return ""
        best, best_t = "", 0.0
        for p in d.glob("故障诊断_*.txt"):
            t = p.stat().st_mtime
            if t > best_t:
                best, best_t = p.name, t
        if best and (time.time() - best_t) < 900:      # 只认 15 分钟内生成的
            return best
    except Exception:
        pass
    return ""


def push_log(line, cls=""):
    with LOCK:
        STATE["logs"].append({"t": line, "c": cls})


def human_secs(sec):
    """把秒数说成人话。同事要的是"我能不能先去干点别的"，不是 7432 秒。"""
    sec = int(round(sec))
    if sec < 60:
        return "%d 秒" % sec
    m = sec // 60
    if m < 60:
        return "%d 分钟" % m
    h, m = m // 60, m % 60
    return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h


def eta_text():
    """按最近一段的推进速度估"还剩多久"。必须持锁调用。

    为什么不用"从开头算全程平均"？下载阶段会退避、冷却、长歇，
    速度是阶跃变化的。用全程平均，退避时估出来的数字会一路偏乐观，
    等于撒谎 —— 同事会一直以为"马上就好了"。
    改用最近一小段（至少覆盖 8 秒）的窗口，能跟上速度的变化。

    取窗口的规则是"能跨过 8 秒的、离现在最近的那一段"：点密的时候
    窗口就是 8 秒左右，点稀的时候自动放宽，不会因为样本太少而算不出来。
    """
    pts = STATE["_eta_pts"]
    if len(pts) < 3:
        return ""
    t1, d1, total = pts[-1]
    if not total or d1 >= total:
        return ""
    # 窗口至少跨 8 秒才可信：太短的窗口会被"正好某条特别快/特别慢"带偏
    t0, d0 = pts[0][0], pts[0][1]
    for i in range(len(pts) - 2, -1, -1):
        if t1 - pts[i][0] >= 8:
            t0, d0 = pts[i][0], pts[i][1]
            break
    span, adv = t1 - t0, d1 - d0
    if span < 8 or adv <= 0:
        return ""
    left = (total - d1) / (adv / span)
    if left < 5:
        return "即将完成"
    return "预计还需 %s" % human_secs(left)


def set_prog(stage, done, total, extra="", pct=None):
    now = time.time()
    with LOCK:
        if stage != STATE["stage"] or done < STATE.get("_last_done", 0):
            STATE["_eta_pts"] = []          # 换阶段 / 进度倒退 → 重新计时
        STATE["_eta_pts"].append((now, done, total))
        STATE["_eta_pts"] = STATE["_eta_pts"][-30:]
        STATE["stage"] = stage
        STATE["done"] = done
        STATE["total"] = total
        STATE["extra"] = extra
        # 总进度：由核心引擎按各阶段耗时权重算好传进来（0~1）。
        # 收不到就退回「本阶段 done/total」—— 直接调 run_job 的别的入口走这条。
        if pct is not None:
            STATE["pct"] = float(pct)
        elif total:
            STATE["pct"] = float(done) / float(total)
        STATE["_last_done"] = done
        STATE["eta"] = eta_text()


def find_web_dir():
    for d in (BASE_DIR / "web", BUNDLE_DIR / "web",
              Path(__file__).resolve().parent / "web"):
        if (d / "index.html").exists():
            return d
    return None


def default_out_dir():
    # 统一用核心引擎里的判断：Windows 优先 D 盘桌面，否则系统桌面。
    # 两处各写一份迟早会改漏，这里只做转调。
    return core.default_out_dir()


# ------------------------------------------------------------------
# 输出位置：让同事自己挑，而不是替他写死一个 D 盘
#
# 为什么值得单独做？办公室的电脑盘符五花八门：有人只有 C 盘，
# 有人资料全放 E 盘。写死一个位置，等于替用户做了一个他没同意过的决定——
# 轻则文件散落到不认识的地方，重则那个盘压根不存在，程序当场报错。
# 所以做成「能浏览、能新建、记得住上次选哪儿」，跟装软件时选安装路径一样。
# ------------------------------------------------------------------
SETTINGS_FILE = BASE_DIR / "用户设置.json"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    try:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------------
# 访问速度：网页上一个「− / ＋」就调完了，不用去翻 config.json
#
# 关键约定：网页上调的速度**只对当前这一次任务有效**，不写盘、不记住。
# 任务一结束就清空，下次开任务回到默认（config.json 里的「每条间隔秒数」，
# 默认 1.6）。这样"临时想快一点"不会变成"以后都快"——
# 快了容易被网站封 IP，不能让一次手滑成为长期设定。
# ------------------------------------------------------------------
SPEED_STEPS = [0.3, 0.5, 0.7, 0.9, 1.2, 1.6, 2.0, 2.5, 3.0]
SPEED_DEFAULT = 1.6

# 本次任务的速度（内存态）。None = 没调过，用 config.json 里的默认值。
SESSION_SPEED = {"v": None}


def clamp_speed(v):
    """把速度收进合法区间 —— 网页传什么进来都不至于把程序搞坏。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return SPEED_DEFAULT
    return min(max(v, core.DELAY_MIN), core.DELAY_MAX)


def speed_now():
    """本次任务实际用多少秒/人。返回 (秒数, 提示语)。

    网页上调过的值（内存里那份）优先；没调过就回落到 config.json 的
    「每条间隔秒数」—— 那里是手写的，得校验，出问题要给一句人话提示。
    """
    v = SESSION_SPEED["v"]
    if v:
        return clamp_speed(v), ""
    return core.resolve_delay()


# ------------------------------------------------------------------
# 自动识别新比赛 + 写回配置
#
# 为什么要能自动识别：以前"换一个比赛"必须由人在 config.json 里手写一段配置，
# 而 field_map（F1/F999 这些）得抓包看。现在发现比赛网站的表单页里嵌了完整
# 表单定义，程序自己就能读出来 —— 同事贴个网址就行，不必等人。
#
# 写回配置是"真改磁盘上的文件"，两道保险：
#   ① 先备份成 config.json.bak（改坏了能捞回来）
#   ② 先写临时文件再 os.replace 替换 —— 中途断电也不会留下半个 JSON
# ------------------------------------------------------------------
PROBED = {}                 # sid → 识别结果。用户确认前不落盘，确认后才写。
PROBED_LOCK = threading.Lock()


def remember_probe(site):
    """把识别结果暂存，返回一个短号给界面。"""
    sid = "%08x" % random.getrandbits(32)
    with PROBED_LOCK:
        if len(PROBED) > 8:          # 只留最近几个，别无限涨
            PROBED.clear()
        PROBED[sid] = site
    return sid


def save_site(site):
    """把一个站点写进 config.json，并设为当前使用的比赛。

    返回 (ok, key, 人话说明)。
    """
    p = Path(core.ROOT) / "config.json"
    try:
        cfg = {}
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            cfg = json.loads(raw)
            try:
                (p.parent / "config.json.bak").write_text(raw, encoding="utf-8")
            except Exception:
                pass          # 备份失败不该拦住正事
        # 站点标识用短号派生，重复添加同一个比赛 = 更新它，不会攒一堆重复项
        key = "site_" + re.sub(r"[^A-Za-z0-9_]", "_", site["shortid"])[:24]
        cfg.setdefault("sites", {})
        cfg["_说明"] = ("换新比赛时，在 sites 里加一项即可，不用改代码。"
                        "也可以在界面上点「＋ 新比赛」自动识别添加。")
        cfg["sites"][key] = {
            "title": site["title"], "home_url": site["home_url"],
            "query_url": site["query_url"], "shortid": site["shortid"],
            "frmid": site["frmid"], "field_map": site["field_map"],
            "list_columns": site["list_columns"],
        }
        cfg["default_site"] = key          # 加完直接就用这个新比赛

        tmp = p.with_name("config.json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)                 # 原子替换，不会留下半个文件
        return True, key, ""
    except PermissionError:
        return False, "", ("写不进配置文件。这个文件夹可能没有写权限，"
                           "把工具整个文件夹复制到「桌面」再试一次。")
    except Exception as e:
        return False, "", "保存失败：%s" % str(e)[:120]


def list_drives():
    """列出本机所有可用盘符（C: / D: / E: …）。"""
    out = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                d = "%s:\\" % chr(ord("A") + i)
                out.append({"name": d[:2], "path": d})
    except Exception:
        pass
    if not out:
        for ch in "CDEFG":
            p = "%s:\\" % ch
            if os.path.isdir(p):
                out.append({"name": p[:2], "path": p})
    return out


def browse_dir(path):
    """列出某个目录下的子目录，给界面上的「浏览」用。

    只列目录、不列文件：同事要选的是"存哪儿"，一堆文件名只会碍事。
    路径不存在时退到最近的可用上级，让界面还能继续走，不至于卡死。
    """
    p = Path(path) if path else Path.home()
    if not p.is_dir():
        probe = p
        while probe != probe.parent and not probe.is_dir():
            probe = probe.parent
        p = probe if probe.is_dir() else Path(os.path.abspath(os.sep))
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name):
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
            if len(dirs) >= 400:
                break
    except (PermissionError, OSError):
        pass
    parent = "" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs, "drives": list_drives()}


def safe_mkdir(parent, name):
    """在 parent 下新建文件夹，返回新路径。名字非法就抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("文件夹名字不能是空的")
    if re.search(r'[\\/:*?"<>|]', name):
        raise ValueError("名字里不能有 \\ / : * ? \" < > | 这些符号")
    p = Path(parent) / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def resolve_out_dir(base, subfolder=False, today=None):
    """把「用户选的位置」+「要不要套日期文件夹」算成最终保存目录。

    单独拎成函数，是为了能被直接测 —— 这段套娃逻辑一旦出错，
    文件会散落到谁也想不到的地方，值得有几条断言盯着。
    """
    base = (base or "").strip() or default_out_dir()
    if not subfolder:
        return base
    d = today or time.strftime("%Y-%m-%d")
    return str(Path(base) / ("证书_%s" % d))


# ------------------------------------------------------------------
def sanitize_filters(raw):
    """把浏览器传来的筛选条件洗干净再用。

    为什么非要过这一道？这些字符串会**直接进日志和故障报告**，
    还会参与每一行的比对。不设上限的话，一次手滑粘进来的东西就能把
    日志撑爆、让报告变成几兆的垃圾文件 —— 而故障报告正是要发出去求助的。
    所以这里把"能用"的条件留下，越界的直接截断，形状不对的直接丢掉。

    返回 None 表示"没有任何有效条件"，等同于不筛。
    """
    if not isinstance(raw, dict):
        return None
    out = {}
    for col, cond in list(raw.items())[:60]:          # 最多 60 列
        if not isinstance(cond, dict) or not isinstance(col, str):
            continue
        col = col.strip()[:120]
        if not col:
            continue
        vals, text = [], ""
        rv = cond.get("values")
        if isinstance(rv, list):
            vals = [str(v)[:200] for v in rv[:400]]   # 每列最多 400 个值
        t = cond.get("text")
        if isinstance(t, str):
            text = t.strip()[:200]                    # 关键词最多 200 字
        if vals or text:
            out[col] = {"values": vals, "text": text}
    return out or None


def job_thread(students_path, out_dir, site_key, province, sample, filters=None):
    with LOCK:
        STATE.update({"running": True, "error": None, "result": None, "eta": "",
                      "stage": "准备中", "done": 0, "total": 0, "extra": "",
                      "pct": 0.0, "paused": False, "_last_done": 0, "_eta_pts": [],
                      # 是不是"只抽查几个人"的试跑 —— 全程挂在状态里，
                      # 界面从头到尾都能显示「试跑中」/「试跑结束」，不用自己猜。
                      "trial": bool(sample), "trial_n": int(sample or 0)})
    try:
        # 访问速度：网页上「访问速度 −／＋」调过的**只存在内存里**，
        # 只对本次任务有效、不写盘；没调过就取 config.json 里那个「每条间隔秒数」。
        delay_sec, delay_note = None, ""
        try:
            delay_sec, delay_note = speed_now()
        except Exception as e:
            delay_note = "读速度设置时出了点问题（%s），本次按默认速度跑。" % e
        if delay_note:
            push_log("  " + delay_note, "warn")
        if delay_sec:
            push_log("  本次速度：每人之间等 %.1f 秒（想改就在上面「访问速度」点 −／＋）"
                     % delay_sec, "dim")

        res = core.run_job(
            students_path, out_dir, site_key=site_key, sample=(sample or None),
            province_filter=(province or None),
            filters=filters,
            delay=delay_sec,
            on_progress=set_prog,
            should_stop=STOP.is_set,
            wait_pause=_pause_gate,
            log_sink=lambda line: push_log(line))
        stats = res["stats"]
        df = res.get("diag_file") or ""
        rty = res.get("retry_xlsx") or ""
        with LOCK:
            STATE["result"] = {
                "cert": stats.get("成功", 0) + stats.get("已完成跳过", 0),
                "schools": res["schools"],
                "miss": stats.get("未获奖无证书", 0),
                "fail": stats.get("查询失败", 0) + stats.get("下载失败", 0),
                # 「暂无证书文件」= 比赛还没开放下载。它**不算失败**，但也绝不能
                # 不露脸：2026-09-21 用户那场国赛全批如此，界面上只显示
                # "证书0 / 未获奖0 / 失败0"，跟"啥也没干"长得一样，人只会更懵。
                "nofile": stats.get("暂无证书文件", 0),
                "out": res["out_root"], "xlsx": res["xlsx"],
                "advice": res.get("advice") or [],
                "diag_file": df,
                "diag_name": os.path.basename(df) if df else "",
                "retry_xlsx": rty,
                "retry_name": os.path.basename(rty) if rty else "",
                "retry_count": res.get("retry_count", 0),
                "disk_total": res.get("disk_total", 0),
                # 试跑：本次实际查了几人 / 筛选后共几人。
                # 界面用它把「试跑结束（8/604）」写全 —— 只给一个 8，等于没说。
                "sampled": res.get("sampled", 0),
                "filtered_total": res.get("filtered_total", 0),
            }
            STATE["out"] = res["out_root"]
            STATE["xlsx"] = res["xlsx"]
            STATE["stage"] = "已完成"
    except Exception as e:
        import traceback
        msg = "%s: %s" % (type(e).__name__, e)
        # 报错时不但要说"错在哪"，还要说"怎么办"——同事联系不上开发时全靠这个。
        # run_job 内部已经给过诊断的（比如名单问题），这里就不再重复，
        # 免得同一件事说两遍、还互相矛盾。
        with LOCK:
            already = any("怎么回事：" in l.get("t", "") for l in STATE["logs"])
        if not already:
            kind = guess_fail_kind(str(e))
            for line in core.EH.explain(kind, msg, tag="程序出错"):
                push_log(line, "err")
            diag = find_latest_report(out_dir)
            if diag:
                push_log("  ★ 故障报告：%s   ← 把这一份原样发给技术支持即可" % diag, "err")
        push_log("（以下是技术细节，看不懂可以直接忽略。要帮忙时，发的是上边那份故障报告，不是这段日志）", "dim")
        push_log(traceback.format_exc(), "dim")
        with LOCK:
            STATE["error"] = msg
            STATE["diag"] = find_latest_report(out_dir)
            STATE["stage"] = "执行失败"
    finally:
        # 速度只对当前这一次任务有效：跑完（正常结束、出错、被停都一样）
        # 立刻清掉，下次开任务回到默认值 —— 免得一次"临时调快"变成长期设定。
        SESSION_SPEED["v"] = None
        PENDING_SPEED["v"] = None
        PAUSE.set()                    # 保险：别让引擎卡在暂停门里出不来
        with LOCK:
            STATE["running"] = False
            STATE["paused"] = False
        # 临时名单用过了就删掉。只删我们自己写进系统临时目录的那一份，
        # 名字前缀对不上就绝不碰 —— 宁可留垃圾，也不能误删用户的文件。
        try:
            p = Path(students_path)
            if p.parent == Path(tempfile.gettempdir()) and p.name.startswith("certdl_"):
                p.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CertDL/1.0"

    def log_message(self, *a):
        pass                                   # 别把访问日志刷到控制台

    # ---- 工具 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        """只接受带令牌的请求，挡住本机其它程序/网页乱调。"""
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == TOKEN:
            return True
        self._json({"ok": False, "msg": "令牌无效"}, 403)
        return False

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _peek(self):
        """名单里有多少人、列名认出来了吗 —— 拖进来 1 秒内回答。

        解析用的是和正式开跑同一个函数（load_list），所以这里说"读得进去"，
        开跑就一定读得进去。如果这里另写一套轻量解析，两边判断不一致才是真坑：
        预检说没问题、开跑却报错，同事会彻底不信这个提示。
        """
        body = self._read_json()
        name = os.path.basename(body.get("filename", "名单.xlsx"))
        b64 = body.get("content_b64") or ""
        if not b64:
            self._json({"ok": False, "msg": "没有收到文件内容"})
            return
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            self._json({"ok": False, "msg": "文件内容解码失败：%s" % e})
            return

        # 用带毫秒的时间戳命名：连着拖两个文件不会互相覆盖
        tmp = Path(tempfile.gettempdir()) / ("certdl_peek_%d_%s"
                                             % (int(time.time() * 1000), name))
        try:
            tmp.write_bytes(raw)
            cfg = core.resolve_config()
            key = body.get("site") or cfg["default_site"]
            site = cfg["sites"].get(key) or cfg["sites"][cfg["default_site"]]
            students, info = core.load_list(str(tmp), site.get("list_columns"), quiet=True)
            if not students:
                self._json({"ok": False,
                            "msg": "这个名单里没有可用的人（姓名为空、或证件号为空）"})
                return
            # 把整张表原样交给界面，让"按表头逐级筛选"能在浏览器里即时算出来。
            # 值统一用 core.cell_text() 转字符串 —— 和后端正式开跑时是**同一个函数**，
            # 两边规则一致，界面上筛出 12 个人，跑的就一定是这 12 个人。
            headers = [h for h in students[0]["_raw"].keys() if h]
            table = [{k: core.cell_text(v) for k, v in s["_raw"].items()}
                     for s in students] if len(students) <= MAX_FILTER_ROWS else []
            self._json({
                "ok": True,
                "total": len(students),
                "rows": info["total_rows"],
                "skipped": info["skipped"],
                "cols": info["columns"],
                "sample": [s["name"] for s in students[:5]],
                "fixed_id": info["fixed_id"],
                "fixed_name": info["fixed_name"],
                "headers": headers,
                "table": table,
                "filterable": bool(table),
            })
        except Exception as e:
            self._json({"ok": False, "msg": peek_error_text(e)})
        finally:
            try:
                tmp.unlink()          # 预检不留垃圾
            except OSError:
                pass

    def _probe(self):
        """识别一个新比赛网址。只识别、只回显，一个字节都不写盘。

        为什么不在这里顺手保存？因为"写配置文件"会改掉同事机器上的状态，
        而识别有可能认错。让他先看到"识别到了什么"再点确认，是这个流程的意义。
        """
        body = self._read_json()
        url = (body.get("url") or "").strip()
        push_log("正在识别新比赛 …", "dim")
        try:
            import site_probe
            r = site_probe.probe(url)
        except Exception as e:
            r = {"ok": False, "msg": "识别时出错：%s" % str(e)[:140]}

        if not r.get("ok"):
            msg = r.get("msg") or "识别失败"
            push_log("识别失败：%s" % msg.replace("\n", " "), "err")
            self._json({"ok": False, "msg": msg})
            return

        site = r["site"]
        sid = remember_probe(site)
        fm = site["field_map"]
        CN = (("姓名", "name"), ("证件号", "id"), ("学校", "school"),
              ("奖项", "award"), ("省", "province"), ("市", "city"),
              ("区县", "district"), ("指导老师", "teacher"))
        # 空白会被当成"识别漏了"，其实常常是这个比赛页面上本来就没这一项
        # （金数据尤其常见：主办方只勾了姓名一个条件）。写清楚，别让人猜。
        fields = [{"cn": c, "fid": fm.get(k) or "（这个页面上没有）"}
                  for c, k in CN]
        push_log("识别成功：%s" % site["title"], "ok")
        push_log("  字段对应：" + "  ".join("%s→%s" % (f["cn"], f["fid"])
                                          for f in fields), "dim")
        self._json({
            "ok": True, "sid": sid, "title": site["title"], "url": site["home_url"],
            "fields": fields, "notes": r.get("notes") or [],
            # 证书文件字段单独给出来：它是最容易认错的一个（2026-09-21 那场国赛
            # 被认成快递模板里的 F13，整批下载全失败），界面上要显示、也要能改。
            "file_url": fm.get("file_url") or "",
        })

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path

        # 单实例探测用。刻意不校验令牌：它只回一句"我是谁"，不含任何可操作的东西，
        # 而第二个进程正是靠这句话认出"原来那个还在跑"，此时它还没有令牌。
        if path == "/api/ping":
            self._json({"app": APP_TAG, "pid": os.getpid()})
            return

        if path in ("/", "/index.html"):
            d = find_web_dir()
            if not d:
                self._json({"ok": False, "msg": "找不到 web/index.html"}, 500)
                return
            html = (d / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", TOKEN)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._guard():
            return

        if path == "/api/sites":
            cfg = core.resolve_config()
            # 默认输出位置优先用上次选过的——同事不必每次重挑一遍
            last = str(load_settings().get("last_out") or "")
            self._json({
                "sites": [{"key": k, "title": v["title"]} for k, v in cfg["sites"].items()],
                "default": cfg["default_site"],
                "default_out": last or default_out_dir(),
                "speed": speed_now()[0],
                "speed_steps": SPEED_STEPS,
            })
        elif path == "/api/browse":
            q = parse_qs(urlparse(self.path).query)
            try:
                info = browse_dir(q.get("path", [""])[0])
                info["ok"] = True
                self._json(info)
            except Exception as e:
                self._json({"ok": False, "msg": "打不开这个位置：%s" % e})
        elif path == "/api/check":
            lines = []
            try:
                import openpyxl, requests
                lines.append({"t": "[OK] openpyxl %s" % openpyxl.__version__, "c": "ok"})
                lines.append({"t": "[OK] requests %s" % requests.__version__, "c": "ok"})
            except ImportError as e:
                lines.append({"t": "[X ] 依赖缺失：%s" % e, "c": "err"})
            cfg = core.resolve_config()
            site = cfg["sites"][cfg["default_site"]]
            fm = site["field_map"]
            try:
                s = core.build_session(site)
                d = s.post(site["query_url"], json={
                    fm["name"]: "__自检__", fm["id"]: "000000000000000000",
                    "FRMID": site["frmid"], "SHORTID": site["shortid"],
                    "PREORNEXT": "FIRST", "PAGEINFO": {}, "PAGESIZE": 10},
                    timeout=30).json()
                lines.append({"t": "[OK] 站点连通：%s（total=%s，0 表示查无此人，属正常）"
                                   % (site["title"], d.get("total")), "c": "ok"})
            except Exception as e:
                lines.append({"t": "[X ] 站点不通：%s" % str(e)[:150], "c": "err"})
            mp = BASE_DIR / "学校名映射.csv"
            lines.append({"t": "[%s] 学校名映射表：%s"
                               % ("OK" if mp.exists() else "·",
                                  "已加载" if mp.exists() else "未创建（可选，用于手工修正校名归并）"),
                          "c": "dim"})
            self._json({"lines": lines})
        elif path == "/api/progress":
            q = parse_qs(urlparse(self.path).query)
            offset = int(q.get("offset", ["0"])[0])
            with LOCK:
                logs = STATE["logs"][offset:]
                self._json({"running": STATE["running"], "stage": STATE["stage"],
                            "done": STATE["done"], "total": STATE["total"],
                            "pct": STATE.get("pct", 0.0),
                            "paused": STATE.get("paused", False),
                            "speed": speed_now()[0],
                            # 试跑标记：界面据此把标题写成「试跑中」/「试跑结束」，
                            # 而不是一律「已完成」。状态要跟事实一致，别糊。
                            "trial": STATE.get("trial", False),
                            "trial_n": STATE.get("trial_n", 0),
                            "extra": STATE["extra"], "logs": logs,
                            "offset": offset + len(logs),
                            "eta": STATE.get("eta", ""),
                            "result": STATE["result"], "error": STATE["error"],
                            "diag": STATE["diag"]})
        else:
            self._json({"ok": False, "msg": "未知接口"}, 404)

    # ---- POST ----
    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path

        if path == "/api/speed":
            # 规则：任务跑着的时候不许随手改 —— 先「暂停」，改完再「继续」。
            # 中途变速会让节流节奏乱掉，同事也看不清"到底按哪个速度在跑"。
            body = self._read_json()
            v = clamp_speed(body.get("speed"))
            if STATE["running"] and not STATE.get("paused"):
                self._json({"ok": False, "speed": v,
                            "msg": "任务正在跑。想改速度，先点「暂停」，改完再点「继续」。"})
                return
            # 只存在内存里：本次任务有效，跑完就作废，下次开任务仍回默认。
            SESSION_SPEED["v"] = v
            if STATE["running"]:              # 暂停中改的 →「继续」时交给引擎换挡
                PENDING_SPEED["v"] = v
            self._json({"ok": True, "speed": v})
            return

        if path == "/api/pause":
            if not STATE["running"]:
                self._json({"ok": False, "msg": "现在没有任务在跑。"})
                return
            PAUSE.clear()
            with LOCK:
                STATE["paused"] = True
                STATE["_eta_pts"] = []        # 暂停期间时间照样在走，别拿旧点算预计
                STATE["eta"] = ""
            push_log("  ⏸ 已暂停（手上这一条处理完就停住）。"
                     "想改速度就在上面「访问速度」调，改完点「继续」。", "warn")
            self._json({"ok": True, "paused": True})
            return

        if path == "/api/resume":
            if not STATE["running"]:
                self._json({"ok": False, "msg": "现在没有任务在跑。"})
                return
            body = self._read_json()
            v = body.get("speed")
            newv = None
            if v is not None:
                newv = clamp_speed(v)
                SESSION_SPEED["v"] = newv
                PENDING_SPEED["v"] = newv
            PAUSE.set()
            with LOCK:
                STATE["paused"] = False
                STATE["_eta_pts"] = []
            push_log("  ▶ 继续" + ("，速度已换成 %.1f 秒/人（从下一个人开始生效）" % newv
                                  if newv else ""), "ok")
            self._json({"ok": True, "paused": False, "speed": newv})
            return

        if path == "/api/start":
            if STATE["running"]:
                self._json({"ok": False, "msg": "已有任务在运行"})
                return
            body = self._read_json()
            name = os.path.basename(body.get("filename", "名单.xlsx"))
            b64 = body.get("content_b64", "")
            if not b64:
                self._json({"ok": False, "msg": "没有收到名单文件"})
                return
            try:
                raw = base64.b64decode(b64)
            except Exception as e:
                self._json({"ok": False, "msg": "名单内容解码失败：%s" % e})
                return
            # 保存位置：用户填了就用他的，没填才用默认值。
            # 位置本身先算「用户选的那个」，再决定要不要往下套一层日期文件夹。
            base_out = (body.get("out") or "").strip() or default_out_dir()
            out = resolve_out_dir(base_out, body.get("subfolder"))

            # 先在这里试着建出来 —— 盘符写错、没有权限，都要在"点开始"这一刻
            # 就拦住并说人话，而不是跑到一半抛一个英文异常出来。
            # 注意顺序：先校验位置、再落临时名单文件。反过来的话，路径非法时会在
            # 系统临时目录白留一个 xlsx 残file，跑一次攒一个，越攒越多。
            try:
                Path(out).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self._json({"ok": False, "msg":
                            "这个保存位置用不了：\n%s\n\n原因：%s\n\n"
                            "请点「浏览…」换一个位置（比如桌面），"
                            "或先手动把文件夹建好再重试。" % (out, e)})
                return

            tmp = Path(tempfile.gettempdir()) / ("certdl_%d_%s" % (int(time.time()), name))
            tmp.write_bytes(raw)
            # 记住的是「用户选的位置」，不是套了日期文件夹之后的路径。
            # 否则下次打开输入框里已经带着日期名，再勾一次就变成 日期/日期 套娃。
            save_settings(last_out=base_out)

            STOP.clear()
            with LOCK:
                STATE["logs"] = []
            threading.Thread(target=job_thread, daemon=True, kwargs=dict(
                students_path=str(tmp), out_dir=out,
                site_key=body.get("site"), province=body.get("province", ""),
                sample=int(body.get("sample") or 0),
                # 界面上一层层勾出来的筛选条件。这里只做形状清洗，
                # 真正的筛选在 load_list 里按和界面同一套规则再算一遍。
                filters=sanitize_filters(body.get("filters")))).start()
            self._json({"ok": True, "out": out})
        elif path == "/api/mkdir":
            body = self._read_json()
            try:
                np = safe_mkdir(body.get("parent") or "", body.get("name") or "")
                self._json({"ok": True, "path": np})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})
        elif path == "/api/peek":
            # 名单一拖进来就先看一眼，别等跑完 45 分钟才发现选错了文件。
            # 这一步只读表头和人名，不发任何网络请求，不碰网站。
            self._peek()
        elif path == "/api/probe":
            # 识别新比赛。只识别、只回显，不写任何文件 ——
            # 写配置是"改磁盘"，必须等用户在界面上确认过再落盘。
            self._probe()
        elif path == "/api/site_add":
            body = self._read_json()
            sid = (body.get("sid") or "").strip()
            with PROBED_LOCK:
                site = PROBED.get(sid)
            if not site:
                self._json({"ok": False,
                            "msg": "这次识别结果已经失效了，请重新识别一次。"})
                return
            # 名字由用户在界面上定。自动识别读的是**网页标题**，而同一个表单模板
            # 被换个比赛接着用时，标题常常还是上一场留下的（2026-09-21 实例：
            # 一场正经比赛被认成「快递信息填写」）。这里只覆盖显示用的名字；
            # 站点 key 由 shortid 派生、字段映射也没动，都不受影响。
            custom = (body.get("title") or "").strip()
            custom_fu = (body.get("file_url") or "").strip()
            if (custom and custom != site["title"]) or (
                    custom_fu and custom_fu != (site["field_map"].get("file_url") or "")):
                site = dict(site)
                site["field_map"] = dict(site["field_map"])
                if custom:
                    site["title"] = custom[:60]
                    push_log("名字改成：%s" % site["title"], "dim")
                if custom_fu:
                    site["field_map"]["file_url"] = custom_fu[:40]
                    push_log("证书文件字段改成：%s" % custom_fu[:40], "dim")
            ok, key, msg = save_site(site)
            if not ok:
                push_log("添加比赛失败：%s" % msg, "err")
                self._json({"ok": False, "msg": msg})
                return
            with PROBED_LOCK:
                PROBED.pop(sid, None)      # 用过了就扔掉，避免误加两次
            push_log("已添加比赛：%s" % site["title"], "ok")
            push_log("  以后打开工具就是它；要换回别的，在上面的下拉框里选。", "dim")
            cfg = core.resolve_config()
            self._json({
                "ok": True, "key": key, "title": site["title"],
                "sites": [{"key": k, "title": v["title"]}
                          for k, v in cfg["sites"].items()],
            })
        elif path == "/api/stop":
            STOP.set()
            PAUSE.set()        # 万一正卡在「暂停」里，得先放它出来才能走收尾流程
            push_log("已请求结束，正在收尾…", "warn")
            self._json({"ok": True})
        elif path == "/api/open":
            q = parse_qs(urlparse(self.path).query)
            what = q.get("what", ["out"])[0]
            given = q.get("path", [""])[0]
            if given:
                # 用户在输入框里手写的路径：给他确认一下这个位置对不对
                target = given
            elif what == "diag":
                # 诊断文件的路径有两个来源，缺一不可：
                #   · 正常跑完（哪怕有下载失败）→ result["diag_file"]，是绝对路径
                #   · 只有崩在异常里 → STATE["diag"]，只有文件名、得跟输出目录拼
                # 2026-09-21 用户现场报的"点了没反应"就出在这儿：原来**只看后者**，
                # 所以"任务正常跑完"这条路 target 恒为空、一声不响。
                # find_latest_report 又只认 15 分钟内生成的报告，隔一会儿再点照样找不到，
                # 它只能当最后的兜底。
                target = str((STATE.get("result") or {}).get("diag_file") or "").strip()
                if not target:
                    base = STATE["out"] or default_out_dir()
                    name = STATE["diag"] or find_latest_report(base)
                    if name:
                        target = str(Path(base) / name)
            elif what == "xlsx":
                target = STATE["xlsx"]
            elif what == "retry":
                target = ((STATE.get("result") or {}).get("retry_xlsx") or "")
            else:
                target = STATE["out"]
            # 打不开时不能装死 —— 用户 2026-09-21 报的"点了没反应"就在这儿。
            # 得说清是"文件还没有"还是"系统不让打开"，界面才有话能回给用户。
            if not target:
                push_log("还没找到可打开的文件 —— 先跑一次任务再看。", "warn")
                self._json({"ok": False, "msg": "还没找到可打开的文件 —— 先跑一次任务再看。"})
                return
            if not Path(target).exists():
                push_log("这个位置现在还不存在：%s（点「开始」时会自动创建）" % target, "warn")
                self._json({"ok": False, "msg":
                            "这个位置现在还不存在：%s（点「开始」时会自动创建）" % target})
                return
            if not core.open_in_file_manager(target):
                push_log("没能自动打开，请手动到这个位置找：%s" % target, "warn")
                self._json({"ok": False, "msg": "系统没能打开它，请手动到这个位置找：%s" % target})
                return
            self._json({"ok": True})
        elif path == "/api/quit":
            self._json({"ok": True})
            clear_runfile()
            threading.Timer(0.4, lambda: os._exit(0)).start()
        else:
            self._json({"ok": False, "msg": "未知接口"}, 404)


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 单实例：双击两次，不该跑出两个程序
#
# 同事双击一次没反应、顺手再双击一次，是最自然的反应。可两个程序同时跑
# 同一份名单、往同一个目录写，结果就是证书缺几份、清单人数对不上，
# 而且事后完全查不出坏在哪一步 —— 这种"不报错的错"最值得防。
#
# 为什么用系统互斥体（内核对象），而不是"看端口有没有被占"？
# 互斥体由内核持有：进程正常退出、崩溃、被任务管理器强杀，都会自动释放。
# 而端口占用会留下各种歧义 —— 万一占着那个端口的是别的软件呢？
# 互斥体没有这个歧义：名字对上，就一定是这个工具的另一个实例。
# ------------------------------------------------------------------
_MUTEX = None


def claim_single_instance():
    """抢到单实例锁返回 True；已经有实例在跑返回 False。"""
    global _MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.CreateMutexW(None, 0, "Local\\CertDL_SingleInstance")
        err = ctypes.get_last_error()
        if not h:
            return True
        if err == 183:                      # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _MUTEX = h
        return True
    except Exception:
        # 拿不到锁也得让程序能跑起来：宁可偶尔重复，也不能打不开。
        return True


def running_url():
    """上一个实例留下的界面地址（含令牌）。拿不到就返回空串。"""
    try:
        d = json.loads(RUNFILE.read_text(encoding="utf-8"))
        u = str(d.get("url") or "")
        if u.startswith("http://127.0.0.1:"):
            return u
    except Exception:
        pass
    return ""


def write_runfile(port, url):
    try:
        RUNFILE.write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "token": TOKEN, "url": url,
             "start": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _proc_alive(pid):
    """那个 pid 对应的进程现在还活着吗？

    只查"进程在不在"，不看它在干嘛。判不准的时候一律返回 True（当它活着）——
    方向必须是"宁可留一次垃圾，也别误删一个真在跑的实例的指路牌"。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            return False                             # 打不开 = 进程已经不在了
        code = ctypes.c_ulong()
        got = k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        if not got:
            return True
        return code.value == 259                     # STILL_ACTIVE
    except Exception:
        return True


def _port_answers(port, timeout=1.2):
    """127.0.0.1:port 现在还听得到人吗？（只做 TCP 连通，不发请求）"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _drop_pointer_files():
    """删掉「运行中.json」+「界面地址.txt」+「打开界面.url」这三个指路文件。"""
    for p in (RUNFILE, BASE_DIR / "界面地址.txt", BASE_DIR / "打开界面.url"):
        try:
            p.unlink()
        except OSError:
            pass


def reap_stale_runfile():
    """清掉上一个进程"没走正常退出流程"留下的指路文件。

    最典型的场景：用户点黑窗口右上角的 X 直接关掉。Windows 下这等于把进程
    当场掐死，finally / atexit 一概不执行，于是「运行中.json」「界面地址.txt」
    「打开界面.url」就全留在程序文件夹里。它们记着**上一次**的端口和一次性
    令牌，早就是废的 —— 留着轻则让人以为程序还开着，重则下次打开
    「打开界面.url」只得到一个打不开的页面。

    判定只看物理事实，不猜：**pid 还在进程表里，且端口还听得到**，才算真在跑。
    （两道一起查，是为了防"pid 恰好被别的进程复用"这种巧合。）
    返回 True = 确实清掉了一份陈旧记录。
    """
    try:
        info = json.loads(RUNFILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:
        _drop_pointer_files()        # 文件坏了（比如只写了一半），也是废的
        return True
    if _proc_alive(info.get("pid")) and _port_answers(info.get("port")):
        return False                 # 真有个实例活着，别动它的指路牌
    _drop_pointer_files()
    return True


def clear_runfile():
    """删掉运行记录 —— 但只删属于自己的那份，别去动别人的。

    连「界面地址.txt」「打开界面.url」一起删：这两个是**启动时给用户指路**用的，
    里面写着本次运行的端口和一次性令牌，程序一退出就全是废信息。
    留着只会在程序文件夹里堆一堆"打不开的地址"，让用户以为坏了。
    （「用户设置.json」不在此列 —— 那是用户自己的选择，得留。）
    """
    try:
        if json.loads(RUNFILE.read_text(encoding="utf-8")).get("pid") != os.getpid():
            return
    except Exception:
        pass
    _drop_pointer_files()


def notify(title, msg):
    """弹一个"必须点掉才消失"的提示框。

    为什么不用黑窗口打印？exe 双击启动时黑窗口一闪就没了，
    同事根本来不及看清。对话框不点掉不会走，这条话才真的传达得到。
    只在"已有实例在跑"这种需要他知情的情况下弹，正常启动不弹——
    不然每次双击都要点一次确定，反而烦人。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
            return
        except Exception:
            pass
    print("[%s] %s" % (title, msg))
    try:
        time.sleep(5)          # 兜底：让黑窗口里的字能被看见
    except Exception:
        pass


def pick_port(start=8731, tries=40):
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 先把上一次"没退干净"留下的指路文件清掉。
    # 最典型的是上一回点黑窗口的 X 直接关 —— 那种关法不给程序任何收拾自己的
    # 机会，于是 运行中.json / 界面地址.txt / 打开界面.url 就留在这儿，
    # 里头记着上一回的端口和早已失效的令牌。不清掉的话，下面"已在运行"的
    # 提示可能拿着一个死地址去开浏览器，同事只会看到"打不开"。
    # 判定只看物理事实（pid 在不在、端口听不听得到），真有人跑就一个都不动。
    try:
        if reap_stale_runfile():
            print("  （顺手清掉了上次没退干净留下的临时文件）")
    except Exception:
        pass

    # 已经有实例在跑 → 把原来那个界面打开给他，然后自己退出。
    # 这一步是防"两个程序同时写同一份名单"的，必须放在最前面。
    if not claim_single_instance():
        url = running_url()
        msg = ("这个工具已经在运行了，不用再打开一个。\n\n"
               "两个窗口同时跑同一份名单，会让证书和清单对不上号，\n"
               "所以这里帮你拦住了。\n\n")
        if url:
            msg += "已经为你打开原来那个界面。\n浏览器没跳出来的话，把下面这一行复制到地址栏：\n\n" + url
        else:
            msg += "请到浏览器里找找已经打开的那个页面；\n找不到就双击程序文件夹里的「打开界面.url」。"
        print(msg)
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        notify("证书下载器已经在运行", msg)
        return

    port = pick_port()
    url = "http://127.0.0.1:%d/?token=%s" % (port, TOKEN)

    print("=" * 62)
    print("  比赛获奖证书批量下载器")
    print("=" * 62)
    print("  界面地址：%s" % url)
    print("")
    print("  【重要】这个黑色窗口不要关 —— 关了程序就停。")
    print("          可以把它最小化，浏览器也可以关掉去干别的事，")
    print("          任务会在后台继续跑，重新打开页面就能看到进度。")
    print("          跑的过程中别让电脑睡眠（建议插上电源）。")
    print("")
    print("  浏览器没自动打开？双击本文件夹里的「打开界面.url」就行。")
    print("  用完后：在页面底部点「关闭程序」，或直接关掉这个黑窗口都可以。")
    print("          （直接点右上角 X 关也没事，留下的临时文件下次启动会自动清。）")
    print("=" * 62)

    # 控制台字符偶尔会被别的程序刷乱，落一份到文件里，排障时能找到地址
    try:
        (BASE_DIR / "界面地址.txt").write_text(
            "界面地址：%s\n启动时间：%s\n\n"
            "浏览器没自动打开？两个办法，随便挑一个：\n"
            "  ① 直接双击本文件夹里的「打开界面.url」（推荐，最省事）\n"
            "  ② 把上面那行地址整行复制到浏览器地址栏\n"
            % (url, time.strftime("%Y-%m-%d %H:%M:%S")),
            encoding="utf-8-sig")
    except Exception:
        pass

    # 再给一个双击就能开的快捷方式——比让同事去复制一长串地址友好得多
    try:
        (BASE_DIR / "打开界面.url").write_text(
            "[InternetShortcut]\r\nURL=%s\r\n" % url, encoding="utf-8")
    except Exception:
        pass

    # 记下"我在跑、端口和令牌是这个"，供后来被双击起来的实例认出并跳转过来
    write_runfile(port, url)

    # 退出时再兜一道：finally 管得住正常退出和 Ctrl+C，管不住 sys.exit() 或
    # 未捕获异常把解释器带走的路径。atexit 补上这一段 —— 多删一次顶多白跑一趟，
    # 少删一次就是给用户留一堆打不开的地址。
    # （点 X 关窗口是**当场掐死进程**，这两道都拦不住；那种情况交给下次启动时的 reap。）
    try:
        atexit.register(clear_runfile)
    except Exception:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if not os.environ.get("CERTDL_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        clear_runfile()


if __name__ == "__main__":
    main()
