# -*- coding: utf-8 -*-
"""自动识别一个新的比赛站点：贴个网址就行，不用人工抓包。

怎么做到的
--------------------------------------------------------------
比赛网站（biaodan100 这一类）的表单页里嵌了一段完整的表单定义：

    var FRM = {"_id": "6a4e48dc...", "FRMNM": "示例比赛...查询",
               "FLDS": [{"LBL": "参赛学生姓名",   "NM": "F1"},
                        {"LBL": "参赛学生证件号", "NM": "F999"},
                        {"LBL": "学校名称",       "NM": "F3"},
                        {"LBL": "所在地区", ... "SUBFLDS": {"PRV": {"NM": "F16"}, ...}},
                        ...]}

`_id` 就是查询接口要的 frmid；每个字段自带「中文标签 ↔ 字段标识」的对应。
所以只要按标签关键词归类，姓名 / 证件号 / 学校 / 奖项 / 省市区 / 指导老师
就全能自动对上——之前靠人工抓包拼字段映射的那一步，程序自己就能做。

两条硬约束（都是为了"宁可失败，不可出错"）
--------------------------------------------------------------
① **识别不全就明确失败**，绝不往 config.json 写半套配置。
   同事拿着半套配置跑满 45 分钟才发现一个都没查到，代价远大于当场报错。
② **标签匹配按「具体 → 宽泛」排序，且每个字段标识只能用一次**。
   反例：`指导老师姓名（选填）` 也含「姓名」，若先匹配「姓名」就会把
   指导老师当成学生姓名——这种错跑完才发现，而且结果看着"像对的"。
"""
import json
import re
import sys

import requests

# 归一化后的目标字段 → 必备程度。missing 里的少一个就判定识别失败。
REQUIRED = ("name", "id", "award", "school", "province", "city", "district")

# 字段标签的匹配规则：**顺序即优先级**，前面的先认领，认领过的标识不再参与。
# 不能重排！例如「老师」必须排在「姓名」前面。
RULES = (
    ("id",       ("证件号", "身份证", "证件号码", "身份证件")),
    ("teacher",  ("指导老师", "指导教师", "老师", "教练")),
    ("name",     ("姓名", "学生名")),
    ("award",    ("奖项", "获奖", "奖等", "名次", "奖项名称")),
    ("school",   ("学校", "单位", "校名")),
)

PROBE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 平台指纹：只按域名认，不做任何猜测。
#
# 认出来有什么用？——让"识别失败"这件事说人话。
# 同事贴了金数据的网址，你告诉他「这是金数据，程序只认表单100」，他就知道
# 下一步该找谁；笼统一句「可能网址错了，也可能网站不认识」，他既没法自查，
# 也没法把问题转给下一个人 —— 只能回来说"它报错了"。
#
# 格式：(域名, 平台名, 是否已有适配器)
PLATFORMS = (
    (("biaodan100.com",), "表单100", True),
    # 金数据 2026-09-20 已适配：旧版 /f/x/s/y、新版 /os/x 两代都支持。
    # 识别逻辑在 platform_jinshuju.py，probe() 按域名直接转过去。
    (("jinshuju.net", "jinshuju.com"), "金数据", True),
    (("wjx.cn", "sojump.com"), "问卷星", False),
    (("mikecrm.com",), "麦客", False),
    (("wenjuan.com", "wj.qq.com"), "腾讯问卷", False),
    (("docs.qq.com",), "腾讯文档收集表", False),
    (("feishu.cn", "larksuite.com"), "飞书问卷", False),
)

# 表单页地址里的 /q/<短号>，查询接口要用
SHORTID_PAT = re.compile(r"/q/([A-Za-z0-9_\-]+)")
FRM_PAT = re.compile(r"var\s+FRM\s*=\s*(\{.*?\})\s*;\s*(?:\n|$)", re.S)


def normalize_url(raw):
    """把用户粘来的网址整成能直接抓的形式。返回 (url, 原因) —— 原因非空即非法。"""
    u = (raw or "").strip()
    if not u:
        return "", "没填网址"
    if " " in u or "\n" in u:
        u = u.split()[0]          # 有时候会连前后说明一起粘进来
    if not u.lower().startswith(("http://", "https://")):
        if "://" in u:
            return "", "只支持 http/https 开头的网址"
        u = "https://" + u        # 只粘了域名也能用
    # 去掉查询串和锚点：表单页带参数会让站点认错
    u = u.split("#")[0].split("?")[0]
    if len(u) > 300:
        return "", "网址太长了，是不是粘错东西了"
    return u, ""


def identify_platform(url):
    """按域名认平台。返回 (平台名, 是否已有适配器)；不认识返回 ("", False)。

    注意"认得出平台"不等于"能用"：金数据、问卷星这些认得出名字，
    但程序没有它们的适配器，照样跑不了。这里只负责把话说准。
    """
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower().split(":")[0]
    for doms, name, ok in PLATFORMS:
        for d in doms:
            if host == d or host.endswith("." + d):
                return name, ok
    return "", False


def _host_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


def unsupported_msg(url):
    """认不出比赛时的提示。分三种情况 —— 每种对应的"下一步动作"完全不同。

    为什么非要分：同事拿到一句笼统的"识别失败"是没法行动的。他需要知道
    "我该怎么办"以及"要转给谁、转什么"。顺带把域名写进提示里，
    他转给技术支持的时候不用再自己描述一遍。
    """
    plat, known = identify_platform(url)
    if plat and known:
        # 平台是支持的，却没读到表单定义 → 十有八九是页面贴错了
        return ("没有找到比赛信息。这个网址是「%s」平台的，但没读到某个具体比赛的"
                "查询页内容。\n"
                "  · 要贴「某个比赛」的成绩查询页（地址形如 …/q/xxxx），"
                "不是你打开平台看到的第一个页面\n"
                "  · 如果贴的确实就是查询页，可能是网站改版了 —— "
                "把这条消息连同网址发给技术支持。" % plat)
    if plat:
        return ("没有找到比赛信息。这个网址属于「%s」平台，"
                "程序目前只认「表单100」和「金数据」这两个平台。\n"
                "把这条消息连同网址一起发给技术支持："
                "他给这个平台接一次适配，\n"
                "以后这个平台的所有比赛就都能自动识别，不用每次都找他。" % plat)
    return ("没有找到比赛信息。程序不认识这个网站（%s）。\n"
            "可能是网址贴错了（要贴成绩查询页，不是首页或报名页），"
            "也可能是遇到了没见过的平台。\n"
            "把这条消息连同网址发给技术支持即可。"
            % (_host_of(url) or "看地址栏"))


def extract_frm(html):
    """从页面里抠出表单定义。抠不到返回 None。"""
    m = FRM_PAT.search(html)
    if not m:
        # 退路：不带行尾分号的写法
        m = re.search(r"var\s+FRM\s*=\s*(\{.*?\})\s*;", html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _flat_fields(flds):
    """把字段列表摊平：顶层字段 + 复合字段（如「所在地区」）的子字段。

    子字段的标签拼成「所在地区§PRV」，方便按子键（PRV/CITY/ZIP）归类。
    """
    out = []
    for f in flds or []:
        if not isinstance(f, dict):
            continue
        nm, lbl = f.get("NM"), (f.get("LBL") or "")
        sub = f.get("SUBFLDS")
        if isinstance(sub, dict) and sub:
            for sk, sv in sub.items():
                if isinstance(sv, dict) and sv.get("NM"):
                    out.append({"lbl": lbl, "sub": sk, "nm": sv["NM"],
                                "typ": f.get("TYP", "")})
        elif nm:
            out.append({"lbl": lbl, "sub": "", "nm": nm, "typ": f.get("TYP", "")})
    return out


def classify_fields(frm):
    """按标签归类字段。返回 (field_map, 备注列表)。

    只做"能明确认出来"的归类；认不出的原样留在备注里，供人核对。
    """
    flat = _flat_fields(frm.get("FLDS"))
    taken = set()
    fm = {}
    notes = []

    # ---- 复合字段「所在地区」的省 / 市 / 区 ----
    # 子键含义：PRV=省、CITY=市、ZIP 或 DTL=区县（这个网站把区县存在 ZIP 位）。
    for want, keys in (("province", ("PRV",)),
                       ("city", ("CITY",)),
                       ("district", ("ZIP", "DTL"))):
        for k in keys:
            hit = next((f for f in flat if f["sub"] == k and f["nm"] not in taken), None)
            if hit:
                fm[want] = hit["nm"]
                taken.add(hit["nm"])
                if want == "district" and k != "ZIP":
                    notes.append("区县字段是按表单子字段推断的，跑之前建议先试跑几个人核对")
                break

    # ---- 普通字段：按规则顺序认领 ----
    for want, words in RULES:
        if want in fm:
            continue
        for f in flat:
            if f["sub"] or f["nm"] in taken or not f["lbl"]:
                continue
            if any(w in f["lbl"] for w in words):
                fm[want] = f["nm"]
                taken.add(f["nm"])
                break

    # ---- 证书文件地址 ----
    # 这个网站的证书附件**固定挂在平台约定的键名 URL 上**（抓包确认过）。
    # 它不是普通表单字段 —— 不在 FLDS 里，所以上面按标签认领那一圈碰不到它。
    #
    # 规则就一句：**一律用 "URL"，不许按标签猜。**
    #
    # 这里连着踩过两次同一个坑，写下来免得再犯：
    #   ① 2026-09-21：原来只要字段 TYP 是 file/upload、或标签带「文件/附件」就抢先用它。
    #      「快递信息填写」这类模板里"上传照片""回执"满地都是 → 证书地址被指到 F13 上，
    #      下载时取到空串、抛 MissingSchema，整批被判成"下载失败"。
    #   ② 2026-09-22：把 ① 删了，却留下一条"标签里含「证书」就用它"。
    #      同一个快递模板里「证书快递地址」「证书快递收件人」「证书收件人电话」
    #      全都含「证书」→ 证书地址被指到 F13 = 「证书快递收件人」（收件人姓名！），
    #      跑起来只会把每个获奖的人都记成"下载失败"。
    #      同一个模板的其它比赛配置用的就是 "URL"。
    #
    # 结论：这个平台只有 "URL" 一个正确答案，任何"看着像"的字段都是赌。
    # 万一哪天平台不再提供它，取到的就是空串 → 会被如实判成「暂无证书文件」，
    # 总比猜错字段、跑出一堆假失败强。
    fm["file_url"] = "URL"
    if fm["file_url"] not in taken:
        taken.add(fm["file_url"])

    # ---- 备注：认不出的字段，方便人工核对有没有漏 ----
    left = [f for f in flat if f["nm"] not in taken and not f["sub"] and f["lbl"]]
    if left:
        notes.append("这些字段没被用到（正常，比如「是否入围决赛」）："
                     + "、".join("%s=%s" % (f["lbl"], f["nm"]) for f in left[:8]))
    return fm, notes


def missing_fields(fm):
    """返回缺哪些必备字段。"""
    return [k for k in REQUIRED if not fm.get(k)]


def probe(raw_url, timeout=25):
    """抓一个比赛网址，返回识别结果。

    返回 {"ok": True, "site": {...}, "notes": [...], "labels": {...}}
      或 {"ok": False, "msg": "人话原因"}
    不写任何文件——落盘由调用方在用户确认后做。
    """
    url, bad = normalize_url(raw_url)
    if bad:
        return {"ok": False, "msg": bad}

    # 金数据是另一套东西（两代产品、两套接口，页面里也没有 var FRM），
    # 交给它自己的适配器，别在这儿硬套表单100 的解析。
    try:
        import platform_jinshuju as JS
        if JS.is_jinshuju(url):
            return JS.probe(url, timeout=timeout)
    except Exception as e:            # 适配器出问题也不能让整个识别挂掉
        return {"ok": False, "msg": "识别金数据网址时出错：%s" % str(e)[:120]}

    try:
        r = requests.get(url, headers={"User-Agent": PROBE_UA,
                                       "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=timeout)
    except requests.exceptions.SSLError:
        return {"ok": False, "msg": "这个网址的证书有问题，打不开。"
                                    "请确认网址是从浏览器地址栏整行复制的。"}
    except requests.exceptions.ProxyError:
        return {"ok": False, "msg": "本机设了代理/VPN，连不出去。"
                                    "先关掉代理或 VPN 再试。"}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "msg": "连不上这个网址。检查一下网络，"
                                    "或者把网址粘到浏览器里看能不能打开。"}
    except requests.exceptions.Timeout:
        return {"ok": False, "msg": "这个网址 25 秒都没响应，"
                                    "稍后再试，或先确认浏览器能打开它。"}
    except Exception as e:
        return {"ok": False, "msg": "打开网址时出错：%s" % str(e)[:120]}

    if r.status_code != 200:
        return {"ok": False, "msg": "这个网址返回了 HTTP %d，"
                                    "请确认它是比赛的成绩查询页。" % r.status_code}

    frm = extract_frm(r.text)
    if not frm or not frm.get("_id"):
        return {"ok": False, "msg": unsupported_msg(url)}

    fm, notes = classify_fields(frm)
    lack = missing_fields(fm)
    if lack:
        cn = {"name": "姓名", "id": "证件号", "award": "奖项", "school": "学校",
              "province": "省", "city": "市", "district": "区县"}
        return {"ok": False, "msg":
                "认出了这个比赛，但有几项对不上：%s。\n"
                "这个比赛的表单和以往的不太一样，需要技术支持看一眼。"
                % "、".join(cn[k] for k in lack)}

    m = SHORTID_PAT.search(url)
    if not m:
        return {"ok": False, "msg":
                "网址里没有找到比赛编号（形如 .../q/xxxx）。\n"
                "请从浏览器地址栏整行复制再试。"}
    shortid = m.group(1)

    host = re.match(r"https?://([^/]+)", url).group(1)
    site = {
        "title": (frm.get("FRMNM") or "未命名比赛").strip(),
        "home_url": url,
        "query_url": "https://%s/web/pubdata/query?SHORTID=%s" % (host, shortid),
        "shortid": shortid,
        "frmid": frm["_id"],
        # 查询条件里带的两个字段（这个网站用 FLTS 标出来），留作交叉校验线索
        "field_map": fm,
        "list_columns": {
            "name": ["查询姓名", "参赛学生姓名", "姓名", "学生姓名", "考生姓名"],
            "id_number": ["查询证件号", "参赛学生身份证号", "证件号", "身份证号",
                          "身份证", "证件号码"],
            "school": ["学校名称", "学校", "就读学校", "所在学校"],
            "province": ["所在地区(省/自治区/直辖市)", "省份", "地区", "省"],
        },
    }
    labels = {k: v for k, v in fm.items()}
    return {"ok": True, "site": site, "notes": notes, "labels": labels,
            "home": url}


def _self_test():
    """自测：传入一个查询页网址，打印识别结果（需要联网，会真的发一次请求）。

    用法：python site_probe.py <查询页网址>
    """
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("用法：python site_probe.py <查询页网址>")
        print("例如：python site_probe.py https://biaodan100.com/q/<SHORTID>")
        return
    r = probe(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    _self_test()
