# -*- coding: utf-8 -*-
"""Throttler 行为验证 —— 不访问任何网站，只验证节流数学与状态机。

为什么要单独测：退避/冷却/熔断这些分支，正常跑通时根本不会触发。
不测就等于没写，等真被限流了才发现逻辑错，代价太大。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "程序"))
from cert_downloader import PACE, Throttler   # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print("  [%s] %-34s %s" % ("OK  " if cond else "FAIL", name, detail))


print("=" * 74)
print("Throttler 节流状态机验证")
print("=" * 74)

# ---------- 1. 正常间隔落在 base ± jitter 区间内 ----------
print("\n1) 正常间隔范围（base=0.10, jitter=0.45 → 应在 0.055~0.145）")
th = Throttler(base=0.10, jitter=0.45, every=5, long_pause=0.50)
vals = []
for i in range(1, 5):
    s, why = th.wait(i)
    vals.append(round(s, 4))
print("     实测：%s" % vals)
check("4 次间隔都在 0.055~0.146", all(0.055 <= v <= 0.146 for v in vals), str(vals))
check("间隔确实有随机抖动（不都相同）", len(set(vals)) > 1, str(len(set(vals))) + " 个不同值")

# ---------- 2. 每 N 条触发长歇 ----------
print("\n2) 第 5 条应触发长歇（every=5, long_pause=0.5）")
s, why = th.wait(5)
print("     实测：%.4f 秒，原因=%s" % (s, why or "(无)"))
check("第 5 条标为「长歇」", why == "长歇", "得到 %r" % why)
check("长歇明显长于普通间隔（>0.30）", s > 0.30, "%.4f" % s)
check("长歇计数 +1", th.rests == 1, "rests=%d" % th.rests)

# ---------- 3. 普通错误 → 指数退避 ----------
print("\n3) 连续 3 次普通错误 → 退避应递增")
th2 = Throttler(base=0.05, err_burst=6, backoff_base=0.10, cooldown=0.20)
rows = []
for _ in range(3):
    th2.note_err()
    s, why = th2.wait(1)
    rows.append((round(s, 3), why))
print("     实测：%s" % rows)
check("三次都走「退避」", all(w == "退避" for _, w in rows), str(rows))
check("退避间隔递增", rows[0][0] < rows[1][0] < rows[2][0], str([r[0] for r in rows]))

# ---------- 4. 限流错误直接跳档 ----------
print("\n4) 单次限流错误（kind='rate'）应直接跳到 streak=3")
th3 = Throttler(base=0.05, backoff_base=0.10)
th3.note_err("rate")
check("streak 直接为 3（普通错误只会是 1）", th3.streak == 3, "streak=%d" % th3.streak)
th3b = Throttler(base=0.05)
th3b.note_err("net")
check("普通错误 streak 为 1", th3b.streak == 1, "streak=%d" % th3b.streak)

# ---------- 5. 连错达阈值 → 冷却 ----------
print("\n5) 连错达 err_burst=4 → 应进入冷却")
th4 = Throttler(base=0.05, err_burst=4, backoff_base=0.05, cooldown=0.30, abort_streak=99)
for _ in range(4):
    th4.note_err()
s, why = th4.wait(1)
print("     实测：%.3f 秒，原因=%s" % (s, why))
check("标为「冷却」", why == "冷却", "得到 %r" % why)
check("冷却时长 = cooldown", abs(s - 0.30) < 1e-6, "%.3f" % s)

# ---------- 6. 成功一次就复位 ----------
print("\n6) 一次成功应把连续错误计数清零")
th4.note_ok()
check("streak 归零", th4.streak == 0, "streak=%d" % th4.streak)
s2, why2 = th4.wait(2)
check("复位后回到普通间隔", why2 == "", "原因=%r" % why2)

# ---------- 7. 连错到 abort 阈值 → 应中止 ----------
print("\n7) 连错达 abort_streak=6 → 应主动中止")
th5 = Throttler(base=0.05, err_burst=3, abort_streak=6)
for n in range(1, 7):
    th5.note_err()
    if n == 5:
        check("第 5 次连错时尚未中止", not th5.should_abort, "streak=%d" % th5.streak)
check("第 6 次连错触发中止", th5.should_abort, "streak=%d" % th5.streak)

# ---------- 8. 档位常量 ----------
print("\n8) PACE 档位定义")
check("含 safe/normal/fast", set(PACE) == {"safe", "normal", "fast"}, str(PACE))
check("档位速度递增 safe > normal > fast",
      PACE["safe"] > PACE["normal"] > PACE["fast"], str(PACE))

# ---------- 9. 全量耗时估算 ----------
print("\n9) 609 人耗时估算（safe 档）")
q = 609 * PACE["safe"]
d = 300 * PACE["safe"] * 1.5 + 300 * 3.0
print("     查询约 %.1f 分钟 + 下载约 %.1f 分钟 = 合计约 %.1f 分钟"
      % (q / 60, d / 60, (q + d) / 60))

print("\n" + "=" * 74)
passed, total = sum(results), len(results)
print("结果：%d/%d 通过 %s" % (passed, total, "✅" if passed == total else "❌ 有失败项"))
print("=" * 74)
sys.exit(0 if passed == total else 1)
