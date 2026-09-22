# -*- coding: utf-8 -*-
"""陈旧运行记录自愈（reap_stale_runfile）的回归测试。

背景：用户点黑窗口的 X 直接关，等于把进程当场掐死，程序没机会清理
      「运行中.json / 界面地址.txt / 打开界面.url」，它们会一直堆在程序文件夹里。
      web_app.py 现在在启动时自检并清掉它们。这个测试盯的就是那段判定逻辑。

判定必须双向都准：
  * 死记录  → 清掉（否则用户拿到一个打不开的地址）
  * 活实例  → 一个都不动（否则会把正在运行的实例的指路牌删掉）
"""
import json
import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "程序"
sys.path.insert(0, str(PROG))
os.environ["CERTDL_NO_BROWSER"] = "1"

import web_app as W   # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [X ] %s %s" % (name, extra))


def wipe():
    for p in (W.RUNFILE, W.BASE_DIR / "界面地址.txt", W.BASE_DIR / "打开界面.url"):
        try:
            p.unlink()
        except OSError:
            pass


def lay(info):
    """摆出"上次没退干净"的场景。info 为 None 表示不写 运行中.json。"""
    wipe()
    (W.BASE_DIR / "界面地址.txt").write_text("stale", encoding="utf-8")
    (W.BASE_DIR / "打开界面.url").write_text("stale", encoding="utf-8")
    if info is not None:
        W.RUNFILE.write_text(json.dumps(info), encoding="utf-8")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


print("== 1. 进程探活 ==")
check("自己这个进程 → 活着", W._proc_alive(os.getpid()) is True)
check("不存在的 pid → 不活着", W._proc_alive(999999) is False)
check("pid 是 None → 不活着", W._proc_alive(None) is False)
check("pid 是空串 → 不活着", W._proc_alive("") is False)

print("== 2. 干净环境：没有运行记录就别动 ==")
wipe()
check("无 运行中.json → reap 返回 False", W.reap_stale_runfile() is False)

print("== 3. 死 pid + 没人听的端口 → 三个文件全清 ==")
lay({"pid": 999999, "port": free_port()})
check("reap 返回 True", W.reap_stale_runfile() is True)
check("运行中.json 已删", not W.RUNFILE.exists())
check("界面地址.txt 已删", not (W.BASE_DIR / "界面地址.txt").exists())
check("打开界面.url 已删", not (W.BASE_DIR / "打开界面.url").exists())

print("== 4. 真有个实例活着 → 一个都不许动 ==")
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
live_port = srv.getsockname()[1]
lay({"pid": os.getpid(), "port": live_port})
check("reap 返回 False（不误删）", W.reap_stale_runfile() is False)
check("运行中.json 还在", W.RUNFILE.exists())
check("界面地址.txt 还在", (W.BASE_DIR / "界面地址.txt").exists())
check("打开界面.url 还在", (W.BASE_DIR / "打开界面.url").exists())

print("== 5. pid 撞上了别的活进程，但端口没人听 → 仍判陈旧 ==")
srv.close()
check("reap 返回 True", W.reap_stale_runfile() is True)
check("运行中.json 已删", not W.RUNFILE.exists())

print("== 6. 运行记录是坏的（只写了一半）→ 也是废的 ==")
lay(None)
W.RUNFILE.write_text('{"pid": 12', encoding="utf-8")
check("reap 返回 True", W.reap_stale_runfile() is True)
check("运行中.json 已删", not W.RUNFILE.exists())
check("打开界面.url 已删", not (W.BASE_DIR / "打开界面.url").exists())

print("== 7. clear_runfile 只删属于自己那份 ==")
lay({"pid": os.getpid() + 999999, "port": free_port()})
W.clear_runfile()
check("别人的记录不碰", W.RUNFILE.exists())
W.RUNFILE.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
W.clear_runfile()
check("自己的记录删掉", not W.RUNFILE.exists())
check("界面地址.txt 一起删", not (W.BASE_DIR / "界面地址.txt").exists())

wipe()
print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
