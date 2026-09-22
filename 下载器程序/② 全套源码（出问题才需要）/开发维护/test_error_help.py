# -*- coding: utf-8 -*-
"""检查故障自救手册的排版与覆盖度（不联网、不碰网站）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import error_help as EH  # noqa: E402

out = []
ok = True


def sec(t):
    out.append("")
    out.append("=" * 70)
    out.append(t)
    out.append("=" * 70)


# ---- 1. 每种故障都要能被渲染出来，且上下分隔线等长 ----
sec("1. 全部故障类型渲染检查")
RULE_LEN = 60
for k in sorted(EH.DIAG.keys()):
    lines = EH.explain(k, detail="示例现场信息：HTTP 403")
    frame = [l for l in lines if set(l) == {"─"}]
    fw = {len(l) for l in frame}
    bad = (fw != {RULE_LEN}) or (len(frame) != 2)
    if bad:
        ok = False
    out.append("  [%-12s] 行数=%2d  分隔线=%s  %s"
               % (k, len(lines), "、".join(str(x) for x in sorted(fw)),
                  "❌ 分隔线异常" if bad else "✅"))

# ---- 2. 检查所有故障都至少有 3 条解决方案 ----
sec("2. 解决方案条数检查（要求 ≥3 条）")
for k in sorted(EH.DIAG.keys()):
    n = len(EH.DIAG[k]["fixes"])
    mark = "✅" if n >= 3 else "❌ 不足 3 条"
    if n < 3:
        ok = False
    out.append("  [%-12s] %d 条  %s  %s" % (k, n, mark, EH.DIAG[k]["name"]))

# ---- 3. 是否含技术黑话（同事看不懂的词） ----
sec("3. 黑话扫描（出现在给同事看的文字里就不合适）")
JARGON = ["异常堆栈", "traceback", "HTTP 5xx 错误码", "timeout 参数", "socket",
          "Traceback", "None", "retry", "session"]
for k in sorted(EH.DIAG.keys()):
    d = EH.DIAG[k]
    blob = " ".join(d["what"] + d["why"] + d["fixes"] + [d.get("warn", ""), d.get("good", "")])
    found = [w for w in JARGON if w in blob]
    if found:
        ok = False
    out.append("  [%-12s] %s" % (k, ("❌ 含 " + "、".join(found)) if found else "✅ 干净"))

# ---- 4. 每种故障都提到了「重跑会跳过已完成」或给出可执行动作 ----
sec("4. 是否有可执行动作（含祈使动词）")
ACTION = ["点", "打开", "换", "重新", "等", "检查", "改", "复制", "连上", "运行", "重启", "联系"]
for k in sorted(EH.DIAG.keys()):
    blob = " ".join(EH.DIAG[k]["fixes"])
    hit = [w for w in ACTION if w in blob]
    if not hit:
        ok = False
    out.append("  [%-12s] 命中 %s  %s" % (k, "/".join(hit[:5]), "✅" if hit else "❌"))

# ---- 5. 打印两个典型样例看排版 ----
sec("5. 样例：被限流（同事最可能遇到的）")
out.extend(EH.explain("blocked", detail="HTTP 403（疑似被限流）"))

sec("6. 样例：中断后的恢复步骤")
out.extend(EH.resume_steps("blocked", done=312, total=609))

sec("7. 样例：作业结束后的建议")
out.extend(EH.finish_advice(
    kinds={"blocked", "pdf_stuck"},
    stats={"成功": 280, "已完成跳过": 0, "未获奖无证书": 300, "查询失败": 17, "下载失败": 12},
    aborted=False))

sec("8. 样例：熔断中止后的建议（本次只跑了 312/609）")
out.extend(EH.finish_advice(
    kinds={"blocked"},
    stats={"成功": 150, "_done": 312, "_total": 609, "查询失败": 40},
    aborted=True))

sec("9. 样例：一切正常时不应该刷屏")
noise = EH.finish_advice(kinds=set(), stats={"成功": 300, "未获奖无证书": 309}, aborted=False)
out.append("  返回行数 = %d  %s" % (len(noise), "✅ 无噪声" if not noise else "❌ 有噪声"))
if noise:
    ok = False

sec("10. 诊断报告落盘")
rp = Path(__file__).with_name("故障诊断_样例.txt")
# 这份样例是给人的实物范本，字段必须跟真实运行时一致：
# 「时间 / 网址 / 名单 / 干了什么 / 影响多少人 / 环境」一个都不能少。
p = EH.write_report(rp, {"blocked", "pdf_stuck"},
                    {"成功": 280, "未获奖无证书": 300, "查询失败": 17, "下载失败": 12},
                    ["[10:00:01] 阶段 1/4 全量查询", "[10:12:30] !! HTTP 403（疑似被限流）"],
                    meta={"本次任务": "全量查询 + 下载证书（节奏 safe，每人约 1.6 秒）",
                          "本次处理": "597 人 / 名单共 597 人",
                          "比赛网址": "https://biaodan100.com/q/<SHORTID>",
                          "查询接口": "https://biaodan100.com/web/pubdata/query?SHORTID=<SHORTID>",
                          "名单文件": "示例名单.xlsx（共 597 人）",
                          "名单路径": "C:\\Users\\<user>\\Desktop\\示例名单.xlsx",
                          "查询站点": "示例比赛",
                          "输出目录": "C:\\Users\\<user>\\Desktop\\证书"},
                    tech=EH.env_info(
                        version="1.4",
                        site={"title": "示例比赛",
                              "query_url": "https://biaodan100.com/web/pubdata/query?SHORTID=<SHORTID>",
                              "field_map": {"name": "F1", "id": "F999", "school": "F3"}},
                        site_key="site_example",
                        proxy="http://127.0.0.1:7890"))
out.append("  报告文件：%s" % p)
out.append("  大小：%d 字节  %s" % (rp.stat().st_size, "✅" if rp.stat().st_size > 500 else "❌"))
if rp.stat().st_size <= 500:
    ok = False

sec("11. 结论")
out.append("  排版与覆盖度检查：%s" % ("全部通过 ✅" if ok else "存在问题 ❌"))

txt = "\n".join(out)
print(txt)
Path(__file__).with_name("error_help_test.txt").write_text(txt, encoding="utf-8")
print("\n\n退出码 =", 0 if ok else 1)
sys.exit(0 if ok else 1)
