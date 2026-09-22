# -*- coding: utf-8 -*-
"""「打开这份报告」按钮的后端路由测试（不联网、不真的弹窗）。

背景：2026-09-21 用户现场报「点开这份报告，没反应」。
根因是 /api/open?what=diag 当时**只读** STATE["diag"]，而那个值只在
**异常分支**才会被赋值 —— 于是"任务正常跑完但有几处失败"这条路，
target 恒为空、接口一声不响地返回成功，界面上就是"点了没反应"。

修好之后取值顺序是三级兜底：
    result["diag_file"]（绝对路径，正常完成也有）
      → STATE["diag"]（只有文件名，得跟输出目录拼）
        → find_latest_report(输出目录)（只认 15 分钟内生成的，最后的兜底）

这个测试**绕开浏览器**，直接打接口，专盯"到底解析到了哪个文件"：
界面那半截（按钮在不在、点了有没有反应）由真机测试管，这里管的是它拿到的结果。

注意：会把 open_in_file_manager 换掉，只记录目标、不真去打开 ——
免得测试在用户电脑上哗啦啦弹一堆记事本。
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "程序"
sys.path.insert(0, str(PROG))

import web_app as W          # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [X ] %s %s" % (name, extra))


PORT = W.pick_port(8960)
HTTPD = None
OPENED = []          # open_in_file_manager 被调用时的目标


def fake_open(target):
    OPENED.append(str(target))
    return True


def post(path, obj=None):
    # path 里可能已经带了查询串（?what=diag），拼接符要跟着变，
    # 否则会拼成 "...?what=diag?token=xxx" —— token 被吞进 what 的值里，直接 403。
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s%stoken=%s" % (PORT, path, sep, W.TOKEN),
        data=json.dumps(obj or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


def reset_state(diag_file=None, diag_name="", out=""):
    W.STATE["result"] = {"diag_file": diag_file} if diag_file is not None else None
    W.STATE["diag"] = diag_name
    W.STATE["out"] = out
    OPENED.clear()


TMP = Path(tempfile.mkdtemp(prefix="certdl_openrpt_"))
REPORT = TMP / "故障诊断_20260921_1710.txt"
REPORT.write_text("（测试用报告）", encoding="utf-8")

# 真去打开会弹窗，换掉它
W.core.open_in_file_manager = fake_open

try:
    HTTPD = W.ThreadingHTTPServer(("127.0.0.1", PORT), W.Handler)
    threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("测试服务已起：127.0.0.1:%d" % PORT)

    # ---------------------------------------------------------------- A
    print("\n=== A. 正常跑完：报告路径在 result 里（这正是当初漏掉的那条路）===")
    reset_state(diag_file=str(REPORT))
    r = post("/api/open?what=diag")
    check("接口返回 ok", r.get("ok") is True, str(r)[:160])
    check("真的去打开了那份报告", OPENED == [str(REPORT)], str(OPENED))

    # ---------------------------------------------------------------- B
    print("\n=== B. 兜底：只有文件名时，跟输出目录拼出来 ===")
    reset_state(diag_name=REPORT.name, out=str(TMP))
    r = post("/api/open?what=diag")
    check("接口返回 ok", r.get("ok") is True, str(r)[:160])
    check("拼出来的路径正确", OPENED == [str(REPORT)], str(OPENED))

    # ---------------------------------------------------------------- C
    print("\n=== C. 兜底第三级：没有记录时，去输出目录找最近生成的报告 ===")
    reset_state(out=str(TMP))
    r = post("/api/open?what=diag")
    check("找着了最近那份报告", r.get("ok") is True, str(r)[:160])
    check("打开的确实是那份", OPENED == [str(REPORT)], str(OPENED))

    # ---------------------------------------------------------------- C2
    print("\n=== C2. 什么都没有、目录里也没有 → 必须明说，不能装死 ===")
    EMPTY = Path(tempfile.mkdtemp(prefix="certdl_empty_"))
    reset_state(out=str(EMPTY))
    r = post("/api/open?what=diag")
    check("返回 ok=False（不再静默成功）", r.get("ok") is False, str(r)[:160])
    check("带了给人看的话", bool((r.get("msg") or "").strip()), str(r)[:160])
    check("没去打开任何东西", OPENED == [], str(OPENED))

    # ---------------------------------------------------------------- D
    print("\n=== D. 路径指向一个已经不存在的文件 → 也要说清 ===")
    reset_state(diag_file=str(TMP / "早就删掉了.txt"))
    r = post("/api/open?what=diag")
    check("返回 ok=False", r.get("ok") is False, str(r)[:160])
    check("提示里点了这个位置", "不存在" in (r.get("msg") or ""), str(r)[:160])

    # ---------------------------------------------------------------- E
    print("\n=== E. 系统不让打开 → 退化成「请手动去找」，而不是装作成功 ===")
    W.core.open_in_file_manager = lambda target: False
    reset_state(diag_file=str(REPORT))
    r = post("/api/open?what=diag")
    check("返回 ok=False", r.get("ok") is False, str(r)[:160])
    check("提示里给了手动路径", str(REPORT) in (r.get("msg") or ""), str(r)[:160])
    W.core.open_in_file_manager = fake_open

    # ---------------------------------------------------------------- F
    print("\n=== F. 令牌仍然把门 ===")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/open?what=diag" % PORT,
            data=b"{}", headers={"Content-Type": "application/json"})
        body = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        check("没令牌 → ok=False", body.get("ok") is False, str(body)[:120])
    except urllib.error.HTTPError as e:
        check("没令牌 → 被拦（HTTP %d）" % e.code, e.code in (401, 403), str(e.code))

finally:
    if HTTPD:
        HTTPD.shutdown()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        shutil.rmtree(EMPTY, ignore_errors=True)
    except NameError:
        pass

print()
print("=" * 66)
print("结果：%d 项通过，%d 项失败" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
