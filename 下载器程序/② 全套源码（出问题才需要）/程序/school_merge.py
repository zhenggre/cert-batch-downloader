# -*- coding: utf-8 -*-
"""
学校名归一化模块
=====================================================================
解决的问题：同一所学校，报名表里被手填成五花八门的写法。

真实数据里的例子（来自本次比赛 120 人采样）：
    武汉市光谷第十五小学 / 武汉市光谷十五小 / 光谷十五小      → 同一所
    武汉市江岸区长春街第三小学 / 长春街三小                  → 同一所
    硚口区水厂路小学 / 水厂路小学 / 武汉市水厂路小学          → 同一所
    湖北大学附属小学 / 湖北大学附属中小学                    → 同一所

三层归并策略（层层兜底，命中率最高的是第一层）：

  第 1 层 · 人员锚定
      同一个人在名单里有「手填名」，在证书接口里有「官方名」。
      于是 手填名 → 官方名 这条映射就被自动建立起来。
      一个学校只要有一人获奖，这所学校所有手填写法就全被锚定了。

  第 2 层 · 规范化比对
      对校名做字符串规范化（剥行政区前缀 / 中文数字转阿拉伯 / 补全缩写），
      再算相似度聚类。处理「光谷十五小 vs 光谷第十五小学」这类纯写法差异。

  第 3 层 · 人工覆盖
      学校名映射.csv 里手动指定的映射优先级最高，用于修正算法判断。
      格式：原始名,规范名     （一行一条，空行和 # 开头的行忽略）

规范名的选择规则：
      簇内若有官方名 → 取出现次数最多的官方名（并列时取最长的，通常最完整）
      簇内全无官方名 → 取最长的手填名（通常写得最全）
      有官方名但写法不一致（如「硚口区水厂路小学」缺「武汉市」）→ 取最长的

对外只暴露一个入口：
    merge_schools(students, map_csv=None) -> MergeResult

    students: [{"name":…, "id":…, "raw_school":…, "official_school":…}, …]
    返回结果的每个元素多了 "school"（最终规范名）和 "school_source"（来源说明）
=====================================================================
"""
import io
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# 相似度阈值：核心串相似度 >= 此值才认为是同一所学校。
# 定得偏保守是故意的——错合并会把两所学校的孩子混进同一个文件夹，且几乎无法察觉；
# 漏合并只是文件夹分裂成两个，一眼就能看出来，还能靠人工覆盖表修。
SIM_THRESHOLD = 0.86

# 复核提示区间：相似度落在这个区间、又没达到合并门槛的，列进报告请人工过目
REVIEW_LOW = 0.62

# 中文数字
_CN_D = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
         "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 行政区划后缀：这些字结尾的词若出现在开头，属于地理位置描述，剥掉
_ADMIN_SUFFIX = ("省", "市", "区", "县", "州", "盟", "旗")

# 全角转半角
_FULL = {ord(c): ord(c) - 0xFEE0 for c in
         "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
         "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"}


def _cn_seq_to_int(s):
    """中文数字串 → 阿拉伯数字。十五→15，三→3，二十→20。转不了返回 None。"""
    if not s:
        return None
    if "十" in s:
        parts = s.split("十")
        head = parts[0]
        tail = parts[1] if len(parts) > 1 else ""
        if head and head not in _CN_D:
            return None
        if tail and tail not in _CN_D:
            return None
        left = _CN_D[head] if head else 1
        right = _CN_D[tail] if tail else 0
        return left * 10 + right
    if len(s) == 1 and s in _CN_D:
        return _CN_D[s]
    return None


def _digits_to_arabic(s):
    """把校名里的中文数字换成阿拉伯数字，并去掉『第』字。
    光谷第十五小学 → 光谷15小学 ；长春街三小 → 长春街3小"""
    def repl(m):
        v = _cn_seq_to_int(m.group(0))
        return str(v) if v is not None else m.group(0)

    s = re.sub(r"[零一二三四五六七八九十]+", repl, s)
    s = re.sub(r"第(\d+)", r"\1", s)
    return s


def _strip_admin_prefix(s):
    """反复剥掉开头的行政区划词。
    武汉市江岸区长春街小学 → 长春街小学
    硚口区水厂路小学       → 水厂路小学
    武汉经济技术开发区新华小学 → 新华小学
    注意：不能把『武汉小学』『湖北大学附属小学』剥坏——『武汉』后面不跟省/市/区/县，不动它。"""
    changed = True
    while changed and len(s) > 4:
        changed = False
        # 匹配开头 2~8 个字 + 行政区划后缀（如 武汉市 / 洪山区 / 襄阳高新技术产业开发区）
        m = re.match(r"^([\u4e00-\u9fa5]{1,8}?(?:省|市|区|县|自治州|开发区|高新区|新区))", s)
        if m:
            rest = s[m.end():]
            # 剥完至少要剩 3 个字，否则说明剥过头了（例如「武汉市区小学」这种极端）
            if len(rest) >= 3:
                s = rest
                changed = True
    return s


def _expand_abbrev(s):
    """补全常见缩写后缀。
    光谷15小 → 光谷15小学 ；XX一中 → XX1中学 ；XX附小 → XX附属小学"""
    s = re.sub(r"(?<![附中])小$", "小学", s)
    s = re.sub(r"附小$", "附属小学", s)
    s = re.sub(r"中$", "中学", s)
    s = re.sub(r"大$", "大学", s)
    return s


def core_name(school):
    """把校名压成「核心串」，用于相似度比对。"""
    if not school:
        return ""
    s = str(school).translate(_FULL)
    s = re.sub(r"[\s\u3000·、,，。.()（）\-—]+", "", s)
    s = _digits_to_arabic(s)
    s = _strip_admin_prefix(s)
    s = _expand_abbrev(s)
    return s


def _similarity(a, b):
    """纯字符串相似度。故意不给「包含关系」加成——
    因为『武珞路小学』包含于『武珞路小学金地分校』，但本部与分校是两所学校；
    『育才小学』与『育才怡康小学』同理。加成会把它们错误地拉进同一簇。"""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def city_tag(school):
    """提取市级/州级标识，用于挡住跨城市同名学校。
    黄石市中山小学 → 黄石 ；武汉市中山小学 → 武汉 ；硚口区水厂路小学 → ''（区级不算市）"""
    if not school:
        return ""
    s = str(school).translate(_FULL).strip()
    s = re.sub(r"^[\u4e00-\u9fa5]{2,5}?(?:省|自治区)", "", s)   # 先去掉省级前缀
    m = re.match(r"^([\u4e00-\u9fa5]{2,4}?)(?:市|自治州|地区)", s)
    if m:
        return m.group(1)
    m = re.match(r"^([\u4e00-\u9fa5]{2,4}?)(?:高新技术产业开发区|经济技术开发区|开发区|高新区|新区)", s)
    if m:
        return m.group(1)
    return ""


def _nums(core):
    """取核心串里的数字集合。编号是学校的本质区别——『光谷第一小学』和『光谷第二小学』
    相似度高达 0.8，但它们绝不是同一所学校。数字不一致就一票否决。"""
    return set(re.findall(r"\d+", core))

def _may_merge(a, b, ca, cb):
    """合并前的两道硬闸门，任何一条不通过都直接否决。返回 (是否放行, 原因)"""
    # 闸门 1：数字集合必须一致
    if _nums(ca) != _nums(cb):
        return False, "编号不同"
    # 闸门 2：两边都写了城市，且城市不同 → 跨市同名，拒绝
    ta, tb = city_tag(a), city_tag(b)
    if ta and tb and ta != tb:
        return False, "跨市同名(%s/%s)" % (ta, tb)
    return True, ""


def _suffix_hit(ca, cb):
    """后缀快速通道。
    校名的构造规律是「行政区划在前 + 学校主体在后」，所以后缀关系远比前缀可靠：
        硚口崇仁路小学 ↗ 崇仁路小学      → 后缀命中，同校
        武汉七一中学   ↗ 七一中学        → 后缀命中，同校
        武珞路小学     ↙ 武珞路小学金地分校 → 是前缀不是后缀，不命中（本部≠分校）
    另要求短串砍掉数字后还剩 >=3 个汉字，避免「2小学」这种退化串被当成有效主体。"""
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if len(short) < 4 or not long_.endswith(short):
        return False
    return len(re.sub(r"\d+", "", short)) >= 3


def _can_merge_pair(a, b, ca, cb, threshold=SIM_THRESHOLD):
    """单对校名的最终裁决：返回 (是否合并, 相似度, 原因码)。
    原因码：suffix 后缀命中 / sim 相似度达标 / num 编号不同 / city 跨市同名
             short 核心串过短 / low 相似度不足
    主流程和自测都调这一个函数，避免「测试和实现各判各的」这种假绿灯。"""
    r = _similarity(ca, cb)
    ok, why = _may_merge(a, b, ca, cb)
    if not ok:
        return False, r, ("num" if why == "编号不同" else "city")
    if _suffix_hit(ca, cb):
        return True, r, "suffix"
    if len(ca) >= 5 and len(cb) >= 5 and r >= threshold:
        return True, r, "sim"
    return False, r, ("short" if r >= threshold else "low")


def load_manual_map(csv_path):
    """读取人工覆盖表：原始名,规范名"""
    path = Path(csv_path)
    if not path.exists():
        return {}
    out = {}
    with io.open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                out[parts[0]] = parts[1]
    return out


class MergeResult(list):
    """list 的子类，额外带一个 report 属性。"""
    report = ""


def merge_schools(students, map_csv=None, threshold=SIM_THRESHOLD, verbose=True):
    """就地给每个学生补上 school / school_source 字段，返回带 report 的列表。"""
    manual = load_manual_map(map_csv) if map_csv else {}

    # ---------- 第 1 层：用「人」把 手填名 → 官方名 锚定起来 ----------
    anchor = defaultdict(Counter)          # 手填名 → Counter(官方名)
    for st in students:
        raw = (st.get("raw_school") or "").strip()
        off = (st.get("official_school") or "").strip()
        if raw and off:
            anchor[raw][off] += 1

    anchor_map = {}                        # 手填名 → 出现最多的官方名
    for raw, cnt in anchor.items():
        anchor_map[raw] = cnt.most_common(1)[0][0]

    # ---------- 收集所有候选名，做相似度聚类 ----------
    # 每个「名字」的统计：出现次数、作为官方名出现的次数
    name_stat = defaultdict(lambda: {"n": 0, "official": 0})
    for st in students:
        raw = (st.get("raw_school") or "").strip()
        off = (st.get("official_school") or "").strip()
        if off:
            name_stat[off]["n"] += 1
            name_stat[off]["official"] += 1
        elif raw:
            name_stat[raw]["n"] += 1

    names = list(name_stat.keys())
    cores = {n: core_name(n) for n in names}

    # 并查集
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 先按「锚定关系」强制合并：手填名 与 它的官方名 必须同簇
    for raw, off in anchor_map.items():
        if raw in parent and off in parent:
            union(raw, off)

    # 再按相似度合并（过三重闸门）
    blocked = []                           # 被闸门挡下的对，用于报告
    review = []                            # 疑似同校但没达标的对，请人工过目
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if find(a) == find(b):
                continue
            ca, cb = cores[a], cores[b]
            can, r, code = _can_merge_pair(a, b, ca, cb, threshold)
            if can:
                union(a, b)
                continue
            if r < REVIEW_LOW:
                continue
            if code == "num":
                blocked.append((a, b, r, "编号不同"))
            elif code == "city":
                blocked.append((a, b, r, "跨市同名"))
            elif code == "short":
                review.append((a, b, r, "核心串过短，信息不足未自动合并"))
            else:
                review.append((a, b, r, "相似度不足"))

    # ---------- 选规范名 ----------
    clusters = defaultdict(list)
    for n in names:
        clusters[find(n)].append(n)

    canon_of = {}                          # 名字 → 规范名
    cluster_info = []                      # 报告用
    for root, members in clusters.items():
        offs = [m for m in members if name_stat[m]["official"] > 0]
        if offs:
            # 官方名里出现最多；并列取最长
            best = max(offs, key=lambda m: (name_stat[m]["official"], len(m)))
        else:
            best = max(members, key=lambda m: (len(m), name_stat[m]["n"]))
        for m in members:
            canon_of[m] = best
        cluster_info.append({
            "canon": best,
            "members": sorted(members, key=lambda m: -name_stat[m]["n"]),
            "has_official": bool(offs),
        })

    # ---------- 第 3 层：人工覆盖（作用于最终结果）----------
    manual_applied = []
    for m in names:
        if m in manual:
            canon_of[m] = manual[m]
        elif canon_of.get(m) in manual:
            canon_of[m] = manual[canon_of[m]]
    for k, v in manual.items():
        if k not in name_stat:
            manual_applied.append((k, v, "无匹配记录"))

    # ---------- 落到每条记录上 ----------
    for st in students:
        raw = (st.get("raw_school") or "").strip()
        off = (st.get("official_school") or "").strip()
        if off and off in canon_of:
            st["school"] = canon_of[off]
            st["school_source"] = "证书官方名"
        elif raw and raw in canon_of:
            st["school"] = canon_of[raw]
            st["school_source"] = "报名表名(已锚定)" if raw in anchor_map else "报名表名(相似归并)"
        else:
            st["school"] = raw or "未知学校"
            st["school_source"] = "无学校信息"

    # ---------- 生成报告 ----------
    lines = []
    lines.append("学校名归一化报告")
    lines.append("=" * 74)
    lines.append("参与归并校名 %d 个 → 归并为 %d 所学校" % (len(names), len(clusters)))
    lines.append("")
    merged_clusters = [c for c in cluster_info if len(c["members"]) > 1]
    lines.append("【被归并的学校】共 %d 所（括号内为该写法在数据中出现的次数）" % len(merged_clusters))
    for c in sorted(merged_clusters, key=lambda x: -sum(name_stat[m]["n"] for m in x["members"])):
        tag = "证书名" if c["has_official"] else "仅手填"
        lines.append("  ▸ %s   [%s]" % (c["canon"], tag))
        for m in c["members"]:
            mark = " ★" if m == c["canon"] else "  "
            lines.append("      %s %s  ×%d" % (mark, m, name_stat[m]["n"]))
    lines.append("")
    only_one = [c for c in cluster_info if len(c["members"]) == 1]
    lines.append("【独立学校】共 %d 所（该学校在数据中只有一种写法）" % len(only_one))
    for c in sorted(only_one, key=lambda x: x["canon"]):
        lines.append("      %s  ×%d" % (c["canon"], name_stat[c["members"][0]]["n"]))
    if blocked:
        lines.append("")
        lines.append("【已拦截 · 判定为不同学校】共 %d 对（跨市同名或编号不同，故意不合并）" % len(blocked))
        for a, b, r, why in sorted(blocked, key=lambda x: -x[2]):
            lines.append("      %s  ✗  %s   相似度%.2f  原因:%s" % (a, b, r, why))
    if review:
        lines.append("")
        lines.append("【建议人工复核】共 %d 对（名字很像但没到合并门槛，若确认是同一所请在映射表里指定）"
                     % len(review))
        for a, b, r, why in sorted(review, key=lambda x: -x[2]):
            lines.append("      %s  ？  %s   相似度%.2f" % (a, b, r))
    if manual:
        lines.append("")
        lines.append("【人工覆盖表】已加载 %d 条" % len(manual))
        for k, v in manual.items():
            lines.append("      %s → %s" % (k, v))
    if manual_applied:
        lines.append("      （其中 %d 条未匹配到任何记录）" % len(manual_applied))

    result = MergeResult(students)
    result.report = "\n".join(lines)
    if verbose:
        print(result.report)
    return result


# ---------------- 自测 ----------------
if __name__ == "__main__":
    # (写法A, 写法B, 期望是否合并, 说明)
    cases = [
        # ---- 应该合并：同一所学校的不同写法（来自本次比赛真实数据）----
        ("武汉市光谷第十五小学", "武汉市光谷十五小", True, "中文数字/缩写"),
        ("武汉市光谷第十五小学", "光谷十五小", True, "缺省市前缀"),
        ("武汉市江岸区长春街第三小学", "长春街三小", True, "缩写 三小"),
        ("硚口区水厂路小学", "水厂路小学", True, "缺区级前缀"),
        ("硚口区水厂路小学", "武汉市水厂路小学", True, "缺市级前缀"),
        ("武汉市洪山区武珞路小学金地分校", "武珞路小学金地分校", True, "缺省市前缀"),
        ("武汉市光谷第二小学", "光谷二小", True, "缩写 二小"),
        ("武汉市江岸区长春街小学", "长春街小学", True, "缺省市前缀"),
        ("硚口崇仁路小学", "崇仁路小学", True, "用户举例：硚口崇仁路=崇仁路"),
        ("武汉市硚口区崇仁路小学", "崇仁路小学", True, "用户举例：官方名 vs 简称"),
        ("武汉市七一中学", "武汉七一中学", True, "缺市字"),
        # ---- 绝对不能合并：相似但不同的学校 ----
        ("黄石市中山小学", "武汉市中山小学", False, "跨市同名（关键反例）"),
        ("武汉市育才小学", "武汉市育才怡康小学", False, "同区不同校，名字是包含关系"),
        ("武汉市光谷第一小学", "武汉市光谷第二小学", False, "编号不同"),
        ("武汉市江岸区长春街小学", "武汉市江岸区长春街第三小学", False, "本校 vs 三小"),
        ("武汉市江岸区长春街小学", "武汉市江岸区长春街第二小学", False, "一小 vs 二小"),
        ("武汉市洪山区武珞路小学", "武汉市洪山区武珞路小学金地分校", False, "本部 vs 分校"),
        ("武汉小学", "武汉市光谷第二小学", False, "毫无关系"),
        ("武汉市鲁巷实验小学", "武汉市实验博雅小学", False, "词序不同的两所学校"),
        ("襄阳市第二小学", "襄阳高新技术产业开发区第二小学", False, "同城不同校"),
    ]
    ok_all = True
    print("%-32s %-30s %-7s %-7s %s" % ("写法 A", "写法 B", "相似度", "判定", "结果"))
    print("-" * 100)
    for a, b, want, desc in cases:
        ca, cb = core_name(a), core_name(b)
        got, r, code = _can_merge_pair(a, b, ca, cb)
        flag = "✅" if got == want else "❌ 失败"
        if got != want:
            ok_all = False
        print("%-32s %-30s %-7.3f %-7s %s   %s" % (
            a, b, r, "合并" if got else "不合并", flag,
            desc if got == want else "%s（期望%s，实际%s，通道=%s）" % (
                desc, "合并" if want else "不合并", "合并" if got else "不合并", code)))
    print("-" * 100)
    print("核心串示例：")
    for a, b, _, _ in cases[:3]:
        print("   %-30s → %s" % (a, core_name(a)))
    print("")
    print("总计 %d 例：%s" % (len(cases), "全部通过 ✅" if ok_all else "存在失败 ❌"))
    sys.exit(0 if ok_all else 1)
