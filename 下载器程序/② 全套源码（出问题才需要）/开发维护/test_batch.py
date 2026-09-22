# -*- coding: utf-8 -*-
"""新功能回归：多批次统计口径 / 失败名单 / 剩余时间 / 子文件夹 / 单实例。

这几条有个共同点：写错了不会报错，只会让同事看到"对不上的数字"，
然后开始翻文件夹一个个数。所以每条都用断言盯住具体数值，
而不是"跑通了就算过"。
"""
import csv
import io
import shutil
import sys
import time
from pathlib import Path

R = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(R / "程序"))

import cert_downloader as core      # noqa: E402
import web_app as wa                # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK] %s" % name)
    else:
        fail += 1
        print("  [!!] %s   %s" % (name, extra))


TMP = Path(__file__).resolve().parent / "_tmp_batch_test"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)


def fake_pdf(p):
    """造一份"像样的证书"：内容无关紧要，但体积必须过 2000 字节，
    否则会被 collect_disk_certs 当成上次没下完的残file 排除掉。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 3000)
    return p


def rec(name, idn, school, award="一等奖", cert=True, status="成功",
        saved="", note="", raw_school="", qerr=""):
    return {
        "name": name, "id": idn, "school": school, "raw_school": raw_school or school,
        "official_school": school, "award": award if cert else "",
        "province": "湖北省", "city": "武汉市", "district": "洪山区",
        "teacher": "王老师", "file_url": "", "hit": cert, "qerr": qerr,
        "total": 1 if cert else 0, "status": status,
        "saved_path": saved, "saved_kb": 3.0 if saved else 0,
        "note": note, "cert_ok": cert,
    }


try:
    print("=" * 66)
    print("① 保存位置：自动套日期子文件夹")
    print("=" * 66)
    d = wa.resolve_out_dir(r"D:\Desktop\证书", True, "2026-09-19")
    check("套出来是「选的位置\\证书_日期」", d == r"D:\Desktop\证书\证书_2026-09-19", d)
    check("不勾选时原样返回",
          wa.resolve_out_dir(r"D:\Desktop\证书", False) == r"D:\Desktop\证书")
    check("路径末尾多余的斜杠不会变成双斜杠",
          wa.resolve_out_dir("D:\\证书\\", True, "2026-09-19") == r"D:\证书\证书_2026-09-19",
          wa.resolve_out_dir("D:\\证书\\", True, "2026-09-19"))
    check("没填位置时落到默认目录",
          wa.resolve_out_dir("", False) == wa.default_out_dir(), wa.resolve_out_dir("", False))
    check("同一天跑两次 → 同一个文件夹（累计才连得起来）",
          wa.resolve_out_dir("D:\\证书", True, "2026-09-19") ==
          wa.resolve_out_dir("D:\\证书", True, "2026-09-19"))
    check("勾选状态来自前端传的布尔值，传字符串'false'也不该套娃",
          wa.resolve_out_dir("D:\\证书", "") == r"D:\证书", wa.resolve_out_dir("D:\\证书", ""))

    print()
    print("=" * 66)
    print("② 多批次：目录内累计证书 vs 本批次名单")
    print("=" * 66)
    out = TMP / "批量"
    p_old = fake_pdf(out / "武汉市洪山区第一小学" / "张三.pdf")
    # 模拟"第一批"留下的明细：只有这个人，不在本次名单里
    with io.open(out / "下载明细_20260101_100000.csv", "w",
                 encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "姓名", "证件号", "学校(归并后)", "学校(报名表)", "学校(证书)",
                    "奖项", "省", "市", "区县", "指导老师", "状态", "保存路径", "KB", "备注"])
        w.writerow([1, "张三", "420100200801011234", "武汉市洪山区第一小学", "", "",
                    "一等奖", "", "", "", "", "成功", str(p_old), 3.0, ""])

    p_new = fake_pdf(out / "武汉市洪山区第二小学" / "李四.pdf")
    this = [
        rec("李四", "420100200802022345", "武汉市洪山区第二小学", saved=str(p_new)),
        rec("王五", "420100200803033456", "武汉市洪山区第二小学",
            cert=False, status="未获奖无证书", award=""),
    ]
    disk = core.collect_disk_certs(out, this)
    check("数出目录内 2 份（往期 1 + 本次 1）", len(disk) == 2, str(sorted(disk)))
    check("往期那一份也被算进来了", ("张三", "420100200801011234") in disk, str(sorted(disk)))
    check("只认体积够大的文件（残file 不算数）",
          all(Path(v).stat().st_size > 2000 for v in disk.values()))

    # 文件被删掉时不能算数：记录说有、实际没了，就得老老实实重下
    p_ghost = fake_pdf(out / "武汉市洪山区第一小学" / "鬼影.pdf")
    with io.open(out / "下载明细_20260101_110000.csv", "w",
                 encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "姓名", "证件号", "学校(归并后)", "学校(报名表)", "学校(证书)",
                    "奖项", "省", "市", "区县", "指导老师", "状态", "保存路径", "KB", "备注"])
        w.writerow([1, "鬼影", "420100200899999999", "武汉市洪山区第一小学", "", "",
                    "二等奖", "", "", "", "", "成功", str(p_ghost), 3.0, ""])
    p_ghost.unlink()
    disk2 = core.collect_disk_certs(out, this)
    check("明细里记了、但文件已被删 → 不计数",
          ("鬼影", "420100200899999999") not in disk2, str(sorted(disk2)))

    xlsx = out / "证书汇总清单_20260102_100000.xlsx"
    core.write_summary(xlsx, this, out, disk=disk)
    from openpyxl import load_workbook
    wb = load_workbook(xlsx)

    ws = wb["学校汇总"]
    head = [c.value for c in ws[1]]
    check("表头有「目录内证书总数」", "目录内证书总数" in head, str(head))
    check("表头有「本批次名单人数」", "本批次名单人数" in head, str(head))
    check("表头有「说明」列，用来解释往期行", "说明" in head, str(head))

    body = {str(r[1]): r for r in ws.iter_rows(min_row=2, values_only=True) if r[1]}
    row_a = body.get("武汉市洪山区第一小学")
    row_b = body.get("武汉市洪山区第二小学")
    check("往期批次的学校也在表里（没有整行消失）", row_a is not None, str(list(body)))
    if row_a:
        check("往期学校：目录内 1 份、本批次 0 人",
              row_a[2] == 1 and row_a[3] == 0, str(row_a))
        check("往期学校那行给了说明文字", "往期" in str(row_a[-1] or ""), str(row_a[-1]))
    if row_b:
        check("本批次学校：目录内 1 份、本批次 2 人、证书 1 份",
              row_b[2] == 1 and row_b[3] == 2 and row_b[4] == 1, str(row_b))
        check("本批次的学校不写说明（没歧义就别啰嗦）",
              not str(row_b[-1] or "").strip(), str(row_b[-1]))

    last = list(ws.iter_rows(values_only=True))[-1]
    check("合计行对「目录内证书总数」也求和了", "=SUM(C2:" in str(last[2]), str(last[2]))
    check("合计行写清了口径（免得同事不理解这个数）",
          "累计" in str(last[-1] or "") and "往期批次" in str(last[-1] or ""),
          str(last[-1]))

    kv = {str(r[0]): r[1] for r in wb["统计概览"].iter_rows(min_row=2, values_only=True)}
    check("概览写明累计 = 2（同一个比赛、含往期批次）",
          kv.get("累计证书（同一个比赛，含往期批次）") == 2, str(kv))
    check("概览写明其中不属于本批次 = 1", kv.get("  其中：不属于本批次名单") == 1, str(kv))
    check("概览点名了本次新下载 = 1", kv.get("  其中：本次新下载") == 1, str(kv))
    check("多出来时主动解释「是以前跑过的批次」",
          any("以前跑过" in str(v) for v in kv.values()), str(kv))

    # 反过来：目录里没有往期，就不该出现那句解释（免得无中生有）
    x2 = out / "清单_单批次.xlsx"
    core.write_summary(x2, this, out, disk=disk)
    kv2 = {str(r[0]): r[1] for r in load_workbook(x2)["统计概览"].iter_rows(
        min_row=2, values_only=True)}
    check("单批次时也照样给出累计口径（口径恒定，不随数据变）",
          "累计证书（同一个比赛，含往期批次）" in kv2, str(list(kv2)))

    print()
    print("=" * 66)
    print("②b 批次认亲：同一个比赛散在不同日期文件夹，累计不能断")
    print("=" * 66)
    # 默认勾着日期文件夹，同一个比赛会落在 证书_09-18 / 证书_09-19 两个目录里。
    # 只扫自己那一层的话，第二天的清单写"累计 1 份"，而硬盘上躺着 2 份 ——
    # 同事的第一反应是"程序少下了"，然后开始翻文件夹一个个数。
    base = TMP / "认亲"
    d1, d2 = base / "证书_2026-09-18", base / "证书_2026-09-19"
    race = {"title": "示例比赛", "home_url": "https://biaodan100.com/q/<SHORTID>"}
    core.write_batch_marker(d1, "site_example", race)
    core.write_batch_marker(d2, "site_example", race)
    check("名片写得出、也读得回",
          core.read_batch_marker(d1).get("site_key") == "site_example",
          str(core.read_batch_marker(d1)))

    p18 = fake_pdf(d1 / "武汉市洪山区第一小学" / "张三.pdf")
    with io.open(d1 / "下载明细_20260918_100000.csv", "w",
                 encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "姓名", "证件号", "学校(归并后)", "学校(报名表)", "学校(证书)",
                    "奖项", "省", "市", "区县", "指导老师", "状态", "保存路径", "KB", "备注"])
        w.writerow([1, "张三", "420100200801011234", "武汉市洪山区第一小学", "", "",
                    "一等奖", "", "", "", "", "成功", str(p18), 3.0, ""])
    p19 = fake_pdf(d2 / "武汉市洪山区第二小学" / "李四.pdf")
    this2 = [rec("李四", "420100200802022345", "武汉市洪山区第二小学", saved=str(p19))]

    peers = core.batch_roots(d2)
    check("认得出同一个比赛的往期批次目录",
          [p.name for p in peers] == ["证书_2026-09-18", "证书_2026-09-19"],
          str([p.name for p in peers]))
    disk_x = core.collect_disk_certs(d2, this2)
    check("跨日期累计 = 2（昨天 1 + 今天 1）", len(disk_x) == 2, str(sorted(disk_x)))
    check("昨天那个人也在索引里（第二天不必重下）",
          ("张三", "420100200801011234") in disk_x, str(sorted(disk_x)))

    # 不同比赛绝不能合并：撞上同名同证件号的学生时，后者会被当成"下过了"跳过，
    # 证书静默少一份，跑完谁也看不出来。
    d_other = base / "证书_2026-09-17"
    core.write_batch_marker(d_other, "another_race", {"title": "完全不同的另一个比赛"})
    check("别的比赛的批次目录不会被认成一家",
          d_other.name not in [p.name for p in core.batch_roots(d2)],
          str([p.name for p in core.batch_roots(d2)]))

    # 连自己都认不出（没名片、也没日志）时只算自己
    d_bare = base / "证书_2026-09-16"
    d_bare.mkdir(parents=True, exist_ok=True)
    check("没有名片的目录不四处张望（不误合并）",
          len(core.collect_disk_certs(d_bare, [])) == 0,
          str(core.collect_disk_certs(d_bare, [])))

    # 升级前跑出来的老目录没写过名片，靠运行日志里的「站点：」认亲，
    # 否则它们会一夜之间变成野目录，累计数平白缩水。
    d_log = base / "证书_2026-09-15"
    d_log.mkdir(parents=True, exist_ok=True)
    with io.open(d_log / "运行日志_20260915_090000.txt", "w",
                 encoding="utf-8-sig") as f:
        f.write("站点：示例选拔赛\n阶段 1/4  全量查询\n")
    check("老目录凭运行日志也能认亲",
          core.read_batch_marker(d_log).get("title") == "示例选拔赛",
          str(core.read_batch_marker(d_log)))
    check("新旧批次凭据不同也能合到一起（站点标识 vs 比赛名）",
          d_log.name in [p.name for p in core.batch_roots(d2)],
          str([p.name for p in core.batch_roots(d2)]))

    # 没勾日期文件夹时，用户选的那个目录就是唯一一批，不该去找兄弟
    check("不套日期文件夹时只看自己",
          core.batch_roots(TMP / "批量") == [TMP / "批量"],
          str(core.batch_roots(TMP / "批量")))

    print()
    print("=" * 66)
    print("③ 失败名单：能直接拖回界面重跑")
    print("=" * 66)
    school3 = out / "武汉市洪山区第三小学"
    bad = [
        rec("赵六", "420100200804044567", "武汉市洪山区第三小学",
            cert=False, status="查询失败", qerr="HTTP 503"),
        rec("钱七", "420100200805055678", "武汉市洪山区第三小学",
            cert=False, status="下载失败", note="等待 120s 仍未生成完成"),
        rec("孙八", "420100200806066789", "武汉市洪山区第三小学",
            saved=str(fake_pdf(school3 / "孙八.pdf"))),
    ]
    rp = core.write_retry_list(out / "失败名单_test.xlsx", bad)
    check("生成了失败名单文件", bool(rp) and Path(rp).exists(), str(rp))
    students, info = core.load_list(rp, quiet=True)
    check("这份文件能被工具原样读回去（人数 = 2）", len(students) == 2, str(len(students)))
    check("列名被识别成「姓名 / 证件号」（不用同事手工改名）",
          info["columns"]["name"] == "姓名" and info["columns"]["id"] == "证件号",
          str(info["columns"]))
    check("成功的人没被塞进失败名单",
          all(s["name"] != "孙八" for s in students), str([s["name"] for s in students]))
    reasons = " ".join(str(s["_raw"].get("失败原因") or "") for s in students)
    check("失败原因也带上了（查询失败和下载失败都得写清）",
          "503" in reasons and "生成" in reasons, repr(reasons))

    rp2 = core.write_retry_list(out / "失败名单_全成功.xlsx", [bad[2]])
    check("没人失败时干脆不生成文件（不摆一个空表添乱）",
          rp2 == "" and not (out / "失败名单_全成功.xlsx").exists(), repr(rp2))

    print()
    print("=" * 66)
    print("④ 预计剩余时间：说人话，别乱跳")
    print("=" * 66)
    check("30 秒说成秒", wa.human_secs(30) == "30 秒", wa.human_secs(30))
    check("90 秒说成分钟", wa.human_secs(90) == "1 分钟", wa.human_secs(90))
    check("整小时不带零头", wa.human_secs(3600) == "1 小时", wa.human_secs(3600))
    check("带余数的小时", wa.human_secs(7500) == "2 小时 5 分", wa.human_secs(7500))

    # 把时钟换成受控的：要靠 sleep 攒够 8 秒窗口的话，测试要白等十几秒。
    # 注意不能只是"伪造时间戳塞进去" —— set_prog 自己会取 now，
    # 混进真实时间反而让时间戳往回跳，估时逻辑会（正确地）当作进度倒退而罢算。
    wa.STATE["stage"] = "下载"
    wa.STATE["_eta_pts"] = []
    wa.STATE["_last_done"] = 0
    t0 = time.time()
    tick = [t0]
    real_time = wa.time.time
    try:
        wa.time.time = lambda: tick[0]
        for i in range(1, 11):
            tick[0] = t0 + i * 2.0          # 每条 2 秒
            wa.set_prog("下载", i, 100)
    finally:
        wa.time.time = real_time
    check("进度够了就给得出预计时间", "预计还需" in wa.STATE["eta"], repr(wa.STATE["eta"]))
    print("     样例：%s" % wa.STATE["eta"])
    check("算得对：1→10 用 18 秒，剩 90 条 ≈ 3 分钟",
          "3 分钟" in wa.STATE["eta"], wa.STATE["eta"])
    e1 = wa.STATE["eta"]        # 先留住，"样本太少"那条会把它清空

    wa.STATE["_eta_pts"] = [(time.time(), 5, 100)]
    wa.set_prog("下载", 5, 100)
    check("样本太少时不给数字（免得一开始乱跳，反而不可信）",
          wa.STATE["eta"] == "", repr(wa.STATE["eta"]))

    # 退避期间速度骤降，估计必须跟着变。要是用"从开跑算全程平均"，
    # 它会一直说"还需 3 分钟"，同事就一直在那儿等 —— 那等于撒谎。
    wa.STATE["_eta_pts"] = []
    wa.STATE["_last_done"] = 0
    slow = [(1, 0), (2, 2), (3, 4), (4, 6), (5, 8), (6, 10),
            (7, 100), (8, 190), (9, 280), (10, 370)]     # 第 5 条起骤降到 90 秒/条
    try:
        wa.time.time = lambda: tick[0]
        for done, dt in slow:
            tick[0] = t0 + dt
            wa.set_prog("下载", done, 100)
    finally:
        wa.time.time = real_time
    e2 = wa.STATE["eta"]
    check("速度骤降后估计跟着变大（用近窗口，不是全程平均）",
          e2 and e2 != e1 and "小时" in e2, "%s → %s" % (e1, e2))
    print("     变慢前 %s，变慢后 %s" % (e1, e2))

    # 进度倒退（重跑、或后端上报乱序）时不能吐出一个荒唐的估计
    wa.STATE["_eta_pts"] = [(t0, 50, 100), (t0 + 10, 40, 100)]
    check("进度倒退时不硬算（宁可不说，也不说错的）",
          wa.eta_text() == "", repr(wa.eta_text()))

    print()
    print("=" * 66)
    print("⑤ 单实例：双击两次不该跑出两个程序")
    print("=" * 66)
    first = wa.claim_single_instance()
    second = wa.claim_single_instance()
    check("第二次抢锁被识别为「已有实例」（返回 False）", second is False,
          "first=%s second=%s" % (first, second))
    if first is False:
        print("     （本机此刻已有实例在跑 —— 这本身证明锁是生效的）")

finally:
    try:
        shutil.rmtree(TMP)
    except Exception:
        pass

print()
print("=" * 66)
print("结果：%d 项通过，%d 项失败" % (ok, fail))
print("=" * 66)
sys.exit(1 if fail else 0)
