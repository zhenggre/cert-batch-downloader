# -*- coding: utf-8 -*-
"""汇编安装器要内嵌的 payload：干净的 ① 便携包 + 精选版 ② 源码。

=====================================================================
这份脚本管的是「什么能进安装包」。判断标准有两条，缺一不可：

  A. 交付必需 —— 同事拿到安装包后要能直接跑（程序本体 + 它的配置）。
  B. 不该外流 —— 作者本机的东西、运行时会重新生成的临时文件、
     带真实数据的测试夹具，一律不进。

2026-09-22 复盘时发现的三个问题，都在这里修掉了：
  1. 里面写着 `ROOT = r"C:\\Users\\<user>\\Desktop\\..."` 的绝对路径 ——
     换台机器就报废，而且把打包机的用户名写死在交付流程里。
     现在从脚本自身位置推导，可用环境变量覆盖。
  2. 排除清单跟 .gitignore 对不上：.gitignore 明说「历史版本不进版本库」，
     这里却主动把 历史版本/ 整个复制进了安装包；`发给同事/`（里面还有
     另一份打包二进制）、`runtime/`（几十 MB 第三方库）、`config.json.bak`
     同样漏网。现在两边对齐。
  3. 最要命的：① 便携包里带着 `运行中.json` / `界面地址.txt` /
     `打开界面.url`。这三个是程序运行时生成的指路文件，里面记着**上一次
     运行的一次性令牌**。虽然令牌只对本机 127.0.0.1 有效、危害有限，
     但把一个 live 令牌打进要发给别人的安装包，本身就是不该发生的事。
     现在一律排除。

另外加了一道自检：打包完成后扫一遍产物，发现本机用户名路径、live 令牌、
或**非演示文件**里的身份证号/手机号，就直接报错中止 —— 打包流程不该
靠人肉记得检查。
"""
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------
# 路径：全部从脚本自身位置推导，不写死任何一台机器的目录
# ----------------------------------------------------------------------
# 这个脚本躺在 <仓库根>/下载器程序/③ 安装包/构建脚本/ 里，于是：
HERE = Path(__file__).resolve().parent          # ③ 安装包/构建脚本
INSTALLER_DIR = HERE.parent                     # ③ 安装包
PKG_ROOT = INSTALLER_DIR.parent                 # 下载器程序
REPO_ROOT = PKG_ROOT.parent                     # 仓库根

APP_SRC = PKG_ROOT / "① 双击就能用（直接用不用装任何东西）" / "证书下载器"
SRC2 = PKG_ROOT / "② 全套源码（出问题才需要）"

# 产物默认放在构建脚本旁边；构建流水线要换地方就设 CERTDL_PAYLOAD_OUT
OUT = Path(os.environ.get("CERTDL_PAYLOAD_OUT") or (HERE / "payload"))

# ----------------------------------------------------------------------
# 排除规则
# ----------------------------------------------------------------------
# ① 便携包（同事实际运行的东西）：只需要排掉运行时状态和构建垃圾。
#    `_internal/` 和 `*.exe` 是程序本体，一个都不能少。
APP_DROP_NAMES = {
    # 程序运行时生成的指路文件 —— 里面有一上一次运行的一次性令牌
    "运行中.json", "界面地址.txt", "打开界面.url",
    # 用户自己的偏好（保存位置之类），不该带作者的
    "用户设置.json",
    "config.json.bak",
    "__pycache__", ".pytest_cache", ".git",
}
APP_DROP_GLOBS = ("*.log", "*改前备份*")

# ② 源码侧：还要再排掉本机环境、旧代码、测试夹具。
#    规则来源与 .gitignore 保持一致 —— 两边**必须同步改**，
#    不然就会出现「.gitignore 说不进库，打包脚本照样塞进安装包」这种自相矛盾。
SRC_DROP_NAMES = {
    "__pycache__", ".pytest_cache", "build", "dist", ".git", "node_modules",
    # 已被接口版取代的早期实现。.gitignore 里也声明不进库。
    # 而且老代码里还留着 unverified 的历史包袱，没必要发给同事。
    "历史版本",
    # 旧的预构建分发副本（里面还压着一份打包二进制，白胖几十 MB）
    "发给同事",
    # 本机 Python 运行环境：几十 MB 第三方库，不是本项目代码
    "runtime", "_runtime_dl", "_internal",
    # 运行时会重新生成的指路文件（含一次性令牌）
    "运行中.json", "界面地址.txt", "打开界面.url", "用户设置.json",
    "config.json.bak",
    # 测试夹具与样例输出。fixture_* 是真实网站页面的快照（含主办方的
    # 手机号和邮箱），故障诊断_样例.txt 里写着打包机的绝对路径。
    "fixture_比赛查询页.html", "fixture_金数据新版_RSC.txt",
    "故障诊断_样例.txt", "docs_test.txt", "error_help_test.txt",
}
SRC_DROP_GLOBS = ("*.log", "*.pyc", "*.exe", "shot_*.png", "fixture_*",
                  "*改前备份*")

# 这两个**保留**：交付包要靠它们才能跑。
# 它们同时被 .gitignore 排除（含作者的站点配置和学校名），
# 但「进交付包」和「进 Git 仓库」是两回事 —— 别照着 .gitignore 一起删。
KEEP_FROM_APP = {"config.json", "学校名映射.csv"}


def _matcher(drop_names, drop_globs):
    def ignore(dirpath, names):
        out = set()
        for n in names:
            if n in drop_names:
                out.add(n)
                continue
            low = n.lower()
            for g in drop_globs:
                if _glob_match(low, g.lower()):
                    out.add(n)
                    break
        return out
    return ignore


def _glob_match(name, pattern):
    """只支持 * 和 ?，够用了。"""
    rx = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(rx, name) is not None


# ----------------------------------------------------------------------
# 自检：产物里不该出现的东西
# ----------------------------------------------------------------------
_ID_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_HOME_RE = re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s\"',;)]+", re.I)
_TOKEN_RE = re.compile(r"\"?token\"?\s*[:=]\s*\"?[0-9a-fA-F]{6,}")

TEXT_EXT = {".txt", ".md", ".json", ".csv", ".html", ".htm", ".js", ".py",
            ".url", ".bat", ".ps1", ".spec", ".log", ".ini", ".cfg", ".xml"}

# 演示 / 测试文件里的号码是构造的，报出来只会淹没真问题。
# 注意：只影响「身份证号 / 手机号」的告警级别；
# 本机用户名路径和 live 令牌在哪都是硬伤，一律 fail。
DEMO_HINT = ("test_", "fixture", "example", "示例", "演示", "demo", "_样例")


def _is_demo(rel):
    low = str(rel).lower()
    return any(h in low for h in DEMO_HINT)


# 行级演示标记：号码所在的那一行自己就写着 demo / 示例 / 样例，
# 说明它是构造的。比如 platform_jinshuju.py 的 _self_test() 用的是
# 金数据官方公开演示页的数据。
LINE_DEMO_HINT = ("demo", "演示", "示例", "样例", "example", "fixture")


def _line_is_demo(line):
    low = line.lower()
    return any(h in low for h in LINE_DEMO_HINT)


def _xlsx_text(path):
    try:
        with zipfile.ZipFile(path) as z:
            return "\n".join(
                z.read(n).decode("utf-8", "replace")
                for n in z.namelist()
                if n.startswith("xl/") and n.endswith(".xml"))
    except Exception:
        return ""


def selfcheck(out_dir):
    """扫一遍产物。返回 (hard, soft)：hard 非空就该中止打包。"""
    hard, soft = [], []
    if not out_dir.is_dir():
        return ["产物目录不存在：%s" % out_dir], soft

    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        rel = p.relative_to(out_dir)
        try:
            if ext == ".xlsx":
                text = _xlsx_text(p)
            elif ext in TEXT_EXT or ext == "":
                if p.stat().st_size > 8 * 1024 * 1024:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            else:
                continue
        except OSError:
            continue

        if _HOME_RE.search(text):
            hard.append("%s：含有本机用户名路径（打包机环境泄漏）" % rel)
        if _TOKEN_RE.search(text):
            hard.append("%s：含有运行令牌字面量" % rel)

        # 身份证号 / 手机号按**行**判断是不是演示数据，不按文件放宽：
        # 同一个文件里既可能有演示块，也可能混进真号码，一刀切会漏。
        for lineno, raw_line in enumerate(text.splitlines(), 1):
            hits = []
            if _ID_RE.search(raw_line):
                hits.append("身份证号")
            if _PHONE_RE.search(raw_line):
                hits.append("手机号")
            if not hits:
                continue
            demo = _is_demo(rel) or _line_is_demo(raw_line)
            where = "%s:%d" % (rel, lineno)
            (soft if demo else hard).append("%s：%s" % (where, "、".join(hits)))
    return hard, soft


# ----------------------------------------------------------------------
def _du(p):
    t = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                t += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return t


def _count(p):
    return sum(1 for f in p.rglob("*") if f.is_file())


def main():
    for need, what in ((APP_SRC, "① 便携包"), (SRC2, "② 全套源码")):
        if not need.is_dir():
            print("[FAIL] 找不到%s：%s" % (what, need))
            print("       这个脚本必须放在 <仓库根>/下载器程序/③ 安装包/构建脚本/ 下。")
            return 2

    if OUT.exists():
        # 这是我们自己上一次生成的构建产物（路径由本脚本决定），
        # 清掉重建是安全的；不用回收站，因为它是可完全重建的中间产物。
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    app_dst = OUT / "证书下载器"
    src_dst = OUT / "源码"

    print("复制 ① 便携包 ...")
    shutil.copytree(APP_SRC, app_dst,
                    ignore=_matcher(APP_DROP_NAMES, APP_DROP_GLOBS))

    print("复制 ② 精选源码（程序 / 开发维护）...")
    src_dst.mkdir()
    ig_src = _matcher(SRC_DROP_NAMES, SRC_DROP_GLOBS)
    for sub in ("程序", "开发维护"):
        s = SRC2 / sub
        if s.is_dir():
            shutil.copytree(s, src_dst / sub, ignore=ig_src)
        else:
            print("  （跳过不存在的 %s）" % s)

    print("\n自检 ...")
    hard, soft = selfcheck(OUT)
    for m in soft:
        print("  [提示] %s" % m)
    for m in hard:
        print("  [问题] %s" % m)

    print("\npayload 体积: %.1f MB / %d 个文件"
          % (_du(OUT) / 1024 / 1024, _count(OUT)))
    print("顶层:", sorted(x.name for x in OUT.iterdir()))

    if hard:
        print("\n[FAIL] 产物里有不该出现的内容，已中止。")
        print("       要么把对应的源文件排除掉，要么确认它是构造的演示数据后")
        print("       加进 DEMO_HINT 白名单（别为了让它过而删检查）。")
        return 1

    print("\nDONE —— payload 就绪：%s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
