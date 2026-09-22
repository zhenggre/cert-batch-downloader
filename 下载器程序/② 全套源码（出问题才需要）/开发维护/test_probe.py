# -*- coding: utf-8 -*-
"""自动识别新比赛：解析逻辑 + 失败路径 + 与人工配置的等价性。

为什么最重要的一条断言是「和 config.json 逐字段一致」：
那个 config.json 是当初人工抓包写出来的，工具用它真的下载过证书。
所以"自动识别的结果 == 人工抓包的结果"就等于证明了自动识别可用。
以后网站改版、字段改名，这条断言会立刻变红 —— 比等同事跑完 45 分钟才发现早得多。

测试分两半：
  · 离线段（1~4）：用「fixture_比赛查询页.html」（真实保存下来的页面快照）跑，
    不联网、可重复。该快照含真实站点数据，不进公开库；没有它时本段自动跳过。
  · 联机段（5）：真访问一次比赛网址。默认跳过，需显式设 CERTDL_LIVE_URL 才跑
    （避免测试脚本里写死某个真实比赛网址）。
"""
import io
import json
import os
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import site_probe as SP        # noqa: E402

OUT = []
OK = True


def sec(t):
    OUT.append("")
    OUT.append("=" * 68)
    OUT.append(t)
    OUT.append("=" * 68)


def chk(name, good, extra=""):
    global OK
    if not good:
        OK = False
    OUT.append("  %-50s %s %s" % (name, "✅" if good else "❌", str(extra)[:110]))


FIXTURE = Path(__file__).with_name("fixture_比赛查询页.html")
# 快照是抓取下来的真实页面，不进公开库（见 .gitignore 第 13 条）。
# 缺快照时离线段整体跳过，不报错。
HTML = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""

# ---------------------------------------------------------------- 1
sec("1. 网址整理（同事粘进来的网址什么样都有）")
for raw, want, tag in [
    ("  https://biaodan100.com/q/zzzzzz  ", "https://biaodan100.com/q/zzzzzz", "首尾空格"),
    ("biaodan100.com/q/zzzzzz", "https://biaodan100.com/q/zzzzzz", "没写 https"),
    ("https://biaodan100.com/q/zzzzzz?from=wechat#top",
     "https://biaodan100.com/q/zzzzzz", "带参数和锚点"),
    ("https://biaodan100.com/q/zzzzzz 这上面能查成绩",
     "https://biaodan100.com/q/zzzzzz", "连着说明一起粘"),
]:
    got, why = SP.normalize_url(raw)
    chk("整理：%s" % tag, got == want and not why, "%r → %r" % (raw.strip(), got))
for raw, tag in (("", "空"), ("   ", "全空格"), ("ftp://a.com/q/x", "ftp 协议")):
    got, why = SP.normalize_url(raw)
    chk("拒绝：%s" % tag, (not got) and bool(why), why)

# ---------------------------------------------------------------- 2
sec("2. 解析真实页面（离线；fixture 是真实抓下来的页面）")
if not HTML:
    OUT.append("  （缺 fixture_比赛查询页.html，跳过。该快照不进公开库。）")
else:
    frm = SP.extract_frm(HTML)
    chk("能从真实页面里抠出表单定义", bool(frm), "抠不到就说明网站改版了")
    if frm:
        chk("拿到表单 ID（frmid）", bool(frm.get("_id")), frm.get("_id"))
        chk("拿到比赛名称", bool(frm.get("FRMNM")), str(frm.get("FRMNM"))[:40])

        fm, notes = SP.classify_fields(frm)
        # 与人工抓包写的 config.json 逐字段比对 —— 这是本文件最关键的一条
        cfg_path = R / "程序" / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            manual = cfg["sites"][cfg.get("default_site") or next(iter(cfg["sites"]))]["field_map"]
            for k, cn in (("name", "姓名"), ("id", "证件号"), ("school", "学校"),
                          ("award", "奖项"), ("province", "省"), ("city", "市"),
                          ("district", "区县"), ("teacher", "指导老师")):
                chk("自动识别的%s == 人工抓包的（%s）" % (cn, manual.get(k)),
                    fm.get(k) == manual.get(k), "自动=%s 人工=%s" % (fm.get(k), manual.get(k)))
            chk("证书文件字段（这个网站固定叫 URL）",
                fm.get("file_url") == manual.get("file_url"), fm.get("file_url"))
        else:
            OUT.append("  （无 config.json，跳过与人工配置的逐字段比对；"
                       "可复制 config.example.json 自建。）")
        chk("没有缺必备字段", not SP.missing_fields(fm), SP.missing_fields(fm))
        OUT.append("")
        OUT.append("  备注（给人核对的）：")
        for n in notes:
            OUT.append("    · " + n)

# ---------------------------------------------------------------- 3
sec("3. 认不出来时必须明确失败（不能写半套配置进去）")
cases = [
    ("普通网页，没有表单定义",
     "<html><body><h1>欢迎</h1><p>这里没有比赛</p></body></html>", False),
    # 这条的 JSON 本身合法，但里面没有 _id（表单 ID）。
    # extract_frm 只管"能不能抠出这段定义"，"有没有 ID"是 probe 的职责 ——
    # 所以这里期望能抠出来，再由下面那条断言盯住 probe 会拒绝它。
    ("页面里有 FRM 但缺表单 ID",
     '<html><script>var FRM = {"FRMNM":"某比赛","FLDS":[]};</script></html>', True),
]
for tag, html, should_extract in cases:
    got = SP.extract_frm(html)
    chk("%s → %s" % (tag, "能抠出定义（由 probe 判不合规）"
                     if should_extract else "抠不出定义"),
        (got is not None) == should_extract, str(got)[:50])

# probe 必须拒绝"有定义但没有表单 ID"的页面。
# 用打桩换掉 requests.get（不联网），把上面那个页面喂给 probe 走完整流程。
class _FakeResp:
    status_code = 200
    text = '<html><script>var FRM = {"FRMNM":"某比赛","FLDS":[]};</script></html>'


_orig_get = SP.requests.get
SP.requests.get = lambda *a, **k: _FakeResp()
try:
    r_noid = SP.probe("https://example.com/q/noid")
    chk("缺表单 ID 的页面 → probe 判失败，且提示是中文",
        (not r_noid.get("ok")) and "没有找到比赛信息" in (r_noid.get("msg") or ""),
        str(r_noid.get("msg"))[:70].replace("\n", " "))
finally:
    SP.requests.get = _orig_get

r_empty = SP.probe("")
chk("空网址：明确说没填", (not r_empty.get("ok")) and "网址" in r_empty.get("msg", ""),
    r_empty.get("msg"))

# 本地 9 号端口必然连不上 —— 秒失败，用来验证"连不上"这条提示。
# 注意：本机若开着代理，这一步会先被代理拦下，提示变成"关掉代理/VPN"——
# 那也是人话。所以断言的是"没有把英文异常原文透给同事"，不是抠某个词。
r_down = SP.probe("https://127.0.0.1:9/q/xxx")
m = str(r_down.get("msg") or "")
chk("连不上时给的是人话（不是英文异常）",
    (not r_down.get("ok")) and m
    and not any(k in m for k in ("Error", "Traceback", "Exception", "[Errno", "codec")),
    m[:80])

# 缺证件号：必须失败，且要指名道姓说缺什么
bad_frm = {"_id": "x" * 24, "FRMNM": "缺字段的比赛", "FLDS": [
    {"LBL": "参赛学生姓名", "NM": "F1", "TYP": "name"},
    {"LBL": "学校名称", "NM": "F3", "TYP": "text"},
    {"LBL": "选拔赛奖项", "NM": "F2", "TYP": "text"},
]}
fm2, _ = SP.classify_fields(bad_frm)
lack = SP.missing_fields(fm2)
chk("缺证件号时确实判为不合格", "id" in lack, lack)
chk("  name / school / award 仍能认出来", fm2.get("name") == "F1"
    and fm2.get("award") == "F2", fm2)

# ---------------------------------------------------------------- 3b
sec("3b 平台识别：识别不了时，要说清「是哪个平台、下一步找谁」")
# 笼统一句"网页里没有比赛信息"同事是没法行动的。他需要知道两件事：
# 这个网址属于哪个平台、以及他该干什么（贴对页面 / 转给谁）。
for u, want, known in [
    ("https://biaodan100.com/q/zzzzzz", "表单100", True),
    ("https://www.biaodan100.com/q/x", "表单100", True),
    # 金数据 2026-09-20 已接适配（两代产品），所以这里是 True ——
    # 这条断言盯着"已支持平台"的清单，改适配时它会提醒你同步。
    ("https://jinshuju.net/f/abcdef/s/123456", "金数据", True),
    ("https://jinshuju.com/os/abcdef", "金数据", True),
    ("https://www.wjx.cn/vm/abcdef.aspx", "问卷星", False),
    ("https://sojump.com/jq/123.aspx", "问卷星", False),
    ("https://mikecrm.com/abc", "麦客", False),
    ("https://docs.qq.com/form/page/xxxx", "腾讯文档收集表", False),
    ("https://some-school.edu.cn/query", "", False),
]:
    got, ok = SP.identify_platform(u)
    chk("认平台：%s" % u.split("//")[1][:32], got == want and ok == known,
        "得到 %r/%s，期望 %r/%s" % (got, ok, want, known))

m_sup = SP.unsupported_msg("https://biaodan100.com/q/zzzzzz")
chk("支持平台上的错页 → 提醒他贴「某个比赛的查询页」",
    "查询页" in m_sup and "技术支持" in m_sup, m_sup[:60].replace("\n", " "))
# 用一个**还没适配**的平台来测这条分支（金数据已支持，走的是上面那条分支）。
# 换 URL 而不是删断言：这条分支本身还得继续盯着。
m_other = SP.unsupported_msg("https://mikecrm.com/abc")
chk("不支持的平台 → 点名平台 + 说清「接一次适配以后就自动」",
    "麦客" in m_other and "只认" in m_other and "适配" in m_other,
    m_other[:60].replace("\n", " "))
m_unknown = SP.unsupported_msg("https://some-school.edu.cn/query")
chk("陌生网站 → 把域名写进提示，方便直接转给别人",
    "some-school.edu.cn" in m_unknown and "技术支持" in m_unknown,
    m_unknown[:60].replace("\n", " "))
chk("三种提示都不透英文异常（同事看不懂）",
    not any(k in (m_sup + m_other + m_unknown)
            for k in ("Error", "Traceback", "Exception")))

# ---------------------------------------------------------------- 4
sec("4. 顺序护栏：别把「指导老师姓名」当成学生姓名")
# 这是真会发生的错：两个标签都含「姓名」。规则表里「老师」必须排在「姓名」前面，
# 一旦被人调换顺序，学生的姓名就会取到老师的值 —— 而查出来的结果"看着像对的"。
poison = {"_id": "y" * 24, "FRMNM": "顺序测试", "FLDS": [
    {"LBL": "指导老师姓名（选填）", "NM": "F6", "TYP": "text"},
    {"LBL": "参赛学生姓名", "NM": "F1", "TYP": "name"},
    {"LBL": "参赛学生证件号", "NM": "F999", "TYP": "text"},
    {"LBL": "学校名称", "NM": "F3", "TYP": "text"},
    {"LBL": "选拔赛奖项", "NM": "F2", "TYP": "text"},
    {"LBL": "所在地区", "TYP": "address",
     "SUBFLDS": {"ZIP": {"NM": "F15"}, "PRV": {"NM": "F16"}, "CITY": {"NM": "F17"}}},
]}
fm3, _ = SP.classify_fields(poison)
chk("老师字段归给 teacher", fm3.get("teacher") == "F6", fm3.get("teacher"))
chk("学生姓名字段归给 name（没被老师抢走）", fm3.get("name") == "F1", fm3.get("name"))
chk("两个字段没有互相串（用到的标识不重复）",
    len(set(fm3.values())) == len(fm3.values()), fm3)
chk("省市区三个子字段各就各位",
    (fm3.get("province"), fm3.get("city"), fm3.get("district")) == ("F16", "F17", "F15"),
    fm3)

# ---------------------------------------------------------------- 5
sec("5. 联机验证（默认跳过；设 CERTDL_LIVE_URL 为你的比赛查询页网址才跑）")
LIVE_URL = (os.environ.get("CERTDL_LIVE_URL") or "").strip()
if not LIVE_URL:
    OUT.append("  （未设 CERTDL_LIVE_URL，跳过。例：")
    OUT.append("    CERTDL_LIVE_URL=https://biaodan100.com/q/<SHORTID> "
               "python test_probe.py）")
else:
    live = SP.probe(LIVE_URL)
    if live.get("ok"):
        s = live["site"]
        chk("线上识别成功", True, s["title"][:36])
        chk("frmid 非空", bool(s.get("frmid")), s.get("frmid"))
        chk("shortid 取自网址",
            s["shortid"] == LIVE_URL.rstrip("/").split("/")[-1].split("?")[0],
            s["shortid"])
        chk("查询接口地址拼得对", "SHORTID=" in (s.get("query_url") or ""),
            s.get("query_url"))
        chk("线上识别的字段与离线一致",
            s["field_map"].get("name") == "F1" and s["field_map"].get("id") == "F999",
            json.dumps(s["field_map"], ensure_ascii=False))
    else:
        chk("线上识别成功（失败原因：%s）" % str(live.get("msg"))[:60], False)
        OUT.append("    （联网失败不算功能问题，但要看清楚原因再判断）")

# ---------------------------------------------------------------- 6
sec("6. 结论")
OUT.append("  自动识别新比赛：%s" % ("全部通过 ✅" if OK else "存在问题 ❌"))

p = Path(__file__).with_name("probe_test.txt")
p.write_text("\n".join(OUT), encoding="utf-8")
print("\n".join(OUT))
sys.exit(0 if OK else 1)
