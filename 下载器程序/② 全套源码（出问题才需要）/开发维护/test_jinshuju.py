# -*- coding: utf-8 -*-
"""金数据适配：解析逻辑（离线）+ 真查一次（联机）。

为什么离线段占大半：
  金数据的页面配置/接口都是真网站给的，断网或改版时联机测试会时红时绿，
  那种测试没人敢信。所以把"抠 JSON、认字段、判条件"这些纯逻辑留在本地，
  用造出来的结构跑，永远可重复。

联机段只发 3 个请求（官方公开演示页），符合"冒烟测试要节制"。
"""
import json
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import platform_jinshuju as JS     # noqa: E402
import cert_downloader as CD       # noqa: E402
import site_probe as SP            # noqa: E402

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
    OUT.append("  %-52s %s %s" % (name, "✅" if good else "❌", str(extra)[:110]))


# ---------------------------------------------------------------- 1
sec("1. URL 形态：旧版 / 新版 / 填报表单 / 别的平台")
for u, want, tag in [
    ("https://jinshuju.net/f/GbCmEV/s/JY5YpH", "old", "旧版对外查询"),
    ("https://jinshuju.com/f/AbCdEf/s/XyZ123", "old", "旧版（com 域）"),
    ("https://jinshuju.com/os/eHfYKT", "new", "新版对外查询"),
    ("https://mkt.jinshuju.com/os/eHfYKT", "new", "新版（最终域名）"),
    ("https://jinshuju.com/os/eHfYKT?q=a:1", "new", "新版带查询串"),
    ("https://jinshuju.net/f/GbCmEV", "", "填报表单（要登录，不是查询页）"),
    ("https://biaodan100.com/q/zzzzzz", "", "表单100"),
]:
    k, parts = JS.kind_of(u)
    chk("认形态：%s" % tag, k == want, "得到 %r" % (k,))

chk("旧版能拆出表单号和查询号",
    JS.kind_of("https://jinshuju.net/f/GbCmEV/s/JY5YpH")[1]
    == {"form": "GbCmEV", "open_search": "JY5YpH"})
chk("新版能拆出查询号",
    JS.kind_of("https://jinshuju.com/os/eHfYKT")[1] == {"os_token": "eHfYKT"})

# ---------------------------------------------------------------- 2
sec("2. 域名判定（认得出是金数据，才转到这个适配器）")
for host, want in [
    ("https://jinshuju.net/f/a/s/b", True),
    ("https://jinshuju.com/os/x", True),
    ("https://mkt.jinshuju.com/os/x", True),
    ("https://a7m5o64y.jinshuju.com/os/x", True),
    ("https://biaodan100.com/q/zzzzzz", False),
    ("https://www.wjx.cn/vm/x.aspx", False),
]:
    chk("is_jinshuju %s" % host.split("/")[2], JS.is_jinshuju(host) == want)

# ---------------------------------------------------------------- 3
sec("3. 从 RSC 流式响应里抠 JSON（新版全靠这步）")
rsc = ('0:{"f":[[["",{"children":["os",{"x":1}]}],'
       '{"name":"选拔赛成绩查询","token":"eHfYKT",'
       '"searchFields":[{"apiCode":"field_2","label":"选手姓名"}],'
       '"displayFieldRules":[{"protected":true,'
       '"field":{"apiCode":"field_2","label":"选手姓名"}}],'
       '"note":"干扰项：} 这个花括号在字符串里 { 不该影响配对"}]\n'
       '1:I[915887,["chunk.js"],"default"]')
cfg = JS._json_at(rsc, "searchFields")
chk("抠出配置对象", isinstance(cfg, dict) and cfg.get("token") == "eHfYKT",
    "token=%s" % (cfg or {}).get("token"))
chk("字符串里的花括号没把配对带偏",
    cfg is not None and cfg.get("note", "").endswith("不该影响配对"),
    str(cfg.get("note"))[:40] if cfg else "")
chk("嵌套数组没干扰（children）", cfg is not None and "searchFields" in cfg)
chk("找不到的 key 返回 None（不瞎猜）",
    JS._json_at(rsc, "notExistKey") is None)
chk("空文本不炸", JS._json_at("", "searchFields") is None)
chk("取「键的值」：结果为纯数据时能抠出来",
    JS._json_value_at('x:{"resultsAndPagination":{"results":[1]}}',
                      "resultsAndPagination") == {"results": [1]})
# 父对象里混着 React flight 标记（$L46）时，只有"取值"这条路能成功 ——
# 这正是新版接口的实际情况，靠 _json_at 会整个解析失败。
# 裸的 $L46（没有引号）才是 RSC 里真正让父对象解析失败的东西。
rsc_dirty = '6:{"children":$L46,"resultsAndPagination":{"results":[{"a":1}]}}'
chk("父对象不是合法 JSON 时，取值仍然成功",
    JS._json_value_at(rsc_dirty, "resultsAndPagination") == {"results": [{"a": 1}]})
chk("（对照）这时候 _json_at 确实会失败",
    JS._json_at(rsc_dirty, "resultsAndPagination") is None)

# ---------------------------------------------------------------- 3b
sec("3b. 真实页面快照（fixture）：网站改版时这里最先变红")
FIX = Path(__file__).with_name("fixture_金数据新版_RSC.txt")
chk("快照文件在", FIX.exists(), str(FIX.name))
if FIX.exists():
    raw = FIX.read_text(encoding="utf-8")
    chk("快照非空", len(raw) > 1000, "%d 字节" % len(raw))
    cfg = JS._json_at(raw, "searchFields")
    chk("真实快照：抠出配置", bool(cfg) and cfg.get("token") == "eHfYKT")
    if cfg:
        chk("真实快照：查询条件两个（姓名+电话）",
            [f.get("label") for f in cfg.get("searchFields") or []]
            == ["选手姓名", "联系电话"],
            str([f.get("label") for f in cfg.get("searchFields") or []]))
        chk("真实快照：结果字段读得到",
            bool(cfg.get("displayFieldRules")))
    res = JS._json_value_at(raw, "resultsAndPagination")
    chk("真实快照：抠出查询结果", bool(res) and len(res.get("results") or []) == 1)
    if res and res.get("results"):
        ev = res["results"][0].get("entryValues") or {}
        chk("真实快照：结果值取得到", ev.get("field_1") == "A001", str(ev)[:60])
        chk("真实快照：能看出字段被脱敏",
            "*" in (ev.get("field_2") or "") or "*" in (ev.get("field_3") or ""),
            str(ev.get("field_2")))

# ---------------------------------------------------------------- 4
sec("4. 字段值 → 下载地址（接不住就返回空，绝不返回假链接）")
cases = [
    ("https://x.com/a.pdf", "https://x.com/a.pdf", "直链字符串"),
    ({"visitUrl": "https://x.com/a.jpg"}, "https://x.com/a.jpg", "对象里的 visitUrl"),
    ({"url": "https://x.com/a.png"}, "https://x.com/a.png", "对象里的 url"),
    ([{"visitUrl": "https://x.com/a.pdf"}], "https://x.com/a.pdf", "数组取第一个"),
    ("a.jpg", "", "相对路径（没法下）"),
    ({"filename": "a.jpg"}, "", "对象里没有地址键"),
    (None, "", "空值"),
]
for v, want, tag in cases:
    got = JS._as_url(v)
    chk("_as_url %s" % tag, got == want, got[:60])

# ---------------------------------------------------------------- 5
sec("5. 字段归类（顺序即优先级，这条错了会静默取错人）")
cond = [
    {"apiCode": "F1", "label": "参赛学生姓名", "type": "NameField", "sms": False},
    {"apiCode": "F999", "label": "参赛学生证件号", "type": "TextField", "sms": False},
]
res = [
    {"apiCode": "F6", "label": "指导老师姓名（选填）", "type": "TextField"},
    {"apiCode": "F1", "label": "参赛学生姓名", "type": "NameField"},
    {"apiCode": "F2", "label": "选拔赛奖项", "type": "TextField"},
    {"apiCode": "F3", "label": "学校名称", "type": "TextField"},
    {"apiCode": "F99", "label": "获奖证书", "type": "AttachmentField"},
]
cm, rmv, _ = JS._classify(cond, res)
chk("姓名认到 F1（不是老师那个 F6）", cm.get("name") == "F1", cm.get("name"))
chk("证件号认到 F999", cm.get("id") == "F999", cm.get("id"))
chk("指导老师认到 F6", rmv.get("teacher") == "F6", rmv.get("teacher"))
chk("奖项认到 F2", rmv.get("award") == "F2", rmv.get("award"))
chk("学校认到 F3", rmv.get("school") == "F3", rmv.get("school"))
chk("证书认到附件字段 F99", rmv.get("file_url") == "F99", rmv.get("file_url"))

# 复合地区字段
res2 = res + [
    {"apiCode": "F16", "label": "省份", "type": "TextField"},
    {"apiCode": "F17", "label": "城市", "type": "TextField"},
]
_, rmv2, _ = JS._classify(cond, res2)
chk("省 / 市能认出来", rmv2.get("province") == "F16" and rmv2.get("city") == "F17",
    "%s/%s" % (rmv2.get("province"), rmv2.get("city")))

# ---------------------------------------------------------------- 6
sec("6. 查询条件填不出来时，必须当场说清（否则 600 人全查不到）")
m = JS.extra_cond_msg(
    [{"apiCode": "field_2", "label": "选手姓名"},
     {"apiCode": "field_3", "label": "联系电话"}],
    {"name": "field_2"})
chk("姓名+电话 → 有提示", bool(m))
chk("提示里点名了填不出的那一列", "联系电话" in m, m[:40].replace("\n", " "))
chk("提示给了两条出路", "主办方" in m and "技术支持" in m)
chk("姓名+证件号 → 不拦",
    JS.extra_cond_msg(
        [{"apiCode": "f1", "label": "姓名"}, {"apiCode": "f2", "label": "证件号"}],
        {"name": "f1", "id": "f2"}) == "")
chk("只有姓名 → 不拦",
    JS.extra_cond_msg([{"apiCode": "f1", "label": "姓名"}], {"name": "f1"}) == "")

# ---------------------------------------------------------------- 7
sec("7. 有没有证书字段 → 决定进不进下载阶段")
chk("有 file_url 就下", bool(JS.cert_file_field(
    {"field_map": {"file_url": "F99"}})))
chk("result_map 里的也算", bool(JS.cert_file_field(
    {"field_map": {}, "result_map": {"file_url": "F99"}})))
chk("没有就不下（不进下载阶段）",
    not JS.cert_file_field({"field_map": {"name": "F1", "school": "F3"}}))

# ---------------------------------------------------------------- 8
sec("8. 证书字节：认内容不认后缀（金数据可能给 JPG）")
chk("PDF 认得出来", CD.cert_magic_ok(b"%PDF-1.4xxxx"))
chk("JPG 认得出来", CD.cert_magic_ok(b"\xff\xd8\xff\xe0xxxx"))
chk("PNG 认得出来", CD.cert_magic_ok(b"\x89PNG\r\n\x1a\nabc"))
chk("HTML 错误页不算证书", not CD.cert_magic_ok(b"<html><body>502"))
chk("后缀跟着内容走：JPG→jpg", CD.cert_ext_of(b"\xff\xd8\xff\xe0") == "jpg")
chk("后缀跟着内容走：PNG→png", CD.cert_ext_of(b"\x89PNG\r\n\x1a\n") == "png")
chk("后缀跟着内容走：PDF→pdf", CD.cert_ext_of(b"%PDF-1.4") == "pdf")
p = CD.unique_path(Path("."), "张三", ext="jpg")
chk("unique_path 能用别的后缀", p.name == "张三.jpg", p.name)

# ---------------------------------------------------------------- 9
sec("9. 贴错页面的提示要分情况（下一步动作不一样）")
m1 = JS.wrong_page_msg("https://jinshuju.net/f/GbCmEV")
chk("填报表单 → 点明这不是查询页", "填报表单" in m1, m1[:30])
chk("填报表单 → 告诉他查询页长什么样", "/s/" in m1 and "/os/" in m1)
m2 = JS.wrong_page_msg("https://jinshuju.net/os/xxxx")
chk("看着像查询页但读不到 → 提示网站可能改版", "改版" in m2 or "技术支持" in m2)

# ================================================================ 联机
sec("10. 联机：官方公开演示页（旧版，真发 2 个请求）")
OLD = "https://jinshuju.net/f/GbCmEV/s/JY5YpH"
try:
    r = JS.probe(OLD)
except Exception as e:
    r = {"ok": False, "msg": "异常 %s" % e}
chk("识别没抛异常", isinstance(r, dict) and ("ok" in r))
if r.get("ok"):
    s = r["site"]
    chk("认出标题", "对外查询" in (s.get("title") or ""), s.get("title"))
    chk("认出查询条件字段", s["field_map"].get("name") == "field_1",
        s["field_map"].get("name"))
    chk("平台标记写进配置", s.get("platform") == "jinshuju")
    chk("诚实说明：没有证书字段",
        any("没有证书文件字段" in n for n in r.get("notes", [])))
    chk("诚实说明：只能按姓名查",
        any("只能按姓名查" in n for n in r.get("notes", [])))
    try:
        sess = JS.build_session(s)
        code, info = JS.query_one(sess, s, "小金", "")
        chk("真查一个人 → 命中", code == "hit", "%s %s" % (code, str(info)[:60]))
    except Exception as e:
        chk("真查一个人 → 命中", False, "异常 %s" % str(e)[:100])
else:
    chk("识别成功（否则后面的查询没法测）", False, r.get("msg", "")[:80])

sec("11. 联机：新版演示页（条件是姓名+电话，应当被拦下）")
NEW = "https://jinshuju.com/os/eHfYKT"
try:
    r2 = JS.probe(NEW)
except Exception as e:
    r2 = {"ok": False, "msg": "异常 %s" % e}
chk("新版能被读出来（不是当成陌生网站）",
    (not r2.get("ok")) and "联系电话" in (r2.get("msg") or ""),
    (r2.get("msg") or "")[:60].replace("\n", " "))

sec("12. 从 site_probe 进去也要能分流到金数据")
try:
    r3 = SP.probe(OLD)
except Exception as e:
    r3 = {"ok": False, "msg": "异常 %s" % e}
chk("走 site_probe 同样认得出来", r3.get("ok") is True,
    (r3.get("msg") or "")[:60])
chk("分回来的确实是金数据那套",
    (r3.get("site") or {}).get("platform") == "jinshuju",
    (r3.get("site") or {}).get("platform"))

# ---------------------------------------------------------------- 收尾
OUT.append("")
OUT.append("=" * 68)
OUT.append("总结果：%s" % ("全部通过 ✅" if OK else "有失败 ❌"))
txt = "\n".join(OUT)
Path(__file__).with_name("jinshuju_test.txt").write_text(txt, encoding="utf-8")
print(txt)
sys.exit(0 if OK else 1)
