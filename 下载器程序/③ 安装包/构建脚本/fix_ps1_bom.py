# -*- coding: utf-8 -*-
"""把 uninstall.ps1 的编码统一成「UTF-8 带 BOM」。

为什么要单独一步：
    PowerShell 5.1 读取**无 BOM** 的脚本文件时，会按系统 ANSI 代码页
    （中文系统 = GBK）解码。脚本里的中文全部变成乱码，直接导致语法错误
    —— 实测报出来的错是“字符串缺少终止符”，但根因是编码不是引号。
    带 BOM 后 PowerShell 才认 UTF-8。

编辑过 uninstall.ps1 之后都要跑一次这个脚本（或者直接跑
test_installer_safety.py，它会在开头自动调用）。
"""
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def ensure_bom(path):
    p = Path(path)
    if not p.is_file():
        return "缺失: %s" % p
    raw = p.read_bytes()
    if raw.startswith(BOM):
        return "已是 UTF-8 with BOM: %s" % p.name
    # 确认内容确实是 UTF-8（不是 GBK），再补 BOM
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return "!! 不是 UTF-8，没敢动（先转码再补 BOM）: %s  %s" % (p.name, e)
    p.write_bytes(BOM + raw)
    return "已补上 BOM: %s" % p.name


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        str(Path(__file__).resolve().parent / "uninstall.ps1")]
    for t in targets:
        print(ensure_bom(t))
