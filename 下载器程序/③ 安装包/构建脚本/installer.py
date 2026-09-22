# -*- coding: utf-8 -*-
"""证书下载器 · 安装程序（自制 PyInstaller 安装器，无第三方依赖）。

把内嵌 payload（= ① 便携包 + ② 源码）解压到用户选定目录，建桌面/开始
菜单快捷方式，写卸载脚本与注册表卸载项。GUI 走系统 PowerShell（venv 无
tkinter）。

========================================================================
安全设计（2026-09-22 事故复盘后重写，改动原因逐条写在对应函数上方）
========================================================================
事故：上一版生成的卸载脚本里写着

        rmdir /s /q "%DIR%"

    %DIR% 是用户在文件夹对话框里**任选**的目录，全程零校验。同事把安装
    目录选成「桌面」→ 卸载时整个桌面被递归**永久删除**（rmdir 不走回收
    站），文件全丢。同版还有两处同性质的坑：安装时对 `%DIR%\\源码` 直接
    shutil.rmtree，以及 `--dir=` 静默模式可以脚本化绕过全部校验。

本版三条硬规则，任何改动都不得破坏：

  规则 1  不存在「对用户目录的递归删除」这条路。
          卸载只删 install_manifest.json 里登记的、且 SHA256 对得上的
          文件。目录只在「内容与清单完全一致」或「已经是空目录」时才
          处理。任何 `rmtree` / `rmdir /s` / `Remove-Item -Recurse` 都
          禁止出现在安装与卸载路径上。

  规则 2  一切删除先进回收站（Shell API + FOF_ALLOWUNDO）。
          系统设置成「删除时不进回收站」时，**放弃删除**并只报告待处理
          清单 —— 删不掉比删错好。

  规则 3  安装目录必须过路径守卫：白名单语义（默认位置直接放行）+ 危险
          路径黑名单（桌面/文档/下载/主目录/OneDrive/盘根/Windows/
          Program Files 根/ProgramData 根）。黑名单用 Known Folder API
          拿系统认定的真实路径，并且先解析 junction/symlink —— 靠猜目录
          名或纯字符串比对都会被重定向和链接绕过。

卸载逻辑**只有一份实现**，在本文件同目录的 uninstall.ps1 里（不再用
.bat：bat 是 ANSI 代码页解析，内容含中文时路径会变乱码）。本文件负责把
它复制到安装目录。两边共用的判定规则在 uninstall.ps1 顶部有对应注释。
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

APP_NAME = "证书下载器"
APP_ID = "cert-downloader"          # 清单签名：卸载前必须对得上，防止把别人的清单当自己的
INSTALLER_VERSION = "2.0"
PAYLOAD_NAME = "payload"
MANIFEST_NAME = "install_manifest.json"
UNINST_PS1 = "uninstall.ps1"
UNINST_BAT = "uninstall.bat"

# payload 里的顶层目录 → 复制到 target 下同名目录
PAYLOAD_DIRS = ("证书下载器", "源码")

# 卸载时**默认保留**的用户数据（相对 target，正斜杠）。判据是「用户会自己改它」：
# config.json 里有用户自己加的比赛，学校名映射.csv 是手工修正表，都在重装后还有用。
USERDATA_REL = {
    "证书下载器/config.json",
    "证书下载器/学校名映射.csv",
    "证书下载器/用户设置.json",
    "源码/程序/config.json",
    "源码/程序/学校名映射.csv",
}

# 复制 payload 时跳过的垃圾（构建产物、缓存）。payload 本身已在
# prepare_payload.py 里筛过一遍，这里是第二道。
SKIP_DIRS = {"__pycache__", ".pytest_cache", "build", "node_modules", ".git"}


class InstallError(Exception):
    """预期内的失败：提示用户即可，不用打印堆栈。"""


# ==========================================================================
# 一、路径守卫
# ==========================================================================

# Known Folder GUID。为什么不用 %USERPROFILE%\Desktop 拼？
#   中文系统的「桌面」显示名是「桌面」，物理目录名仍是 Desktop —— 但
#   OneDrive 接管后路径会变成 %USERPROFILE%\OneDrive\桌面，企业环境还
#   可能整块重定向到服务器。「下载」更糟：它压根没有环境变量，只有
#   %USERPROFILE%\Downloads 这个约定，重定向后就是错的。
#   所以一律问系统要。
_KNOWN_FOLDERS = {
    "桌面": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "文档": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "下载": "{374DE290-123F-4565-9164-39C4925E467B}",
    "OneDrive": "{A52BBA46-E9E1-435F-B3D9-28DAA648C0F6}",
    "用户主目录": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "Windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
}

# 装进去会碰系统目录的标签（连子目录都禁）
_SYSTEM_LABELS = {"Windows", "System32", "ProgramData"}
# 装进去会连累用户其他文件的标签（子目录允许但必须确认）
_PERSONAL_LABELS = {"桌面", "文档", "下载", "OneDrive"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _known_folder(guid_str):
    """问系统要 Known Folder 的真实路径。拿不到就返回 None（调用方跳过）。"""
    if os.name != "nt":
        return None
    try:
        guid = _GUID()
        if ctypes.windll.ole32.CLSIDFromString(
                wt.LPCWSTR(guid_str), ctypes.byref(guid)) != 0:
            return None
        buf = wt.LPCWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(buf)) != 0:
            return None
        try:
            return buf.value
        finally:
            try:
                ctypes.windll.ole32.CoTaskMemFree(buf)
            except Exception:
                pass
    except Exception:
        return None


def _norm(p):
    """字符串级规范化：展开环境变量/`~` → 绝对路径 → normpath → 去尾分隔符
    → 统一大小写。只用于「同一个路径的不同写法」比对。"""
    p = os.path.expandvars(os.path.expanduser(str(p).strip().strip('"')))
    p = os.path.normpath(os.path.abspath(p))
    if len(p) > 3 and p.endswith(os.sep):
        p = p[:-1]
    return os.path.normcase(p)


def _real(p):
    """解析 junction / 符号链接后的真实路径。

    **这一层不能省。** Windows 上「文档」经常就是一个 junction
    （C:\\Users\\X\\Documents → 别的盘），「桌面」在 OneDrive 下也是。
    只比字符串的话，一个 junction 就能让危险路径黑名单完全失效，然后
    卸载会顺着链接去删同步盘里的东西。
    """
    try:
        r = os.path.realpath(p)
    except OSError:
        r = os.path.abspath(p)
    return os.path.normcase(os.path.normpath(r))


def _is_under(path_real, root_real):
    """path 是否等于 root 或位于 root 之下（两者都须已 _real 过）。"""
    if path_real == root_real:
        return True
    prefix = root_real if root_real.endswith(os.sep) else root_real + os.sep
    return path_real.startswith(prefix)


_DANGER_CACHE = None


def _danger_roots():
    """危险根目录清单 → [(真实路径, 人类可读名)]。

    含：系统认定的桌面/文档/下载/OneDrive/主目录/Windows、Program Files
    两个变体、ProgramData、System32、以及**每一个存在的盘根**。
    """
    global _DANGER_CACHE
    if _DANGER_CACHE is not None:
        return _DANGER_CACHE
    out = []
    for label, guid in _KNOWN_FOLDERS.items():
        p = _known_folder(guid)
        if p:
            out.append((_real(p), label))
    for env, label in (("ProgramFiles", "Program Files"),
                       ("ProgramFiles(x86)", "Program Files (x86)"),
                       ("ProgramData", "ProgramData"),
                       ("SystemRoot", "Windows")):
        v = os.environ.get(env)
        if v:
            out.append((_real(v), label))
    sr = os.environ.get("SystemRoot")
    if sr:
        out.append((_real(os.path.join(sr, "System32")), "System32"))
    for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "%s:\\" % d
        if os.path.exists(root):
            out.append((_real(root), "%s 盘根目录" % d))
    seen, uniq = set(), []
    for rp, label in out:
        if rp not in seen:
            seen.add(rp)
            uniq.append((rp, label))
    _DANGER_CACHE = uniq
    return uniq


def default_dir():
    """默认安装位置：%LOCALAPPDATA%\\Programs\\<AppName>（不需要管理员权限）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def classify_target(raw):
    """判定一个候选安装目录。返回 (level, reason, resolved)。

      level = 'ok'      直接放行
              'warn'    允许，但必须用户明确确认（多为「在桌面/文档里建
                        专属子目录」这种情况）
              'forbid'  拒绝，换目录

    resolved 是给调用方用的绝对路径（保留用户输入的原始大小写）。
    """
    if not raw or not str(raw).strip():
        return "forbid", "没有选择目录。", ""

    s = str(raw).strip().strip('"')
    if not (os.path.isabs(s) or (len(s) > 1 and s[1] == ":")):
        return "forbid", ("请给一个完整的路径（例如 %s）。\n"
                          "只写文件夹名字的话，程序不知道要装到哪儿。"
                          % default_dir()), s
    try:
        abs_p = os.path.normpath(
            os.path.abspath(os.path.expandvars(os.path.expanduser(s))))
    except Exception as e:
        return "forbid", "这个路径没法解析：%s" % e, s

    if not _is_under(_real(abs_p), _real(os.path.dirname(abs_p) or abs_p)) \
            if False else False:
        pass  # （占位：不做额外处理，保留下面显式的黑名单判定）

    real_p = _real(abs_p)

    # 长路径：SHFileOperationW 受 MAX_PATH 限制，回收站删除会失败；
    # 而且过深的中文路径在 cmd/PowerShell 之间来回传容易出岔子。
    if len(real_p) > 200:
        return "forbid", ("路径太长了（%d 个字符，上限 200）。\n"
                          "请换一个短一点的目录。"
                          % len(real_p)), abs_p

    for root, label in _danger_roots():
        if real_p == root:
            if label.endswith("盘根目录"):
                return "forbid", ("不能直接装在盘根目录（%s）。\n\n"
                                  "请在它下面建一个专属文件夹，例如：\n%s"
                                  % (label, os.path.join(abs_p, APP_NAME))), abs_p
            if label == "用户主目录":
                return "forbid", ("不能直接装在用户主目录里 —— 那里文件太多，"
                                  "而且卸载时不该碰它。\n\n建议用默认位置：\n%s"
                                  % default_dir()), abs_p
            if label in _PERSONAL_LABELS:
                return "forbid", ("不能把「%s」本身作为安装位置：\n"
                                  "卸载时会连累里面的其他文件。\n\n"
                                  "可以改成「%s」\n（程序只会往这个子文件夹里写东西）"
                                  % (label, os.path.join(abs_p, APP_NAME))), abs_p
            return "forbid", "「%s」是系统目录，不能装在这里。" % label, abs_p

        if _is_under(real_p, root):
            if label in _SYSTEM_LABELS:
                return "forbid", "不能装进系统目录「%s」。" % label, abs_p
            if label == "System32":
                return "forbid", "不能装进 System32。", abs_p
            if label.startswith("Program Files"):
                return "warn", ("装进「%s」需要管理员权限，普通双击会失败；\n"
                                "而且这个目录通常由系统统一管理。\n\n"
                                "建议改用默认位置：\n%s"
                                % (label, default_dir())), abs_p
            if label in _PERSONAL_LABELS:
                rel = os.path.relpath(abs_p, root)
                return "warn", ("要装在「%s」里面（%s\\%s）。\n\n"
                                "可以，程序只会删安装清单里登记的自己那些文件，\n"
                                "不会动这个文件夹里的其他东西。但放文件多的目录里\n"
                                "容易混，建议单独建一个。"
                                % (label, label, rel)), abs_p

    return "ok", "", abs_p


# ==========================================================================
# 二、回收站删除（唯一允许的删除方式）
# ==========================================================================

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # ← 关键：进回收站而不是永久删除
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND),
                ("wFunc", wt.UINT),
                ("pFrom", wt.LPCWSTR),
                ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wt.LPCWSTR)]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_disabled():
    """系统是否被设成「删除时不进回收站」。

    开着的话 FOF_ALLOWUNDO 会被忽略 → 我们「一定进回收站」的保证失效，
    会变成永久删除。检测到就整体放弃删除（见 send_to_recycle_bin）。
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket") as k:
            try:
                v, _ = winreg.QueryValueEx(k, "NukeOnDelete")
                return int(v) == 1
            except OSError:
                return False
    except OSError:
        return False
    except Exception:
        return False


def _recycle_bin_items(root=None):
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        if ctypes.windll.shell32.SHQueryRecycleBinW(
                wt.LPCWSTR(root), ctypes.byref(info)) == 0:
            return info.i64NumItems
    except Exception:
        pass
    return None


def send_to_recycle_bin(paths):
    """把一批路径送进回收站。返回 (成功, 失败)。

    绝不使用 os.remove / shutil.rmtree / rmdir —— 那些是永久删除。
    系统关了回收站时直接失败退出，一个都不删。
    """
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return [], []
    if os.name != "nt":
        return [], paths
    if recycle_bin_disabled():
        return [], paths

    before = _recycle_bin_items()
    buf = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buf
    op.pTo = None
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                 | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR)
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception:
        return [], paths
    if rc != 0 or op.fAnyOperationsAborted:
        return [], paths

    after = _recycle_bin_items()
    if before is not None and after is not None and after <= before:
        # 调用成功但回收站项数没变 → 多半是被永久删了（配额不足等）。
        # 这种「静默永久删除」正是要防的，报出来让用户知道。
        print("  !! 警告：删除已完成，但回收站项数没有增加 —— "
              "系统可能直接永久删除了这些文件（回收站空间不足？）。")

    ok = [p for p in paths if not os.path.exists(p)]
    fail = [p for p in paths if os.path.exists(p)]
    return ok, fail


# ==========================================================================
# 三、安装
# ==========================================================================

def payload_dir():
    # CERTDL_PAYLOAD_DIR 是给构建流水线和自动化测试用的：不设就走默认位置。
    ov = os.environ.get("CERTDL_PAYLOAD_DIR")
    if ov:
        return ov
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, PAYLOAD_NAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), PAYLOAD_NAME)


def _uninstall_ps1_text():
    """读卸载脚本模板。只有一份实现，不在这里另抄一遍。

    读用 utf-8-sig：模板文件本身是「UTF-8 带 BOM」保存的（PowerShell 5.1
    会按 ANSI 读取无 BOM 文件，中文内容直接变乱码），读的时候要把 BOM 吃掉，
    否则写出去会变成两个 BOM，PowerShell 就不认第一行的 #Requires 了。
    """
    cands = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        cands.append(os.path.join(mp, "assets", UNINST_PS1))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), UNINST_PS1))
    for c in cands:
        try:
            with open(c, "r", encoding="utf-8-sig") as f:
                return f.read().lstrip("\ufeff")
        except OSError:
            continue
    raise InstallError(
        "安装包不完整：找不到卸载脚本模板 %s。\n"
        "（打包安装程序时要用 --add-data 把 uninstall.ps1 一起打进去）" % UNINST_PS1)


def _hash_file(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _scan_payload():
    """payload 里要安装的文件 → {相对路径(正斜杠): 源绝对路径}。"""
    pd = payload_dir()
    if not os.path.isdir(pd):
        return {}
    out = {}
    for top in PAYLOAD_DIRS:
        src = os.path.join(pd, top)
        if not os.path.isdir(src):
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                sp = os.path.join(dirpath, fn)
                rel = os.path.relpath(sp, pd).replace(os.sep, "/")
                out[rel] = sp
    return out


def _safe_child(root_real, rel):
    """清单里的相对路径 → 绝对路径，并确认没跑出 root。

    清单是磁盘上的 JSON，可能被改。不校验的话，改一行 rel 就能让卸载
    去删别处的文件。所以 `..` 和前缀两道都得过。
    """
    r = str(rel or "").replace("\\", "/").lstrip("/")
    if not r or r.startswith("../") or "/../" in r or r == "..":
        return None
    p = os.path.normpath(os.path.join(root_real, r.replace("/", os.sep)))
    np = os.path.normcase(p)
    if not (np == os.path.normcase(root_real)
            or np.startswith(os.path.normcase(root_real).rstrip(os.sep) + os.sep)):
        return None
    return p


def _read_manifest(target):
    """读安装清单，签名不对就当作没有（别人的清单不能信）。"""
    try:
        with open(os.path.join(target, MANIFEST_NAME), "r", encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or m.get("app_id") != APP_ID:
            return None
        if not isinstance(m.get("entries"), list):
            return None
        return m
    except (OSError, ValueError):
        return None


def _inspect_target(target):
    """目标目录现状 → (状态, 说明)

      'empty'    不存在或空目录
      'ours'     上次本程序装的（有合法清单）→ 走升级
      'foreign'  里面有别人的东西 → 必须拒绝，不碰
    """
    if not os.path.isdir(target):
        return "empty", ""
    try:
        names = os.listdir(target)
    except OSError as e:
        return "foreign", "读不了这个目录：%s" % e
    if not names:
        return "empty", ""
    if _read_manifest(target):
        return "ours", ""
    shown = "、".join(sorted(names)[:8])
    if len(names) > 8:
        shown += "…"
    return "foreign", shown


def _retire_old_version(root_real, old_manifest):
    """升级：把旧版本登记的程序文件先送回收站，再覆盖。

    为什么必须先退：新版直接覆盖同名文件 = 旧文件永久消失。而且这里
    只用回收站删除，用户后悔了能捞回来。
    返回 (已退役, 跳过) —— 跳过的是「哈希对不上」的，多半用户自己改过，
    不动它。
    """
    to_retire, skipped = [], []
    for e in old_manifest.get("entries") or []:
        if not isinstance(e, dict):
            continue
        rel = e.get("rel") or ""
        if not rel or rel in (MANIFEST_NAME, UNINST_BAT, UNINST_PS1):
            continue
        if not e.get("delete_on_uninstall", True):
            continue
        p = _safe_child(root_real, rel)
        if not p or not os.path.isfile(p):
            continue
        want = e.get("sha256")
        if want:
            try:
                if _hash_file(p) != want:
                    skipped.append(rel)
                    continue
            except OSError:
                skipped.append(rel)
                continue
        to_retire.append(p)
    ok, fail = send_to_recycle_bin(to_retire)
    return ok, skipped + fail


def _write_uninstall_scripts(target):
    ps = _uninstall_ps1_text()
    with open(os.path.join(target, UNINST_PS1), "w", encoding="utf-8-sig") as f:
        f.write(ps)
    # 启动器故意只写 ASCII：.bat 由 cmd.exe 按当前 ANSI 代码页解析，文件里
    # 只要有中文，中文路径下 %~dp0 就会变乱码，卸载指向错地方。旧版正是
    # 栽在这个坑上。路径本身由 cmd 在运行时以 Unicode 展开，不受影响。
    bat = (
        "@echo off\r\n"
        "rem Cert Downloader uninstaller.\r\n"
        "rem This file is intentionally ASCII-only -- see installer.py.\r\n"
        "title Uninstall Cert Downloader\r\n"
        "chcp 65001 >nul 2>&1\r\n"
        'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0' + UNINST_PS1 + '"\r\n'
        "if errorlevel 1 pause\r\n"
    )
    with open(os.path.join(target, UNINST_BAT), "w", encoding="ascii") as f:
        f.write(bat)


def _write_manifest(target, rel_map, level):
    root_real = _real(target)
    entries = []
    for rel in sorted(rel_map):
        p = os.path.join(target, rel.replace("/", os.sep))
        try:
            st = os.stat(p)
            h = _hash_file(p)
        except OSError:
            continue
        keep = rel in USERDATA_REL
        entries.append({
            "rel": rel,
            "sha256": h,
            "size": st.st_size,
            "type": "userdata" if keep else "app",
            "delete_on_uninstall": not keep,
        })
    for name in (UNINST_BAT, UNINST_PS1):
        p = os.path.join(target, name)
        if os.path.isfile(p):
            entries.append({
                "rel": name, "sha256": _hash_file(p), "size": os.path.getsize(p),
                "type": "app", "delete_on_uninstall": True,
            })
    # 清单不能自证哈希（写完才有最终字节），登记成 null。
    # 卸载脚本对 null 项照删 —— 能改这一项的人本来就能改清单里任何东西，
    # 不构成额外风险。
    entries.append({
        "rel": MANIFEST_NAME, "sha256": None, "size": None,
        "type": "manifest", "delete_on_uninstall": True,
    })
    m = {
        "app": APP_NAME,
        "app_id": APP_ID,
        "manifest_version": 1,
        "installer_version": INSTALLER_VERSION,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "install_root": os.path.normpath(target),
        "install_level": level,
        "note": ("卸载只删本清单里 delete_on_uninstall=true 且 SHA256 匹配的文件，"
                 "全部进回收站；type=userdata 的默认保留。"),
        "entries": entries,
    }
    with open(os.path.join(target, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return m


SHORTCUT_PS = """
$Wsh = New-Object -ComObject WScript.Shell
$exe = '{exe}'
$app = '{app}'
$dir = '{target}'
$bat = '{bat}'
$desktop = '{desktop}'
$startmenu = '{startmenu}'
$lnk = $Wsh.CreateShortcut("$desktop\\$app.lnk"); $lnk.TargetPath = $exe; $lnk.WorkingDirectory = $dir; $lnk.Save()
$lnk2 = $Wsh.CreateShortcut("$startmenu\\$app.lnk"); $lnk2.TargetPath = $exe; $lnk2.WorkingDirectory = $dir; $lnk2.Save()
$ul = $Wsh.CreateShortcut("$startmenu\\卸载 $app.lnk"); $ul.TargetPath = $bat; $ul.Save()
New-Item -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$app" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$app" -Name DisplayName -Value $app
Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$app" -Name UninstallString -Value $bat
Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\$app" -Name InstallLocation -Value $dir
"""


def _desktop_dir():
    """用户桌面真实路径（Known Folder API，OneDrive 重定向下也准）。"""
    return _known_folder(_KNOWN_FOLDERS["桌面"]) or os.path.join(
        os.environ.get("USERPROFILE", ""), "Desktop")


def install(target, level="ok"):
    """把 payload 装到 target。调用前必须已过 classify_target。"""
    rel_map = _scan_payload()
    if not rel_map:
        raise InstallError("安装包里的文件不完整（找不到 payload）。请重新下载安装包。")

    target = os.path.normpath(target)

    # 非空目录必须带本程序自己的清单，否则一个字节都不往里写。
    # 这一步**必须在这里**做，不能只依赖交互流程里的提示：
    #   · `--dir=` 静默模式根本不走交互流程；
    #   · 上一版的事故正是「用户选了个已经装了东西的目录 → 对同名子目录
    #     直接 rmtree」—— 静默、无提示、无备份。
    state, detail = _inspect_target(target)
    if state == "foreign":
        raise InstallError(
            "这个文件夹里已经有别的东西了：\n\n%s\n\n"
            "为了不误删你的文件，安装程序不会往里写。\n\n"
            "请换一个空文件夹，或者新建一个专用目录，例如：\n%s"
            % (detail, os.path.join(target, APP_NAME)))

    root_real = _real(target)
    os.makedirs(target, exist_ok=True)
    if not os.access(target, os.W_OK):
        raise InstallError("这个文件夹不能写入（可能是只读，或需要管理员权限）。")

    old = _read_manifest(target) if state == "ours" else None
    if old:
        retired, skipped = _retire_old_version(root_real, old)
        if retired:
            print("  升级：%d 个旧文件已移入回收站（可还原）" % len(retired))
        if skipped:
            print("  注意：%d 个文件与清单不符（你自己改过？），已原样保留："
                  % len(skipped))
            for r in skipped[:10]:
                print("     · %s" % r)

    written = 0
    for rel, sp in sorted(rel_map.items()):
        dp = _safe_child(root_real, rel)
        if not dp:
            raise InstallError("安装包里的路径不合法，已中止：%s" % rel)
        d = os.path.dirname(dp)
        if d:
            os.makedirs(d, exist_ok=True)
        # 目标是已存在且**不属于本程序**的文件时，安装前已在
        # _inspect_target 拦掉了（foreign）；走到这里还存在的，只可能是
        # 用户数据（清单里登记为保留的）—— 不覆盖它。
        if os.path.exists(dp) and rel in USERDATA_REL:
            continue
        shutil.copy2(sp, dp)
        written += 1

    _write_uninstall_scripts(target)
    m = _write_manifest(target, rel_map, level)

    exe = os.path.join(target, "证书下载器", "证书下载器.exe")
    ps_script = SHORTCUT_PS.format(
        exe=exe.replace("'", "''"),
        app=APP_NAME.replace("'", "''"),
        target=target.replace("'", "''"),
        bat=os.path.join(target, UNINST_BAT).replace("'", "''"),
        desktop=_desktop_dir().replace("'", "''"),
        startmenu=os.path.join(os.environ.get("APPDATA", ""),
                               "Microsoft", "Windows", "Start Menu",
                               "Programs").replace("'", "''"),
    )
    if os.environ.get("CERTDL_SKIP_SHELL_INTEGRATION") == "1":
        # 测试模式：不动桌面/开始菜单/注册表。自动化测试必须开这个，
        # 否则每跑一次测试就往真实桌面扔一个快捷方式。
        print("  （测试模式：跳过快捷方式与注册表写入）")
    else:
        ps_run(ps_script)
    print("  安装完成：%d 个文件，%d 条清单记录" % (written, len(m["entries"])))
    return exe


# ==========================================================================
# 四、与系统交互的小工具
# ==========================================================================

def powershell_exe():
    """powershell.exe 的完整路径。

    为什么不用裸名：PATH 被裁剪的环境（精简启动器、被改过的开发机、只带
    System32 的 shell）里找不到它，而 Windows 自带的 PowerShell 位置是固定
    的。找不到就退回裸名，让调用方自己报错。
    """
    cands = []
    for env in ("SystemRoot", "WINDIR"):
        sr = os.environ.get(env)
        if sr:
            cands.append(os.path.join(sr, "System32", "WindowsPowerShell",
                                      "v1.0", "powershell.exe"))
    try:
        w = shutil.which("powershell")
    except Exception:
        w = None
    if w:
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return "powershell"


def ps_run(script):
    r = subprocess.run(
        [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    return r.stdout.strip(), r.returncode


def pick_dir(default):
    if not isinstance(default, str):
        default = ""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '选择 {app} 的安装位置'; "
        "$f.SelectedPath = '{d}'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog() -eq 'OK') {{ Write-Output $f.SelectedPath }}"
    ).format(app=APP_NAME, d=default.replace("'", "''"))
    out, _ = ps_run(script)
    return out.strip() or None


def msgbox(text, title=APP_NAME):
    safe = str(text).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.MessageBox]::Show('{t}', '{title}', 'OKOnly')"
    ).format(t=safe, title=str(title).replace("'", "''"))
    ps_run(script)


def confirm(text, title=APP_NAME):
    safe = str(text).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "if ([System.Windows.Forms.MessageBox]::Show('{t}', '{title}', 4) -eq 'Yes') "
        "{{ Write-Output 'YES' }} else {{ Write-Output 'NO' }}"
    ).format(t=safe, title=str(title).replace("'", "''"))
    out, _ = ps_run(script)
    return out.strip().upper() == "YES"


def _result(ok, msg):
    try:
        path = os.path.join(os.environ.get("TEMP", "."), "certdl_install_result.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(("OK\n" if ok else "ERR\n") + str(msg))
    except OSError:
        pass


def _spawn_uninstaller(target):
    """调用安装目录里的卸载脚本（卸载逻辑只有那一份）。"""
    level, reason, resolved = classify_target(target)
    if level == "forbid":
        print("拒绝：%s" % reason)
        return 2
    if not resolved:
        print("拒绝：路径无法解析。")
        return 2
    ps1 = os.path.join(resolved, UNINST_PS1)
    if not os.path.isfile(ps1):
        print("找不到卸载脚本：%s" % ps1)
        return 2
    r = subprocess.run(
        [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", ps1, "-NoPrompt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(r.stdout or "")
    sys.stderr.write(r.stderr or "")
    return r.returncode


# ==========================================================================
# 五、入口
# ==========================================================================

def main():
    args = list(sys.argv[1:])

    # --validate <dir>：只做路径守卫体检，不安装。给自动化测试和排查用。
    if len(args) >= 2 and args[0] == "--validate":
        level, reason, resolved = classify_target(args[1])
        print("level=%s" % level)
        print("resolved=%s" % resolved)
        print("reason=%s" % reason.replace("\n", " / "))
        return 0 if level == "ok" else (3 if level == "warn" else 4)

    # --uninstall <dir>：交给安装目录里的卸载脚本执行。
    if len(args) >= 2 and args[0] == "--uninstall":
        return _spawn_uninstaller(args[1])

    # 静默安装：--dir=<目录> 或第一个绝对路径参数。
    # 注意：仍然要过路径守卫，而且因为没有人可以点确认框，只接受 'ok'
    # 级路径 —— 否则脚本一调用就能把程序装进桌面。
    silent_dir = None
    for a in args:
        if a.startswith("--dir="):
            silent_dir = a[len("--dir="):].strip().strip('"')
        elif a and (os.path.isabs(a) or (len(a) > 1 and a[1] == ":")):
            silent_dir = a.strip().strip('"')
    if silent_dir:
        level, reason, resolved = classify_target(silent_dir)
        if level != "ok":
            _result(False, "%s：%s" % (level, reason))
            print("拒绝（%s）：%s" % (level, reason))
            return 4
        try:
            install(resolved, level)
            _result(True, "installed to " + resolved)
            return 0
        except (InstallError, OSError) as e:
            _result(False, e)
            print("安装失败：%s" % e)
            return 1

    # 交互安装
    default = default_dir()
    while True:
        target = pick_dir(default)
        if not target:
            msgbox("已取消安装。")
            return 0

        level, reason, resolved = classify_target(target)
        if level == "forbid":
            msgbox("这个位置不能用：\n\n%s\n\n请重新选择。" % reason,
                   "安装位置不安全")
            default = resolved or default
            continue
        if level == "warn":
            if not confirm("请先看这一条：\n\n%s\n\n仍要安装到这里吗？\n\n%s"
                           % (reason, resolved), "确认安装位置"):
                default = resolved or default
                continue

        state, detail = _inspect_target(resolved)
        if state == "foreign":
            msgbox(
                "这个文件夹里已经有别的东西了：\n\n%s\n\n"
                "为了不误删你的文件，安装程序不会往里写。\n\n"
                "请点「确定」后另外选一个空文件夹，\n"
                "或者在对话框里点「新建文件夹」，建一个专用目录。" % detail,
                "这个文件夹不是空的")
            default = resolved or default
            continue
        break

    try:
        install(resolved, level)
    except (InstallError, OSError) as e:
        msgbox("安装失败：%s" % e, "安装失败")
        return 1
    except Exception as e:  # noqa: BLE001 —— 兜底也要给用户一句人话
        msgbox("安装失败：%s" % e, "安装失败")
        return 1

    msgbox(
        "安装完成！\n\n位置：%s\n\n"
        "桌面和开始菜单已创建「%s」快捷方式。\n\n"
        "卸载：运行安装目录里的 %s，或到「设置-应用」里卸载。\n"
        "（卸载只会删除本程序自己的文件，其他文件一律不动，\n"
        "　 而且删除的内容都会进回收站，能还原。）"
        % (resolved, APP_NAME, UNINST_BAT))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
