# -*- coding: utf-8 -*-
"""site_probe 认「证书文件地址」的规则回归（纯逻辑，不发网络请求）。

这个规则**连着踩过两次同一个坑**，所以测试盯得死一点：

  2026-09-21：原来只要字段 TYP 是 file/upload、或标签带「文件/附件」就抢先用它。
              「快递信息填写」那类模板里这种字段满地都是 → 证书地址被指到 F13，
              下载时取到空串、抛 MissingSchema，整批被判成"下载失败"。
  2026-09-22：把上面那条删了，却留了一条"标签含「证书」就用它"。
              同一个快递模板里「证书快递地址」「证书快递收件人」「证书收件人电话」
              全都含「证书」→ 证书地址被指到 F13 = 「证书快递收件人」（收件人姓名！）。

**结论：这个平台只有平台约定的键名 "URL" 一个正确答案，任何"看着像"的字段都是赌。**
（同平台其它比赛的配置，用的就是 "URL"。）

所以下面最硬的一条断言是：**file_url 永远不等于表单里的任何一个字段名。**
"""
import sys
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PROG))
import site_probe as SP          # noqa: E402

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK] %s → %s" % (name, got))
    else:
        FAIL += 1
        print("  [X ] %s → 得到 %r，期望 %r" % (name, got, want))


def fld(nm, lbl, typ="text", sub=None):
    d = {"NM": nm, "LBL": lbl, "TYP": typ}
    if sub:
        d["SUBFLDS"] = sub
    return d


# 一份最小的、能被认全的表单（姓名/证件号/学校/奖项/指导老师/省市县）
BASE = [
    fld("F2", "查询姓名"),
    fld("F999", "查询证件号"),
    fld("F5", "学校名称"),
    fld("F9", "奖项名称"),
    fld("F8", "指导老师"),
    fld("F1", "所在地区", sub={"PRV": {"NM": "F10"},
                              "CITY": {"NM": "F11"},
                              "ZIP": {"NM": "F12"}}),
]


def file_url_of(extra):
    frm = {"FLDS": BASE + list(extra)}
    fm, _notes = SP.classify_fields(frm)
    return fm.get("file_url")


def field_names(extra):
    """这些字段名，任何一个都不许被当成证书地址。"""
    frm = {"FLDS": BASE + list(extra)}
    return {f["nm"] for f in SP._flat_fields(frm.get("FLDS")) if not f["sub"]}


print("== A. 2026-09-21 那个坑：页面有『快递单号照片』（标签带『文件』之意 + file 类型）==")
check("不许把快递字段当证书地址",
      file_url_of([fld("F13", "快递单号照片", typ="file")]), "URL")

print("\n== B. 2026-09-22 那个坑：整张表就是『快递信息填写』模板（国赛 a4xzxj 的真实字段）==")
A4XZXJ = [
    fld("F2", "参赛学生姓名", typ="name"),
    fld("F999", "参赛学生证件号"),
    fld("F4", "证书快递地址", typ="address",
        sub={"ZIP": {"NM": "F1"}, "PRV": {"NM": "F10"},
             "CITY": {"NM": "F11"}, "DTL": {"NM": "F12"}}),
    fld("F13", "证书快递收件人", typ="name"),      # ← 就是它被误当成证书地址
    fld("F14", "证书收件人电话", typ="phone"),
    fld("F5", "学校名称"),
    fld("F6", "参赛赛项"),
    fld("F8", "指导老师姓名"),
    fld("F9", "决赛获奖情况"),
]
got = file_url_of(A4XZXJ)
check("国赛那张表 → 只用 URL", got, "URL")
check("绝不能是 F13（证书快递收件人 = 收件人姓名）", got != "F13", True)
check("也不能是 F1/F14（证书快递地址 / 收件人电话）",
      got not in ("F1", "F14"), True)
check("识别出的奖项/学校没受影响（F9/F5）", SP.classify_fields(
    {"FLDS": BASE + A4XZXJ})[0].get("award"), "F9")

print("\n== C. 页面里根本没有文件类字段（老比赛就是这样）==")
check("回落平台约定的 URL", file_url_of([]), "URL")

print("\n== D. 字段列表里真的有个叫 URL 的字段 ==")
check("用那个 URL 字段",
      file_url_of([fld("URL", "证书下载地址", typ="upload")]), "URL")

print("\n== E. 只有泛化的『附件』字段 → 也不冒险 ==")
check("长得像文件的字段不许当证书地址",
      file_url_of([fld("F30", "相关附件", typ="file")]), "URL")

print("\n== F. 关键反转：就算有个字段标签写着『获奖证书』，也不许拿它 ==")
# 昨天那条"标签含『证书』就优先"的决定**是错的**：快递模板里含「证书」的字段
# 全是快递信息。这个平台只有 URL 是对的 —— 猜字段等于赌，赌输就是整批假失败。
check("标签含『证书』的字段也不许当证书地址",
      file_url_of([fld("F13", "上传回执", typ="file"),
                   fld("F20", "获奖证书", typ="file")]), "URL")

print("\n== G. 最硬的一条：证书地址永远不等于表单里的任何字段名 ==")
cases = {
    "国赛快递模板": A4XZXJ,
    "带『证书』标签": [fld("F20", "获奖证书", typ="file")],
    "泛化附件": [fld("F30", "相关附件", typ="file")],
    "带 file 类型的收件人": [fld("F13", "证书快递收件人", typ="file")],
}
for label, extra in cases.items():
    fu = file_url_of(extra)
    check("%s → 不是任何字段名" % label, fu not in field_names(extra), True)

print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
