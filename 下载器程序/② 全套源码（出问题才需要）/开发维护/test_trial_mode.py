# -*- coding: utf-8 -*-
"""「试跑」必须自报家门 —— 源码级回归（快，不联网、不起服务）。

2026-09-22 的事故：用户筛出 604 人、「先随机试跑」这个勾**默认就是开的**，
他没注意，于是只跑了 8 个人；而报告上写着「全部完成 ✅」、汇总清单里写着
「名单总人数（本批次）= 8」。他（以及任何拿到这份报告的同事）只会得出
两个错误结论之一：「名单就 8 个人」或者「工具坏了」。

**这是典型的"不报错的错"** —— 程序没崩、报告很整齐、但结论完全错了。
按项目军规，这种必须配独立检测点。下面每一条都钉着一个具体环节。

规则（四处都要说话，缺一处就等于没说）：
  1. 界面：默认**不勾**试跑；勾了立刻亮提示；跑时标题「试跑中」；跑完「试跑结束」
  2. 日志：开头 ★ 喊一嗓子；结尾「试跑结束」，且**不许写「全部完成」**
  3. 汇总清单：「统计概览」头几行写明"本次是试跑 / 筛完几人 / 本次查几人"
  4. 同事版说明书：讲清"试跑结束 ≠ 全量结果"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROG = ROOT / "程序"
HTML = (PROG / "web" / "index.html").read_text(encoding="utf-8")
CORE = (PROG / "cert_downloader.py").read_text(encoding="utf-8")
APP = (PROG / "web_app.py").read_text(encoding="utf-8")
ASM = (Path(__file__).with_name("assemble.py")).read_text(encoding="utf-8")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [X ] %s   %s" % (name, extra))


print("== A. 界面：默认不勾试跑 ==")
# 这勾默认是开的，就是这次事故的根：用户选了 604 人，程序默默只跑 8 个。
check("「先随机试跑」默认不勾（源码里没有 checked）",
      'id="try" checked' not in HTML, "index.html 里 #try 还带着 checked")
check("勾上时有醒目提示条（try-warn）", "try-warn" in HTML)
check("提示条里写明了「不是全部人」",
      "不是全部人" in HTML or "只抽查" in HTML)

print("\n== B. 界面：跑的时候/跑完，标题必须换 ==")
check("运行中标题会写「试跑中」", "试跑中" in HTML)
check("跑完标题会写「试跑结束」", "试跑结束" in HTML)
check("跑完给出一条专门的试跑说明（trial-note）", "trial-note" in HTML)
check("说明里点了「没跑多少人」这个数", "filtered_total" in HTML and "sampled" in HTML)

print("\n== C. 引擎日志：开头喊、结尾改口 ==")
check("抽样处有 ★ 级提醒", "★ 这是「试跑」" in CORE)
check("提醒里同时给了「筛完几人」和「本次查几人」",
      "筛选后名单有 %d 人" in CORE and "本次只随机查了 %d 人" in CORE)
check("结尾会写「试跑结束」", "试跑结束" in CORE)
# 关键：试跑分支里绝不能出现「全部完成」—— 那四个字就是误会之源。
i_trial = CORE.find("试跑结束 🧪")
i_done = CORE.rfind('emit("全部完成 ✅")')
check("「全部完成 ✅」只留给非试跑那条分支",
      i_trial != -1 and i_done != -1 and i_trial < i_done,
      "trial=%d done=%d" % (i_trial, i_done))

print("\n== D. 汇总清单：统计概览头几行要说清 ==")
check("write_summary 接受 trial 参数", "def write_summary(" in CORE and "trial=None" in CORE)
check("会写「本次是「试跑」」这一行", "本次是「试跑」" in CORE)
check("会写「筛选后名单总人数（本次没跑）」", "筛选后名单总人数（本次没跑）" in CORE)
check("会写「本次实际查询人数」", "本次实际查询人数" in CORE)
check("调用处把 trial 传进去了",
      "trial=({" in CORE and '"sampled": sampled_n' in CORE)

print("\n== E. 接口：把试跑信息带给界面 ==")
check("progress 里带 trial 标记", '"trial": STATE.get("trial"' in APP)
check("progress 里带 trial_n（试跑几人）", '"trial_n"' in APP)
check("result 里带 sampled / filtered_total",
      '"sampled"' in APP and '"filtered_total"' in APP)
check("起任务时就把 trial 挂进状态", '"trial": bool(sample)' in APP)

print("\n== F. 同事版说明书：讲清「试跑结束 ≠ 全量」 ==")
check("说明书里有「试跑结束」这一说法", "试跑结束" in ASM)
check("说明书说明了试跑默认不勾", "默认是关的" in ASM or "默认**不勾**" in ASM)
check("说明书还在教人怎么跑全量（去掉勾）", "去掉勾" in ASM or "把勾去掉" in ASM)
# 旧版写的是"保持勾选「先随机试跑」" —— 默认改掉之后这句话就是反的了。
check("没有残留「保持勾选「先随机试跑」」这种过时说法",
      "保持勾选「先随机试跑」" not in ASM)

print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
