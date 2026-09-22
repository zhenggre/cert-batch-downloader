# -*- coding: utf-8 -*-
"""熔断中止路径 + 返回结构 验证。

熔断本来要连错 18 次才触发，中间还有 120 秒冷却——真等要 20 多分钟。
这里只把时间常数缩小（逻辑一行没改），几秒就能跑到同一条代码路径。
"""
import json
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import cert_downloader as CD  # noqa: E402

OUT = []
OK = True


def sec(t):
    OUT.append("")
    OUT.append("=" * 70)
    OUT.append(t)
    OUT.append("=" * 70)


def chk(name, got, want=True):
    global OK
    good = (got == want)
    if not good:
        OK = False
    OUT.append("  %-56s %s" % (name, "✅" if good else "❌ 期望 %s，实际 %s" % (want, got)))


# ---- 把时间常数调小，逻辑保持不变 ----
_orig = CD.Throttler.__init__


def patched(self, *a, **kw):
    kw["err_burst"] = 2          # 连错 2 次 → 冷却
    kw["cooldown"] = 0.5         # 冷却 0.5 秒（原 120）
    kw["backoff_base"] = 0.05    # 退避基数 0.05 秒（原 6）
    kw["abort_streak"] = 3       # 连错 3 次 → 熔断（原 18）
    kw["long_pause"] = 0.1
    _orig(self, *a, **kw)


CD.Throttler.__init__ = patched

# ---- 假站点：连接必被拒绝 ----
cfg = json.loads((R / "程序" / "config.json").read_text(encoding="utf-8"))
s = cfg["sites"][cfg.get("default_site") or next(iter(cfg["sites"]))]
s["home_url"] = "http://127.0.0.1:9/q/zzzzzz"
s["query_url"] = "http://127.0.0.1:9/web/pubdata/query"
s["title"] = "（模拟：熔断场景）"
tmp = R / "开发维护" / "_tmp_cfg_abort.json"
tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

outdir = R / "开发维护" / "诊断测试_熔断"
if outdir.exists():
    for f in outdir.iterdir():
        try:
            f.unlink()
        except Exception:
            pass

sec("1. 熔断：连续异常时应主动中止并给出恢复步骤（不用等 18 次，逻辑相同）")
logs = []
res = None
try:
    res = CD.run_job(str(R / "开发维护" / "名单示例_演示用.xlsx"), str(outdir),
                     config_path=str(tmp), pace="fast", limit=40, log_sink=logs.append)
except Exception as e:
    OUT.append("  运行异常：%s: %s" % (type(e).__name__, e))
joined = "\n".join(logs)

chk("日志出现「主动中止」", "主动中止" in joined)
chk("给出了「任务提前停止」说明", "任务提前停止" in joined)
chk("说明了已处理多少人（不白干）", "本次已处理" in joined)
chk("给出了恢复步骤（第 1 步）", "第 1 步｜" in joined)
chk("恢复步骤里含「重新运行」那一步", "重新双击程序" in joined)
chk("恢复步骤跟真实故障对得上（代理 → 先处理代理）",
    ("第 1 步｜退出 VPN" in joined) or ("第 1 步｜等 30 分钟" in joined))
chk("说明会自动跳过已完成", "自动跳过" in joined)
chk("提醒不要反复重试", "不要连续反复重试" in joined or "千万不要连着反复" in joined
    or "换成热点" in joined)
chk("确实没有跑完全部 40 人", res is None or len(res["results"]) < 40)

sec("2. 返回结构：界面要用的字段都得有")
chk("返回里含 advice 字段", isinstance(res, dict) and "advice" in res)
chk("advice 非空（有故障就该有建议）", bool(res and res.get("advice")))
chk("返回里含 diag_file", bool(res and res.get("diag_file")))
chk("故障报告文件真实存在", bool(res and res.get("diag_file")
                                 and Path(res["diag_file"]).exists()))
chk("返回里含 log_file", bool(res and res.get("log_file")))

sec("3. 故障报告内容完整性")
if res and res.get("diag_file"):
    c = Path(res["diag_file"]).read_text(encoding="utf-8")
    chk("报告含「故障诊断报告」标题", "故障诊断报告" in c)
    chk("报告含名单文件名", "名单示例_演示用.xlsx" in c)
    # 用户的原话：「他不知道发哪一份文件出来」「文件上要标注清楚干的什么、
    # 什么时间、网址是什么、名单是什么」。拿文件的人不会修，他只负责转发，
    # 所以这几项必须写进文件本体 —— 这一组断言就是那个需求的护栏。
    chk("报告含比赛网址（接手的人不用再问）", "比赛网址" in c and "127.0.0.1:9" in c)
    chk("报告含名单完整路径", "名单路径" in c)
    chk("报告含名单人数", "人）" in c)
    chk("报告含「本次任务」（干了什么）", "本次任务" in c)
    chk("报告顶上直接给出结论", "出了什么问题：" in c)
    chk("报告含本次统计", "本次统计" in c)
    chk("报告含故障与处理方法", "故障与处理方法" in c)
    chk("报告含完整日志", "完整日志" in c)
    chk("报告含运行环境信息（版本/系统）", "程序版本" in c and "操作系统" in c)
    chk("报告写明这份文件就是用来转发的", "原样发给技术支持" in c)
    OUT.append("")
    OUT.append("  ── 报告开头 22 行 ──")
    for l in c.splitlines()[:22]:
        OUT.append("  " + l)

sec("4. 日志末尾的建议（同事截图/复制给技术支持看的就是这段）")
tail = logs[-16:]
OUT.append("")
for l in tail:
    OUT.append("  " + l)

sec("5. 结论")
OUT.append("  熔断与返回结构：%s" % ("全部通过 ✅" if OK else "存在问题 ❌"))

txt = "\n".join(OUT)
Path(__file__).with_name("abort_test.txt").write_text(txt, encoding="utf-8")
sys.exit(0 if OK else 1)
