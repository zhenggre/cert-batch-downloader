# -*- coding: utf-8 -*-
"""「自定义识别结果」的后端契约测试（不依赖外网）。

背景：2026-09-21 用户的比赛被识别成「快递信息填写」（网页标题是模板残留），
      而且证书文件字段被认成 F13、导致整批下载失败。所以名字和证书文件字段
      都必须允许用户自己改。

这条链路的"识别"那半截要联网（网站不稳定时会挂），所以这里绕开它：
直接往 PROBED 里塞一份假的识别结果，然后**真调 /api/site_add**，
看落进 config.json 的东西对不对。

验证：
  A 自定义名字生效
  B 自定义证书文件字段生效
  C 没动过的字段一个都没变
  D 站点 key 仍由 shortid 派生（改名字不会另开一个条目）
  E 加完自动设为当前比赛
  F 名字为空时不当成"改名"（回落识别结果，不写空字符串）
  G 没传 title/file_url 时，保持识别原样（老界面/老脚本调用仍然可用）
"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG = HERE.parent / "程序"
sys.path.insert(0, str(PROG))

import web_app as W          # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % name)
    else:
        FAIL += 1
        print("  [X ] %s %s" % (name, extra))


CFG = Path(W.core.ROOT) / "config.json"
BACKUP = CFG.read_text(encoding="utf-8") if CFG.exists() else None

FAKE_SITE = {
    "title": "快递信息填写",                     # ← 就是这个模板残留的标题
    "home_url": "https://biaodan100.com/q/zzzzzz",
    "query_url": "https://biaodan100.com/web/pubdata/query?SHORTID=zzzzzz",
    "shortid": "zzzzzz",
    "frmid": "FAKEFRMID",
    "field_map": {"name": "F2", "id": "F999", "award": "F9", "school": "F5",
                  "province": "F10", "city": "F11", "district": "F1",
                  "teacher": "F8", "file_url": "F13"},   # ← 认错的证书字段
    "list_columns": {"name": ["姓名"]},
}

SID = "TEST_SID_CUSTOM"
PORT = W.pick_port(8950)
HTTPD = None


def post(path, obj):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s?token=%s" % (PORT, path, W.TOKEN),
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


def seed():
    W.PROBED[SID] = dict(FAKE_SITE, field_map=dict(FAKE_SITE["field_map"]))


def cfg_now():
    return json.loads(CFG.read_text(encoding="utf-8"))


try:
    HTTPD = W.ThreadingHTTPServer(("127.0.0.1", PORT), W.Handler)
    threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("测试服务已起：127.0.0.1:%d" % PORT)

    # ---------- A/B/C/D/E：改名 + 改证书字段 ----------
    print("\n=== A~E. 改名 + 改证书文件字段，然后真的落盘 ===")
    seed()
    r = post("/api/site_add", {"sid": SID,
                               "title": "示例比赛 · 国赛（我起的名）",
                               "file_url": "URL"})
    check("接口返回 ok", r.get("ok") is True, str(r)[:200])
    check("接口回显的是我起的名字", r.get("title") == "示例比赛 · 国赛（我起的名）",
          str(r.get("title")))

    cfg = cfg_now()
    key = cfg.get("default_site", "")
    s = (cfg.get("sites") or {}).get(key, {})
    check("A 配置里的名字 = 我起的名字",
          s.get("title") == "示例比赛 · 国赛（我起的名）", str(s.get("title")))
    check("B 证书文件字段 = 我改的 URL",
          (s.get("field_map") or {}).get("file_url") == "URL",
          str((s.get("field_map") or {}).get("file_url")))
    fm = s.get("field_map") or {}
    check("C 其余字段一个没动（姓名 F2 / 学校 F5 / 区县 F1）",
          fm.get("name") == "F2" and fm.get("school") == "F5" and fm.get("district") == "F1",
          json.dumps(fm, ensure_ascii=False))
    check("D 站点 key 由 shortid 派生，没有因为改名另开条目",
          key == "site_zzzzzz", "key=" + key)
    check("E 加完自动设为当前比赛", cfg.get("default_site") == key)
    check("E2 没有攒出第二个 zzzzzz 条目",
          sum(1 for k, v in (cfg.get("sites") or {}).items()
              if (v or {}).get("shortid") == "zzzzzz") == 1)

    # ---------- F：名字传空白 ----------
    print("\n=== F. 名字传空白 → 不写空字符串，回落成识别出来的那个 ===")
    seed()
    post("/api/site_add", {"sid": SID, "title": "   ", "file_url": ""})
    s2 = (cfg_now().get("sites") or {}).get("site_zzzzzz", {})
    check("名字保持识别值（没被清成空）", s2.get("title") == "快递信息填写",
          repr(s2.get("title")))
    check("证书字段保持识别值 F13", (s2.get("field_map") or {}).get("file_url") == "F13",
          str((s2.get("field_map") or {}).get("file_url")))

    # ---------- G：老调用方式（只传 sid）仍然可用 ----------
    print("\n=== G. 只传 sid（老界面/老脚本的调法）===")
    seed()
    r3 = post("/api/site_add", {"sid": SID})
    check("仍然 ok", r3.get("ok") is True, str(r3)[:200])
    check("名字还是识别出来的那个", r3.get("title") == "快递信息填写", str(r3.get("title")))

    # ---------- H：sid 失效时的提示 ----------
    print("\n=== H. 识别结果已失效（sid 用过了）===")
    r4 = post("/api/site_add", {"sid": SID, "title": "x"})
    check("明确告知要重新识别，而不是静默", r4.get("ok") is False and "重新识别" in (r4.get("msg") or ""),
          str(r4)[:200])

finally:
    if BACKUP is not None:
        CFG.write_text(BACKUP, encoding="utf-8")
        print("\n（config.json 已还原成测试前的样子）")
    if HTTPD:
        HTTPD.shutdown()

print("\n%d 项通过，%d 项失败" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
