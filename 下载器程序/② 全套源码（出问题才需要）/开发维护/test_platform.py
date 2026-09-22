# -*- coding: utf-8 -*-
"""Windows 路径与文件管理器测试。

项目已定为只跑 Windows（2026-09-19 用户拍板）。这里只保留"纯 Windows
场景下也必须正确"的验证：

  ① default_out_dir()：优先 D 盘桌面；**D 盘不存在时退回系统桌面**。
     为什么必须测：同事的笔记本可能只有 C 盘。写死 `D:\\` 会让程序
     在"点开始"的那一刻直接崩，而且报的是英文异常。
  ② open_in_file_manager()：失败要吞掉异常——它只是个便利功能，
     打不开文件夹不该把整个作业带崩。
  ③ 反向检查：确认代码里没有残留的 macOS 专属文案/分支
     （`open_in_file_manager` 里那处平台判断是有意保留的，单列出来）。
"""
import pathlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))
import cert_downloader as CD          # noqa: E402
import error_help as EH               # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK] %s" % name)
    else:
        fail += 1
        print("  [!!] %s   %s" % (name, extra))


print("=" * 68)
print("① default_out_dir()：本机有 D 盘时应落在 D 盘桌面")
print("=" * 68)
d = CD.default_out_dir()
print("     实测返回值：%s" % d)
check("返回绝对路径", Path(d).is_absolute(), d)
check("目录名是「证书」", Path(d).name == "证书", Path(d).name)
has_d = Path("D:/Desktop").is_dir()
print("     本机 D:\\Desktop 是否存在：%s" % has_d)
if has_d:
    check("有 D 盘时 → D:\\Desktop\\证书", d.replace("/", "\\").lower().startswith("d:\\desktop"),
          d)
else:
    check("无 D 盘时 → 系统桌面", str(Path.home()) in d, d)

print()
print("=" * 68)
print("②【关键】伪装成「这台电脑没有 D 盘」，看会不会崩")
print("=" * 68)
_orig = pathlib.Path.is_dir


def fake_is_dir(self):
    """只对 D:\\Desktop 撒谎：告诉程序它不存在。"""
    if str(self).replace("\\", "/").lower().rstrip("/") == "d:/desktop":
        return False
    return _orig(self)


pathlib.Path.is_dir = fake_is_dir
try:
    d2 = CD.default_out_dir()
finally:
    pathlib.Path.is_dir = _orig

print("     实测返回值：%s" % d2)
check("不会崩，且能给出一个可用位置", bool(d2))
check("退回系统桌面（不再指向 D 盘）",
      not d2.replace("/", "\\").lower().startswith("d:\\"), d2)
check("仍然以「证书」结尾", Path(d2).name == "证书", Path(d2).name)
check("恢复伪装后行为不变（D 盘还在就还回 D 盘）",
      CD.default_out_dir() == d, CD.default_out_dir())

print()
print("=" * 68)
print("③ open_in_file_manager()：失败必须吞异常")
print("=" * 68)
r = CD.open_in_file_manager(str(Path.home() / "__绝对不存在的目录__xyz"))
print("     打开一个不存在的路径 → 返回 %r（不该抛异常）" % r)
check("不存在的路径返回 False 而不是抛异常", r is False, repr(r))
r2 = CD.open_in_file_manager("")
check("空字符串也不炸", r2 is False, repr(r2))

print()
print("=" * 68)
print("④ 反向检查：不该再有 macOS 专属内容")
print("=" * 68)
eh_src = (ROOT / "程序" / "error_help.py").read_text(encoding="utf-8")
check("error_help.py 已无 IS_MAC", "IS_MAC" not in eh_src)
check("error_help.py 已无 PLATFORM_FIXES", "PLATFORM_FIXES" not in eh_src)
check("error_help.py 已无 platform_fixes()", "platform_fixes" not in eh_src)
check("error_help.py 不再以 sys.platform 分支", "darwin" not in eh_src)
check("error_help.py 不提「系统设置 →」（那是 macOS 菜单）", "系统设置" not in eh_src)
check("代理文案给的是 Windows 路径",
      "网络和 Internet" in "\n".join(EH.explain("proxy")))

cd_src = (ROOT / "程序" / "cert_downloader.py").read_text(encoding="utf-8")
check("cert_downloader.py 不再真的 import winreg（改用标准库 getproxies）",
      "import winreg" not in cd_src and "winreg.OpenKey" not in cd_src)
check("open_in_file_manager 仍保留（Windows 走 os.startfile）",
      "os.startfile" in cd_src and "def open_in_file_manager" in cd_src)
check("输出目录不再写死 D 盘字面量（改由 default_out_dir 决定）",
      'r"D:\\Desktop\\证书"' not in cd_src and "default_out_dir()" in cd_src)

# 2026-09-19 定"仅 Windows"后清掉的分支 —— 加个护栏，免得以后又混回来
check("cert_downloader.py 已无 darwin 分支", "darwin" not in cd_src)
check("cert_downloader.py 已无 xdg-open（Linux 分支）", "xdg-open" not in cd_src)
check("删分支后没留下孤儿 import subprocess",
      "import subprocess" not in cd_src,
      "若 subprocess 确实又被用到，请把这条断言改成检查实际调用点")
check("os.startfile 只有一处调用（没有 if/else 分平台）",
      cd_src.count("os.startfile(") == 1, "出现 %d 次" % cd_src.count("os.startfile("))

print()
print("=" * 68)
print("⑤ 网页服务：输出目录自定义的后端能力就位")
print("=" * 68)
wa_src = (ROOT / "程序" / "web_app.py").read_text(encoding="utf-8")
for token, desc in [
    ("def list_drives", "列出盘符"),
    ("def browse_dir", "浏览目录"),
    ("def safe_mkdir", "新建文件夹"),
    ("/api/browse", "浏览接口"),
    ("/api/mkdir", "新建接口"),
    ("last_out", "记住上次选的位置"),
    ("打开界面.url", "浏览器没弹开时的兜底入口"),
    ("GetLogicalDrives", "枚举盘符用系统 API"),
]:
    check("web_app.py 含「%s」" % desc, token in wa_src)

html = (ROOT / "程序" / "web" / "index.html").read_text(encoding="utf-8")
for token, desc in [
    ("btn-browse", "「浏览…」按钮"),
    ('id="pick"', "选文件夹弹层"),
    ("pk-mk", "新建文件夹按钮"),
    ("选一个保存证书的文件夹", "弹层标题"),
    ("别关掉那个黑色窗口", "「别关黑窗口」提示"),
    ("btn-newsite", "「＋ 换比赛」按钮"),
    ('id="newsite"', "换比赛弹层"),
    ("ns-probe", "「自动识别」按钮"),
]:
    check("index.html 含「%s」" % desc, token in html)

print()
print("=" * 68)
print("⑥ 打包清单：能 import 的模块必须显式声明")
print("=" * 68)
# 少声明一个模块，程序不会报错，只会"点了没反应"——最坏的一类故障。
# site_probe 还是在函数里 import 的，更容易被漏掉，所以盯死它。
pack_src = (ROOT / "开发维护" / "pack.py").read_text(encoding="utf-8")
for mod, why in (("site_probe", "自动识别新比赛"),
                 ("error_help", "故障自救手册"),
                 ("school_merge", "学校名归一化")):
    check("pack.py 声明了 %s（%s）" % (mod, why),
          '"--hidden-import", "%s"' % mod in pack_src)

print()
print("=" * 68)
print("结果：%d 项通过，%d 项失败" % (ok, fail))
print("=" * 68)
sys.exit(1 if fail else 0)
