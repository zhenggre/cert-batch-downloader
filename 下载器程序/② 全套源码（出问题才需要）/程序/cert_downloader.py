# -*- coding: utf-8 -*-
"""
比赛获奖证书批量下载器 · 核心引擎
=====================================================================
流程（顺序很关键）：
  ① 全量查询      —— 只发查询请求，拿到每个人的奖项/学校/证书地址（1.6 秒/人）
  ② 学校名归一化  —— 用全部人的数据做锚定+聚类，把五花八门的手填校名归并
  ③ 下载证书      —— 按归一化后的学校名建文件夹，把 PDF 放进去
  ④ 生成汇总清单  —— 一个 Excel 说清「哪几个学校、每校各多少人」

为什么先全量查询再下载？
  归一化的第 1 层靠「人员锚定」——同一个人既有手填校名、又有证书官方校名，
  这条映射就自动建立了。数据越全，锚定覆盖越广，归并越准。

用法：
  python cert_downloader.py run --input 名单.xlsx              # 输出到桌面下的「证书」
  python cert_downloader.py run --input 名单.xlsx --out ./结果  # 自己指定输出目录
  python cert_downloader.py run --input 名单.xlsx --sample 8 --seed 7
  python cert_downloader.py check            # 自检：站点连通性 + 依赖
=====================================================================
"""
import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# 打包成 exe 后 __file__ 指向解压出的临时目录，配置/映射表必须去 exe 所在目录找，
# 否则同事改 config.json 会改到无效位置。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
ROOT = BASE_DIR

from school_merge import load_manual_map, merge_schools   # noqa: E402
import error_help as EH                                  # noqa: E402

# 程序版本。故障诊断报告里会写出去 —— 收到"它出错了"的反馈时，
# 第一件要确认的就是"跑的是哪一版"。改功能就 +1。
APP_VERSION = "1.5"

# 出厂默认配置 = 占位模板。首次启动会据此生成 config.json，
# 用户需把 <...> 换成自己要查的比赛查询页；也可在界面点「＋ 新比赛」自动识别。
DEFAULT_CONFIG = {
    "default_site": "site_example",
    "sites": {
        "site_example": {
            "title": "<比赛名称>",
            "home_url": "https://biaodan100.com/q/<SHORTID>",
            "query_url": "https://biaodan100.com/web/pubdata/query?SHORTID=<SHORTID>",
            "shortid": "<SHORTID>",
            "frmid": "<从查询页源码里抓的 frmid>",
            "field_map": {"name": "F1", "id": "F999", "award": "F2", "school": "F3",
                          "province": "F16", "city": "F17", "district": "F15",
                          "teacher": "F6", "file_url": "URL"},
            "list_columns": {"name": ["查询姓名", "参赛学生姓名", "姓名", "学生姓名", "考生姓名"],
                             "id_number": ["查询证件号", "参赛学生身份证号", "证件号", "身份证号",
                                           "身份证", "证件号码"],
                             "school": ["学校名称", "学校", "就读学校", "所在学校"],
                             "province": ["所在地区(省/自治区/直辖市)", "省份", "地区", "省"]},
        }
    },
}

# 这些字样出现在结果里代表「没查到」，不作为有效证书
NOT_FOUND_HINTS = ["未查询到", "查询不到", "无此", "没有找到", "不存在"]


def log(msg, sink=None):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # 输出被重定向到文件、而系统编码是 GBK 时，⚠ ✓ 这类符号会让 print 抛异常。
        # 一行日志不该把整个作业搞崩，降级成"能显示多少算多少"。
        try:
            enc = sys.stdout.encoding or "gbk"
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
    except Exception:
        pass
    if sink is not None:
        sink(line)


def sanitize(name):
    """把字符串变成合法的文件名/文件夹名。

    过滤的是 Windows 的非法字符集（反斜杠、斜杠、冒号、星号、问号、
    双引号、尖括号、竖线，加上控制字符）。这套规则最严，照它过滤出来的
    名字在哪儿都合法，不必分平台写两份。
    """
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(". ") or "未命名"


# ------------------------------------------------------------------
# 字符归一化：把「同一个证件的不同写法」收敛成一种
#
# 为什么非做不可？名单是家长/老师手填的，同一个人在不同表格里可能是
# 「4201 0219 …」（中间带空格）、末位小写 x、全角数字等写法。
# 网站存的只有标准形式，只要有一个字符对不上，查询就返回「查无此人」——
# 而程序会老老实实把它记成「未获奖」。
#
# 这类失败最阴的地方在于：不报错、不崩溃、事后也查不出来。
# 609 人里漏 3 个，没人会发现。所以必须在发出请求之前就抹平。
# ------------------------------------------------------------------
def normalize_id(v):
    """证件号归一化：全角转半角 → 去掉所有空白 → 末位校验码统一大写。"""
    s = unicodedata.normalize("NFKC", str(v or ""))
    s = re.sub(r"\s+", "", s)
    if s and s[-1] in ("x", "X"):
        s = s[:-1] + "X"
    return s


def normalize_name(v):
    """姓名归一化：全角转半角 → 去掉内部空白（「张 三」→「张三」）。

    只对含中文的名字去空格：中文姓名里不该有空格，出现就是误输入。
    英文/外文名（如「John Smith」）的空格是有意义的，一律不动。
    也刻意不动「·」——少数民族姓名里的间隔符不能丢。
    """
    s = unicodedata.normalize("NFKC", str(v or "")).strip()
    if re.search(r"[\u4e00-\u9fff]", s):
        s = re.sub(r"\s+", "", s)
    return s


# ------------------------------------------------------------------
# 跟系统打交道的小工具（Windows 专用）
#
# 为什么单独拎出来？这两件事都得调系统自带的能力，散落在主流程里
# 迟早会漏改，最后变成"点了没反应"这种哑巴故障。
# ------------------------------------------------------------------
def default_out_dir():
    """默认证书输出目录 —— 只是个开场默认值，同事随时能在界面上改。

    习惯上放「桌面\证书」：这台机器桌面挪到了 D 盘，就用 D 盘那个；
    桌面还在 C 盘的机器，自动落到系统桌面下。两种情况都能用。
    """
    if os.name == "nt":
        d = Path("D:/Desktop")
        if d.is_dir():
            return str(d / "证书")
    return str(Path.home() / "Desktop" / "证书")


def open_in_file_manager(target):
    """在资源管理器里打开目录/文件，返回是否成功。

    用 os.startfile —— 效果等同于同事自己双击那个文件夹。
    这只是个便利功能（让同事一眼看到证书在哪），失败不影响主流程，异常一律吞掉。
    """
    p = str(target or "").strip()
    if not p:                      # 空路径不试——有些系统会当"打开当前目录"而谎报成功
        return False
    try:
        os.startfile(p)                    # 等同于双击，效果就是弹出资源管理器
        return True
    except Exception:
        return False


def resolve_config(path=None):
    p = Path(path) if path else (ROOT / "config.json")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))          # 深拷贝
    if p.exists():
        user_cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg["default_site"] = user_cfg.get("default_site", cfg["default_site"])
        cfg["sites"].update(user_cfg.get("sites", {}))
        # 速度这一项也得带过来 —— 这里只挑 default_site/sites 的话，
        # 同事在 config.json 里改的速度会被顺手丢掉，怎么改都不生效。
        if "每条间隔秒数" in user_cfg:
            cfg["每条间隔秒数"] = user_cfg["每条间隔秒数"]
    return cfg


# ------------------------------------------------------------------
# 名单读取
# ------------------------------------------------------------------
# 模糊匹配时要避开的字眼：这些词一旦出现在表头里，说明它是个"选择框/类型/联系方式"，
# 不是我们想要的真正值列。典型坑：「选择证件类型」会抢在「参赛学生身份证号」前面被选中，
# 结果拿"身份证"三个字当号码去查，全军覆没。
_COL_TRAP_WORDS = ("类型", "选择", "监护人", "家长", "老师", "手机", "电话", "联系")


def _pick_column(headers, candidates, keywords=None):
    """在表头里挑一列：先精确匹配候选名，再按关键词模糊匹配。

    模糊匹配分两轮：先只在"干净"的表头里找（避开类型/选择/监护人这类干扰列），
    实在找不到才退回最宽松的匹配。这样「学校名称」这类正常列不受影响，
    同时挡掉「选择证件类型」冒充证件号、「监护人姓名及手机号」冒充姓名。

    keywords 可以传一个词，也可以传一组词（任中其一即可）。证件号那列尤其需要
    多个词：表头可能写「证件号」，也可能写「身份证号」「身份证号码」——
    后者里根本没有"证件"两个字，只认一个词就会整列识别失败、直接罢工。
    """
    hs = [str(h).strip() for h in headers]
    for c in candidates or []:
        if c in hs:
            return c
    kws = [keywords] if isinstance(keywords, str) else list(keywords or [])
    if kws:
        for h in hs:
            if any(k in h for k in kws) and not any(w in h for w in _COL_TRAP_WORDS):
                return h
        for h in hs:
            if any(k in h for k in kws):
                return h
    return None


def cell_text(v):
    """把一个单元格的值转成字符串（唯一的规则）。

    名单里的东西五花八门：数字、浮点、日期、None、前后带空格。
    这里**只此一处**定义转换规则，因为筛选界面（浏览器里那份数据）和
    正式开跑（这里）必须按同一套规则比字符串 —— 两边各写一份的话，
    迟早出现"界面上筛出 12 人、实际跑了 15 人"这种对不上账的鬼事，
    而且极难查。
    """
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _row_match_filters(row, filters):
    """这一行满足全部筛选条件吗？各列之间是 AND（越筛越窄）。

    每一列的条件长这样：
        {"values": ["武汉市光谷第一小学", ...],   # 勾选的值 → 精确相等
         "text":   "武汉"}                        # 关键词   → 包含匹配

    同一列内部是 OR：勾了一堆值本来就是"其中之一"的意思，再加个关键词框，
    合起来用 OR 才符合直觉（勾了 A 校、又想顺带看 B 类，不该互相排斥）。

    表头里没有这个列名时，这个条件当不存在 —— 界面上的列是从同一份表头
    取的，正常不会发生；真发生了也宁可少筛一层，不要把整份名单清空。
    """
    for col, cond in (filters or {}).items():
        if not isinstance(cond, dict):
            continue
        vals = [str(v) for v in (cond.get("values") or [])]
        text = str(cond.get("text") or "").strip()
        if not vals and not text:
            continue                       # 空条件 = 这列不筛
        if col not in row:
            continue
        v = cell_text(row.get(col))
        hit = (bool(vals) and v in vals) or (bool(text) and text in v)
        if not hit:
            return False
    return True


def _filters_desc(filters):
    """把筛选条件说成人话，写进日志。同事看到"跑了多少人"的同时，
    得知道"这是按什么筛出来的"，否则他会以为程序漏了人。"""
    parts = []
    for col, cond in (filters or {}).items():
        vals = [str(v) for v in (cond.get("values") or [])]
        text = str(cond.get("text") or "").strip()
        if not vals and not text:
            continue
        bits = []
        if vals:
            bits.append("、".join(vals[:6]) + ("…" if len(vals) > 6 else ""))
        if text:
            bits.append("含「%s」" % text)
        parts.append("%s = %s" % (col, " 或 ".join(bits)))
    return "；".join(parts) if parts else "无"


def load_list(path, columns=None, province_filter=None, quiet=False, filters=None):
    """读名单，返回 [{"name","id","raw_school"}]，并附带表头诊断信息。

    quiet=True 时只读取、不打印 —— 界面上的"上传后立刻看看有多少人"
    也走这个函数，那次不该在日志里留痕，否则正式开跑时同事会看到
    同样的话出现两遍，以为跑了两次。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("找不到名单文件：%s" % path)

    columns = columns or {}
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = list(next(rows_iter))
        except StopIteration:
            raise ValueError("名单文件是空的")
        data = [dict(zip([str(h).strip() if h is not None else "" for h in headers], r))
                for r in rows_iter]
        wb.close()
    elif p.suffix.lower() == ".csv":
        with io.open(p, "r", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            data = list(rd)
    else:
        raise ValueError("只支持 .xlsx / .xlsm / .csv，当前是 %s" % p.suffix)

    if not data:
        raise ValueError("名单里没有数据行")

    headers = list(data[0].keys())
    c_name = _pick_column(headers, columns.get("name"), ("姓名",))
    c_id = _pick_column(headers, columns.get("id_number"), ("证件", "身份证"))
    c_school = _pick_column(headers, columns.get("school"), ("学校",))
    c_prov = _pick_column(headers, columns.get("province"), ("地区", "省"))

    if not c_name:
        raise ValueError("名单里找不到「姓名」列。现有列名：%s" % headers)
    if not c_id:
        raise ValueError("名单里找不到「证件号」列。现有列名：%s" % headers)

    cell = cell_text        # 转字符串的规则只有一份，见模块顶上的 cell_text()

    students, skipped = [], 0
    fix_id = fix_name = 0
    for i, r in enumerate(data, 2):
        name_raw = cell(r.get(c_name))
        id_raw = cell(r.get(c_id))
        if not name_raw or not id_raw or name_raw.lower() in ("nan", "none"):
            skipped += 1
            continue
        if province_filter and c_prov:
            if province_filter not in cell(r.get(c_prov)):
                continue
        # 表头多级筛选：界面上一层层勾出来的条件，在这里按同一套规则再算一遍。
        # 为什么不直接信界面给的人数？因为界面那个数是浏览器里现算的，
        # 万一两边规则有出入，同事看到"筛出 12 人"却下来 15 份，只会以为程序坏了。
        # 后端只认自己算出来的结果。
        if filters and not _row_match_filters(r, filters):
            continue
        # 归一化之后才拿去查；和原始写法不同时把原值留着，万一查不到还能兜一次
        name, idn = normalize_name(name_raw), normalize_id(id_raw)
        if idn != id_raw:
            fix_id += 1
        if name != name_raw:
            fix_name += 1
        students.append({
            "row": i, "name": name, "id": idn,
            "name_alt": name_raw if name_raw != name else "",
            "id_alt": id_raw if id_raw != idn else "",
            "raw_school": cell(r.get(c_school)) if c_school else "",
            "province_raw": cell(r.get(c_prov)) if c_prov else "",
            "_raw": r,
        })
    info = {"file": p.name, "columns": {"name": c_name, "id": c_id,
                                        "school": c_school, "province": c_prov},
            "total_rows": len(data), "loaded": len(students), "skipped": skipped,
            "fixed_id": fix_id, "fixed_name": fix_name}
    if quiet:
        return students, info
    log("名单：%s  共 %d 行 → 有效 %d 条（跳过空行 %d）"
        % (p.name, len(data), len(students), skipped))
    # 动过名单里的数据，就必须明说。用户有权知道程序改了什么，
    # 否则万一结果有偏差，他连该怀疑哪里都不知道。
    if fix_id or fix_name:
        log("      已自动修正写法：证件号 %d 条、姓名 %d 条"
            "（空格／全角字符／末位小写 x 之类，只用于查询，原文件未改动）" % (fix_id, fix_name))
    log("      识别列：姓名=%s 证件=%s 学校=%s 省份=%s"
        % (c_name, c_id, c_school or "无", c_prov or "无"))
    return students, info


# ------------------------------------------------------------------
# 查询 & 下载
# ------------------------------------------------------------------
def build_session(site):
    # 每个平台的握手方式不一样（金数据要先 GET 查询页拿 cookie 才认请求），
    # 所以按平台分发，别把各家的细节混进这一处。
    if site.get("platform") == "jinshuju":
        import platform_jinshuju as JS
        JS.set_logger(log)
        return JS.build_session(site)
    import requests
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": site["home_url"],
        "Origin": re.match(r"^(https?://[^/]+)", site["home_url"]).group(1),
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"),
    })
    try:
        s.get(site["home_url"], timeout=30)
    except Exception as e:
        log("预热请求失败（继续）：%s" % e)
    return s


# ------------------------------------------------------------------
# 礼貌节流：宁可慢，也不要被网站封 IP
# ------------------------------------------------------------------
# 风控盯的是「节奏」而不是单次请求。固定间隔、连错不停、零延迟，这三样最容易触发封禁。
# 别人家办公室通常共用一个出口 IP，一旦被封是整个公司都查不了 —— 所以这里默认走最稳档。
PACE = {
    "safe": 1.6,     # 稳妥（默认）：609 人约 16 分钟查询，像人手点的速度
    "normal": 0.9,
    "fast": 0.3,     # 只在网络状况极好、确认对方没风控时用
}

# 给不懂代码的同事留的口子：config.json 里那个「每条间隔秒数」。
# 下限卡在 0.3 秒 —— 再快就不像人在点，容易被网站风控把整个公司的出口 IP 封掉。
# 这活我干过一次丢人的事（超速 170 倍被封 IP），所以下限写死，改不动。
DELAY_MIN = 0.3
DELAY_MAX = 10.0


def resolve_delay(path=None):
    """从 config.json 读「每条间隔秒数」，并夹到安全范围。

    返回 (秒数, 提示语)：
      · 没配 / 配得正常 —— 提示语为空字符串；
      · 配得不是数字 —— 退回默认档，给出提示；
      · 配得太快或太慢 —— 夹到边界，给出提示。
    提示语由调用方打进日志，让同事知道自己写的数为什么没生效。
    """
    default = PACE["safe"]
    try:
        cfg = resolve_config(path)
    except Exception as e:
        # 同事多半是用记事本改的时候把引号/逗号弄丢了，JSON 就烂了。
        # 这种情况不能让整个任务崩掉 —— 退回默认速度，并告诉他哪儿错了。
        return default, ("读 config.json 出错了（%s），本次按默认 %.1f 秒跑。"
                         "大概是改的时候把引号、逗号弄丢了 —— "
                         "最省事的办法是删掉这个文件，程序会自动重建一份。" % (e, default))
    raw = cfg.get("每条间隔秒数", default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default, ("config.json 里的「每条间隔秒数」不是数字（写的是 %r），"
                         "已按默认 %.1f 秒跑。" % (raw, default))
    if val <= 0:
        return default, ("config.json 里的「每条间隔秒数」是 %g，不合法，"
                         "已按默认 %.1f 秒跑。" % (val, default))
    if val < DELAY_MIN:
        return DELAY_MIN, ("config.json 里「每条间隔秒数」写的是 %g 秒，太快了 —— "
                           "这么跑会被网站当成机器人，把公司 IP 封掉。"
                           "已自动放慢到 %.1f 秒（再快改不动）。" % (val, DELAY_MIN))
    if val > DELAY_MAX:
        return DELAY_MAX, ("config.json 里「每条间隔秒数」写的是 %g 秒，太慢了，"
                           "已按 %.1f 秒跑。" % (val, DELAY_MAX))
    return val, ""


class Throttler:
    """间隔 + 随机抖动 + 出错指数退避 + 定期长歇 + 连错冷却。

    四层保护，任何一层触发都会自动放慢，不需要人工干预：
      1) 抖动  每条之间乘随机系数，不形成固定节拍（固定节奏最像机器人）
      2) 退避  出错后等待指数增长；429/403/5xx 直接跳到较大档位
      3) 长歇  每 N 条多歇一次，模拟人翻页时的停顿
      4) 冷却  连续错误达阈值就整体停一会儿，绝不硬刚
    """

    def __init__(self, base=1.6, jitter=0.45, every=80, long_pause=10.0,
                 err_burst=6, cooldown=120.0, backoff_base=6.0, backoff_max=180.0,
                 abort_streak=None):
        self.base = base
        self.jitter = jitter
        self.every = every
        self.long_pause = long_pause
        self.err_burst = err_burst
        self.cooldown = cooldown
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.abort_streak = abort_streak or err_burst * 3
        self.streak = 0      # 连续错误数
        self.rests = 0       # 长歇次数
        self.cooldowns = 0   # 冷却次数

    def note_ok(self):
        self.streak = 0

    def note_err(self, kind=""):
        """记一次错误。

        kind 为 blocked / rate / server 时，说明是对面明确拒绝或故障
        （403、429、5xx），直接跳到较大退避档——这类问题不会因为
        "再试快一点"就变好，反而越试越糟。
        """
        if kind in ("blocked", "rate", "server"):
            self.streak = max(self.streak + 1, 3)
        else:
            self.streak += 1

    @property
    def should_abort(self):
        return self.streak >= self.abort_streak

    def wait(self, idx):
        """按当前状态决定等多久并等待，返回 (秒数, 原因)。"""
        why = ""
        if self.streak >= self.err_burst:
            secs = self.cooldown
            why = "冷却"
            self.cooldowns += 1
        elif self.streak:
            secs = min(self.backoff_base * (2 ** (self.streak - 1)), self.backoff_max)
            secs *= random.uniform(0.9, 1.15)
            why = "退避"
        else:
            secs = self.base * random.uniform(1 - self.jitter, 1 + self.jitter)
            if self.every and idx % self.every == 0:
                secs += self.long_pause * random.uniform(0.8, 1.5)
                why = "长歇"
                self.rests += 1
        time.sleep(secs)
        return secs, why


def query_one(sess, site, name, idnum, retry=3):
    """查一个人 → (状态码, 信息字典)。状态码：hit / miss / error

    重试本身也退避 —— 被限流时立刻连打，是最容易把 IP 打没的行为。"""
    if site.get("platform") == "jinshuju":
        import platform_jinshuju as JS
        JS.set_logger(log)
        return JS.query_one(sess, site, name, idnum, retry)
    fm = site["field_map"]
    payload = {fm.get("_frmid_key", "FRMID"): site["frmid"],
               fm["name"]: name, fm["id"]: idnum,
               "SHORTID": site["shortid"],
               "PREORNEXT": "FIRST", "PAGEINFO": {}, "PAGESIZE": 10}
    last, kind = "", ""
    for attempt in range(retry):
        try:
            r = sess.post(site["query_url"], json=payload, timeout=40)
            # 先看状态码：403/429 是明确的限流信号，5xx 说明服务端扛不住，两者都要让路
            if r.status_code in (403, 429) or r.status_code >= 500:
                # 先分清"这个错误是谁发的"：本地/公司代理代发的 502/403，
                # 和网站自己回的，处理办法完全不同。
                px = proxy_in_env()
                if looks_like_proxy_response(r):
                    kind = "proxy"
                    last = "HTTP %d（被本机/公司代理拦下%s）" % (
                        r.status_code, "，当前代理=" + px if px else "")
                elif r.status_code >= 500 and px:
                    # 本机走代理时，5xx 优先怀疑代理转发失败——因为"傻等半小时"
                    # 的代价比"检查一下代理"大得多。
                    kind = "proxy"
                    last = "HTTP %d（本机有代理 %s，可能是代理转发失败）" % (r.status_code, px)
                elif r.status_code in (403, 429):
                    kind, last = "blocked", "HTTP %d（网站拒绝了请求，疑似被限流）" % r.status_code
                elif r.status_code == 503:
                    kind, last = "server", "HTTP 503（网站暂时不可用，也可能是限流）"
                else:
                    kind, last = "server", "HTTP %d（对方服务器故障）" % r.status_code
                time.sleep(3.0 * (attempt + 1) * random.uniform(1.0, 1.4))
                continue
            d = r.json()
            if d.get("total"):
                row = d["rows"][0]
                return "hit", {
                    "award": (row.get(fm["award"]) or "").strip(),
                    "school": (row.get(fm["school"]) or "").strip(),
                    "province": (row.get(fm["province"]) or "").strip(),
                    "city": (row.get(fm["city"]) or "").strip(),
                    "district": (row.get(fm["district"]) or "").strip(),
                    "teacher": (row.get(fm["teacher"]) or "").strip(),
                    "file_url": (row.get(fm["file_url"]) or "").strip(),
                    "total": d.get("total"),
                }
            return "miss", {"total": 0}
        except Exception as e:
            kind = classify_exc(e)
            last = "%s: %s" % (type(e).__name__, str(e)[:120])
            time.sleep(2.0 * (attempt + 1) * random.uniform(1.0, 1.3))
    return "error", {"err": last, "kind": kind}


def proxy_in_env():
    """本机是否配置了代理（环境变量或系统代理）。返回代理地址，没有则空串。

    为什么要管这个？公司电脑经常挂统一代理，或同事自己开着 VPN/加速器。
    这种情况下请求是先发给代理的，代理送不出去时会回一个它自己造的 502——
    看起来就像"比赛网站崩了"，实际是本地代理的问题。
    分不清这两件事，给出的建议就完全跑偏。

    用 urllib 标准库拿系统代理（Windows 上它自己去读注册表），
    比自己摸 winreg 可靠，也不用为读注册表额外引入模块。
    """
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            return v
    try:
        from urllib.request import getproxies
        px = getproxies()       # 形如 {'http': '127.0.0.1:65513', 'https': ...}
        for scheme in ("https", "http"):
            v = px.get(scheme)
            if v:
                return str(v)
    except Exception:
        pass
    return ""


def looks_like_proxy_response(r):
    """判断这个错误响应是不是本地/公司代理代发的（而不是目标网站自己回的）。"""
    try:
        h = {str(k).lower(): str(v).lower() for k, v in r.headers.items()}
    except Exception:
        h = {}
    if any(k in h for k in ("via", "x-proxy-id", "x-squid-error", "proxy-connection")):
        return True
    srv = h.get("server", "")
    if "proxy" in srv or "squid" in srv or "gateway" in srv:
        return True
    try:
        body = r.text[:2000].lower()
    except Exception:
        body = ""
    return any(k in body for k in ("squid", "err_proxy", "代理服务器", "proxy error"))


def classify_exc(e):
    """把网络异常翻译成故障类型，好让 error_help 给出对得上的自救方案。

    顺序很重要：域名解析失败本身也是 ConnectionError，必须先判出来，
    否则会被误判成"被限流"，给出完全没用的建议。"""
    import requests

    name = type(e).__name__.lower()
    msg = str(e).lower()
    if isinstance(e, requests.exceptions.ProxyError) or "proxyerror" in name:
        return "proxy"
    if isinstance(e, requests.exceptions.Timeout) or "timeout" in name or "timed out" in msg:
        return "timeout"
    if ("name resolution" in msg or "getaddrinfo" in msg or "11001" in msg
            or "nodename" in msg or "temporary failure" in msg):
        return "dns"
    if "cannot connect to proxy" in msg:
        return "proxy"
    if ("refused" in msg or "10061" in msg or "reset" in msg or "10054" in msg
            or "aborted" in msg or "forbidden" in msg or "403" in msg or "429" in msg):
        return "blocked"
    return "net"


def classify_download_err(err):
    """下载失败的原因归类。"""
    if not err:
        return "unknown"
    if "生成" in err:
        return "pdf_stuck"
    if "既非" in err and "PDF" in err:
        return "pdf_weird"
    low = err.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "refused" in low or "reset" in low or "10054" in low or "10061" in low:
        return "blocked"
    if "getaddrinfo" in low or "name resolution" in low or "11001" in low:
        return "dns"
    return "net"


def is_no_file(target, file_url):
    """页面上压根没给出证书文件地址 —— 这不是「下载失败」。

    2026-09-21 用户那次国赛：成绩刚出、证书还没开放下载，每个人取到的
    file_url 都是空串，整批被判成「下载失败」（抛的还是 MissingSchema
    这种同事看不懂的东西）—— 既误导人，又白白喂给退避/冷却/熔断计数，
    跑出一份全是假失败的名单。所以单列成一类「暂无证书文件」。

    判据只看「有没有地址」，与网络无关：
      · target 已经有值 → 是续跑命中的老文件，跳过优先，不算这一类；
      · file_url 空 / 纯空白 → 页面上确实没给地址 → 属这一类。

    抽成纯函数是为了能被单测盯住（埋在下载循环里就测不到）；
    没有地址时**绝不能去发请求**，否则又会拿 MissingSchema 冒充"网络失败"。
    """
    return bool(target is None and not str(file_url or "").strip())


def fetch_pdf(sess, url, max_wait=120):
    """反复请求直到拿到真正的 PDF。
    这个站点首次请求只回 4 字节 'init'（服务端在生成），约 2 秒后再请求即可。
    判定一律看文件头魔术字节，绝不信 URL 后缀（这里的后缀写着 .docx，实际是 PDF）。
    轮询刻意放慢并带抖动：一份证书生成期间只问几次，别敲门敲成攻击。"""
    t0, tries, last = time.time(), 0, ""
    while time.time() - t0 < max_wait:
        tries += 1
        try:
            r = sess.get(url, timeout=90)
            b = r.content
            if cert_magic_ok(b):
                return b, "", tries
            if b[:4] == b"init" or len(b) < 200:
                last = "服务端生成中"
            else:
                return None, "既非 PDF/JPG/PNG 也非 init，前 20 字节=%r" % b[:20], tries
        except Exception as e:
            return None, "下载异常 %s: %s" % (type(e).__name__, str(e)[:100]), tries
        time.sleep(random.uniform(2.5, 4.0))
    return None, "等待 %ds 仍未生成完成（%s）" % (max_wait, last or "超时"), tries


# ------------------------------------------------------------------
# 批次认亲：同一个比赛散在几个日期文件夹里，得能把它们认成一家
#
# 勾了「自动建日期文件夹」（默认就是勾上的）之后，9 月 18 号跑的落在
# 证书_2026-09-18，19 号再跑的落在 证书_2026-09-19 —— 两个兄弟目录。
# 这带来两个必须回答的问题：
#   ① 汇总清单说「这个位置一共多少份证书」，只数自己那一层就永远是本批次的数，
#      同事看着硬盘上明明有 600 份、清单写着 300 份，只会以为程序漏下了。
#   ② 第二天再跑时，同一个人要不要重下？重下是白等 15 分钟；不重下就得先
#      跨目录知道"他昨天已经下来了"。
#
# 而这两件事都靠不住"目录名像不像"来判 —— 父目录下可能同时躺着别的比赛。
# 两个比赛撞上同名同证件号的学生时，认错亲的后果是：后者被当成"昨天下过了"
# 直接跳过，证书静默少一份，跑完谁也不看得出来。
#
# 所以每个批次目录里留一张名片，写明自己是哪个比赛。
# ------------------------------------------------------------------
BATCH_DIR_PAT = re.compile(r"^证书_(\d{4}-\d{2}-\d{2})$")
MARKER_NAME = "比赛信息.json"


def write_batch_marker(out_root, site_key="", site=None):
    """在批次目录里留一张名片：这个文件夹是哪个比赛留下的。

    写失败也不影响主流程（老机器可能没权限），所以整体吞掉异常。
    认不出亲的代价只是"累计少算"，不是"算错"。
    """
    site = site or {}
    data = {"site_key": site_key or "",
            "title": site.get("title") or "",
            "home_url": site.get("home_url") or "",
            "written": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        d = Path(out_root)
        d.mkdir(parents=True, exist_ok=True)
        p = d / MARKER_NAME
        # 这里刻意用不带 BOM 的 utf-8：这张名片是给程序读的（json.load），
        # 带 BOM 会让某些解析器直接报错。给人看的 txt 才用 utf-8-sig。
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return str(p)
    except Exception:
        return ""


def _site_from_log(d):
    """老目录没有名片时，退而从「运行日志_*.txt」里认「站点：xxx」那一行。

    升级之前跑出来的批次目录都走这条路，否则它们会一夜之间变成"野目录"，
    累计数平白缩水 —— 那比不认亲更让人慌。
    """
    try:
        logs = sorted(Path(d).glob("运行日志_*.txt"))
    except Exception:
        return ""
    for f in logs:
        try:
            with io.open(f, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("站点："):
                        return line[3:].strip()
                    if "阶段 1" in line:      # 过头了就别再翻了
                        break
        except Exception:
            continue
    return ""


def read_batch_marker(d):
    """读批次目录的名片。返回 {} 表示认不出。

    认不出的目录一律不参与合并 —— 宁可少算，不可算错。
    """
    d = Path(d)
    try:
        p = d / MARKER_NAME
        if p.exists():
            with io.open(p, "r", encoding="utf-8") as f:
                v = json.load(f)
            if isinstance(v, dict) and (v.get("site_key") or v.get("title")):
                return v
    except Exception:
        pass
    t = _site_from_log(d)
    return {"site_key": "", "title": t} if t else {}


def _same_race(a, b):
    """两个批次目录是不是同一个比赛。

    双方都带站点标识时以它为准（最硬）；只有一方带 —— 升级前跑出来的老目录
    没写过名片，只在运行日志里留了比赛名 —— 才退到比比赛名。
    凭据对不上或缺失就判"不是一家"：宁可少算，不可算错。
    """
    ka = (a.get("site_key") or "").strip()
    kb = (b.get("site_key") or "").strip()
    if ka and kb:
        return ka == kb
    ta = (a.get("title") or "").strip()
    tb = (b.get("title") or "").strip()
    return bool(ta) and ta == tb


def batch_roots(out_root):
    """要和 out_root 一起统计的批次目录：它自己 + 同级里同一个比赛的兄弟。

    只有 out_root 自己就长成「证书_YYYY-MM-DD」时才去找兄弟 ——
    没勾日期文件夹时，用户选的那个目录就是唯一的一批，不该四处张望。
    """
    root = Path(out_root)
    if not BATCH_DIR_PAT.match(root.name):
        return [root]
    mine = read_batch_marker(root)
    try:
        peers = [p for p in root.parent.glob("证书_*")
                 if p.is_dir() and BATCH_DIR_PAT.match(p.name)]
    except Exception:
        peers = []
    outs = [root]
    for p in peers:
        if p == root:
            continue
        # mine 为空 = 连自己都认不出，那就只算自己，别乱认兄弟
        if mine and _same_race(mine, read_batch_marker(p)):
            outs.append(p)
    return sorted(set(outs))


# ------------------------------------------------------------------
# 断点续跑：靠「(姓名, 证件号)」记账，而不是看文件在不在
#
# 证书文件名是用户指定的「名字.pdf」，只含姓名。若拿"文件在不在"判断
# 这条做没做过，同一个学校有两个同名的人时，第二个会被当成"上一轮下过了"
# 直接跳过 —— 证书静默丢掉，而且不报任何错。
#
# 所以改成读历次运行留下的「下载明细 CSV」，按 (姓名, 证件号) 建索引。
# 附带好处：谁手动删掉了某份证书，重跑时会正确补回来。
# ------------------------------------------------------------------
def load_done_index(out_root, min_bytes=2000, roots=None):
    """扫输出目录里历次的「下载明细」CSV，重建「已经下好了」的索引。

    返回 {(姓名, 证件号): 文件路径}。只收状态为成功、**且文件确实还在**的——
    光有记录不够，文件可能被用户删了或挪走了，那就得老老实实重下。

    扫描范围不止 out_root 一层：勾了日期文件夹时，同一个比赛的往期批次躺在
    同级的兄弟目录里，必须一起扫 —— 否则第二天再跑，昨天已经下好的几百个人
    会全部重下一遍，白等十几分钟。
    """
    idx = {}
    for root in (roots if roots is not None else batch_roots(out_root)):
        try:
            files = sorted(Path(root).glob("下载明细_*.csv"))
        except Exception:
            continue
        for f in files:                # 按文件名排序 ≈ 按时间先后，新的覆盖旧的
            try:
                with io.open(f, "r", encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        if (row.get("状态") or "").strip() not in ("成功", "已完成跳过"):
                            continue
                        name = (row.get("姓名") or "").strip()
                        idn = (row.get("证件号") or "").strip()
                        path = (row.get("保存路径") or "").strip()
                        if not (name and idn and path):
                            continue
                        p = Path(path)
                        try:
                            if p.exists() and p.stat().st_size > min_bytes:
                                idx[(name, idn)] = str(p)
                        except OSError:
                            pass
            except Exception:
                continue
    return idx


def unique_path(folder, base, min_bytes=2000, ext="pdf"):
    """在 folder 下挑一个不冲突的文件名：首选「名字.pdf」，被占则「名字_2.pdf」。

    若撞上的是上次没下完留下的残file（很小），就直接覆盖它——
    否则越跑越多「名字_3.pdf」，同事根本看不懂这些都是谁。

    ext 是给金数据这类平台准备的：它们的证书可能是 JPG/PNG 而不是 PDF，
    后缀必须跟真实格式一致，不然双击打不开。
    """
    p = folder / ("%s.%s" % (base, ext))
    if not p.exists():
        return p
    try:
        if p.stat().st_size <= min_bytes:
            return p
    except OSError:
        return p
    k = 2
    while (folder / ("%s_%d.%s" % (base, k, ext))).exists():
        k += 1
    return folder / ("%s_%d.%s" % (base, k, ext))


def cert_magic_ok(b):
    """只看文件头，不看后缀、不看 Content-Type。

    表单100 的证书链接后缀是 .docx、字节其实是 PDF —— 从那儿得来的教训。
    金数据的证书又可能是 JPG，所以这里三种都认。
    """
    return (b[:4] == b"%PDF" or b[:3] == b"\xff\xd8\xff"
            or b[:8] == b"\x89PNG\r\n\x1a\n")


def cert_ext_of(b):
    """按真实内容定后缀（默认 pdf）。"""
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "pdf"


# ------------------------------------------------------------------
# 多批次：这个目录里现在到底有多少份证书
#
# 同事常常分几次跑 —— 先跑一批 300 人，过几天再补一批 300 人，都存到同一个位置。
# 如果汇总清单只统计"本批次名单"，第二份清单会写「证书 300 份」，
# 而文件夹里其实躺着 600 份。数字对不上，他的第一反应是"程序少下了"或者
# "证书丢了"，然后开始翻文件夹一个个数 —— 这是白白浪费的一小时。
#
# 所以清单必须同时说清两件事：本批次名单的情况、这个目录里累计有多少份。
# ------------------------------------------------------------------
def collect_disk_certs(out_root, extra=None, min_bytes=2000):
    """数出输出目录里实际存在多少份证书，返回 {(姓名, 证件号): 路径}。

    数据来源有两处，缺一不可：
      · 历次运行留下的「下载明细 CSV」（往期批次）
      · 本批次刚下好的 extra（明细 CSV 是在汇总之后才写的，不能只读文件）
    最后逐一核对文件确实还在 —— 记录说有、文件已被删掉的，不能算数。
    """
    idx = load_done_index(out_root, min_bytes=min_bytes)
    for r in (extra or []):
        if r.get("cert_ok") and r.get("saved_path"):
            try:
                p = Path(r["saved_path"])
                if p.exists() and p.stat().st_size > min_bytes:
                    idx[(r["name"], r["id"])] = str(p)
            except OSError:
                pass
    return idx


# ------------------------------------------------------------------
# 汇总清单 Excel
# ------------------------------------------------------------------
def write_summary(xlsx_path, records, out_root, disk=None, trial=None):
    """生成汇总清单：学校汇总 / 获奖明细 / 未获奖 / 统计概览。

    两个统计口径必须同时出现，只报一个就会误导人：
      · 本批次   —— records（这次名单里的人），school 字段已由归一化填好
      · 累计口径   —— disk（同一个比赛现在实际有多少份证书，含往期批次；
                        勾了日期文件夹时，往期批次在同级的兄弟文件夹里）
    同事分几次跑时这两个数天然不同，只写其中一个，他会以为程序漏下了。

    trial —— 试跑时必传 {"filtered": 筛选后总人数, "sampled": 本次实际查了几人}。
    不传的后果：清单上只有"名单总人数 8"这一行，看的人会当成"这批就 8 个人"，
    而真相是另外 596 人压根没跑。所以试跑必须自报家门。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="2F5597")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)

    def style_header(ws, ncol):
        for c in range(1, ncol + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
        ws.freeze_panes = "A2"

    def autofit(ws, widths=None):
        if widths:
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = border
                cell.alignment = Alignment(vertical="center")

    wb = Workbook()

    # ---- Sheet1 学校汇总 ----
    ws = wb.active
    ws.title = "学校汇总"
    by_school = defaultdict(lambda: {"cert": 0, "join": 0, "awards": Counter()})
    for r in records:
        st = by_school[r["school"]]
        st["join"] += 1
        if r["cert_ok"]:
            st["cert"] += 1
            st["awards"][r["award"] or "未标注"] += 1

    # 目录里实际有多少份：按「证书所在的学校文件夹」点出来（文件夹名就是 sanitize 后的校名）
    disk = disk or {}
    disk_by_school = Counter()
    for pth in disk.values():
        try:
            disk_by_school[Path(pth).parent.name] += 1
        except Exception:
            pass

    # 行集合 = 本批次的学校 ∪ 目录里实际存在的学校文件夹。
    # 只列本批次的话，往期批次的学校会整行消失，同事反而更慌。
    folders = {sanitize(s): s for s in by_school}
    for f in disk_by_school:
        folders.setdefault(f, f)

    all_awards = Counter()
    for r in records:
        if r["cert_ok"] and r["award"]:
            all_awards[r["award"]] += 1
    # 奖项有天然等级，按等级排而不是按数量排，读表的人才不会看迷糊
    grade = ["特等奖", "一等奖", "二等奖", "三等奖", "优秀奖", "优胜奖", "入围奖", "参与奖"]

    def award_key(a):
        for i, g in enumerate(grade):
            if g in a:
                return (i, -all_awards[a])
        return (len(grade), -all_awards[a])

    award_cols = sorted(all_awards, key=award_key)

    head = ["序号", "学校名称", "目录内证书总数", "本批次名单人数", "本批次证书",
            "未获奖人数"] + award_cols + ["说明"]
    ws.append(head)
    rows = [(f, disp, by_school.get(disp, {"cert": 0, "join": 0, "awards": Counter()}))
            for f, disp in folders.items()]
    # 证书多的排前面：同事核对时最先关心的是"哪所学校文件最多"
    rows.sort(key=lambda x: (-disk_by_school.get(x[0], 0), -x[2]["cert"], -x[2]["join"], x[0]))
    for i, (folder, disp, st) in enumerate(rows, 1):
        note = "本批次名单里没有这所学校（往期批次）" if st["join"] == 0 else ""
        ws.append([i, disp, disk_by_school.get(folder, 0), st["join"], st["cert"],
                   st["join"] - st["cert"]] +
                  [st["awards"].get(a, 0) for a in award_cols] + [note])
    style_header(ws, len(head))
    autofit(ws, [6, 42, 14, 14, 12, 12] + [10] * len(award_cols) + [30])
    total_row = ws.max_row + 1
    ws.cell(row=total_row, column=1, value="合计").font = Font(bold=True)
    for c in range(3, 7 + len(award_cols)):
        col = get_column_letter(c)
        ws.cell(row=total_row, column=c,
                value="=SUM(%s2:%s%d)" % (col, col, total_row - 1)).font = Font(bold=True)
    ws.cell(row=total_row, column=7 + len(award_cols),
            value="「目录内证书总数」= 这个比赛累计下到的证书，含往期批次"
                  "（勾了日期文件夹时，往期批次在同级的其它「证书_日期」文件夹里）").font = \
        Font(bold=True, size=10, color="7F1D1D")

    # ---- Sheet2 获奖明细 ----
    ws2 = wb.create_sheet("获奖明细")
    head2 = ["序号", "姓名", "学校", "奖项", "指导老师",
             "省", "市", "区县", "报名表填写校名", "证书文件名", "状态"]
    ws2.append(head2)
    n = 0
    for r in records:
        if not r["cert_ok"]:
            continue
        n += 1
        ws2.append([n, r["name"], r["school"], r["award"], r["teacher"],
                    r["province"], r["city"], r["district"], r["raw_school"],
                    Path(r["saved_path"]).name if r["saved_path"] else "", r["status"]])
    style_header(ws2, len(head2))
    autofit(ws2, [6, 12, 34, 10, 12, 10, 12, 12, 30, 16, 12])

    # ---- Sheet3 未获奖 / 异常 ----
    ws3 = wb.create_sheet("未获奖及异常")
    head3 = ["序号", "姓名", "证件号", "学校", "状态", "备注"]
    ws3.append(head3)
    n = 0
    for r in records:
        if r["cert_ok"]:
            continue
        n += 1
        ws3.append([n, r["name"], r["id"], r["school"], r["status"], r["note"]])
    style_header(ws3, len(head3))
    autofit(ws3, [6, 12, 24, 34, 14, 40])

    # ---- Sheet4 统计概览 ----
    ws4 = wb.create_sheet("统计概览")
    ws4.append(["项目", "数值"])
    hit = sum(1 for r in records if r["cert_ok"])
    m_new = sum(1 for r in records if r["status"] == "成功")
    m_skip = sum(1 for r in records if r["status"] == "已完成跳过")
    m_disk = len(disk)
    m_old = max(0, m_disk - hit)
    # 参与累计的批次文件夹：勾了日期文件夹之后，往期批次是兄弟目录。
    # 只写一句"含往期批次"，同事还得自己翻硬盘去对是哪些文件夹，索性列出来。
    roots = batch_roots(out_root)
    past = [p.name for p in roots if str(p) != str(Path(out_root))]
    stat = []
    if trial:
        # 试跑必须写在最前面 —— 有人只截第一屏看，那就让他截到这一句。
        stat += [
            ("⚠ 本次是「试跑」：只随机抽查，不是全量结果", ""),
            ("   筛选后名单总人数（本次没跑）", trial["filtered"]),
            ("   本次实际查询人数", trial["sampled"]),
            ("   ↑ 要出全量结果：去掉「先随机试跑」的勾，重跑一次", ""),
        ]
    stat += [
        ("名单总人数（本批次）", len(records)),
        ("成功下载证书", hit),
        ("  其中：本次新下载", m_new),
        ("  其中：上次跑过、本次跳过", m_skip),
        ("未获奖（无证书）", sum(1 for r in records if r["status"] == "未获奖无证书")),
        ("查询失败", sum(1 for r in records if r["status"] == "查询失败")),
        ("下载失败", sum(1 for r in records if r["status"] == "下载失败")),
        ("暂无证书文件（比赛还没开放下载 / 查询页未提供）",
         sum(1 for r in records if r["status"] == "暂无证书文件")),
        ("本批次涉及学校数", len(by_school)),
        ("其中：有证书的学校", sum(1 for _, v in by_school.items() if v["cert"] > 0)),
        ("累计证书（同一个比赛，含往期批次）", m_disk),
        ("  其中：不属于本批次名单", m_old),
        ("证书保存目录", str(out_root)),
    ]
    if past:
        stat.append(("往期批次文件夹（同一个比赛）", "、".join(past)))
    stat.append(("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    for k, v in stat:
        ws4.append([k, v])
    if m_disk > hit:
        where = "（分散在 %d 个日期文件夹里）" % len(roots) if past else ""
        ws4.append([
            "提示",
            "这个比赛累计下来一共有 %d 份证书%s，比本批次的 %d 份多 %d 份。"
            "多出来的是以前跑过的批次，属正常，不用重跑。"
            % (m_disk, where, hit, m_disk - hit)])
    style_header(ws4, 2)
    autofit(ws4, [26, 60])

    wb.save(xlsx_path)
    return str(xlsx_path)


# ------------------------------------------------------------------
# 失败名单：让"重跑一次"这件事变成拖一个文件
#
# 为什么单出一份？查询失败/下载失败的人才是真正需要再来一次的。
# 让同事从几百行的汇总清单里手动筛出这些人，既费时又容易漏；
# 而且自己拼出来的表，列名未必能被工具认出来，拖回来反而报错 —— 更糟。
#
# 所以表头刻意写成「姓名」「证件号」这两个 load_list 认识的列名，
# 这份文件拖回界面就能直接跑，不需要任何手工整理。
# ------------------------------------------------------------------
RETRY_STATUS = ("查询失败", "下载失败")


def write_retry_list(xlsx_path, results):
    """把查询/下载失败的人导成一份可重跑的 Excel。没人失败就返回空串。"""
    rows = [r for r in results if r["status"] in RETRY_STATUS]
    if not rows:
        return ""

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="9C2B2B")

    wb = Workbook()
    ws = wb.active
    ws.title = "待重跑名单"
    head = ["姓名", "证件号", "学校", "状态", "失败原因"]
    ws.append(head)
    for r in rows:
        # 原因要合并两个字段：查询失败的记在 qerr，下载失败的记在 note。
        # 只取一个的话，另一种失败在表里就是空白 —— 同事看着一份没写原因的
        # 失败名单，根本判断不出该等一会儿重跑、还是该去查代理。
        reason = (r.get("note") or "").strip() or (r.get("qerr") or "").strip()
        ws.append([r["name"], r["id"], r["school"] or r.get("raw_school", ""),
                   r["status"], reason])
    for c in range(1, len(head) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = hdr_fill
        cell.font = Font(color="FFFFFF", bold=True, size=11)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    ws.freeze_panes = "A2"
    for i, w in enumerate([14, 24, 34, 14, 44], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="center")
    # 第一行留一句话给同事：这份表怎么用
    ws.cell(row=ws.max_row + 2, column=1,
            value="怎么用：把这份文件直接拖回工具界面，点「开始」即可只重跑这些人。"
                  "（表头的「姓名 / 证件号」就是工具能识别的列名，不用改名）").font = \
        Font(size=10, color="7F1D1D")
    wb.save(xlsx_path)
    return str(xlsx_path)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def run_job(input_path, out_root, site_key=None, sample=None, seed=1, limit=None,
            delay=None, province_filter=None, config_path=None, pace="safe",
            on_progress=None, should_stop=None, log_sink=None, filters=None,
            wait_pause=None):
    """执行一次完整作业。GUI 和命令行都调它。

    delay 显式给数值时优先用它；否则按 pace 档位取间隔（safe = 最稳，默认）。

    wait_pause：可选回调。每处理一个人之前会调它一次 —— 界面按了「暂停」时
    它就阻塞在那儿；按「继续」后返回一个秒数表示「换成这个新速度」，返回
    None 表示速度没变。这样同事就能"停下来改速度、接着往下跑"。

    filters：按表头做的多级筛选（界面上一层层勾出来的条件），形如
        {"学校": {"values": ["武汉市光谷第一小学"], "text": ""},
         "奖项": {"values": ["一等奖"], "text": "一等"}}
    各列之间是 AND（越筛越窄）；同一列内 values 和 text 之间是 OR。
    为空 = 不筛，用全量名单。
    """
    cfg = resolve_config(config_path)
    key = site_key or cfg["default_site"]
    site = cfg["sites"][key]

    run_log = []            # 全部日志存一份，作业结束写成"运行日志.txt"
    seen_kinds = set()      # 已经给过完整自救方案的故障类型，避免同类问题刷屏

    def emit(msg, stamp=True):
        """写一行日志。

        stamp=True 会给这一行打上时刻 —— 报告里那份「完整日志」要能当
        时间线看：同事只会说"跑到一半就不动了"，有了时间戳，接手的人
        才能把故障和当时的网络/网站情况对上，否则只能靠猜。
        stamp=False 用于同一时刻连着吐的整块说明（故障说明一吐十几行，
        每行都盖一遍时间戳反而挡住正文）。
        界面那份（log_sink）始终不带时间戳，免得一屏全被括号占满。
        """
        run_log.append(("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))
                       if stamp else str(msg))
        log(msg, log_sink)

    # ---- 把 4 个阶段拼成一条总进度条（0~1）----
    # 以前每个阶段各自从 0 走到 100，视觉上像"跑完一遍又重来一遍"。
    # 这里按预估耗时给各段分区间：查询 ≈ N×间隔，下载 ≈ 0.47N×间隔×1.5
    # （0.47 = 历史上有证书的比例）。段内按条数线性，整条只往右走、不回头。
    _tot = {"seg": None, "frac": 0.0}

    def _seg_of(stage):
        # 惰性算：第一次报进度时，students 和 base_delay 都已经定了
        if _tot["seg"] is None:
            n = max(1, len(students))
            tq = n * base_delay
            td = n * 0.47 * base_delay * 1.5
            w1 = tq / (tq + td) if (tq + td) else 0.6
            w1 = min(max(w1, 0.30), 0.85)        # 挡掉极端名单
            _tot["seg"] = {"查询": (0.0, w1),
                           "归一化": (w1, w1 + 0.01),
                           "下载": (w1 + 0.01, 0.99),
                           "汇总": (0.99, 1.0)}
        return _tot["seg"].get(stage, (0.0, 1.0))

    def progress(stage, done, total, extra=""):
        a, b = _seg_of(stage)
        r = (float(done) / float(total)) if total else 1.0
        r = min(max(r, 0.0), 1.0)
        frac = a + (b - a) * r
        frac = max(frac, _tot["frac"])           # 只增不减，别让进度条往回退
        _tot["frac"] = frac
        if on_progress:
            on_progress(stage, done, total, extra, frac)
    stopped = lambda: bool(should_stop and should_stop())

    def pause_gate():
        """暂停门。界面按了「暂停」就卡在这儿；按「继续」后把新速度带回来
        （没改速度则返回 None）。放在每处理一个人之前，所以暂停最多等人
        当前这一条处理完，不会被从半路掐断。"""
        if wait_pause:
            return wait_pause()
        return None

    def diag(kind, detail="", tag="故障处理"):
        """报错时的自救提示：同一类故障只详细说一次，之后只提醒一句。"""
        k = kind or "unknown"
        if k in seen_kinds:
            emit("    " + EH.brief(k))
            return
        seen_kinds.add(k)
        # 整块说明只给首行打时间戳，后面的行跟着走 —— 读起来是一个整体
        for i, line in enumerate(EH.explain(k, detail, tag=tag)):
            emit(line, stamp=(i == 0))

    # 节流器：查询与下载各用一个，各自计数、互不干扰
    base_delay = delay if delay else PACE.get(pace, PACE["safe"])
    # 日志和报告里怎么称呼这个速度：正好是某个档位就报档位名，否则报具体秒数。
    # 不然同事把默认的 1.6 传进来、日志却写"自定义 1.6 秒"，看着莫名其妙。
    pace_label = next(("%s 档" % nm for nm, v in PACE.items() if abs(v - base_delay) < 1e-9),
                      "自定义 %g 秒" % base_delay)
    th = Throttler(base=base_delay)

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    px = proxy_in_env()          # 提前算：故障报告的环境信息里要用它

    # 留一张「这个文件夹是哪个比赛」的名片。勾了日期文件夹之后，同一个比赛
    # 会散在几个兄弟目录里，跨目录累计和跨天续跑都靠这张名片认亲；
    # 认不出亲的目录不会被合并，所以写失败也不影响结果，只是少算累计。
    write_batch_marker(out_root, key, site)

    # 故障报告顶上的「现场信息」：干了什么 / 哪个网址 / 哪份名单 / 存到哪了。
    # 这几项是硬要求 —— 拿到文件的同事不会修，他只会把文件转给下一个人；
    # 所以文件必须自己把现场交代清楚。不然接手的人还得回头问
    # "跑的哪个比赛、用的哪份名单"，一来一回又是半天。
    # 报告脱敏用的姓名清单。必须在 sample / limit 截断**之前**存下来 ——
    # 否则报告里只剩抽到的那几个人，反而漏了真正需要隐藏的。
    report_names = []

    report_meta = {
        "本次任务": "准备阶段（正在读名单）",
        "本次处理": "—",
        "比赛网址": site.get("home_url") or "",
        "查询接口": site.get("query_url") or "",
        "名单文件": Path(input_path).name,
        "名单路径": str(Path(input_path).resolve()),
        "查询站点": site["title"],
        "输出目录": str(out_root),
    }
    report_tech = EH.env_info(version=APP_VERSION, site=site,
                              site_key=key, proxy=px)

    def fail_report(kind, detail):
        """还没正式开跑就失败（比如名单读不了）时，也落一份报告。
        同事拿到这个文件，才说得清"到底哪儿出问题了"。"""
        try:
            p = EH.write_report(
                out_root / ("故障诊断_%s.txt" % stamp), {kind}, {}, run_log,
                meta=report_meta, tech=report_tech, names=report_names)
            if p:
                emit("  ★ 故障报告：%s   ← 求助时把这一份发给技术支持即可" % Path(p).name)
            return p
        except Exception:
            return ""

    emit("站点：%s" % site["title"])
    if px:
        emit("环境提示：本机走了网络代理（%s）。若后面出现连不上网站，优先检查代理/VPN。" % px)
    try:
        students, linfo = load_list(input_path, site.get("list_columns"), province_filter,
                                    filters=filters)
    except FileNotFoundError as e:
        for line in EH.explain("list_file", e, tag="名单读取失败"):
            emit(line)
        fail_report("list_file", str(e))
        raise
    except ValueError as e:
        msg = str(e)
        kind = "list_empty" if ("空" in msg or "没有数据行" in msg) else "list_missing"
        for line in EH.explain(kind, msg, tag="名单读取失败"):
            emit(line)
        fail_report(kind, msg)
        raise

    # 名单读进来了：把人数补进报告的现场信息 —— "哪份名单、多少人"是接手的人
    # 判断影响面大小的第一手依据（597 人的名单挂掉和 8 人的挂掉，处理优先级不同）。
    report_names = [s.get("name") for s in students]
    report_meta["名单文件"] = "%s（共 %d 人）" % (Path(input_path).name, len(students))

    if province_filter:
        emit("已按省份筛选：%s → %d 人" % (province_filter, len(students)))
    if filters:
        emit("已按表头筛选：%s" % _filters_desc(filters))
        emit("    筛完剩 %d 人（没筛中的不查、也不下载）" % len(students))
    if not students:
        why = (("按省份「%s」筛选后没有剩下任何人" % province_filter) if province_filter
               else ("按当前筛选条件（%s）没有剩下任何人" % _filters_desc(filters)))
        for line in EH.explain("list_empty", why, tag="名单读取失败"):
            emit(line)
        fail_report("list_empty", why)
        raise ValueError("筛选后名单为空，请放宽筛选条件")

    # 筛完剩多少人 —— 试跑时要拿它跟"实际查了几人"对照着说，
    # 否则报告上只剩一个 8，没人知道被漏掉的是 596 个人。
    filtered_total = len(students)
    sampled_n = 0

    if sample:
        random.seed(seed)
        students = random.sample(students, min(sample, len(students)))
        sampled_n = len(students)
        emit("随机抽取 %d 人（seed=%d）：%s" % (sampled_n, seed,
                                              "、".join(s["name"] for s in students)))
        # 试跑最坑的地方是"看起来像跑完了"：名单 600 人、结果只有 8 行，
        # 报告里却写着"全部完成"—— 谁看都会以为工具坏了，或者名单本来就只有 8 个人。
        # 2026-09-22 用户就是这么被坑的（勾选默认是开的，他没注意）。
        # 所以这里当头喊一嗓子，跑完的结尾还要再喊一次。
        emit("★ 这是「试跑」：筛选后名单有 %d 人，本次只随机查了 %d 人，"
             "**不是全量结果**！" % (filtered_total, sampled_n))
        emit("   想跑全部人：把「先随机试跑」的勾去掉，再点开始。")
    if limit:
        students = students[:limit]

    # ========== 阶段 ① 全量查询 ==========
    emit("—" * 56)
    emit("阶段 1/4  全量查询（先摸清所有人的奖项与官方校名）")
    emit("    节奏：%s，每条间隔 %.1f 秒 ±45%%，每 %d 条长歇一次；被限流会自动退避"
         % (pace_label, base_delay, th.every))
    sess = build_session(site)
    records = []
    hit_n = 0                    # 已命中（有证书）的人数，进度条和日志都要用
    t0 = time.time()
    aborted = False
    last_kind = ""
    for i, stu in enumerate(students, 1):
        new_delay = pause_gate()
        if stopped():
            emit("已手动停止（查询阶段，第 %d 条）" % i)
            break
        if new_delay:
            base_delay = new_delay
            th.base = new_delay
            emit("    接着跑：速度已换成每条间隔 %.1f 秒" % new_delay)
        if th.should_abort:
            aborted = True
            emit("!! 连续 %d 次异常，通道已经不稳（很可能被网站限流了），"
                 "主动中止以免 IP 被拉黑。" % th.streak)
            rk = last_kind if last_kind in ("blocked", "proxy", "dns") else "blocked"
            for line in EH.resume_steps(rk, len(records), len(students)):
                emit(line)
            break
        code, info = query_one(sess, site, stu["name"], stu["id"])
        # 按归一化后的写法没查到，但原始写法和它不一样 → 拿原值再查一次。
        # 宁可多花一次请求，也不能因为名单里多打了一个空格，
        # 就把一个孩子的证书悄悄判成「未获奖」。
        if code == "miss" and (stu.get("id_alt") or stu.get("name_alt")):
            th.wait(1)              # 补一次节流（传 1 是为了避开长歇计数）
            code, info = query_one(sess, site,
                                   stu.get("name_alt") or stu["name"],
                                   stu.get("id_alt") or stu["id"])
            if code == "hit":
                info["_alt_hit"] = 1
        if code == "error":
            last_kind = info.get("kind") or "unknown"
            th.note_err(last_kind)
            diag(last_kind, info.get("err"), tag="查询出错")
        else:
            th.note_ok()
        rec = {
            "name": stu["name"], "id": stu["id"], "raw_school": stu["raw_school"],
            "official_school": info.get("school", ""),
            "award": info.get("award", ""), "province": info.get("province", ""),
            "city": info.get("city", ""), "district": info.get("district", ""),
            "teacher": info.get("teacher", ""), "file_url": info.get("file_url", ""),
            "hit": code == "hit", "qerr": info.get("err", ""),
            "total": info.get("total", 0),
        }
        records.append(rec)
        if rec["hit"]:
            hit_n += 1
        # 进度条每一条都要更新 —— 以前这里跟着日志一起节流（i % 20 == 0），
        # 结果几百人的查询阶段进度条全程不动，看着像卡死。
        # 日志仍然节流（免得刷屏），进度条必须实时。
        progress("查询", i, len(students), "命中 %d" % hit_n)
        if i % 20 == 0 or i == len(students):
            emit("    查询 %d/%d  命中 %d  已用 %.0fs"
                 % (i, len(students), hit_n, time.time() - t0))
        if i < len(students):
            secs, why = th.wait(i)
            if why == "冷却":
                emit("    !! 连续 %d 次异常 → 冷却 %.0f 秒（不硬刚，避免被封）" % (th.streak, secs))
            elif why == "退避":
                emit("    !! 第 %d 次异常 → 退避 %.0f 秒" % (th.streak, secs))
            elif why == "长歇":
                emit("    长歇 %.0f 秒（每 %d 条一次）" % (secs, th.every))
    emit("阶段 1 完成：%d 人，其中有证书 %d 人，耗时 %.0f 秒"
         % (len(records), sum(1 for r in records if r["hit"]), time.time() - t0))
    if aborted:
        emit("!! 查询被提前中止，本次只用已查到的 %d 人做后续统计。" % len(records))

    # ========== 阶段 ② 学校名归一化 ==========
    emit("—" * 56)
    emit("阶段 2/4  学校名归一化（把五花八门的手填校名归为一所）")
    progress("归一化", 0, 1)
    merged = merge_schools(records, map_csv=ROOT / "学校名映射.csv", verbose=False)
    before = len({(r.get("raw_school") or "") for r in records if r.get("raw_school")})
    after = len({r["school"] for r in records})
    emit("校名 %d 种写法 → 归并为 %d 所学校" % (before, after))
    merge_rpt = out_root / ("学校名归并报告_%s.txt" % stamp)
    # 给人看的文件一律 utf-8-sig（UTF-8 带 BOM）：记事本认 BOM、现代工具也认 BOM，
    # 两头都不乱码。纯 UTF-8 在中文 Windows 记事本里会被按 GBK 猜，一样是方块。
    merge_rpt.write_text(merged.report, encoding="utf-8-sig")
    emit("归并详情见：%s" % merge_rpt.name)
    for school, cnt in Counter(r["school"] for r in records).most_common(8):
        emit("    %-38s %d 人" % (school, cnt))
    if after > 8:
        emit("    …另有 %d 所学校" % (after - 8))
    progress("归一化", 1, 1)

    # ========== 阶段 ③ 下载证书 ==========
    emit("—" * 56)
    emit("阶段 3/4  下载证书并分类归档 → %s" % out_root)
    cert_root = out_root
    results = []
    t1 = time.time()
    todo = [r for r in records if r["hit"]]

    # 主办方没把证书放进查询结果（金数据这类平台很常见），就根本没有文件可下。
    # 这时安静跳过、只出汇总清单 —— 让每个人都在日志里报一遍"下载失败"
    # 只会让人以为工具坏了，其实是没有可下的东西。
    import platform_jinshuju as JS
    has_file = bool(JS.cert_file_field(site))
    if not has_file and todo:
        emit("※ 这个查询页没有证书文件字段（主办方没把证书放进可查询的结果里），"
             "本次只查成绩并出汇总清单，不下载证书。")
        # 状态跟下面"逐个人地址为空"那一类是**同一个**：对同事来说都是
        # "这次没有可下载的证书"，只是原因不同 —— 所以用同一格统计、同一个界面提示，
        # 靠 note 说清是哪一种。别再造一个没人认领的状态：
        # 原先这里写的 "无证书文件" 统计、日志、界面、测试四处都没接，等于隐形。
        results = [{**r, "status": "暂无证书文件", "saved_path": "",
                    "saved_kb": 0, "cert_ok": False,
                    "note": "查询页没有证书文件字段（主办方没把证书放进可查询结果）"}
                   for r in todo]
        todo = []

    # 下载比查询重：每份都要等对方把 PDF 生成出来（首次只回 4 字节 init），
    # 所以间隔取查询的 1.5 倍。宁可多花二十分钟，也别把对方惹毛。
    th2 = Throttler(base=base_delay * 1.5)

    # 断点续跑索引：之前下好的证书按「姓名 + 证件号」认，不按文件名——
    # 证书文件名只含姓名，同校同名的第二个人会被误判成"已下过"而静默丢弃。
    done_idx = load_done_index(cert_root)
    if done_idx:
        emit("检测到此前已下好的证书 %d 份，本次自动跳过"
             "（按「姓名+证件号」核对，不会漏掉同名的人）" % len(done_idx))

    last_dl_kind = ""
    for i, rec in enumerate(todo, 1):
        new_delay = pause_gate()
        if stopped():
            emit("已手动停止（下载阶段，第 %d 条）" % i)
            break
        if new_delay:
            base_delay = new_delay
            th2.base = new_delay * 1.5          # 下载间隔始终是查询的 1.5 倍
            emit("    接着跑：下载速度已换成每份间隔 %.1f 秒" % (new_delay * 1.5))
        if th2.should_abort:
            aborted = True
            emit("!! 下载阶段连续 %d 次异常，通道已经不稳（很可能被限流了），"
                 "主动中止以免 IP 被拉黑。" % th2.streak)
            rk = last_dl_kind if last_dl_kind in ("blocked", "proxy", "dns") else "blocked"
            for line in EH.resume_steps(rk, len(results), len(todo)):
                emit(line)
            break

        folder = cert_root / sanitize(rec["school"])
        saved_kb, note, status = 0, "", ""
        target = None

        # ① 先查续跑索引（认人，不认文件名）
        prev = done_idx.get((rec["name"], rec["id"]))
        if prev:
            try:
                pp = Path(prev)
                if pp.exists() and pp.stat().st_size > 2000:
                    target, status = pp, "已完成跳过"
                    saved_kb = round(pp.stat().st_size / 1024, 1)
            except OSError:
                target = None

        # ② 没有就下载；文件名被占时才加序号（这才真的是「同校同名」）
        # 页面上压根没给出证书文件地址 → 这不是"网络失败"。
        # 2026-09-21 用户那次：国赛还没开放下载，每个人取到的地址都是空的，
        # 结果整批被判成"下载失败"（抛的还是 MissingSchema 这种黑话），既误导人，
        # 又白白喂给退避计数器、跑出一份全是假失败的名单。这里单列成一类。
        no_file = is_no_file(target, rec.get("file_url"))
        if no_file:
            status = "暂无证书文件"
            note = "比赛还没开放下载（页面上还没有证书文件）"

        if target is None and not no_file:
            folder.mkdir(parents=True, exist_ok=True)
            base = sanitize(rec["name"])
            body, err, tries = fetch_pdf(sess, rec["file_url"])
            if body:
                # 后缀由文件内容决定：有的平台给的是 JPG 证书，
                # 存成 .pdf 双击打不开，同事只会说"下下来是坏的"。
                ext = cert_ext_of(body)
                target = unique_path(folder, base, ext=ext)
                if target.name != (base + "." + ext):
                    note = "同校同名，已加序号"
                target.write_bytes(body)
                status, saved_kb = "成功", round(len(body) / 1024, 1)
                if tries > 1:
                    note = (note + " " if note else "") + "生成等待 %d 次" % tries
            else:
                status, note = "下载失败", err
                last_dl_kind = classify_download_err(err)
                diag(last_dl_kind, err, tag="下载证书失败")

        # 记功过：退避 / 冷却 / 熔断全靠这个计数。漏了它，被限流时
        # 程序会以为一切正常，继续以固定节奏硬刚，把 IP 打得更死。
        if status == "下载失败":
            th2.note_err(last_dl_kind)
        else:
            th2.note_ok()

        results.append({**rec, "status": status, "saved_path": str(target) if saved_kb else "",
                        "saved_kb": saved_kb, "note": note, "cert_ok": saved_kb > 0})
        emit("    %d/%d  %s | %s | %s → %s%s%s"
             % (i, len(todo), rec["name"], rec["school"][:20], rec["award"] or "-",
                status, (" %.0fKB" % saved_kb) if saved_kb else "", (" " + note[:40]) if note else ""))
        progress("下载", i, len(todo), rec["name"])
        if i < len(todo):
            secs, why = th2.wait(i)
            if why == "长歇":
                emit("    长歇 %.0f 秒（每 %d 份一次）" % (secs, th2.every))
            elif why == "冷却":
                emit("    !! 连续 %d 次异常 → 冷却 %.0f 秒（不硬刚，避免被封）" % (th2.streak, secs))
            elif why == "退避":
                emit("    !! 第 %d 次异常 → 退避 %.0f 秒" % (th2.streak, secs))

    # 未命中的人也要进记录表
    done_names = {(r["name"], r["id"]) for r in results}
    for rec in records:
        if (rec["name"], rec["id"]) not in done_names:
            results.append({**rec, "status": "未获奖无证书" if not rec.get("qerr") else "查询失败",
                            "saved_path": "", "saved_kb": 0, "cert_ok": False,
                            "note": rec.get("qerr", "")})

    emit("阶段 3 完成：成功 %d 份，耗时 %.0f 秒"
         % (sum(1 for r in results if r["cert_ok"]), time.time() - t1))

    # ========== 阶段 ④ 汇总清单 ==========
    emit("—" * 56)
    emit("阶段 4/4  生成汇总清单")
    progress("汇总", 0, 3)
    xlsx = out_root / ("证书汇总清单_%s.xlsx" % stamp)

    # 先把「这个位置一共多少份」数出来：往期批次靠历次明细 CSV，
    # 本批次刚下的还没写进 CSV，直接拿 results 补上。
    disk_certs = collect_disk_certs(out_root, results)
    this_batch = sum(1 for r in results if r["cert_ok"])
    if len(disk_certs) > this_batch:
        emit("    这个比赛累计已有证书 %d 份（含以前跑过的批次，"
             "往期批次可能躺在别的日期文件夹里），本次名单涉及 %d 份。"
             % (len(disk_certs), this_batch))
    write_summary(xlsx, results, out_root, disk=disk_certs,
                  trial=({"filtered": filtered_total, "sampled": sampled_n}
                         if sampled_n else None))
    progress("汇总", 1, 3)

    detail_csv = out_root / ("下载明细_%s.csv" % stamp)
    with io.open(detail_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "姓名", "证件号", "学校(归并后)", "学校(报名表)", "学校(证书)",
                    "奖项", "省", "市", "区县", "指导老师", "状态", "保存路径", "KB", "备注"])
        for i, r in enumerate(results, 1):
            w.writerow([i, r["name"], r["id"], r["school"], r["raw_school"],
                        r.get("official_school", ""), r["award"], r["province"],
                        r["city"], r["district"], r["teacher"], r["status"],
                        r["saved_path"], r["saved_kb"], r["note"]])
    progress("汇总", 2, 3)

    # 失败的人才需要"再来一次"：单独出一份，拖回界面就能重跑
    retry_xlsx = ""
    n_fail = sum(1 for r in results if r["status"] in RETRY_STATUS)
    if n_fail:
        retry_xlsx = write_retry_list(out_root / ("失败名单_%s.xlsx" % stamp), results)
    progress("汇总", 3, 3)

    stat = Counter(r["status"] for r in results)
    schools_with_cert = len({r["school"] for r in results if r["cert_ok"]})
    emit("=" * 56)
    if aborted:
        emit("本次运行已结束（中途停止）")
    elif sampled_n:
        # 这里绝不能写"全部完成" —— 试跑连名单都没跑完，这四个字就是误会之源：
        # 同事看到"全部完成 + 未获奖 8 人"，只会得出"这批就 8 个人"的结论。
        emit("试跑结束 🧪 —— 只查了 %d 人，这不是全量结果！" % sampled_n)
    else:
        emit("全部完成 ✅")
    if sampled_n:
        emit("  ⚠ 筛选后名单 %d 人，本次只随机查了 %d 人，剩下 %d 人没跑。"
             % (filtered_total, sampled_n, filtered_total - sampled_n))
        emit("     想看全量结果：去掉「先随机试跑」的勾，再跑一次。")
    emit("  证书文件：%d 份（涉及 %d 所学校）" % (stat.get("成功", 0) + stat.get("已完成跳过", 0),
                                          schools_with_cert))
    emit("  未获奖  ：%d 人" % stat.get("未获奖无证书", 0))
    if stat.get("暂无证书文件"):
        emit("  暂无证书文件：%d 人（这次没有可下载的证书 —— 多半是比赛还没开放下载，"
             "过几天用同一份名单再跑一次就行；这不是失败，不用当故障处理）"
             % stat["暂无证书文件"])
    if stat.get("查询失败") or stat.get("下载失败"):
        emit("  失败    ：查询 %d 人 / 下载 %d 份"
             % (stat.get("查询失败", 0), stat.get("下载失败", 0)))
    emit("  汇总清单：%s" % xlsx)
    emit("  明细 CSV：%s" % detail_csv)
    emit("  证书目录：%s" % out_root)
    if retry_xlsx:
        emit("  失败名单：%s（%d 人）← 想再试一次的话，把这份文件拖回界面就行"
             % (Path(retry_xlsx).name, n_fail))

    # ---- 出过问题就把「原因 + 怎么办」写清楚，并落盘成文件 ----
    stat_view = dict(stat)
    stat_view["_done"] = len(results)
    stat_view["_total"] = len(students)

    log_file = None
    try:
        log_file = out_root / ("运行日志_%s.txt" % stamp)
        log_file.write_text("\n".join(run_log), encoding="utf-8-sig")
    except Exception:
        log_file = None

    for line in EH.finish_advice(seen_kinds, stat_view, aborted=aborted):
        emit(line)

    # 报告顶上的「干了什么、影响多大」，尽量写成人话，接手的人一眼能定位
    report_meta["本次任务"] = ("全量查询 + 下载证书（节奏 %s，每人约 %.1f 秒）%s"
                          % (pace_label, base_delay, "⚠ 中途被手动停止" if aborted else ""))
    report_meta["本次处理"] = "%d 人 / 名单共 %d 人" % (len(results), len(students))

    diag_file = None
    if seen_kinds or aborted or stat.get("查询失败") or stat.get("下载失败"):
        try:
            p = EH.write_report(
                out_root / ("故障诊断_%s.txt" % stamp), seen_kinds, stat_view, run_log,
                meta=report_meta, tech=report_tech, names=report_names)
            diag_file = Path(p) if p else None
        except Exception as e:
            emit("（故障报告写入失败：%s，可忽略，不影响结果）" % e)
    if diag_file:
        emit("  ★ 故障报告：%s   ← 需要求助时，把这一份原样发给技术支持即可" % diag_file.name)
    if log_file:
        emit("  运行日志：%s" % log_file.name)

    return {"results": results, "xlsx": str(xlsx), "csv": str(detail_csv),
            "stats": dict(stat), "schools": schools_with_cert, "out_root": str(out_root),
            "log_file": str(log_file) if log_file else "",
            "diag_file": str(diag_file) if diag_file else "",
            "retry_xlsx": retry_xlsx, "retry_count": n_fail,
            "disk_total": len(disk_certs),
            # 试跑的两个数：筛完剩几人 / 本次实际查了几人。
            # 界面拿它把"试跑结束（8/604）"写清楚 —— 少了这个，界面只剩一个 8。
            "filtered_total": filtered_total, "sampled": sampled_n,
            "advice": list(EH.finish_advice(seen_kinds, stat_view, aborted=aborted))}


# ------------------------------------------------------------------
# 命令行
# ------------------------------------------------------------------
def cmd_check(args):
    """自检：依赖 + 站点连通性 + 配置可读性。"""
    print("=" * 60)
    print("环境自检")
    print("=" * 60)
    problems = []
    try:
        import openpyxl
        print("[OK] openpyxl %s（读写 Excel）" % openpyxl.__version__)
    except ImportError:
        problems.append("缺 openpyxl")
        print("[X ] openpyxl 未安装")
    try:
        import requests
        print("[OK] requests %s（网络请求）" % requests.__version__)
    except ImportError:
        problems.append("缺 requests")
        print("[X ] requests 未安装")
    print("[OK] Python %s" % sys.version.split()[0])

    cfg = resolve_config(args.config)
    print("\n已配置的站点：")
    for k, v in cfg["sites"].items():
        print("   %-18s %s" % (k, v["title"]))
    site = cfg["sites"][cfg["default_site"]]
    fm = site["field_map"]
    try:
        s = build_session(site)
        d = s.post(site["query_url"], json={
            fm["name"]: "__连通性测试__", fm["id"]: "000000000000000000",
            "FRMID": site["frmid"], "SHORTID": site["shortid"],
            "PREORNEXT": "FIRST", "PAGEINFO": {}, "PAGESIZE": 10}, timeout=30).json()
        print("[OK] 站点连通，接口返回 total=%s（0 = 查无此人，属正常）" % d.get("total"))
    except Exception as e:
        problems.append("站点不通")
        print("[X ] 站点连通性测试失败：%s" % str(e)[:120])

    print("\n学校名映射表：%s" % ("已存在" if (ROOT / "学校名映射.csv").exists() else "未创建（可选）"))
    print("\n结论：%s" % ("全部正常 ✅" if not problems else "有问题 → %s" % "、".join(problems)))
    return 0 if not problems else 1


def main():
    ap = argparse.ArgumentParser(description="比赛获奖证书批量下载器")
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="执行一次作业")
    p_run.add_argument("--input", required=True, help="名单 .xlsx / .csv")
    p_run.add_argument("--out", default=default_out_dir(), help="证书输出目录")
    p_run.add_argument("--site", help="站点 key（默认用 config.json 里的 default_site）")
    p_run.add_argument("--config", help="配置文件路径")
    p_run.add_argument("--sample", type=int, help="随机抽 N 人试跑")
    p_run.add_argument("--seed", type=int, default=1)
    p_run.add_argument("--limit", type=int)
    p_run.add_argument("--pace", choices=["safe", "normal", "fast"], default="safe",
                       help="访问节奏：safe 最稳（默认）/ normal / fast（确认无风控时才用）")
    p_run.add_argument("--delay", type=float, default=None,
                       help="手工指定间隔秒数（覆盖 --pace，谨慎使用）")
    p_run.add_argument("--province", help="只要该省份的人（按名单省份列筛选）")

    p_chk = sub.add_parser("check", help="环境与站点自检")
    p_chk.add_argument("--config")

    args = ap.parse_args()
    try:
        # 只在输出被重定向（管道/文件）时强制 UTF-8 —— 双击 bat 的黑窗口是 GBK 控制台，
        # 强转 UTF-8 会让自检输出在同事屏幕上变乱码；管道里则相反，GBK 会乱。
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.cmd == "check":
        sys.exit(cmd_check(args))
    if args.cmd != "run":
        ap.print_help()
        sys.exit(0)

    try:
        run_job(args.input, args.out, site_key=args.site, sample=args.sample,
                seed=args.seed, limit=args.limit, delay=args.delay, pace=args.pace,
                province_filter=args.province, config_path=args.config)
    except Exception as e:
        log("执行失败：%s" % e)
        raise
    # 让同事能直接看到结果（在资源管理器里把证书目录打开）
    open_in_file_manager(args.out)


if __name__ == "__main__":
    main()
