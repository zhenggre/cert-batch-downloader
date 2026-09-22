# -*- coding: utf-8 -*-
"""网页界面 · 故障提示的端到端检查
验证：界面结构齐全 → 报表接口返回诊断 → 任务失败时界面能拿到"原因 + 怎么办"
"""
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))
import web_app  # noqa: E402

# 本机环境配了 HTTP 代理，访问 127.0.0.1 也会被它拦，测试里直连
S = requests.Session()
S.trust_env = False
PORT = 8899
BASE = "http://127.0.0.1:%d" % PORT
TK = web_app.TOKEN
buf = []
OK = True


def p(s=""):
    buf.append(str(s))


def chk(name, good):
    global OK
    if not good:
        OK = False
    p("  %-56s %s" % (name, "✅" if good else "❌"))


httpd = ThreadingHTTPServer(("127.0.0.1", PORT), web_app.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(1.5)

p("=" * 70)
p("1. 界面结构：故障提示要用的元素都在")
p("=" * 70)
html = S.get(BASE + "/", timeout=10).text
chk("令牌已注入（页面能调接口）", "__TOKEN__" not in html)
chk("有「遇到问题怎么办」折叠区", 'id="advice-box"' in html)
chk("有故障报告提示条", 'id="diag-tip"' in html)
chk("有结果面板标题（可改文案）", 'id="done-title"' in html)
chk("有日志自动着色函数", "guessClass" in html)
chk("诊断分隔线有专门样式", "#log .rule" in html)
chk("解决方案行有专门样式", "#log .fix" in html)
chk("失败时会显示报告链接", "/api/open?what=diag" in html)

p("")
p("=" * 70)
p("2. 报表接口要带上诊断信息（否则界面无从显示）")
p("=" * 70)
s = S.get(BASE + "/api/progress?token=%s&offset=0" % TK, timeout=10).json()
chk("progress 接口含 diag 字段", "diag" in s)
chk("progress 接口含 error 字段", "error" in s)
chk("progress 接口含 result 字段", "result" in s)

p("")
p("=" * 70)
p("3. 真实失败：让任务读一份不存在的名单（走 web 端的异常分支）")
p("=" * 70)
outdir = ROOT / "开发维护" / "界面诊断测试"
if outdir.exists():
    for f in outdir.iterdir():
        try:
            f.unlink()
        except Exception:
            pass

with web_app.LOCK:
    web_app.STATE.update({"logs": [], "error": None, "result": None, "diag": ""})
web_app.job_thread(str(ROOT / "开发维护" / "__不存在的名单__.xlsx"), str(outdir),
                   None, "", 0)

with web_app.LOCK:
    st = dict(web_app.STATE)

chk("任务被标记为失败", bool(st.get("error")))
chk("state 里记住了故障报告名", bool(st.get("diag")))
chk("报告名格式正确（故障诊断_*.txt）",
    bool(st.get("diag")) and st["diag"].startswith("故障诊断_") and st["diag"].endswith(".txt"))

logs = [l["t"] for l in st.get("logs", [])]
joined = "\n".join(logs)
chk("界面日志里有诊断块", ("【名单读取失败】" in joined) or ("【程序出错】" in joined))
chk("诊断说对了原因（是文件找不到，不是表头写错）",
    "找不到名单文件" in joined and "找不到需要的列" not in joined)
chk("同一件事只诊断一次（不重复、不矛盾）", joined.count("怎么回事：") == 1)
chk("报告文件名只提示一次（不啰嗦）", joined.count("★ 故障报告") == 1)
chk("技术细节被标为灰色提示（不吓着同事）", "以下是技术细节" in joined)

p("")
p("  ── 同事在网页日志区看到的最后 20 行 ──")
for l in logs[-20:]:
    p("  " + l)

p("")
p("=" * 70)
p("4. 报表接口返回故障信息（前端据此弹提示）")
p("=" * 70)
s = S.get(BASE + "/api/progress?token=%s&offset=0" % TK, timeout=10).json()
chk("接口返回了 error 文案", bool(s.get("error")))
chk("接口返回了报告文件名", bool(s.get("diag")))
p("      error = %s" % str(s.get("error"))[:80])
p("      diag  = %s" % s.get("diag"))

p("")
p("=" * 70)
p("5. 结果面板的数据结构（有故障时要带建议）")
p("=" * 70)
with web_app.LOCK:
    web_app.STATE["error"] = None
    web_app.STATE["diag"] = ""
    web_app.STATE["result"] = {
        "cert": 280, "schools": 40, "miss": 300, "fail": 17,
        "advice": ["本次运行的问题", "· 查询失败 17 人：隔 30 分钟重跑即可"],
        "diag_file": "D:/x/故障诊断_1.txt", "diag_name": "故障诊断_1.txt",
    }
s = S.get(BASE + "/api/progress?token=%s&offset=0" % TK, timeout=10).json()
r = s.get("result") or {}
chk("结果里带 advice 列表", isinstance(r.get("advice"), list) and len(r["advice"]) == 2)
chk("结果里带 diag_name（界面显示用）", r.get("diag_name") == "故障诊断_1.txt")
chk("统计字段齐全", all(k in r for k in ("cert", "schools", "miss", "fail")))

p("")
p("=" * 70)
p("6. 结论")
p("=" * 70)
p("  网页界面故障提示：%s" % ("全部通过 ✅" if OK else "存在问题 ❌"))

(ROOT / "开发维护" / "ui_diag_test.txt").write_text("\n".join(buf), encoding="utf-8")
sys.exit(0 if OK else 1)
