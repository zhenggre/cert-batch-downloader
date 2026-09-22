# -*- coding: utf-8 -*-
"""调用 PyInstaller 打包（写成脚本避免超长命令行）。"""
import subprocess
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onedir", "--console",
    "--name", "证书下载器",
    "--add-data", str(R / "程序" / "web") + ";web",
    "--hidden-import", "openpyxl",
    "--hidden-import", "requests",
    # 故障自救手册：是被 import 的模块，正常会被自动收集；
    # 显式写出来是为了万一改名/移动时能立刻发现（缺了它程序就少了"教同事怎么办"的能力）
    "--hidden-import", "error_help",
    "--hidden-import", "school_merge",
    # 自动识别新比赛。它在 web_app 里是函数内 import（用到才加载），
    # 静态分析虽然通常能扫到，但漏一次就是"换比赛功能点了没反应"这种哑巴故障 ——
    # 显式声明，让缺模块直接在打包阶段暴露，而不是等同事用的时候才发现。
    "--hidden-import", "site_probe",
    # 金数据适配（两代产品）。同样是函数内 import，漏了的表现是
    # "贴金数据网址识别不了"，而且只在同事真遇到金数据时才暴露。
    "--hidden-import", "platform_jinshuju",
    # 早就不依赖 pandas 了（改用 openpyxl 读写表格）。
    # 但打包环境里装着 pandas，PyInstaller 会顺着分析进去，白塞几十 MB。
    "--exclude-module", "pandas",
    "--exclude-module", "numpy",
    "--exclude-module", "matplotlib",
    "--exclude-module", "scipy",
    "--exclude-module", "PIL",
    # lxml：openpyxl 把它当"可选的加速器"，装上就用、没装就走内置 ElementTree。
    # 我们的代码从不直接 import 它，而打包机的 Python 里恰好装着 lxml，
    # PyInstaller 就顺着 openpyxl 把它整包带了进来 —— 实测白胖 6.5 MB
    # （etree.cp313 3.9MB + objectify 1.7MB + html 子包 0.6MB …）。
    # 2026-09-21 排掉后体积从 31.5 MB 回到 ~25 MB，试跑验证行为不变。
    "--exclude-module", "lxml",
    "--exclude-module", "pytest",
    "--exclude-module", "setuptools",
    "--exclude-module", "pydoc",
    "--exclude-module", "tkinter",
    "--distpath", str(R / "发给同事"),
    "--workpath", str(R / "开发维护" / "build"),
    "--specpath", str(R / "开发维护"),
    str(R / "程序" / "web_app.py"),
]
print("CMD:", " ".join(cmd))
r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
print("EXIT:", r.returncode)
out = (r.stdout or "") + "\n--- STDERR ---\n" + (r.stderr or "")
(R / "开发维护" / "pyi_build3.log").write_text(out, encoding="utf-8")
print(out[-2500:])
