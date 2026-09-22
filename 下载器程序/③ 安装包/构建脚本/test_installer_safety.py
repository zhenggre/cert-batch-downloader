# -*- coding: utf-8 -*-
"""安装器安全检查 · 自动化测试

覆盖 2026-09-22 事故的全部 P0，以及路径守卫的边界：

  P0-1  卸载递归删除用户选的整个目录（旧版 `rmdir /s /q "%DIR%"`）
  P0-2  安装时静默 rmtree 已存在的同名目录（旧版对 `%DIR%\\源码` 直接 rmtree）
  P0-3  --dir= 静默模式绕过全部校验

只在本脚本自己的沙盒目录里操作：不碰真实桌面、不碰项目文件、不动注册表。
所有删除都走回收站，误删能从回收站捞回来。

用法：  python test_installer_safety.py
退出码：0 全部通过，1 有用例失败
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import installer  # noqa: E402
from fix_ps1_bom import ensure_bom  # noqa: E402

RESULTS = []


def _run(argv, **kw):
    """带超时的 subprocess.run：卡住就自己断，不把整个测试挂死。"""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=240, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(argv, -1, str(e.stdout or ""), "TIMEOUT")


def run_py(*args):
    """跑 installer.py 的指定参数。

    env 每次动态构造：CERTDL_PAYLOAD_DIR 是在测试过程中才设置的，用模块级
    快照会漏掉它。带 PYTHONUTF8 是因为 Python 在 Windows 上默认按 ANSI 输出，
    中文提示经管道回传会变成乱码。
    """
    return _run([sys.executable, "-u", str(HERE / "installer.py"), *args],
                env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))


def run_ps(*args):
    return _run([installer.powershell_exe(), "-NoProfile",
                 "-ExecutionPolicy", "Bypass", *args])


def check_ps1_syntax(path):
    """用 PowerShell 自己的解析器检查脚本语法。

    exit code 才是判据 —— PS 5.1 经管道回传的中文会乱码，别去解析文本。
    这一步必须放在最前面：脚本有语法错的话，后面所有用例都会以
    「莫名其妙跑不完」的形式失败，白查半天。
    """
    script = (
        "$e=$null;"
        "$null=[System.Management.Automation.Language.Parser]::ParseFile("
        "'%s',[ref]$null,[ref]$e);"
        "if($e){exit 1}else{exit 0}" % str(path).replace("'", "''"))
    r = subprocess.run([installer.powershell_exe(), "-NoProfile", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return r.returncode == 0


def ps_errors(completed):
    """从 PowerShell 的输出里挑出错误标记。

    为什么要专门盯这个：$ErrorActionPreference = 'Continue' 会把「参数写错」
    「命令不存在」这类错误静静吃掉，表达式求值成 $null，于是
    `-not (Test-Path ...)` 恒为真 —— 脚本会一路「正常」跑完，还打出一份
    看着没问题的报告，而实际上什么都没删。不盯这个的话，这类 bug 只能靠
    人肉在报告里发现异常。
    """
    txt = ((completed.stdout or "") + "\n" + (completed.stderr or ""))
    marks = ("FullyQualifiedErrorId", "ParameterBindingException",
             "CommandNotFoundException", "PropertyNotFoundException",
             "MethodInvocationException")
    return [ln.strip() for ln in txt.splitlines() if any(m in ln for m in marks)]


def check(name, got, want, extra=""):
    ok = (got == want)
    RESULTS.append((ok, name, got, want, extra))
    mark = "PASS" if ok else "FAIL"
    line = "  [%s] %s" % (mark, name)
    if not ok:
        line += "\n         期望: %r\n         实际: %r" % (want, got)
    if extra:
        line += "\n         %s" % extra
    print(line)
    return ok


def head(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def known(label):
    return installer._known_folder(installer._KNOWN_FOLDERS[label])


# ==========================================================================
# 一、路径守卫
# ==========================================================================
def test_path_guard():
    head("一、路径守卫（白名单语义 + 危险路径黑名单）")

    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    desktop = known("桌面") or os.path.join(home, "Desktop")
    documents = known("文档") or os.path.join(home, "Documents")
    downloads = known("下载") or os.path.join(home, "Downloads")
    onedrive = known("OneDrive")
    sysroot = os.environ.get("SystemRoot", "C:\\Windows")

    print("  本机真实路径（Known Folder API 取得，不是猜的）：")
    print("    桌面 = %s" % desktop)
    print("    文档 = %s" % documents)
    print("    下载 = %s" % downloads)
    if onedrive:
        print("    OneDrive = %s" % onedrive)
    print()

    cases = [
        ("空字符串",                  "",                                   "forbid"),
        ("只有空格",                  "   ",                                "forbid"),
        ("相对路径",                  "tools\\证书下载器",                   "forbid"),
        ("盘根 C:\\",                 "C:\\",                               "forbid"),
        ("盘根 D:\\",                 "D:\\",                               "forbid"),
        ("桌面本身",                  desktop,                              "forbid"),
        ("文档本身",                  documents,                            "forbid"),
        ("下载本身",                  downloads,                            "forbid"),
        ("用户主目录本身",            home,                                 "forbid"),
        ("Windows 目录",              os.path.join(sysroot, ""),            "forbid"),
        ("System32",                  os.path.join(sysroot, "System32"),    "forbid"),
        ("Program Files 根",          os.environ.get("ProgramFiles", "C:\\Program Files"), "forbid"),
        (".. 绕回桌面",               os.path.join(desktop, "..", os.path.basename(desktop)), "forbid"),
        (".. 一路绕到盘根",           os.path.join(desktop, "..", "..", ".."), "forbid"),
        ("桌面下的专属子目录",        os.path.join(desktop, "证书下载器"),   "warn"),
        ("文档下的专属子目录",        os.path.join(documents, "证书下载器"), "warn"),
        ("下载下的专属子目录",        os.path.join(downloads, "证书下载器"), "warn"),
        ("Program Files 下的子目录",  os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "证书下载器"), "warn"),
        ("普通用户目录",              os.path.join(home, "tools", "证书下载器"), "ok"),
    ]
    if onedrive:
        cases.append(("OneDrive 本身", onedrive, "forbid"))
        cases.append(("OneDrive 下的子目录", os.path.join(onedrive, "证书下载器"), "warn"))
    if os.path.exists("D:\\"):
        cases.append(("D 盘普通目录", "D:\\tools\\证书下载器", "ok"))

    for label, path, want in cases:
        level, reason, _ = installer.classify_target(path)
        check("路径守卫 · %s" % label, level, want,
              extra=("理由: %s" % reason.split("\n")[0]) if reason else "")


# ==========================================================================
# 二、安装：不该删的东西一个都不能删
# ==========================================================================
def build_payload(root):
    """造一份假 payload，结构跟真实的一样（含 userdata 文件）。"""
    pd = Path(root) / "payload"
    app = pd / "证书下载器"
    src = pd / "源码" / "程序"
    app.mkdir(parents=True)
    src.mkdir(parents=True)
    (app / "证书下载器.exe").write_bytes(b"MZ" + b"\0" * 512)
    (app / "使用说明.txt").write_text("说明文档", encoding="utf-8")
    (app / "config.json").write_text('{"sites": {}}', encoding="utf-8")
    (app / "学校名映射.csv").write_text("# 修正表\n", encoding="utf-8")
    (app / "_internal").mkdir()
    (app / "_internal" / "python313.dll").write_bytes(b"\0" * 256)
    (src / "cert_downloader.py").write_text("# 引擎\n", encoding="utf-8")
    (src / "config.json").write_text('{"sites": {}}', encoding="utf-8")
    return str(pd)


def test_install(sandbox):
    head("二、安装（P0-2 / P0-3）")
    pd = build_payload(sandbox)
    os.environ["CERTDL_PAYLOAD_DIR"] = pd
    os.environ["CERTDL_SKIP_SHELL_INTEGRATION"] = "1"

    target = os.path.join(sandbox, "安装目标")
    foreign = os.path.join(sandbox, "别人的文件夹")

    # --- 2.1 装到空目录 ---
    r = run_py("--dir=" + target)
    check("装到空目录 · 退出码", r.returncode, 0, extra=(r.stdout or "")[-300:])
    check("证书下载器.exe 已落盘",
          os.path.isfile(os.path.join(target, "证书下载器", "证书下载器.exe")), True)
    check("卸载脚本已生成",
          os.path.isfile(os.path.join(target, "uninstall.ps1")), True)
    check("卸载启动器已生成",
          os.path.isfile(os.path.join(target, "uninstall.bat")), True)

    mp = os.path.join(target, "install_manifest.json")
    check("安装清单已生成", os.path.isfile(mp), True)
    man = {}
    if os.path.isfile(mp):
        with open(mp, "r", encoding="utf-8") as f:
            man = json.load(f)
    check("清单 app_id 正确", man.get("app_id"), installer.APP_ID)
    rels = {e["rel"] for e in man.get("entries", [])}
    check("清单登记了 exe", "证书下载器/证书下载器.exe" in rels, True)
    check("清单登记了 _internal 里的 dll",
          "证书下载器/_internal/python313.dll" in rels, True)
    check("清单登记了 manifest 自己", "install_manifest.json" in rels, True)
    check("清单记录里没有绝对路径",
          all(not os.path.isabs(e["rel"]) for e in man.get("entries", [])), True)
    userdata = {e["rel"] for e in man.get("entries", []) if e.get("type") == "userdata"}
    check("config.json 被标为 userdata", "证书下载器/config.json" in userdata, True)

    # 卸载脚本落盘后必须是 UTF-8 with BOM，否则 PowerShell 5.1 读成一堆乱码
    raw = open(os.path.join(target, "uninstall.ps1"), "rb").read(3)
    check("卸载脚本带 UTF-8 BOM", raw, b"\xef\xbb\xbf")
    bat = open(os.path.join(target, "uninstall.bat"), "rb").read()
    check("卸载启动器是纯 ASCII（不会被代码页搞乱）",
          all(b < 128 for b in bat), True)

    # --- 2.2 装到「别人的文件夹」必须被拒 ---
    os.makedirs(foreign, exist_ok=True)
    with open(os.path.join(foreign, "老板要的报表.xlsx"), "wb") as f:
        f.write(b"PK\x03\x04")
    with open(os.path.join(foreign, "照片.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff")
    r = run_py("--dir=" + foreign)
    check("装到非空目录 · 被拒绝（退出码非 0）", r.returncode != 0, True,
          extra=(r.stdout or "")[-300:])
    check("非空目录里的文件没被动过",
          sorted(os.listdir(foreign)),
          sorted(["老板要的报表.xlsx", "照片.jpg"]))

    # --- 2.3 静默模式装到危险路径必须被拒（P0-3）---
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    desktop = known("桌面") or os.path.join(home, "Desktop")
    for label, p in [("桌面", desktop),
                     ("盘根", os.environ.get("SystemDrive", "C:") + "\\"),
                     ("用户主目录", home)]:
        before = sorted(os.listdir(p)) if os.path.isdir(p) else None
        r = run_py("--dir=" + p)
        after = sorted(os.listdir(p)) if os.path.isdir(p) else None
        check("静默模式装到「%s」· 被拒绝" % label, r.returncode != 0, True,
              extra=(r.stdout or "")[-200:])
        check("静默模式装到「%s」· 该目录内容未变" % label, after, before)

    return target


# ==========================================================================
# 三、卸载：只删自己的，而且进回收站
# ==========================================================================
def test_uninstall(sandbox, target):
    head("三、卸载（P0-1：不许递归删用户目录）")

    # 目标目录里塞一份「用户自己的东西」，卸载必须一根汗毛都不动
    userdir = os.path.join(target, "我的资料")
    os.makedirs(userdir, exist_ok=True)
    with open(os.path.join(userdir, "简历.docx"), "wb") as f:
        f.write(b"PK\x03\x04doc")
    with open(os.path.join(userdir, "照片.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xffjpg")
    with open(os.path.join(target, "说明.txt"), "w", encoding="utf-8") as f:
        f.write("这是用户自己放的")

    desktop = known("桌面") or ""
    desktop_before = sorted(os.listdir(desktop)) if os.path.isdir(desktop) else None

    ps1 = os.path.join(target, "uninstall.ps1")
    check("卸载脚本存在", os.path.isfile(ps1), True)

    rb_before = installer._recycle_bin_items()
    r = run_ps("-File", ps1, "-NoPrompt")
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    print("\n  ---- 卸载脚本输出 ----")
    for line in out.splitlines():
        print("  | " + line)
    print("  ----------------------\n")

    check("卸载脚本没有吞掉 PowerShell 报错", ps_errors(r), [],
          extra="报错被 Continue 吞掉时，脚本会假装成功，报告看不出来")

    check("程序本体已删除",
          os.path.exists(os.path.join(target, "证书下载器", "证书下载器.exe")), False)
    check("_internal 已删除",
          os.path.exists(os.path.join(target, "证书下载器", "_internal")), False)
    check("源码已删除",
          os.path.exists(os.path.join(target, "源码", "程序", "cert_downloader.py")), False)
    check("安装清单已删除",
          os.path.exists(os.path.join(target, "install_manifest.json")), False)

    check("用户数据 config.json 被保留",
          os.path.isfile(os.path.join(target, "证书下载器", "config.json")), True)
    check("用户数据 学校名映射.csv 被保留",
          os.path.isfile(os.path.join(target, "证书下载器", "学校名映射.csv")), True)

    check("用户自己的文件夹还在", os.path.isdir(userdir), True)
    check("用户自己的文件没被动过",
          sorted(os.listdir(userdir)) if os.path.isdir(userdir) else None,
          sorted(["照片.jpg", "简历.docx"]))
    check("用户自己放的 说明.txt 还在",
          os.path.isfile(os.path.join(target, "说明.txt")), True)

    desktop_after = sorted(os.listdir(desktop)) if os.path.isdir(desktop) else None
    check("桌面内容未变", desktop_after, desktop_before)

    # 回收站里必须真的多了东西 —— 这是「删除进了回收站而不是永久删除」的
    # 唯一硬证据。用 SHQueryRecycleBin 查（O(1)），不用 Shell.Application
    # 遍历回收站（项数一多就慢到近乎卡死）。
    rb_after = installer._recycle_bin_items()
    check("回收站项数增加了（证明走回收站，不是永久删除）",
          (rb_before is not None and rb_after is not None and rb_after > rb_before),
          True, extra="卸载前=%s 卸载后=%s" % (rb_before, rb_after))


# ==========================================================================
# 四、卸载脚本的自保：清单不可信就一个都不动
# ==========================================================================
def test_uninstall_guard(sandbox):
    head("四、卸载脚本的自保（清单不可信时拒绝执行）")

    case = os.path.join(sandbox, "清单签名不对")
    os.makedirs(os.path.join(case, "证书下载器"), exist_ok=True)
    with open(os.path.join(case, "证书下载器", "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    shutil.copy2(os.path.join(HERE, "uninstall.ps1"),
                 os.path.join(case, "uninstall.ps1"))
    with open(os.path.join(case, "install_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"app": "别人的程序", "app_id": "someone-else",
                   "install_root": case,
                   "entries": [{"rel": "证书下载器/a.txt", "sha256": None,
                                "type": "app", "delete_on_uninstall": True}]},
                  f, ensure_ascii=False)
    r = run_ps("-File", os.path.join(case, "uninstall.ps1"), "-NoPrompt")
    check("guard 用例没有吞掉 PowerShell 报错", ps_errors(r), [])
    check("app_id 不对 · 拒绝卸载", r.returncode != 0, True)
    check("app_id 不对 · 文件一个没删",
          os.path.isfile(os.path.join(case, "证书下载器", "a.txt")), True)

    case2 = os.path.join(sandbox, "目录对不上")
    os.makedirs(os.path.join(case2, "证书下载器"), exist_ok=True)
    with open(os.path.join(case2, "证书下载器", "b.txt"), "w", encoding="utf-8") as f:
        f.write("y")
    shutil.copy2(os.path.join(HERE, "uninstall.ps1"),
                 os.path.join(case2, "uninstall.ps1"))
    with open(os.path.join(case2, "install_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"app": installer.APP_NAME, "app_id": installer.APP_ID,
                   "install_root": r"C:\完全不同的地方",
                   "entries": [{"rel": "证书下载器/b.txt", "sha256": None,
                                "type": "app", "delete_on_uninstall": True}]},
                  f, ensure_ascii=False)
    r = run_ps("-File", os.path.join(case2, "uninstall.ps1"), "-NoPrompt")
    check("guard 用例没有吞掉 PowerShell 报错", ps_errors(r), [])
    check("清单目录对不上 · 拒绝卸载", r.returncode != 0, True)
    check("清单目录对不上 · 文件一个没删",
          os.path.isfile(os.path.join(case2, "证书下载器", "b.txt")), True)

    # 篡改清单里的 rel，想让它删到安装目录外面去 → 必须被挡
    case3 = os.path.join(sandbox, "路径穿越")
    os.makedirs(os.path.join(case3, "证书下载器"), exist_ok=True)
    victim = os.path.join(sandbox, "别处的重要文件.txt")
    with open(victim, "w", encoding="utf-8") as f:
        f.write("不能被删")
    shutil.copy2(os.path.join(HERE, "uninstall.ps1"),
                 os.path.join(case3, "uninstall.ps1"))
    with open(os.path.join(case3, "install_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"app": installer.APP_NAME, "app_id": installer.APP_ID,
                   "install_root": case3,
                   "entries": [{"rel": "../别处的重要文件.txt", "sha256": None,
                                "type": "app", "delete_on_uninstall": True}]},
                  f, ensure_ascii=False)
    r = run_ps("-File", os.path.join(case3, "uninstall.ps1"), "-NoPrompt")
    check("guard 用例没有吞掉 PowerShell 报错", ps_errors(r), [])
    check("清单里的 ../ 穿越 · 目标文件安然无恙", os.path.isfile(victim), True)


def main():
    print("安装器安全检查 · 沙盒测试")
    print("沙盒用系统临时目录，所有删除都走回收站。")
    print("  " + ensure_bom(HERE / "uninstall.ps1"))
    ok = check_ps1_syntax(HERE / "uninstall.ps1")
    check("uninstall.ps1 通过 PowerShell 语法检查", ok, True,
          extra="不通过的话，后面的用例全都会以「跑不完」的形式失败")
    if not ok:
        print("\n!! 卸载脚本语法不过，先修它，后面的测试没意义。")
        return 1

    sandbox = tempfile.mkdtemp(prefix="certdl_safety_")
    print("沙盒：%s" % sandbox)
    try:
        test_path_guard()
        target = test_install(sandbox)
        test_uninstall(sandbox, target)
        test_uninstall_guard(sandbox)
    finally:
        print()
        print("沙盒保留在：%s" % sandbox)
        print("（里面的「别人的文件夹」「我的资料」是故意留的，看完可以整个删掉）")

    failed = [r for r in RESULTS if not r[0]]
    print()
    print("=" * 74)
    print("  共 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    print("=" * 74)
    if failed:
        print("失败清单：")
        for _, name, got, want, _e in failed:
            print("  · %s  期望 %r / 实际 %r" % (name, want, got))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
