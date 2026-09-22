# -*- coding: utf-8 -*-
"""发布包文档校验：确认同事看到的使用说明.txt 编码正确、内容覆盖到位。"""
import re
import subprocess
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent          # ② 全套源码（出问题才需要）
OUTER = R.parent                                     # 公司电脑用（①/② 并列的那一层）
sys.path.insert(0, str(R / "程序"))
sys.stdout.reconfigure(encoding="utf-8")

OUT = []
OK = True

# ---- 1. 重新组装发布包（只写文档/配置，不动 exe）----
r = subprocess.run([sys.executable, str(R / "开发维护" / "assemble.py")],
                   capture_output=True, cwd=str(R))
OUT.append("assemble.py 退出码：%d" % r.returncode)
if r.returncode != 0:
    OK = False
    OUT.append("stderr: %s" % r.stderr.decode("utf-8", "ignore")[:400])
OUT.append("")

p = OUTER / "① 双击就能用（直接用不用装任何东西）" / "证书下载器" / "使用说明.txt"
size = p.stat().st_size if p.exists() else 0
OUT.append("说明书路径：%s" % p)
OUT.append("存在：%s   大小：%d 字节" % (p.exists(), size))
OUT.append("")

raw = p.read_bytes() if p.exists() else b""
txt = ""
enc_used = "(读不出)"
# 为什么顺序是 utf-8-sig 优先：一个 GBK 文件用 utf-8 解必然失败，会落到 gbk 分支；
# 反过来一个 UTF-8 文件也可能被 GBK "解成功"（出一串汉字）。所以只能靠 BOM 一锤定音。
# 踩过的坑：说明书用 GBK 写，记事本能显示（靠系统代码页猜对），
# 但预览面板 / 浏览器 / VSCode / 微信里打开全是方块 —— 同事看到的就是"乱码说明"。
for enc in ("utf-8-sig", "utf-8", "gbk"):
    try:
        txt = raw.decode(enc)
        enc_used = enc
        break
    except Exception:
        continue

OUT.append("=" * 70)
OUT.append("1. 编码与完整性（记事本打开不能乱码，更不能是空的）")
OUT.append("=" * 70)


def chk(name, good, extra=""):
    global OK
    if not good:
        OK = False
    OUT.append("  %-52s %s %s" % (name, "✅" if good else "❌", extra))


EMPTY = len(txt) < 500
chk("组装脚本执行成功", r.returncode == 0)
chk("说明书有实质内容（不是空文件）", not EMPTY, "%d 字节" % size)
chk("带 UTF-8 BOM（记事本认 BOM，不会按 GBK 猜）",
    raw[:3] == b"\xef\xbb\xbf", "开头字节=" + repr(raw[:3]))
chk("按 UTF-8 解码（不是 GBK）", enc_used == "utf-8-sig", "实际编码=" + enc_used)
chk("没有乱码替换符", (not EMPTY) and "\ufffd" not in txt)
chk("中文正常（能找到「证书」二字）", "证书" in txt)
chk("换行是 Windows 风格（\\r\\n）", b"\r\n" in raw)

OUT.append("")
OUT.append("=" * 70)
OUT.append("2. 内容覆盖：同事能不能自己查到办法")
OUT.append("=" * 70)
chk("有「出问题了怎么办」总章节", "出问题了怎么办" in txt)
chk("开头就说明有「故障诊断」文件可转发", "故障诊断_" in txt)
chk("现象 1：被限流/封 IP", "现象 1" in txt and "封 IP" in txt)
chk("现象 1 给了手机热点方案", "热点" in txt)
chk("现象 1 提醒别反复硬试", "不要连着反复" in txt)
chk("现象 2：连不上网站（含先自己用浏览器验证）", "现象 2" in txt and "浏览器手动打开" in txt)
chk("现象 3：代理/VPN（含关系统代理步骤）", "现象 3" in txt and "使用代理服务器" in txt)
chk("现象 4：网站故障 5xx", "现象 4" in txt)
chk("现象 5：名单列名问题", "现象 5" in txt)
chk("现象 6：证书未生成", "现象 6" in txt)
chk("现象 7：未获奖不是漏了", "现象 7" in txt)
chk("现象 8：浏览器没打开界面", "现象 8" in txt)
chk("现象 9：杀毒软件拦截", "现象 9" in txt)
chk("现象 10：跑一半停了怎么办", "现象 10" in txt)
chk("现象 11：找不到名单文件", "现象 11" in txt and "找不到名单文件" in txt)
chk("现象 12：假 Excel（后缀被改过）",
    "现象 12" in txt and "另存为" in txt and "后缀" in txt)
chk("现象 13：暂无证书文件（比赛还没开放下载）",
    "现象 13" in txt and "还没开放下载" in txt)
# 现象编号要从 1 起连续。只数"有没有 10 条"是不够的：
# 中间漏一个号、或者同一号写了两遍，同事按现象号去翻就会翻不到。
nums = sorted({int(m) for m in re.findall(r"现象 (\d+)", txt)})
chk("现象编号从 1 起连续不重不漏（%s）" % ("、".join(map(str, nums)) or "无"),
    nums == list(range(1, len(nums) + 1)) and len(nums) >= 12)
chk("说明了「重跑会自动跳过」", "自动跳过" in txt)
chk("说明了速度预期（不要调快）", "不要" in txt and "节奏" in txt)
chk("说明了学校归并规则", "归并" in txt)

OUT.append("")
OUT.append("=" * 70)
OUT.append("2b. 新增的能力，说明书里有没有说清楚")
OUT.append("=" * 70)
chk("说了会自动建日期子文件夹", "日期文件夹" in txt and "证书_今天日期" in txt)
chk("说了分几次跑时两个数字各自什么意思",
    "目录内证书总数" in txt and "本批次名单人数" in txt)
chk("说了失败名单怎么用（拖回界面重跑）", "失败名单" in txt and "拖回" in txt)
chk("说了双击两次不会跑出两个程序", "已经在运行" in txt)
chk("说了进度条下面显示预计剩余时间", "预计还需" in txt)
chk("说了拖进去就报人数（选错文件能立刻发现）", "多少人" in txt)
chk("说了保存位置能自选（不再是写死的）", "浏览" in txt and "保存" in txt)
# 用户的明确要求：求助对象要写具体的人（技术支持），不许再写"管理员"——
# 同事不知道管理员是谁，写了等于没写。唯一例外是 Windows 系统术语
# "以管理员身份运行"（那是功能名，不是求助对象）。
chk("求助联系人写的是技术支持",
    "技术支持" in txt)
chk("没有再把求助对象写成「管理员」", "管理员" not in txt)
# 用户的原话：「这个文件是指什么文件？要给他指明出来，要不然他不知道发哪一份
# 文件出来。」所以说明书不能只说"把文件发出去"，必须点名发哪一份、它在哪、
# 里面有什么 —— 否则同事很可能把《使用说明》本身发出去，对方打开全是操作手册。
chk("点名了求助要发的是「故障诊断_xxx.txt」这一份",
    "故障诊断_" in txt and "不是这份《使用说明》" in txt)
chk("说清了那份文件放在哪（保存文件夹里，跟证书一起）",
    "保存文件夹" in txt and "跟下载好的证书放在一起" in txt)
chk("说清了报告里写了什么（时间/网址/名单/为什么/怎么办）",
    "网址" in txt and "名单" in txt and "为什么会这样" in txt)
chk("说了界面上能直接打开那份报告", "打开这份报告" in txt)
# 唯一没有报告的场合：程序压根没起来。这条不写清楚，同事会干等一份
# 永远不存在的文件，最后连"截个图"都想不到。
chk("说了程序起不来、没有报告时怎么办（截图）",
    "连界面都没打开" in txt and "截个图" in txt)
# 换比赛已经从"找人加配置"变成"同事自助"了，说明书必须说清楚是自助的 ——
# 不然同事还是会先去问人，这个功能等于白做。
chk("说了换比赛可以自助（＋ 换比赛 / 自动识别）",
    "＋ 换比赛" in txt and "自动识别" in txt)
chk("换比赛的自助步骤里有「先试跑验证」这一步",
    "换比赛" in txt and "先随机试跑" in txt)
# 2026-09-21 用户那场国赛被认成「快递信息填写」（模板残留的标题）。所以：
# ① 名字必须能自己改；② 认错的证书文件字段会导致整批下载失败，说明书必须预警。
chk("说了识别出来的比赛名可以自己改", "改成你要的" in txt)
chk("说了证书文件字段认错会导致整批下不下来", "整批一个都下不下来" in txt)
# 点「打开这份报告」曾经点不开（接口只认异常分支留下的路径）。
# 修好了，但说明书仍要有"自己去文件夹找"的兜底 —— 免得哪天又哑火时同事干等。
chk("说了报告点不开时自己进文件夹找", "不用为这个专门等人" in txt)
chk("末尾有「维护提示」（常见问题可自行补充）",
    "维护提示" in txt and txt.find("维护提示") > len(txt) * 0.85)
# 用户的明确要求：杀软提醒要放在**最前面**，不能只藏在"现象 9"里。
# 同事第一次双击就被拦，他只会认为"这工具有毒"，然后不会点第二次。
chk("杀软提醒在开头（不在后半段）",
    "第一次打开，请先看这一条" in txt
    and txt.index("第一次打开，请先看这一条") < len(txt) // 3,
    "位置 %s / 全文 %s" % (txt.find("第一次打开，请先看这一条"), len(txt)))

OUT.append("")
OUT.append("=" * 70)
OUT.append("3. 说明书里没有技术黑话（同事看不懂的词）")
OUT.append("=" * 70)
JARGON = ["traceback", "Traceback", "异常堆栈", "socket", "None",
          "KeyboardInterrupt", "import ", "def "]
for w in JARGON:
    chk("不含 %r" % w, (not EMPTY) and (w not in txt))

OUT.append("")
OUT.append("=" * 70)
OUT.append("4. 源码护栏：程序生成的「给人看的 txt」一律 utf-8-sig")
OUT.append("=" * 70)
# 同事的保存目录里会同时躺着说明书、运行日志、归并报告、故障诊断。
# 只要有一个是纯 UTF-8（无 BOM），记事本就会按 GBK 猜 → 那一个就是方块。
# 反过来用 GBK，预览面板 / 浏览器 / 微信里又是方块。
# 所以统一 utf-8-sig，并在这里盯住，别让新代码写回纯 utf-8 / gbk。
ROOT = R
BAD_ENC = re.compile(r'encoding\s*=\s*["\'](utf-8|gbk|gb2312)["\']')
for mod in ("cert_downloader.py", "web_app.py", "error_help.py"):
    src = (ROOT / "程序" / mod).read_text(encoding="utf-8")
    hits = []
    for m in BAD_ENC.finditer(src):
        seg = src[max(0, m.start() - 260):m.start()]
        # 只看"写"的：write_text / open(..., "w"
        if "write_text" in seg or re.search(r'open\s*\([^)]*"w', seg):
            # 跳过机器读的文件：JSON 带 BOM 会让部分解析器出错，必须保持纯 utf-8；
            # .url 快捷方式是纯 ASCII，无所谓；.bak 是 JSON 的备份，同理。
            # 这层是**黑名单**，判据是"文件名/变量名"而不是 json.dump ——
            # json.dump(...) 写在 open(...) 之后，前面 260 字符里根本扫不到。
            # 所以以后新增一个"机器读的文件"，必须把它的变量名加到这里，
            # 否则护栏会误报（误报比不报更烦，会让人干脆把护栏删掉）。
            if ("json.dump" in seg or "SETTINGS_FILE" in seg or "RUNFILE" in seg
                    or "MARKER_NAME" in seg
                    or ".url" in seg or ".json" in seg or ".bak" in seg):
                continue
            hits.append(src[max(0, m.start() - 60):m.end() + 20].replace("\n", " "))
    chk("%s 里没有给「人看的 txt」用无 BOM 编码" % mod, not hits,
        " | ".join(hits)[:110])

OUT.append("")
OUT.append("=" * 70)
OUT.append("5. 结论")
OUT.append("=" * 70)
OUT.append("  发布包文档：%s" % ("全部通过 ✅" if OK else "存在问题 ❌"))

(Path(__file__).with_name("docs_test.txt")).write_text("\n".join(OUT), encoding="utf-8")
sys.exit(0 if OK else 1)
