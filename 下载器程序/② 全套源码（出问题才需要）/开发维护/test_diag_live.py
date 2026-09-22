# -*- coding: utf-8 -*-
"""真实故障模拟：把站点地址故意改坏，验证程序给出的建议是否对得上。

两种环境都要测，因为同事电脑上这两种都可能出现：
  A. 无代理（大多数同事的常态）→ 应识别为「连不上网站」/「被限流」
  B. 有代理（公司统一代理、VPN、加速器）→ 应识别为「代理问题」，绝不能误报成网站故障

全程用假地址，不会碰到真网站，对比赛站点零压力。
"""
import json
import os
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import cert_downloader as CD  # noqa: E402

BASE = json.loads((R / "程序" / "config.json").read_text(encoding="utf-8"))
OUT = []
OK = True


def sec(t):
    OUT.append("")
    OUT.append("=" * 70)
    OUT.append(t)
    OUT.append("=" * 70)


def build_cfg(kind):
    cfg = json.loads(json.dumps(BASE))
    s = cfg["sites"][cfg.get("default_site") or next(iter(cfg["sites"]))]
    if kind == "dns":
        s["home_url"] = "http://no-such-host-xyz-12345.invalid/q/zzzzzz"
        s["query_url"] = "http://no-such-host-xyz-12345.invalid/web/pubdata/query"
        s["title"] = "（模拟：域名不存在）"
    elif kind == "refused":
        s["home_url"] = "http://127.0.0.1:9/q/zzzzzz"
        s["query_url"] = "http://127.0.0.1:9/web/pubdata/query"
        s["title"] = "（模拟：连接被拒绝）"
    p = R / "开发维护" / ("_tmp_cfg_%s.json" % kind)
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


def run(kind, outdir_name, checks):
    """跑一次，返回 (日志全文, 输出目录)"""
    global OK
    cfg = build_cfg(kind)
    outdir = R / "开发维护" / outdir_name
    if outdir.exists():
        for f in outdir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
    logs = []
    try:
        CD.run_job(str(R / "开发维护" / "名单示例_演示用.xlsx"), str(outdir),
                   config_path=str(cfg), pace="fast", limit=2, log_sink=logs.append)
    except Exception as e:
        OUT.append("  运行异常：%s: %s" % (type(e).__name__, e))
    joined = "\n".join(logs)
    body = [l.split("] ", 1)[1] for l in logs if "] " in l]

    for nm, good in checks(joined, outdir):
        if not good:
            OK = False
        OUT.append("  %-54s %s" % (nm, "✅" if good else "❌"))

    OUT.append("")
    OUT.append("  ── 同事实际会看到的日志（前 24 行）──")
    for l in body[:24]:
        OUT.append("  " + l)
    OUT.append("  …（共 %d 行）" % len(body))
    return joined


def common_checks(joined, outdir):
    return [
        ("日志里出现【查询出错】诊断", "【查询出错】" in joined),
        ("给出了「怎么回事」", "怎么回事" in joined),
        ("给出了「怎么办」", "怎么办" in joined),
        ("至少 3 条可选方案", all(m in joined for m in ("①", "②", "③"))),
        ("结尾给出处理建议", "本次运行遇到的问题" in joined or "任务提前停止" in joined),
        ("生成了故障报告", len(list(outdir.glob("故障诊断_*.txt"))) == 1),
        ("生成了运行日志", len(list(outdir.glob("运行日志_*.txt"))) == 1),
    ]


# ==================================================================
# A. 无代理环境（临时摘掉代理环境变量，模拟同事电脑的常态）
# ==================================================================
saved = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
for k in list(saved):
    os.environ.pop(k, None)

sec("A1. 无代理环境 · 域名不存在（模拟「网站已关闭 / 网址写错 / 断网」）")


def chk_dns(joined, outdir):
    return common_checks(joined, outdir) + [
        ("判定为「连不上这个网站」", "连不上这个网站" in joined),
        ("未误报为「被限流封 IP」", "封 IP" not in joined),
        ("未误报为「代理问题」", "被代理/VPN 拦住" not in joined),
        ("建议里含「先用浏览器打开网址确认」", "用浏览器手动打开" in joined),
    ]


run("dns", "诊断测试_无代理_dns", chk_dns)

sec("A2. 无代理环境 · 连接被拒绝（模拟「被网站限流/拉黑」）")


def chk_blocked(joined, outdir):
    return common_checks(joined, outdir) + [
        ("判定为「被网站限流」", "封 IP" in joined),
        ("建议里含「手机热点」（最快方案）", "热点" in joined),
        ("建议里含「等 30 分钟」", "30 分钟" in joined),
        ("提醒了「别反复硬试」", "不要连着反复点" in joined or "千万不要连着反复" in joined),
    ]


run("refused", "诊断测试_无代理_refused", chk_blocked)

# 恢复代理环境变量
os.environ.update(saved)

# ==================================================================
# B. 有代理环境（正是这台机器的真实情况）
# ==================================================================
sec("B1. 有代理环境 · 假域名 → 应判定为「代理问题」，不能说是网站崩了")
OUT.append("  本机代理：%s" % (CD.proxy_in_env() or "(无)"))


def chk_proxy(joined, outdir):
    if not CD.proxy_in_env():
        # 本机压根没配代理，这段就无从验证。跳过 == 不是失败，
        # 免得在没挂代理的电脑上跑出一个"假故障"，反过来误导人。
        OUT.append("  （本机没有配置代理，本段跳过——不影响 A 段结论）")
        return []
    return common_checks(joined, outdir) + [
        ("判定为「代理/VPN 拦住」", "被代理/VPN 拦住" in joined),
        ("建议里含「退出 VPN/加速器」或「关掉系统代理」",
         "VPN" in joined and "代理" in joined),
        ("建议里含「手机热点」绕开方案", "热点" in joined),
        ("给了退路：关掉代理还不行就按网站故障处理",
         "网站服务器故障" in joined or "网站确实挂了" in joined),
    ]


run("dns", "诊断测试_有代理", chk_proxy)

sec("B2. 反向检查：有代理时，日志开头应主动提醒代理这件事")


def chk_notice(joined, outdir):
    px = CD.proxy_in_env()
    if not px:
        OUT.append("  （本机没有配置代理，本段跳过）")
        return []
    # 注意措辞是跟着实现走的：这里是"本机走了网络代理"，不是"配置了网络代理"。
    # 断言写死旧措辞，改一次文案就会报一次假失败。
    return [
        ("日志开头提示了本机配了代理", "本机走了网络代理" in joined),
        ("提示里带上了代理地址（一眼看出卡在哪一层）", px in joined),
        ("提示了后续怎么排查（优先查代理/VPN）", "优先检查代理/VPN" in joined),
    ]


run("dns", "诊断测试_有代理2", chk_notice)

sec("结论")
OUT.append("  真实故障模拟：%s" % ("全部通过 ✅" if OK else "存在问题 ❌"))

txt = "\n".join(OUT)
Path(__file__).with_name("live_diag_test.txt").write_text(txt, encoding="utf-8")
sys.exit(0 if OK else 1)
