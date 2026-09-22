# -*- coding: utf-8 -*-
"""故障处理链路专项测试：分类准确性 + 报告落盘 + 不刷屏

这个测试不碰比赛网站（除了最后一个联网小样本），跑起来很安全。
"""
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

import requests  # noqa: E402
import cert_downloader as CD  # noqa: E402
import error_help as EH  # noqa: E402

out = []
ok = True


def sec(t):
    out.append("")
    out.append("=" * 70)
    out.append(t)
    out.append("=" * 70)


def chk(name, got, want):
    global ok
    good = got == want
    if not good:
        ok = False
    out.append("  %-58s %s" % ("%s → %s" % (name, got), "✅" if good else "❌ 期望 " + str(want)))


# ------------------------------------------------------------------
sec("1. 网络异常 → 故障类型映射（映射错了会给同事完全没用的建议）")
cases = [
    ("连接超时", requests.exceptions.ConnectTimeout("connect timeout"), "timeout"),
    ("读取超时", requests.exceptions.ReadTimeout("read timed out"), "timeout"),
    ("域名解析失败",
     requests.exceptions.ConnectionError(
         "HTTPSConnectionPool(host='biaodan100.com', port=443): Max retries exceeded "
         "with url: /q/zzzzzz (Caused by NameResolutionError(\"Failed to resolve "
         "'biaodan100.com' ([Errno 11001] getaddrinfo failed)\"))"),
     "dns"),
    ("目标主机积极拒绝",
     requests.exceptions.ConnectionError(
         "[WinError 10061] 由于目标计算机积极拒绝，无法连接。"), "blocked"),
    ("连接被强制重置",
     requests.exceptions.ConnectionError(
         "[WinError 10054] 远程主机强迫关闭了一个现有的连接。"), "blocked"),
    ("证书错误（不是封锁，别误报）",
     requests.exceptions.SSLError("certificate verify failed"), "net"),
    ("其它未知异常", ValueError("说不清的问题"), "net"),
]
for nm, exc, want in cases:
    chk(nm, CD.classify_exc(exc), want)

# ------------------------------------------------------------------
sec("2. 下载失败原因 → 故障类型")
dcases = [
    ("服务端生成中", "pdf_stuck"),
    ("等待 120s 仍未生成完成（服务端生成中）", "pdf_stuck"),
    ("既非 PDF 也非 init，前 20 字节=b'<html>'", "pdf_weird"),
    ("下载异常 ConnectionError: [WinError 10054]", "blocked"),
    ("下载异常 ReadTimeout: HTTPConnectionPool", "timeout"),
    ("", "unknown"),
]
for err, want in dcases:
    chk(err[:26] if err else "(空字符串)", CD.classify_download_err(err), want)

# ------------------------------------------------------------------
sec("3. 节流器对「被限流」的反应（要立刻跳到较大退避档）")
th = CD.Throttler(base=0.01, jitter=0, every=0)
th.note_err("blocked")
chk("被限流一次后的连错计数（应直接跳到 3）", th.streak, 3)
th2 = CD.Throttler(base=0.01, jitter=0, every=0)
th2.note_err("net")
chk("普通网络错误后的计数（应只 +1）", th2.streak, 1)
th3 = CD.Throttler(base=0.01, jitter=0, every=0, abort_streak=3)
for _ in range(3):
    th3.note_err("blocked")
chk("连错达上限是否触发熔断", th3.should_abort, True)

# ------------------------------------------------------------------
sec("4. 名单文件不存在 → 应产出故障报告（这一步不联网）")
testdir = R / "开发维护" / "诊断测试"
if testdir.exists():
    for f in testdir.iterdir():
        f.unlink()
logs = []
try:
    CD.run_job(str(R / "开发维护" / "__根本没有这个文件__.xlsx"), str(testdir),
               log_sink=logs.append)
    chk("应抛出异常", "没抛", "FileNotFoundError")
except FileNotFoundError:
    chk("抛出 FileNotFoundError", True, True)
except Exception as e:
    chk("抛出了预期外的异常", type(e).__name__, "FileNotFoundError")

reports = list(testdir.glob("故障诊断_*.txt")) if testdir.exists() else []
chk("生成了故障报告文件", len(reports) == 1, True)
if reports:
    content = reports[0].read_text(encoding="utf-8")
    chk("报告里含「找不到名单文件」", "找不到名单文件" in content, True)
    chk("报告里含解决方案（①②③）", "①" in content and "②" in content, True)
    chk("报告里含完整日志", "站点：" in content, True)

joined = "\n".join(logs)
chk("日志里给出了自救提示", "怎么办" in joined, True)
chk("日志里提示了报告文件名", "故障诊断_" in joined, True)

# ------------------------------------------------------------------
sec("5. 同类故障不刷屏（连续 5 次同一错误只详细说一次）")
seen, lines = set(), []


def diag(kind, detail="", tag="故障处理"):
    k = kind or "unknown"
    if k in seen:
        lines.append("BRIEF:" + EH.brief(k))
        return
    seen.add(k)
    for l in EH.explain(k, detail, tag=tag):
        lines.append(l)


for i in range(5):
    diag("blocked", "HTTP 403（疑似被限流）", tag="查询出错")
full = [l for l in lines if l.strip().startswith("【")]
brief = [l for l in lines if l.startswith("BRIEF:")]
chk("完整说明只出现 1 次", len(full), 1)
chk("其余 4 次只给一行提示", len(brief), 4)
chk("日志总行数可控（< 30 行）", len(lines) < 30, True)
out.append("     实际行数：%d（5 次报错）" % len(lines))

# ------------------------------------------------------------------
sec("6. 输出编码边界：系统编码是 GBK 时，特殊符号不能让程序崩")
import io  # noqa: E402

real_stdout = sys.stdout
buf = io.BytesIO()
wrapper = io.TextIOWrapper(buf, encoding="gbk", errors="strict", write_through=True)
sys.stdout = wrapper
crashed = None
try:
    # ⚠ ✓ 都编不进 GBK，早先会让 print 抛 UnicodeEncodeError，整个作业跟着挂掉
    CD.log("★ 测试特殊符号：⚠ 警告 ✓ 通过 ※ 注意 ①②③")
    CD.log("普通中文日志也应正常")
except Exception as e:
    crashed = "%s: %s" % (type(e).__name__, e)
finally:
    try:
        wrapper.flush()
    except Exception:
        pass
    sys.stdout = real_stdout

chk("GBK 控制台下写日志不抛异常", crashed, None)
got = buf.getvalue().decode("gbk", "replace")
chk("日志内容确实写出去了", len(got.strip()) > 0, True)
out.append("     实际写出：%s" % got.strip().replace("\n", " | ")[:90])

# ------------------------------------------------------------------
sec("7. 结论")
out.append("  故障处理链路：%s" % ("全部通过 ✅" if ok else "存在问题 ❌"))

txt = "\n".join(out)
Path(__file__).with_name("fault_flow_test.txt").write_text(txt, encoding="utf-8")
sys.exit(0 if ok else 1)
