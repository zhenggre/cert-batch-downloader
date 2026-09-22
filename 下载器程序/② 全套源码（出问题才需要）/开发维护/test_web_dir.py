# -*- coding: utf-8 -*-
"""网页服务端到端验证：保存位置自定义 + 安全边界 + 换比赛。

刻意不碰真网站的查询接口：start 只用"非法路径"，在目录校验阶段就被拦下，
不会启动任务。唯一一次真实网络访问是 ⑧ 段的"识别新比赛"——
那一步走的是比赛网站的表单页和查询接口，属于功能本身，避不开；
默认跳过，需显式设 CERTDL_TEST_URL 为真实比赛查询页网址才跑。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))
import requests                       # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK] %s" % name)
    else:
        fail += 1
        print("  [!!] %s   %s" % (name, extra))


addr = R / "程序" / "界面地址.txt"
url_file = R / "程序" / "打开界面.url"
settings_file = R / "程序" / "用户设置.json"
runfile = R / "程序" / "运行中.json"
# 这些都是"程序跑起来才会生成"的，先删干净，免得上一次的残留让测试假通过
for f in (addr, url_file, settings_file, runfile):
    if f.exists():
        f.unlink()

env = dict(os.environ, CERTDL_NO_BROWSER="1")
proc = subprocess.Popen([sys.executable, str(R / "程序" / "web_app.py")], cwd=str(R), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        encoding="utf-8", errors="replace")

base = token = ""
try:
    for _ in range(80):
        time.sleep(0.3)
        if addr.exists():
            t = addr.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=(\w+)", t)
            if m:
                base, token = "http://127.0.0.1:%s" % m.group(1), m.group(2)
                break
    check("服务启动并写出界面地址", bool(base), addr.read_text(encoding='utf-8')[:80] if addr.exists() else "无")

    if not base:
        raise SystemExit(1)
    Q = {"token": token}

    print()
    print("=" * 66)
    print("① 安全边界：没有令牌应被挡")
    print("=" * 66)
    r = requests.get(base + "/api/sites", timeout=10)
    check("无令牌 → 403", r.status_code == 403, str(r.status_code))
    r = requests.get(base + "/api/browse", timeout=10)
    check("无令牌访问 browse → 403", r.status_code == 403, str(r.status_code))

    print()
    print("=" * 66)
    print("② 保存位置：默认值 + 盘符枚举")
    print("=" * 66)
    j = requests.get(base + "/api/sites", params=Q, timeout=10).json()
    check("sites 返回 default_out", bool(j.get("default_out")), str(j.get("default_out")))
    print("     默认保存位置：%s" % j.get("default_out"))

    j = requests.get(base + "/api/browse", params=Q, timeout=10).json()
    check("browse 返回 ok", j.get("ok") is True)
    drives = [d["name"] for d in j.get("drives", [])]
    print("     识别到盘符：%s" % drives)
    check("至少识别到 1 个盘符", len(drives) >= 1)
    check("C 盘在列表里", "C:" in drives, str(drives))
    check("返回当前目录路径", bool(j.get("path")), str(j.get("path")))

    j = requests.get(base + "/api/browse", params=dict(Q, path="D:\\"), timeout=10).json()
    check("能列出 D 盘根目录", j.get("ok") and j.get("path", "").upper().startswith("D:"),
          str(j.get("path")))
    names = [d["name"] for d in j.get("dirs", [])]
    print("     D:\\ 下的文件夹（前 8 个）：%s" % names[:8])
    check("能列出台式机真实存在的文件夹", len(names) >= 1, str(names))

    j2 = requests.get(base + "/api/browse", params=dict(Q, path="Z:\\根本不存在"),
                      timeout=10).json()
    check("给一个不存在的路径 → 优雅退回，不报错",
          j2.get("ok") is True and bool(j2.get("path")), str(j2)[:110])

    print()
    print("=" * 66)
    print("③ 新建文件夹")
    print("=" * 66)
    tmp_parent = Path(os.environ.get("TEMP", ".")) / "certdl_web_test"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    j = requests.post(base + "/api/mkdir", params=Q, timeout=10,
                      json={"parent": str(tmp_parent), "name": "证书"}).json()
    check("新建成功", j.get("ok") is True, str(j)[:110])
    newp = Path(j.get("path") or "_")
    check("文件夹真的建出来了", newp.is_dir(), str(newp))

    j = requests.post(base + "/api/mkdir", params=Q, timeout=10,
                      json={"parent": str(tmp_parent), "name": "非法/名字"}).json()
    check("非法名字被拒绝且给出中文原因",
          j.get("ok") is False and "不能" in (j.get("msg") or ""), str(j)[:110])

    j = requests.post(base + "/api/mkdir", params=Q, timeout=10,
                      json={"parent": str(tmp_parent), "name": "  "}).json()
    check("空名字被拒绝", j.get("ok") is False, str(j)[:110])

    print()
    print("=" * 66)
    print("④ 非法保存路径：点「开始」时就该拦住，而不是跑一半崩")
    print("=" * 66)
    import base64
    src_list = R / "开发维护" / "名单示例_演示用.xlsx"
    b64 = base64.b64encode(src_list.read_bytes()).decode()
    j = requests.post(base + "/api/start", params=Q, timeout=20, json={
        "filename": "名单示例_演示用.xlsx", "content_b64": b64,
        "out": "Z:\\没有这个盘\\证书", "site": "site_example",
        "province": "", "sample": 0}).json()
    check("拒绝并返回 ok=False", j.get("ok") is False, str(j)[:130])
    check("提示里有「保存位置」字样（说人话）", "保存位置" in (j.get("msg") or ""),
          (j.get("msg") or "")[:150])
    check("提示里给了下一步建议（点「浏览…」）", "浏览" in (j.get("msg") or ""))
    print("     实际提示：%s" % (j.get("msg") or "").replace("\n", " | ")[:170])

    print()
    print("=" * 66)
    print("⑤ 浏览器没弹开时的兜底入口")
    print("=" * 66)
    check("生成了「打开界面.url」", url_file.exists())
    if url_file.exists():
        c = url_file.read_text(encoding="utf-8", errors="replace")
        check("这是一个 Windows 快捷方式（InternetShortcut）",
              "[InternetShortcut]" in c and "URL=" in c, c[:80])
        check("里面带的是本次运行的真实地址", base in c, c[:120])
        check("提示语指向双击该文件",
              "打开界面.url" in addr.read_text(encoding="utf-8", errors="replace"))

    print()
    print("=" * 66)
    print("⑥ 记住上次选择的位置")
    print("=" * 66)
    settings = settings_file
    # 上面那次 start 被拒（路径非法），压根没成功过 —— 就不该留下"上次位置"。
    # 旧版测试在这里断言"文件该存在"，是把"正确拒绝"误判成了失败。
    check("被拒绝的位置不会被记下来", not settings.exists(),
          ("不该存在，却写进了：%s"
           % settings.read_text(encoding="utf-8")[:80]) if settings.exists() else "")

    # 真正验证"记住位置"这个能力：直接调网页端的读写函数走一轮。
    # 不经过 start，就不会真去查网站。
    import web_app as wa
    want = str(tmp_parent / "证书")
    wa.save_settings(last_out=want, site="site_example")
    got = wa.load_settings()
    check("存进去的位置能原样读回来", got.get("last_out") == want, str(got))
    check("真正落成了磁盘上的 JSON 文件",
          settings.exists() and "last_out" in settings.read_text(encoding="utf-8"))
    check("后写的项不会把先写的项冲掉", got.get("site") == "site_example", str(got))

    # 这份是测试造的：交付包里不该预置（用户第一次点「开始」时该由程序自己建）
    settings.unlink()
    check("删掉后不报错、安静退回默认值", wa.load_settings() == {}, str(wa.load_settings()))

    print()
    print("=" * 66)
    print("⑦ 单实例守护 + 名单预检")
    print("=" * 66)
    r = requests.get(base + "/api/ping", timeout=10)
    check("ping 不带令牌也能访问（第二个实例得靠它认出前面那个）",
          r.status_code == 200, str(r.status_code))
    check("ping 自报是证书下载器", (r.json() or {}).get("app") == "certdl", str(r.json())[:90])

    # 真正有说服力的测法：服务跑在另一个进程里，本进程去抢锁应当抢不到。
    # 同进程调两次只能证明"变量是通的"，跨进程才证明锁真的落在内核上。
    check("跨进程：本进程再抢锁 → 认出服务已经占着",
          wa.claim_single_instance() is False)

    j = requests.post(base + "/api/peek", params=Q, timeout=60,
                      json={"filename": "名单示例_演示用.xlsx", "content_b64": b64,
                            "site": "site_example"}).json()
    check("拖进来就能报出人数", j.get("ok") and (j.get("total") or 0) > 0, str(j)[:140])
    print("     识别到 %s 人，前几个：%s" % (j.get("total"), j.get("sample")))
    check("回显了认出来的列名", bool((j.get("cols") or {}).get("name")), str(j.get("cols")))
    check("给了抽样姓名（同事靠这个确认是不是要的那份名单）",
          isinstance(j.get("sample"), list) and len(j.get("sample")) >= 1,
          str(j.get("sample")))

    bad_b64 = base64.b64encode(b"this is not a spreadsheet at all").decode()
    j2 = requests.post(base + "/api/peek", params=Q, timeout=60,
                       json={"filename": "假的.xlsx", "content_b64": bad_b64,
                             "site": "site_example"}).json()
    check("给一个不是表格的文件 → 明确说读不了，不是崩掉",
          j2.get("ok") is False and bool(j2.get("msg")), str(j2)[:140])
    check("提示是中文人话，不是 Python 原文（同事看不懂 zip file）",
          "zip" not in (j2.get("msg") or "").lower()
          and "另存为" in (j2.get("msg") or ""), (j2.get("msg") or "")[:160])
    print("     提示：%s" % (j2.get("msg") or "").replace("\n", " / ")[:150])

    print()
    print("=" * 66)
    print("⑧ 换比赛：贴网址自动识别 + 添加")
    print("=" * 66)
    # 这一段会真的写 config.json，所以先把原文件整份读进内存，跑完原样写回去。
    # 不这么做的话，跑一次测试就把同事电脑上的比赛列表改掉了。
    cfg_path = R / "程序" / "config.json"
    cfg_bak = R / "程序" / "config.json.bak"
    cfg_orig = cfg_path.read_bytes()
    had_bak = cfg_bak.exists()
    bak_orig = cfg_bak.read_bytes() if had_bak else None
    try:
        j = requests.post(base + "/api/probe", params=Q, timeout=60,
                          json={"url": ""}).json()
        check("空网址 → 明确提示，不是崩掉",
              j.get("ok") is False and bool(j.get("msg")), str(j)[:120])

        live_url = (os.environ.get("CERTDL_TEST_URL") or "").strip()
        if not live_url:
            print("  [--] 未设 CERTDL_TEST_URL，跳过「识别新比赛」"
                  "（该段需真实网址，且会写 config.json）")
            j = {"ok": False, "msg": "skipped: CERTDL_TEST_URL 未设"}
        else:
            j = requests.post(base + "/api/probe", params=Q, timeout=90,
                              json={"url": live_url}).json()
            check("识别真实网址成功", j.get("ok") is True, str(j)[:170])
        if j.get("ok"):
            fields = {f["cn"]: f["fid"] for f in (j.get("fields") or [])}
            check("回显了字段对应关系（同事要能核对一眼）",
                  fields.get("姓名") == "F1" and fields.get("证件号") == "F999",
                  str(fields))
            check("回显了比赛名称", bool(j.get("title")), str(j.get("title"))[:60])
            sid = j.get("sid")

            j2 = requests.post(base + "/api/site_add", params=Q, timeout=30,
                               json={"sid": sid}).json()
            check("添加成功", j2.get("ok") is True, str(j2)[:170])
            check("返回的站点列表里带上了新比赛",
                  len(j2.get("sites") or []) >= 2,
                  str(j2.get("sites"))[:140])

            new_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            sites = new_cfg.get("sites") or {}
            old_keys = set((json.loads(cfg_orig.decode("utf-8")).get("sites") or {}).keys())
            check("config.json 里确实多了新站点", len(sites) >= 2, list(sites))
            check("老站点还在（没被覆盖掉）", bool(old_keys) and old_keys <= set(sites),
                  list(sites))
            check("设成了当前使用的比赛",
                  str(new_cfg.get("default_site", "")).startswith("site_"),
                  new_cfg.get("default_site"))
            check("写之前留了备份 config.json.bak（改坏了能捞回来）", cfg_bak.exists())

            j3 = requests.post(base + "/api/site_add", params=Q, timeout=30,
                               json={"sid": sid}).json()
            check("同一个识别结果不能重复添加（防手抖点两下）",
                  j3.get("ok") is False, str(j3)[:120])

        j4 = requests.post(base + "/api/site_add", params=Q, timeout=30,
                           json={"sid": "deadbeef"}).json()
        check("乱传 sid → 拒绝，不会写坏配置", j4.get("ok") is False, str(j4)[:120])
    finally:
        cfg_path.write_bytes(cfg_orig)
        if had_bak:
            cfg_bak.write_bytes(bak_orig)
        elif cfg_bak.exists():
            cfg_bak.unlink()
        print("     （config.json 已还原成测试前的样子）")

    print()
    print("=" * 66)
    print("⑨ 干净的退出")
    print("=" * 66)
    j = requests.post(base + "/api/quit", params=Q, timeout=10).json()
    check("quit 接口正常返回", j.get("ok") is True)
    time.sleep(1.5)
    check("退出时清掉运行记录（免得下次被误判成「已经在运行」）", not runfile.exists())

finally:
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.terminate()
    try:
        out = proc.stdout.read() if proc.stdout else ""
    except Exception:
        out = ""
    if out:
        print()
        print("----- 服务端控制台输出 -----")
        print(out[:1800])

print()
print("=" * 66)
print("结果：%d 项通过，%d 项失败" % (ok, fail))
print("=" * 66)
sys.exit(1 if fail else 0)
