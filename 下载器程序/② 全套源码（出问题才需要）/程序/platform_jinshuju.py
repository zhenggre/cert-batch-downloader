# -*- coding: utf-8 -*-
"""金数据（jinshuju）平台适配：自动识别 + 批量查询。

为什么要单独一个文件
--------------------------------------------------------------
金数据和表单100 完全是两套东西：接口地址、请求格式、结果结构都不一样。
硬塞进 site_probe（负责识别）或 cert_downloader（负责主流程）会把两件事搅在一起，
以后换平台就得在那两个文件里到处插 if。这里把金数据自己的事全包了，
主程序只按 site["platform"] 分发过来。

两代产品，URL 一眼能分清，接口完全不同
--------------------------------------------------------------
  旧版  …/f/<表单号>/s/<查询号>   → POST https://jinshuju.com/graphql（Apollo 持久化查询）
  新版  …/os/<查询号>             → GET  同一个地址，加 header RSC: 1（Next.js 服务端渲染）

全是真跑出来的，不是猜的：
  · 旧版用官方帮助文档里的演示页 jinshuju.net/f/GbCmEV/s/JY5YpH 验证
  · 新版用官方宣传页里的公开演示 jinshuju.com/os/eHfYKT 验证
两代的查询、字段定义、结果结构都已实测对上。

一个必须提前讲清的边界
--------------------------------------------------------------
能不能下载证书，**取决于主办方有没有把证书放进「查询结果」里**。
金数据只是个查询工具：主办方在后台勾选"允许查看哪些字段"，
没勾证书附件的话，我们只能查到成绩，拿不到证书文件。这不是程序的问题，
识别阶段会明确告诉用户"这个查询页没有证书文件字段"。
"""
import json
import re
import time

import requests

PLATFORM = "jinshuju"

GQL_URL = "https://jinshuju.com/graphql"

# Apollo 持久化查询（APQ）的哈希：前端只发哈希、不发 query 文本，
# 服务端按哈希找预存的语句。这两个值是 2026-09 从线上请求里抄下来的。
# 哪天金数据改了前端，这两个值会变 —— 失效时的表现是"查询一律查不到"，
# 而不是报错，所以 query 里专门做了哈希失效的提示（见 _apq_expired）。
HASH_CONF = "603984e3d5b8f54b2ea36b3548f85291e6b77622d107d318fc2222f2e9f3fb0d"
HASH_QUERY = "4133e63f9f437b7a9e9d8490ba79932cce2ea138ad17348e6657dd728e4612f5"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# 名单里认列的候选表头（和表单100 用同一套）
LIST_COLUMNS = {
    "name": ["查询姓名", "参赛学生姓名", "姓名", "学生姓名", "考生姓名", "选手姓名"],
    "id_number": ["查询证件号", "参赛学生身份证号", "证件号", "身份证号",
                  "身份证", "证件号码"],
    "school": ["学校名称", "学校", "就读学校", "所在学校"],
    "province": ["所在地区(省/自治区/直辖市)", "省份", "地区", "省"],
}

# 字段标签归类规则：**顺序即优先级**，认领过的字段标识不再参与。
# 为什么「老师」必须排在「姓名」前面：`指导老师姓名` 也含「姓名」，
# 顺序反了会把老师当成学生 —— 而这种错跑完才能发现，且结果"看着像对的"。
RULES = (
    ("id", ("证件号", "身份证", "证件号码", "身份证件", "证件")),
    ("teacher", ("指导老师", "指导教师", "老师", "教练")),
    ("name", ("姓名", "学生名", "选手姓名")),
    ("award", ("奖项", "获奖", "奖等", "名次", "奖项名称", "等次", "荣誉")),
    ("school", ("学校", "单位", "校名")),
)

# 结果字段里，哪些算"证书/附件"
FILE_TYPE_WORDS = ("attachment", "file", "image", "picture", "photo", "upload")
FILE_LABEL_WORDS = ("证书", "附件", "文件", "图片", "照片", "证明")

# 必备只有「姓名」：能按姓名查，这个页面就对我们有用。
#
# 为什么不把证件号也列为必备？—— 金数据的查询条件是**主办方自己勾的**，
# 实测两个官方演示页分别只勾了「姓名」和「姓名+联系电话」，一个都没勾证件号。
# 强制要求证件号的话，绝大多数金数据查询页都会被判成"识别失败"，适配等于白做。
# 所以：有证件号条件就两个都发（更准），没有就只发姓名，并在备注里说明风险。
REQUIRED = ("name",)

_log = print


def set_logger(fn):
    """接上主程序的日志函数（避免和主程序循环 import）。"""
    global _log
    _log = fn


def log(msg):
    try:
        _log(msg)
    except Exception:
        pass


# ------------------------------------------------------------------
# URL 形态
# ------------------------------------------------------------------
def kind_of(url):
    """判断是哪一代产品。返回 (kind, parts)，kind 为 old / new / ""。

    两代的 URL 长得完全不一样，而接口也跟着不一样，所以这一步必须准：
      · /f/<表单号>/s/<查询号>  —— 旧版对外查询（帮助文档里叫"对外查询链接"）
      · /os/<查询号>            —— 新版对外查询（2025 年后的主推形态）
    """
    m = re.search(r"/f/([A-Za-z0-9_\-]+)/s/([A-Za-z0-9_\-]+)", url or "")
    if m:
        return "old", {"form": m.group(1), "open_search": m.group(2)}
    m = re.search(r"/os/([A-Za-z0-9_\-]+)", url or "")
    if m:
        return "new", {"os_token": m.group(1)}
    # 只有 /f/<表单号> 是填报表单（要登录、没有查询接口），不能拿来分析
    return "", {}


def is_jinshuju(url):
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower().split(":")[0]
    return host == "jinshuju.net" or host.endswith(".jinshuju.net") \
        or host == "jinshuju.com" or host.endswith(".jinshuju.com")


def wrong_page_msg(url):
    """贴错页面的提示（分情况，因为下一步动作完全不同）。"""
    if re.search(r"/f/[A-Za-z0-9_\-]+/?$", url or ""):
        return ("这是一个金数据**填报表单**的地址（…/f/xxxx），不是成绩查询页。\n"
                "要贴的是「对外查询页」：旧版形如 …/f/xxxx/s/yyyy（注意后面还有 /s/），"
                "新版形如 …/os/xxxx。\n"
                "查询页地址在主办方发给你的那条通知里，一般叫「成绩查询链接」。")
    return ("没有读出这个金数据查询页的内容。\n"
            "  · 确认贴的是「成绩查询页」，不是金数据首页或表单编辑页\n"
            "  · 旧版形如 …/f/xxxx/s/yyyy，新版形如 …/os/xxxx\n"
            "  · 如果确实是查询页，可能是网站改版了 —— 把这条消息连同网址发给技术支持。")


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
def _balanced(text, start):
    """从 start 处的 '{' 或 '[' 开始，按引号感知配对，取出完整片段。"""
    op = text[start]
    cl = "}" if op == "{" else "]"
    depth, i, in_str, esc = 0, start, False, False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == op:
                depth += 1
            elif c == cl:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


def _brace_json(text, start):
    return _balanced(text, start)


def _json_value_at(text, key):
    """直接取 `"key": {...}` 的**值**对象。

    为什么不能都用 _json_at：RSC 响应里混着 React flight 的标记
    （`$L46`、`$undefined` 之类），**包着结果的父对象往往不是合法 JSON**，
    往左找父对象会整个解析失败。而 resultsAndPagination 自己的值
    是一段纯数据，直接取它才稳。
    """
    m = re.search(r'"%s"\s*:\s*' % re.escape(key), text)
    if not m:
        return None
    i = m.end()
    if i >= len(text) or text[i] not in "{[":
        return None
    raw = _balanced(text, i)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _json_at(text, key, limit=400):
    """在一段文本（RSC 流式响应）里找出包含 key 的那个 JSON 对象。

    RSC 响应不是 JSON，是一堆 `编号:JSON` 的行。与其猜行边界，
    不如直接找 key 的位置，往前找最近的 `{` 再括号配对——
    这样网站在对象里加字段、改编号都不会影响我们。
    """
    p = text.find('"%s"' % key)
    if p < 0:
        return None
    starts = [m.start() for m in re.finditer(r"\{", text[:p])]
    for s in reversed(starts[-limit:]):
        raw = _brace_json(text, s)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    return None


def _as_url(v):
    """把一个字段值尽力解析成可下载的地址。

    附件字段的值形态我们没能拿到真样本（公开演示页里没有附件），
    所以这里按"能想到的都接住"来写：字符串直链 / 对象里的 url /
    多个附件取第一个。**接不住的时候返回空串，绝不返回看起来像 URL 的垃圾** ——
    返回错地址会让几百个人静默存下一堆打不开的文件。
    """
    if isinstance(v, str):
        s = v.strip()
        return s if s.startswith(("http://", "https://")) else ""
    if isinstance(v, dict):
        for k in ("visitUrl", "visit_url", "url", "downloadUrl", "download_url",
                  "fileUrl", "file_url", "link", "href"):
            u = v.get(k)
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                return u
        return ""
    if isinstance(v, list):
        for it in v:
            u = _as_url(it)
            if u:
                return u
    return ""


def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return str(v).strip()


# ------------------------------------------------------------------
# 会话
# ------------------------------------------------------------------
def build_session(site):
    """金数据自己的会话：先 GET 一次查询页拿 cookie（它的接口认 cookie）。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": site.get("home_url", ""),
    })
    try:
        s.get(site.get("home_url", ""), timeout=30)
    except Exception as e:
        log("预热请求失败（继续）：%s" % str(e)[:100])
    return s


# ------------------------------------------------------------------
# 拿页面配置（字段定义）
# ------------------------------------------------------------------
def _fetch_conf_old(sess, form, os_id, timeout=25):
    payload = [{"operationName": "PublishedOpenSearch",
                "variables": {"fromQueryForm": False, "fromPublished": True,
                              "showExtraConfig": True, "formId": form,
                              "openSearchId": os_id},
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH_CONF}}}]
    r = sess.post(GQL_URL,
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  timeout=timeout)
    d = r.json()
    if isinstance(d, list):
        d = d[0] if d else {}
    return (d.get("data") or {}).get("publishedOpenSearch") or {}, d


def _fetch_conf_new(sess, url, timeout=25):
    txt = _get_rsc(sess, url, timeout=timeout)
    if not txt:
        return {}, {}
    # 配置对象和结果在同一个响应里，配置里一定有 searchFields
    conf = _json_at(txt, "searchFields")
    return conf or {}, {}


def _get_rsc(sess, url, timeout=25, q=None):
    """新版取数：加 RSC: 1 头，服务端会把结果一并渲染进流式响应里。

    编码必须自己指定：这个响应不带 charset，requests 会按 latin-1 猜，
    中文全变乱码 —— 且这种错不会报错，只会让你以为"查不到人"。
    """
    u = url if not q else url + ("&" if "?" in url else "?") + "q=" + q
    try:
        r = sess.get(u, headers={"RSC": "1"}, timeout=timeout)
    except Exception as e:
        log("RSC 请求失败：%s" % str(e)[:100])
        return ""
    try:
        return r.content.decode("utf-8", errors="replace")
    except Exception:
        return r.text or ""


def _fields_of(conf, kind):
    """从配置里拆出「查询条件字段」和「结果字段」，统一成 [{apiCode,label,type,protected}]。"""
    if kind == "old":
        cond = []
        for n in (conf.get("combinationsFields") or {}).get("nodes", []) or []:
            cond.append({"apiCode": n.get("apiCode"), "label": n.get("label") or "",
                         "type": n.get("type") or "", "protected": False,
                         "sms": False})
        res = []
        for n in (conf.get("readableFields") or {}).get("nodes", []) or []:
            res.append({"apiCode": n.get("apiCode"), "label": n.get("label") or "",
                        "type": n.get("type") or "", "protected": False,
                        "sms": False})
        return cond, res
    # 新版
    cond = []
    for f in conf.get("searchFields") or []:
        cond.append({"apiCode": f.get("apiCode"), "label": f.get("label") or "",
                     "type": f.get("type") or "", "protected": False,
                     "sms": bool(f.get("smsVerification"))})
    res = []
    for r in conf.get("displayFieldRules") or []:
        f = r.get("field") or {}
        res.append({"apiCode": f.get("apiCode"), "label": f.get("label") or "",
                    "type": f.get("type") or "",
                    "protected": bool(r.get("protected")), "sms": False})
    return cond, res


def _classify(cond, res):
    """按标签归类：(查询条件映射, 结果映射, 备注)。

    两代产品的字段都在同一套 label 体系里，所以归类规则共用。
    """
    taken, cond_map, res_map, notes = set(), {}, {}, []

    # 先看查询条件：只有被主办方设为条件的字段才能用来查
    for want, words in RULES:
        for f in cond:
            if f["apiCode"] in taken or not f["label"]:
                continue
            if any(w in f["label"] for w in words):
                cond_map[want] = f["apiCode"]
                taken.add(f["apiCode"])
                break

    # 结果字段：奖项 / 学校 / 省市区 / 老师 / 证书
    for want, words in RULES:
        if want in res_map:
            continue
        for f in res:
            if f["apiCode"] in taken or not f["label"]:
                continue
            if any(w in f["label"] for w in words):
                res_map[want] = f["apiCode"]
                break

    # 省 / 市 / 区：金数据有复合地址字段，也有分开的三个字段
    for want, words in (("province", ("省",)), ("city", ("市",)), ("district", ("区", "县"))):
        if want in res_map:
            continue
        for f in res:
            if f["apiCode"] in {v for k, v in res_map.items()} or not f["label"]:
                continue
            if any(w in f["label"] for w in words):
                res_map[want] = f["apiCode"]
                break

    # 证书文件：类型是附件/图片，或标签里写明了
    for f in res:
        typ = (f["type"] or "").lower()
        if any(w in typ for w in FILE_TYPE_WORDS) or \
                any(w in f["label"] for w in FILE_LABEL_WORDS):
            res_map["file_url"] = f["apiCode"]
            break

    # 姓名/证件号如果在结果里也有，用结果的（用于回写、脱敏判断）
    for want in ("name", "id", "award", "school", "province", "city", "district",
                 "teacher"):
        if want in res_map and want in cond_map and res_map[want] != cond_map[want]:
            notes.append("%s：查询条件用 %s、结果用 %s（两个字段不同，已分别记录）"
                         % (want, cond_map[want], res_map[want]))
    return cond_map, res_map, notes


# ------------------------------------------------------------------
# 识别：贴网址 → 出配置（不写盘，用户确认后才落盘）
# ------------------------------------------------------------------
def extra_cond_msg(cond, cond_map):
    """主办方勾了我们填不出来的查询条件时，返回提示；能全填出来则返回 ""。

    抽成函数是为了能离线测 —— 这种情况一旦漏判，表现是"600 人全部查不到"，
    而同事只会以为孩子们都没获奖。
    """
    used = set(cond_map.values())
    extra = [f for f in cond if f["apiCode"] not in used and f["label"]]
    if not extra:
        return ""
    names = "、".join(f["label"] for f in extra)
    have = "、".join(f["label"] for f in cond if f["apiCode"] in used) or "（无）"
    return ("这个查询页要求同时输入：%s、%s。\n"
            "名单里只有「姓名」和「证件号」，填不出「%s」，所以一个人也查不到。\n"
            "  · 找主办方把查询条件改成只用「姓名」或「姓名+证件号」\n"
            "  · 或者把网址发给技术支持，让他看看能不能把这个条件接上"
            % (have, names, names))


def cert_file_field(site):
    """这个比赛有没有「证书文件」字段。没有就不该进下载阶段。

    金数据这类平台上，主办方没把证书放进可查询的结果里是很常见的，
    这时候一条都下不到 —— 与其每人报一次"下载失败"，不如干脆不进下载。
    """
    return ((site.get("result_map") or {}).get("file_url")
            or (site.get("field_map") or {}).get("file_url") or "")


def probe(raw_url, timeout=25):
    """识别一个金数据查询页。返回结构同 site_probe.probe。"""
    url = (raw_url or "").strip().split("#")[0].split("?")[0]
    kind, parts = kind_of(url)
    if not kind:
        return {"ok": False, "msg": wrong_page_msg(url)}

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Content-Type": "application/json;charset=UTF-8",
                         "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        first = sess.get(url, timeout=timeout)   # 拿 cookie，也让服务端认识这个会话
    except Exception as e:
        return {"ok": False, "msg": "打开网址时出错：%s" % str(e)[:120]}

    # 关键：jinshuju.com/os/xxx 会被 302 到 mkt.jinshuju.com/os/xxx，
    # 而**只有最终那个域名**才认 RSC 请求（打在原域名上会拿到一份不含数据的响应，
    # 而且照样返回 200 —— 表现为"能打开但查不到人"，极难排查）。所以一律用最终地址。
    final = (first.url or url).split("#")[0].split("?")[0]

    conf, raw = ({}, {})
    if kind == "old":
        conf, raw = _fetch_conf_old(sess, parts["form"], parts["open_search"], timeout)
        if not conf:
            return {"ok": False, "msg": wrong_page_msg(url)}
        title = conf.get("title") or ""
        if conf.get("passwordRequired"):
            return {"ok": False, "msg":
                    "这个查询页设了访问密码（只有拿到密码的人才能查）。\n"
                    "找主办方要密码；本工具没法绕过密码。"}
    else:
        txt = _get_rsc(sess, final, timeout=timeout)
        if not txt:
            return {"ok": False, "msg": wrong_page_msg(url)}
        conf = _json_at(txt, "searchFields") or {}
        if not conf:
            return {"ok": False, "msg": wrong_page_msg(url)}
        title = conf.get("name") or conf.get("title") or ""
        if conf.get("passwordRequired") or conf.get("accessPassword"):
            return {"ok": False, "msg":
                    "这个查询页设了访问密码（只有拿到密码的人才能查）。\n"
                    "找主办方要密码；本工具没法绕过密码。"}

    cond, res = _fields_of(conf, kind)
    cond_map, res_map, notes = _classify(cond, res)

    # 短信验证 = 必须收短信才能查 → 明确做不了，别让他白跑
    for f in cond:
        if f["sms"] and f["apiCode"] in cond_map.values():
            return {"ok": False, "msg":
                    "这个查询页的「%s」开了短信验证（要用手机收验证码才能查）。\n"
                    "这类验证本工具做不了 —— 找主办方关掉短信验证，或改用别的查询条件。"
                    % f["label"]}

    lack = [k for k in REQUIRED if not cond_map.get(k)]
    if lack:
        cn = {"name": "姓名", "id": "证件号"}
        return {"ok": False, "msg":
                "认出了这个比赛（%s），但查询条件里没有%s。\n"
                "主办方配置的是这些条件：%s。\n"
                "只能按主办方设的条件查 —— 把这个网址发给技术支持看一眼。"
                % (title or "未命名", "、".join(cn[k] for k in lack),
                   "、".join(f["label"] for f in cond if f["label"]) or "（读不到）")}

    # 主办方勾了**两个以上**条件时，必须全都填对才查得到（官方文档：「添加条件」
    # = 填表人必须填写多个条件才能查询）。名单里只有姓名和证件号，
    # 多出来一个「联系电话」之类的，我们一个都查不到 ——
    # 这个必须当场说清，否则同事跑完 600 人全是"未获奖"，还以为真是这样。
    bad = extra_cond_msg(cond, cond_map)
    if bad:
        return {"ok": False, "msg": bad}

    # 查询条件用 cond_map（只有主办方设为条件的字段才发得出去）；
    # 结果取值优先用结果侧自己的字段——两者偶尔不是同一个（比如条件是"姓名"，
    # 结果里另有一个"选手姓名"），混用会取到空值。
    fm = dict(cond_map)
    res_only = {}
    for k, v in res_map.items():
        if k in ("name", "id") and cond_map.get(k) and cond_map[k] != v:
            res_only[k] = v
        else:
            fm[k] = v

    # ---- 该说清楚的边界，一条都别省 ----
    if not cond_map.get("id"):
        notes.append("※ 这个查询页的查询条件里没有证件号（主办方只勾了：%s），"
                     "所以只能按姓名查。\n"
                     "  同名的人可能查到同一条结果 —— 名单里有重名的话，"
                     "跑完请抽查几个。" % "、".join(f["label"] for f in cond if f["label"]))
    if not res_map.get("file_url"):
        notes.append("※ 这个查询页没有证书文件字段 —— 能批量查到成绩并出汇总清单，"
                     "但下载不了证书。要让主办方在「对外查询」的结果里勾选证书附件。")
    if not res_map.get("school"):
        notes.append("※ 结果里没有学校字段 —— 证书不会按学校分文件夹，会全部放在一起。")
    if not res_map.get("award"):
        notes.append("※ 结果里没有奖项字段 —— 汇总清单里奖项那一列会是空的。")

    masked = [f["label"] for f in res if f["protected"]]
    if masked:
        notes.append("※ 主办方把「%s」设成了脱敏显示（查出来是「张*」这种打码的样子）。\n"
                     "  这是网站设置，程序改不了：证书文件名会是打码的名字，"
                     "按名单原样命名更保险。" % "、".join(masked))
    if kind == "old" and (conf.get("fuzzyMode")):
        notes.append("※ 这个查询页开了模糊匹配：一个名字可能匹配到多条，"
                     "程序取第一条，请抽查几个核对。")

    site = {
        "platform": PLATFORM,
        "title": (title or "未命名比赛").strip(),
        # 查询请求打在最终域名上（见 probe 里那段说明），
        # home_url 保留用户贴的原始地址，界面上显示他熟悉的那一个。
        "home_url": url,
        "query_url": GQL_URL if kind == "old" else final,
        "jinshuju": dict(kind=kind, **parts),
        "field_map": fm,
        "result_map": res_only,
        "list_columns": LIST_COLUMNS,
    }
    labels = {k: v for k, v in fm.items()}
    return {"ok": True, "site": site, "notes": notes, "labels": labels, "home": url}


# ------------------------------------------------------------------
# 查询一个人
# ------------------------------------------------------------------
def _apq_expired(d):
    """判断是不是 APQ 哈希失效（金数据改前端后会这样）。"""
    if not isinstance(d, dict):
        return False
    errs = d.get("errors")
    if not errs:
        return False
    return any("PersistedQueryNotFound" in str(e.get("message", "")) or
               "PersistedQuery" in str(e.get("message", "")) for e in errs
               if isinstance(e, dict))


def query_one(sess, site, name, idnum, retry=3):
    """查一个人 → (hit / miss / error, 信息字典)。与 cert_downloader.query_one 同构。"""
    js = site.get("jinshuju") or {}
    fm = site["field_map"]
    kind = js.get("kind") or kind_of(site.get("home_url", ""))[0]

    # 按主办方配置的条件组装：配了证件号就两个都发，没配就只发姓名。
    # 旧版多条件是"同组内 AND"（官方文档：添加条件 = 必须全部匹配）。
    cond = {}
    if fm.get("name"):
        cond[fm["name"]] = name
    if fm.get("id") and idnum:
        cond[fm["id"]] = idnum
    if not cond:
        return "error", {"err": "这个比赛的查询条件字段没配好（缺姓名）", "kind": "unknown"}

    rm = site.get("result_map") or {}

    def get(row, key):
        code = rm.get(key) or fm.get(key)
        return _as_text(row.get(code)) if code else ""

    def pick(rows):
        """多条结果时挑最可能是本人的那条。

        只按姓名查（主办方没勾证件号）时最容易撞名。这时如果结果里
        恰好能看到证件号，就拿它比对；看不到就取第一条并标记 _multi，
        让汇总清单里能看出来"这条是猜的"。
        """
        if len(rows) <= 1 or not idnum:
            return rows[0], False
        code = rm.get("id") or fm.get("id")
        if code:
            want = re.sub(r"\s", "", idnum).upper()
            for r in rows:
                if re.sub(r"\s", "", _as_text(r.get(code))).upper() == want:
                    return r, False
        return rows[0], True

    last, kind_err = "", ""
    for attempt in range(retry):
        try:
            if kind == "old":
                rows, total, raw = _query_old(sess, js, cond)
            else:
                rows, total, raw = _query_new(sess, site, cond)
            if not rows:
                return "miss", {"total": 0}
            row, multi = pick(rows)
            total = total or len(rows)
            file_url = ""
            fcode = rm.get("file_url") or fm.get("file_url")
            if fcode:
                file_url = _as_url(row.get(fcode))
            return "hit", {
                "award": get(row, "award"), "school": get(row, "school"),
                "province": get(row, "province"), "city": get(row, "city"),
                "district": get(row, "district"), "teacher": get(row, "teacher"),
                "file_url": file_url, "total": total,
                "_multi": multi or total > 1,
                "_raw": raw,
            }
        except Exception as e:
            kind_err = "net"
            last = "%s: %s" % (type(e).__name__, str(e)[:120])
            time.sleep(2.0 * (attempt + 1))
    return "error", {"err": last, "kind": kind_err}


def _query_old(sess, js, cond):
    payload = [{"operationName": "QuerySearchEntriesCount",
                "variables": {"formToken": js.get("form"),
                              "openSearchId": js.get("open_search"),
                              "queries": [cond], "forceProtected": False},
                "extensions": {"persistedQuery": {"version": 1,
                                                  "sha256Hash": HASH_QUERY}}}]
    r = sess.post(GQL_URL,
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  timeout=40)
    if r.status_code in (403, 429) or r.status_code >= 500:
        raise RuntimeError("HTTP %d" % r.status_code)
    d = r.json()
    if isinstance(d, list):
        d = d[0] if d else {}
    if _apq_expired(d):
        raise RuntimeError("金数据接口变了（APQ 哈希失效），请把网址发给技术支持")
    conn = (d.get("data") or {}).get("publishedOpenSearchEntries") or {}
    return (conn.get("nodes") or []), conn.get("totalCount") or 0, d


def _q_str(cond):
    """新版查询串：`字段名:值`，多个条件用 `+` 连接（实测形态）。"""
    import urllib.parse
    parts = []
    for k, v in cond.items():
        parts.append("%s:%s" % (k, urllib.parse.quote(str(v), safe="")))
    return "+".join(parts)


def _query_new(sess, site, cond):
    base = site.get("query_url") or site.get("home_url")
    txt = _get_rsc(sess, base, q=_q_str(cond))
    if not txt:
        raise RuntimeError("取不到查询结果（页面请求失败）")
    res = (_json_value_at(txt, "resultsAndPagination")
           or _json_at(txt, "resultsAndPagination") or {})
    rows = []
    for it in (res.get("results") or []):
        rows.append(it.get("entryValues") or it.get("fieldValues") or {})
    return rows, (res.get("totalCount") or res.get("total") or len(rows)), res


def _self_test():
    """联网自测：识别 + 查一个人（用的是官方公开演示页）。"""
    import sys
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    for u in ("https://jinshuju.net/f/GbCmEV/s/JY5YpH",
              "https://jinshuju.com/os/eHfYKT"):
        print("=" * 60)
        print(u)
        r = probe(u)
        print(json.dumps({k: v for k, v in r.items() if k != "site"},
                         ensure_ascii=False, indent=1))
        if not r.get("ok"):
            continue
        s = build_session(r["site"])
        print("--> query:", query_one(s, r["site"], "小金", "")[0:1])
    # 下面这一组是金数据官方公开演示页自带的测试数据（姓名 李一凡、
    # 号码 138****5678 都是演示页公开写着的），不是任何真实学生信息。
    print("OS demo: 李一凡 / 13812345678")
    r = probe("https://jinshuju.com/os/eHfYKT")
    if r.get("ok"):
        s = build_session(r["site"])
        code, info = query_one(s, r["site"], "李一凡", "13812345678")  # demo 数据：官方演示页公开写着的样例
        print("-->", code, json.dumps({k: v for k, v in info.items() if k != "_raw"},
                                      ensure_ascii=False))


if __name__ == "__main__":
    _self_test()
