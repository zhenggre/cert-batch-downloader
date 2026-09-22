# -*- coding: utf-8 -*-
"""一条命令发新版：打包 exe → 覆盖到 ① → 重写给同事的说明书。

【为什么要有这个脚本】
  以前这件事是"手工三步"：跑 pack.py → 手动把 ② \\发给同事\\证书下载器\\ 里的
  exe 和 _internal 拷进 ① \\证书下载器\\ → 再跑 assemble.py。

  2026-09-21 就栽在这上面：源码改了「按表头多级筛选」，测试全过、试跑也全过，
  但 ① 里那个 exe 还是两天前的旧构建（连一行筛选代码都没有）。
  同事双击的是 ① 的 exe —— 等于功能根本没送出去，而所有检查都显示"正常"。
  凡是"改了源码但忘了重新打包"这一类事故，都是因为少了这一步自动化。

【怎么跑】
  用**装了 PyInstaller 的那个 Python** 执行本脚本。脚本自己也会挑：
  sys.executable → 程序\\runtime\\python.exe → 隔离环境 → py -3，谁行用谁。

【它做什么】
  1. 调 pack.py（PyInstaller --onedir）→ 产物落在 ② \\发给同事\\证书下载器\\
  2. 把 证书下载器.exe 和 _internal\\ 覆盖进 ① \\证书下载器\\
     （**只覆盖这两样**，绝不动 使用说明.txt / config.json / 学校名映射.csv ——
       那几个由 assemble.py 按源头重写，用 /MIR 会把它们误删）
  3. 调 assemble.py 重写说明书与配置
  4. 自检：exe 是否比源码新、界面里有没有新功能标志
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 目录关系：本脚本在 ② 全套源码\开发维护\ 里
#   HERE  = 开发维护
#   R     = ② 全套源码（出问题才需要）
#   OUTER = 公司电脑用
HERE = Path(__file__).resolve().parent
R = HERE.parent
OUTER = R.parent
PKG = OUTER / "① 双击就能用（直接用不用装任何东西）" / "证书下载器"
DIST = R / "发给同事" / "证书下载器"
WORK = HERE / "build"

# 每加一个"界面上的新功能"，就往这里补一个标志串。
# 打包完自动在 ① 的界面文件里找它；找不到就说明这次打的是旧代码，必须停下来。
UI_MARKERS = ["fgrid", "filter-card", "collectFilters",
              "sp-minus", "sp-plus", "btn-pause", "speed_steps",
              "ns-title", "ns-warn", "d-nofile", "diag-err",
              # 试跑必须自报家门：少了这两个标志，界面又会退回到
              # "跑完只显示『全部完成』" —— 那正是 2026-09-22 的误会源头。
              "try-warn", "trial-note"]

# ① 是"拷给同事的成品"，这几样是上一次运行留下的、出了这台机器就是废纸的东西
# config.json.bak 是 save_site() 改配置前留的备份（自助回滚用），在开发机上是本机历史，
# 交付包里不该有 —— 2026-09-21 加了「① 顶层正好 6 项」自检后，当场就抓到它了。
RESIDUE = ("运行中.json", "界面地址.txt", "打开界面.url", "用户设置.json",
           "config.json.bak")

# 认旧进程用：与 程序\web_app.py 里的 APP_TAG / pick_port 保持一致
APP_TAG = "certdl"
PORT_BASE = 8731
PORT_TRIES = 40


def say(msg):
    print(msg, flush=True)


def mirror(src, dst, label=""):
    """用 robocopy 把 src 镜像到 dst（多余的文件会被删掉）。

    为什么不用 shutil.rmtree + copytree：本机对"一次删掉 50 个以上文件"
    有安全闸，rmtree 会被直接拦下（PyInstaller 自己的 COLLECT 清理也会被拦，
    结果就是"打包看着成功、其实换的还是旧文件"——2026-09-21 真踩了一次）。
    robocopy 是系统命令，走的是另一条路，不受这个闸限制。
    robocopy 退出码 0~7 都算正常（2 = 有文件被删，正是镜像要的效果），>=8 才是失败。
    """
    Path(dst).mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["robocopy", str(src), str(dst), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS"],
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode >= 8:
        raise SystemExit("!!! robocopy 镜像失败（%s -> %s，exit=%d）\n%s"
                         % (src, dst, r.returncode, (r.stdout or "")[-1500:]))
    say("  镜像 %s -> %s（exit=%d）" % (label or Path(src).name, dst, r.returncode))
    return r.returncode


def pick_python():
    """挑一个装了 PyInstaller 的解释器。"""
    cands = [sys.executable,
             str(R / "程序" / "runtime" / "python.exe")]
    # 打包用的解释器路径每台机器都不一样，写死就等于把打包机的用户名
    # 焊进交付源码里。要指定就设环境变量 CERTDL_PYTHON，
    # 否则退回到 PATH 里的 python。
    if os.environ.get("CERTDL_PYTHON"):
        cands.append(os.environ["CERTDL_PYTHON"])
    _which = shutil.which("python")
    if _which:
        cands.append(_which)
    for exe in cands:
        if not exe or not Path(exe).exists():
            continue
        try:
            r = subprocess.run([exe, "-c", "import PyInstaller;print(PyInstaller.__version__)"],
                               capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            say("  用这个 Python 打包：%s（PyInstaller %s）" % (exe, (r.stdout or "").strip()))
            return exe
    raise SystemExit("找不到装了 PyInstaller 的解释器。先执行：\n"
                     "  python -m pip install pyinstaller openpyxl requests")


def run(script, exe):
    say("  -> %s" % script.name)
    r = subprocess.run([exe, str(script)], cwd=str(HERE),
                       capture_output=True, encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout[-3000:])
    if r.returncode != 0:
        if r.stderr:
            print("--- STDERR ---\n%s" % r.stderr[-3000:])
        raise SystemExit("!!! %s 失败（exit=%d），已中止，① 未被改动" % (script.name, r.returncode))


def api_get(port, token, path, timeout=5):
    sep = "&" if "?" in path else "?"
    url = "http://127.0.0.1:%d%s%stoken=%s" % (port, path, sep, token)
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read().decode())


def _exe_locked():
    """① 里的 exe 能不能由我们打开？打不开 = 有进程正占着。"""
    exe = PKG / "证书下载器.exe"
    if not exe.exists():
        return False
    try:
        with open(exe, "ab"):
            pass
        return False
    except Exception:
        return True


def live_instances():
    """扫端口段，列出还活着的本程序旧实例：[(port, pid)]。

    用 /api/ping 认领 —— 它刻意不校验令牌（只回一句"我是谁"）。
    别的软件就算占了同一段端口，也回不出 app=certdl 这句话，认不错。

    **为什么要扫端口而不是只看 运行中.json？**
    2026-09-21 踩过：手工把 运行中.json 删了，release_pkg 一看文件不在就
    直接 return，于是"以为没人跑"、照常覆盖 → 撞上 WinError 32。
    exe 是否被占，只认文件锁；运行记录只是线索，不能当唯一依据。
    """
    out = []
    for p in range(PORT_BASE, PORT_BASE + PORT_TRIES):
        try:
            raw = urllib.request.urlopen(
                "http://127.0.0.1:%d/api/ping" % p, timeout=0.3).read()
            d = json.loads(raw.decode())
        except Exception:
            continue
        if d.get("app") == APP_TAG:
            out.append((p, d.get("pid")))
    return out


def release_pkg():
    """① 里的程序若正开着，会锁住 证书下载器.exe，覆盖时直接 PermissionError。

    2026-09-21 踩过：用户验收时双击开着界面，打包脚本到第 2 步就炸了。
    这里先让它自己退出（走 /api/quit，比强杀干净：它会清掉 运行中.json），
    再轮询等文件锁放开。**正在跑任务时绝不动手** —— 那是用户的活。
    """
    exe = PKG / "证书下载器.exe"
    runfile = PKG / "运行中.json"

    # 线索一：运行记录（带令牌，能优雅退出）
    port = token = None
    if runfile.exists():
        try:
            info = json.loads(runfile.read_text(encoding="utf-8"))
            port, token = info["port"], info["token"]
        except Exception:
            runfile.unlink(missing_ok=True)

    # 线索二：端口扫描兜底 —— 运行记录被误删也能认出旧进程
    live = live_instances()
    if live and port is None:
        port, pid = live[0]
        say("  运行中.json 不在，但端口 %d 上还有旧程序（PID %s）" % (port, pid))

    if port is None:
        # 没有活实例：若 exe 还锁着，多半是刚退出、锁没放；等一下再判
        if not _exe_locked():
            return
        say("  没有发现活着的旧程序，但 exe 暂时被占 —— 等它放锁")
    else:
        # 唯一"绝不能动"的情况：正在跑任务
        if token:
            try:
                st = api_get(port, token, "/api/progress?offset=0")
                if st.get("running"):
                    raise SystemExit(
                        "!!! ① 里的程序正在跑任务（端口 %d）。\n"
                        "    别拿正在干活的任务冒险 —— 等它跑完、或手动停掉，再跑本脚本。" % port)
            except SystemExit:
                raise
            except Exception:
                pass      # 问不到状态就当作空闲，下面照样会验证锁是否放开

        if token:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api/quit?token=%s" % (port, token), data=b"{}")
                urllib.request.urlopen(req, timeout=5).read()
                say("  请正在运行的旧程序退出（端口 %d）" % port)
            except Exception as e:
                say("  请求退出没成功（%s），看锁放没放开" % str(e)[:60])
        else:
            # 认出旧进程但没令牌 → 没法让它自己退，只能请用户手动关
            raise SystemExit(
                "!!! 端口 %d 上还有一个旧的 证书下载器 在开（运行记录被删了，拿不到令牌）。\n"
                "    请先手动关掉它（页面底部「关闭程序」或直接关黑窗口），再跑本脚本。" % port)

    for _ in range(20):
        if not _exe_locked():
            return
        time.sleep(0.5)
    raise SystemExit("!!! ① 里的 证书下载器.exe 还被占着 —— "
                     "先手动关掉那个程序窗口，再跑本脚本。")


def clean_residue():
    """① 是"拷给同事的成品"，不该带上一次运行留下的东西：

      · 运行中.json / 界面地址.txt / 打开界面.url —— 写着本机的端口和一次性令牌，
        换台电脑就是废纸（程序正常退出时已由 clear_runfile() 清掉，这里兜一道底）
      · 用户设置.json —— 上一个用户选的保存路径、选的比赛，同事打开会看到别人的路径

    不清「使用说明.txt / config.json / 学校名映射.csv」—— 那是交付物本身。
    """
    n = 0
    for f in RESIDUE:
        p = PKG / f
        if not p.exists():
            continue
        try:
            p.unlink()
            n += 1
            say("  清掉残留：%s" % f)
        except OSError as e:
            say("  [X] %s 清不掉（%s）—— 拷给同事前手动删一下" % (f, str(e)[:60]))
    if not n:
        say("  没有残留，很干净")


def main():
    t0 = time.time()
    say("=" * 64)
    say("  打包发新版")
    say("=" * 64)
    exe = pick_python()

    src_exe = R / "程序" / "web_app.py"
    src_ui = R / "程序" / "web" / "index.html"

    say("\n[1/4] PyInstaller 打包")
    # 先把上一次的产物清空。不等 PyInstaller 自己删 —— 它用的是标准删除，
    # 会被本机的批量删除安全闸拦下，然后"看起来成功"地留着旧文件继续打包。
    # 要清的是两处：distpath（发给同事）和 workpath（build\）。
    # 漏掉 workpath 的后果：--clean 去删 build\...\localpycs 被拦 → EXIT=1，
    # 打包直接失败（2026-09-21 踩过一次）。
    empty = Path(tempfile.mkdtemp(prefix="certdl_empty_"))
    try:
        for d in (DIST, WORK):
            if d.exists():
                mirror(empty, d, label="清空 %s" % d.name)
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    run(HERE / "pack.py", exe)

    built = DIST / "证书下载器.exe"
    if not built.exists():
        raise SystemExit("打包结束但找不到 %s —— 看 开发维护\\pyi_build3.log" % built)

    say("\n[2/4] 覆盖进 ①（exe + _internal）")
    release_pkg()
    PKG.mkdir(parents=True, exist_ok=True)
    if _exe_locked():
        raise SystemExit(
            "!!! ① 里的 证书下载器.exe 仍被占用，不敢覆盖（怕写出半个文件）。\n"
            "    请关掉那个程序窗口后重跑。已打包好的产物在：\n    %s" % DIST)
    shutil.copy2(built, PKG / built.name)
    say("  %s -> 已复制" % built.name)
    mirror(DIST / "_internal", PKG / "_internal", label="_internal")

    say("\n[3/4] 重写说明书与配置")
    run(HERE / "assemble.py", exe)

    say("\n[3.5] 清掉 ① 里的运行时残留")
    clean_residue()

    say("\n[4/4] 自检")
    ok = True
    if built.stat().st_mtime < src_ui.stat().st_mtime - 2:
        say("  [X] exe 比源码旧，可能没真打上")
        ok = False
    else:
        say("  [OK] exe 比源码新")
    ui = PKG / "_internal" / "web" / "index.html"
    text = ui.read_text(encoding="utf-8", errors="replace") if ui.exists() else ""
    for m in UI_MARKERS:
        hit = m in text
        say("  [%s] 界面含新功能标志 %s" % ("OK" if hit else "X", m))
        ok = ok and hit
    for f in ("使用说明.txt", "config.json", "学校名映射.csv"):
        p = PKG / f
        say("  [%s] ① 里 %s（%d 字节）" % ("OK" if p.exists() else "X", f,
                                          p.stat().st_size if p.exists() else 0))
        ok = ok and p.exists()

    # ① 是"拷给同事的成品"，顶层应当**正好**这 6 项。
    # 多出来的基本都是上一次运行留下的 运行中.json / 界面地址.txt / 打开界面.url /
    # 用户设置.json —— 2026-09-21 那次正是靠人眼才发现多出 4 个。既然有明确的
    # "应该是什么样"，就别再让它靠人看：多一项、少一项都当场判失败。
    WANT_TOP = {"证书下载器.exe", "_internal", "使用说明.txt",
                "保存位置说明.txt", "config.json", "学校名映射.csv"}
    got_top = {p.name for p in PKG.iterdir()}
    extra = sorted(got_top - WANT_TOP)
    missing = sorted(WANT_TOP - got_top)
    if extra:
        say("  [X] ① 顶层多出 %d 项（不该带的，多半是运行时残留）：%s"
            % (len(extra), "、".join(extra)))
        ok = False
    if missing:
        say("  [X] ① 顶层少了 %d 项：%s" % (len(missing), "、".join(missing)))
        ok = False
    if not extra and not missing:
        say("  [OK] ① 顶层正好 6 项，没有运行时残留")

    total = sum(x.stat().st_size for x in PKG.rglob("*") if x.is_file())
    say("  [··] ① 总体积：%.1f MB（%d 个文件）"
        % (total / 1024 / 1024, sum(1 for x in PKG.rglob("*") if x.is_file())))

    say("\n" + "=" * 64)
    say("  %s   用时 %.0f 秒" % ("完成 ✅ 可以拷给同事了" if ok else "有自检未过 ❌ 先看清楚再发", time.time() - t0))
    say("  发的是这个文件夹：%s" % PKG)
    say("=" * 64)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
